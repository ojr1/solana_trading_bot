"""
trade_execution.py

Trade execution engine: gets swap quotes, and builds/signs/submits swaps
via Jupiter. Roadmap Step 2, all points now implemented:
  1. get_quote() — interface + quote fetching (read-only)
  2. build_signed_transaction() — gets unsigned tx, signs locally (SENSITIVE)
  3. submit_transaction() — broadcasts via Helius RPC (SENSITIVE)
  4. confirm_transaction() — polls until finalised on-chain
  5. execute_swap() — orchestrates all of the above

execute_swap() is NOT wired into runner.py's live pipeline - calling it with
a real mint and amount executes a REAL trade, but nothing in the dry-run bot
calls it. main() below only runs the read-only quote test and never touches
signing or submission.

STAGE 1 SAFETY (added 27 Aug 2026):

- SLIPPAGE_BPS and PRIORITY_FEE_LAMPORTS now default from config.py rather
  than a hardcoded 100 bps and no priority fee at all. Both legs (buy and
  sell) go through execute_swap(), so both use the same config values.
- Every network call has an explicit timeout and bounded retries with
  exponential backoff via _request_with_retries() - no more unbounded waits,
  and no more a single dropped packet failing a swap outright.
- execute_swap() now checks config.DRY_RUN itself, at the point closest to
  signing, rather than relying on every future caller to remember to check
  first. While DRY_RUN is true it logs exactly what would have been
  submitted - mint, amount, resolved slippage and priority fee, and the
  expected output from a real quote - and returns without ever calling
  build_signed_transaction() or submit_transaction(). This is on top of, not
  instead of, the fact that nothing currently calls execute_swap() at all.
- confirm_transaction() returning False used to just flow through
  execute_swap() as a soft `"confirmed": False` field in the result dict -
  easy for a future caller to overlook and treat as a success. It now raises
  FillNotConfirmedError instead: an unconfirmed fill is an abandoned trade,
  never assumed to have happened, and a caller cannot accidentally ignore it
  by not checking a field.
"""

import asyncio
import base64
import logging

import aiohttp
from solders.transaction import VersionedTransaction
from solders import message as solders_message

import config
import wallet
# STAGE 5 (28 Aug 2026): wallet.get_keypair()/get_public_key() are called at
# point of use inside build_signed_transaction() below, not imported by name
# here. wallet.py's Keypair is now built lazily on first use rather than at
# import time, so binding wallet.public_key directly at this file's own
# import time (the previous form: "from wallet import ... public_key as
# wallet_public_key") would fail - that name no longer exists as a plain
# module attribute the moment this file is imported.

log = logging.getLogger("trade_execution")

JUPITER_QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL = "https://api.jup.ag/swap/v1/swap"

SOL_MINT = "So11111111111111111111111111111111111111112"
# mint = the unique on-chain address identifying a specific token (SOL's is this fixed constant)

# Every network call in this file uses this timeout and this retry policy.
# MAX_RETRIES total attempts, waiting RETRY_BACKOFF_BASE_SECONDS * 2^(n-1)
# between them (1s, 2s, 4s), then the attempt is abandoned - never retried
# forever, and never silently treated as having succeeded.
REQUEST_TIMEOUT_SECONDS = 10
MAX_RETRIES = 3
RETRY_BACKOFF_BASE_SECONDS = 1.0


class FillNotConfirmedError(Exception):
    """
    A transaction was submitted but never confirmed on-chain within the
    timeout. The trade is abandoned - it must NEVER be treated as filled.
    Stale, assumed fills bypassing entry guards have caused real losses.
    """


def _jupiter_headers():
    return {"x-api-key": config.JUPITER_API_KEY} if config.JUPITER_API_KEY else {}


async def _request_with_retries(session, method, url, **kwargs):
    """
    Runs one HTTP request with a timeout and bounded exponential backoff.

    method is "get" or "post". On a transient failure (network error or
    timeout) it retries up to MAX_RETRIES times total; after the last
    attempt it raises rather than retrying forever, so a call site never
    hangs indefinitely and never has to guess whether "no response yet"
    means "still trying" or "gave up silently".
    """
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    request = getattr(session, method)

    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with request(url, timeout=timeout, **kwargs) as response:
                return await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_exc = exc
            if attempt == MAX_RETRIES:
                break
            backoff = RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning(
                "%s %s failed (attempt %d/%d): %s - retrying in %.1fs",
                method.upper(), url, attempt, MAX_RETRIES, exc, backoff,
            )
            await asyncio.sleep(backoff)

    log.error("%s %s abandoned after %d attempts: %s",
              method.upper(), url, MAX_RETRIES, last_exc)
    raise RuntimeError(
        f"{method.upper()} {url} failed after {MAX_RETRIES} attempts: {last_exc}"
    )


async def get_quote(output_mint: str, amount_sol: float, slippage_bps: int = None) -> dict:
    """
    Asks Jupiter for a swap quote: SOL -> output_mint.

    output_mint: the target token's mint address (e.g. a meme coin contract address)
    amount_sol: how much SOL to swap, in SOL (not lamports)
    slippage_bps: slippage tolerance in basis points [bps = 1/100th of a
        percent; 100 bps = 1%]. Defaults to config.SLIPPAGE_BPS (2500, i.e.
        25%) - the same value used for both the buy and the sell leg.

    Returns Jupiter's raw quote response — expected output amount, price impact,
    and route. Read-only: no funds move, nothing is signed.
    """
    if slippage_bps is None:
        slippage_bps = config.SLIPPAGE_BPS

    amount_lamports = int(amount_sol * 1_000_000_000)

    params = {
        "inputMint": SOL_MINT,
        "outputMint": output_mint,
        "amount": amount_lamports,
        "slippageBps": slippage_bps,
    }

    async with aiohttp.ClientSession() as session:
        return await _request_with_retries(
            session, "get", JUPITER_QUOTE_URL,
            params=params, headers=_jupiter_headers(),
        )


async def build_signed_transaction(quote: dict, priority_fee_lamports: int = None) -> bytes:
    """
    Gets the unsigned swap transaction from Jupiter and signs it locally.
    SENSITIVE — this is the moment the private key authorises a transaction.
    Returns signed transaction bytes. Nothing has been broadcast yet.

    priority_fee_lamports defaults to config.PRIORITY_FEE_LAMPORTS. There is
    no bribe or Jito bundle parameter here - submission is direct through
    Helius, so this is the only fee lever this bot has.
    """
    if priority_fee_lamports is None:
        priority_fee_lamports = config.PRIORITY_FEE_LAMPORTS

    payload = {
        "quoteResponse": quote,
        "userPublicKey": str(wallet.get_public_key()),
        "wrapAndUnwrapSol": True,  # auto-converts SOL <-> WSOL, since SOL itself isn't an SPL token
        "prioritizationFeeLamports": priority_fee_lamports,
    }

    async with aiohttp.ClientSession() as session:
        data = await _request_with_retries(
            session, "post", JUPITER_SWAP_URL,
            json=payload, headers=_jupiter_headers(),
        )

    unsigned_tx_b64 = data["swapTransaction"]
    raw_tx = VersionedTransaction.from_bytes(base64.b64decode(unsigned_tx_b64))

    keypair = wallet.get_keypair()
    signature = keypair.sign_message(solders_message.to_bytes_versioned(raw_tx.message))
    signed_tx = VersionedTransaction.populate(raw_tx.message, [signature])

    return bytes(signed_tx)


async def submit_transaction(signed_tx_bytes: bytes) -> str:
    """
    Broadcasts the signed transaction via Helius RPC.
    SENSITIVE — this step actually moves funds on-chain. Irreversible.
    Returns the transaction signature.
    """
    signed_tx_b64 = base64.b64encode(signed_tx_bytes).decode("utf-8")
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendTransaction",
        "params": [signed_tx_b64, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}],
    }

    async with aiohttp.ClientSession() as session:
        data = await _request_with_retries(
            session, "post", config.HELIUS_RPC_URL, json=payload,
        )

    if "result" not in data:
        raise RuntimeError(f"Transaction submission failed: {data}")

    return data["result"]


async def confirm_transaction(signature: str, timeout_seconds: int = 60) -> bool:
    """
    Polls Helius until the transaction is confirmed on-chain, or times out.
    [confirmed = the blockchain has processed and finalised it, not just accepted it]

    Each individual poll has its own REQUEST_TIMEOUT_SECONDS timeout. A
    single dropped poll does not fail the whole wait - it is logged and the
    outer loop tries again on its own 2-second cadence, still bounded by
    timeout_seconds overall, so this can never hang indefinitely.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignatureStatuses",
        "params": [[signature], {"searchTransactionHistory": True}],
    }
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)

    elapsed = 0
    async with aiohttp.ClientSession() as session:
        while elapsed < timeout_seconds:
            try:
                async with session.post(config.HELIUS_RPC_URL, json=payload,
                                        timeout=timeout) as response:
                    data = await response.json()
                status = data["result"]["value"][0]
                if status is not None and status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return True
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.warning("Confirmation poll for %s failed: %s - retrying", signature, exc)

            await asyncio.sleep(2)
            elapsed += 2

    return False


async def execute_swap(output_mint: str, amount_sol: float,
                       slippage_bps: int = None, priority_fee_lamports: int = None) -> dict:
    """
    Full pipeline: quote -> (DRY_RUN: log and stop) -> build+sign -> submit -> confirm.
    SENSITIVE — while DRY_RUN is false, calling this with a real mint and
    amount executes a REAL trade.

    Raises FillNotConfirmedError if the transaction was submitted but never
    confirmed - the caller must never treat that as a successful fill.

    Returns the transaction signature and the quote used. Note: this does not
    yet parse the actual realised fill amount from the confirmed transaction —
    cross-check the signature on Solscan to see exactly what was received.
    """
    if slippage_bps is None:
        slippage_bps = config.SLIPPAGE_BPS
    if priority_fee_lamports is None:
        priority_fee_lamports = config.PRIORITY_FEE_LAMPORTS

    quote = await get_quote(output_mint, amount_sol, slippage_bps)
    if "outAmount" not in quote:
        raise RuntimeError(f"Quote failed, aborting before any signing: {quote}")

    if config.DRY_RUN:
        log.info(
            "DRY RUN - would submit: %.4f SOL -> %s | slippage %d bps | "
            "priority fee %d lamports | expected out %s",
            amount_sol, output_mint, slippage_bps, priority_fee_lamports,
            quote.get("outAmount"),
        )
        return {
            "signature": None,
            "confirmed": False,
            "dry_run": True,
            "quote": quote,
            "solscan_url": None,
        }

    signed_tx = await build_signed_transaction(quote, priority_fee_lamports)
    signature = await submit_transaction(signed_tx)
    confirmed = await confirm_transaction(signature)

    if not confirmed:
        raise FillNotConfirmedError(
            f"Transaction {signature} was submitted but never confirmed "
            f"within the timeout. Trade abandoned - do NOT assume it "
            f"filled. Check https://solscan.io/tx/{signature} manually "
            f"before taking any further action on this position."
        )

    return {
        "signature": signature,
        "confirmed": True,
        "dry_run": False,
        "quote": quote,
        "solscan_url": f"https://solscan.io/tx/{signature}",
    }


async def main():
    """Read-only quote test. Does not sign or submit anything."""
    USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    BONK_MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"

    # Test 1: stablecoin pair (deep liquidity, sanity check)
    quote_usdc = await get_quote(USDC_MINT, amount_sol=0.05)
    if "outAmount" in quote_usdc:
        out_usdc = int(quote_usdc["outAmount"]) / 1_000_000  # USDC has 6 decimals
        print(f"USDC quote: 0.05 SOL -> {out_usdc:.4f} USDC (impact: {quote_usdc.get('priceImpactPct', 'n/a')})")
    else:
        print("USDC quote failed:", quote_usdc)

    # Test 2: real meme coin (BONK), the actual kind of pair the bot will trade
    quote_bonk = await get_quote(BONK_MINT, amount_sol=0.05)
    if "outAmount" in quote_bonk:
        out_bonk = int(quote_bonk["outAmount"]) / 100_000  # BONK has 5 decimals
        print(f"BONK quote: 0.05 SOL -> {out_bonk:.0f} BONK (impact: {quote_bonk.get('priceImpactPct', 'n/a')})")
    else:
        print("BONK quote failed:", quote_bonk)


if __name__ == "__main__":
    asyncio.run(main())
