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
    timeout. Genuinely UNKNOWN whether it landed - it may still confirm
    later. The trade must NEVER be treated as filled on this signal alone.
    Stale, assumed fills bypassing entry guards have caused real losses.
    """


class TransactionRevertedError(Exception):
    """
    A transaction was confirmed on-chain but FAILED - Solana's own
    getSignatureStatuses `err` field was non-null (e.g. slippage tolerance
    exceeded). Unlike FillNotConfirmedError, this is a KNOWN outcome: no
    funds moved for this swap, and it is safe to treat as if it never
    happened rather than needing to keep polling or investigating.
    """


class TransactionSubmissionError(RuntimeError):
    """
    submit_transaction() failed - the network call to broadcast either
    errored outright or Helius returned no result. Whether the transaction
    nonetheless reached the network before the failure is NOT knowable from
    this exception alone. local_signature (derived from the signed bytes,
    before submission was ever attempted - see build_signed_transaction())
    is carried so a caller can poll for it later rather than having nothing
    to check.
    """

    def __init__(self, message, local_signature=None):
        super().__init__(message)
        self.local_signature = local_signature


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


async def build_signed_transaction(quote: dict, priority_fee_lamports: int = None):
    """
    Gets the unsigned swap transaction from Jupiter and signs it locally.
    SENSITIVE — this is the moment the private key authorises a transaction.
    Nothing has been broadcast yet.

    Returns (signed_tx_bytes, local_signature) - local_signature (a base58
    string) is derived from the signature the keypair just produced, not
    from anything Helius has told us. Stage 10 (30 Aug 2026): captured here,
    immediately after signing and before submit_transaction() is ever
    called, so a submission that fails BEFORE a response arrives - network
    drop, timeout, anything - still leaves a known signature that can be
    polled for later, rather than genuinely no idea what to check. See
    TransactionSubmissionError, which carries this value through such a
    failure.

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

    local_signature = str(signed_tx.signatures[0])

    return bytes(signed_tx), local_signature


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


async def _fetch_signature_status(session, signature):
    """
    One poll of getSignatureStatuses for a single signature. Returns the
    status dict (may carry 'err': absent, null, or a non-null failure
    detail, plus 'confirmationStatus'), or None if the signature is not yet
    known to the RPC node at all. Raises on network failure -
    confirm_transaction()'s polling loop, not this helper, decides whether
    to log-and-retry. Split out so tests can mock exactly this call without
    needing to fake aiohttp itself.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignatureStatuses",
        "params": [[signature], {"searchTransactionHistory": True}],
    }
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    async with session.post(config.HELIUS_RPC_URL, json=payload,
                            timeout=timeout) as response:
        data = await response.json()
    return data["result"]["value"][0]


async def confirm_transaction(signature: str, timeout_seconds: int = 60) -> str:
    """
    Polls Helius until the transaction resolves on-chain, or times out.
    [confirmed = the blockchain has processed and finalised it, not just accepted it]

    Returns one of three strings, deliberately not a bool - "timed out
    waiting" and "confirmed but failed" are different, known-vs-unknown
    outcomes that callers must be able to tell apart (see
    LIVE_EXECUTION_PLAN.md's failure taxonomy):

        "confirmed" - landed on-chain and succeeded (err is null or absent)
        "failed"    - landed on-chain but REVERTED (err is non-null) - a
                      KNOWN outcome, safe to treat as "nothing happened"
        "timeout"   - never resolved within timeout_seconds - genuinely
                      UNKNOWN, must never be assumed either way

    status.get("err") returns None uniformly whether the key is present
    with a null value or absent entirely, so both are handled identically
    and correctly by construction - no separate branch needed for "absent".

    Each individual poll has its own REQUEST_TIMEOUT_SECONDS timeout. A
    single dropped poll does not fail the whole wait - it is logged and the
    outer loop tries again on its own 2-second cadence, still bounded by
    timeout_seconds overall, so this can never hang indefinitely.
    """
    elapsed = 0
    async with aiohttp.ClientSession() as session:
        while elapsed < timeout_seconds:
            try:
                status = await _fetch_signature_status(session, signature)
                if status is not None and status.get("confirmationStatus") in ("confirmed", "finalized"):
                    if status.get("err") is not None:
                        log.error(
                            "Transaction %s confirmed but REVERTED on-chain: %s",
                            signature, status.get("err"),
                        )
                        return "failed"
                    return "confirmed"
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.warning("Confirmation poll for %s failed: %s - retrying", signature, exc)

            await asyncio.sleep(2)
            elapsed += 2

    return "timeout"


async def execute_swap(output_mint: str, amount_sol: float,
                       slippage_bps: int = None, priority_fee_lamports: int = None) -> dict:
    """
    Full pipeline: quote -> (DRY_RUN: log and stop) -> build+sign -> submit -> confirm.
    SENSITIVE — while DRY_RUN is false, calling this with a real mint and
    amount executes a REAL trade.

    Raises FillNotConfirmedError if the transaction was submitted but never
    confirmed within the timeout - genuinely unknown, may still land later.
    Raises TransactionRevertedError if it confirmed but FAILED on-chain -
    known outcome, no funds moved. Raises TransactionSubmissionError
    (carrying local_signature) if submission itself failed. The caller must
    never treat any of these as a successful fill.

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

    signed_tx_bytes, local_signature = await build_signed_transaction(
        quote, priority_fee_lamports,
    )

    try:
        signature = await submit_transaction(signed_tx_bytes)
    except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
        # Submission itself failed - whether the transaction nonetheless
        # reached the network is NOT knowable from this exception alone.
        # local_signature (captured before this call was ever attempted) is
        # the one thing that lets a caller check later rather than guessing.
        raise TransactionSubmissionError(
            f"submit_transaction failed for locally-signed tx "
            f"{local_signature}: {exc}. Whether it landed anyway is "
            f"unknown - poll this signature before assuming either outcome.",
            local_signature=local_signature,
        ) from exc

    status = await confirm_transaction(signature)

    if status == "failed":
        raise TransactionRevertedError(
            f"Transaction {signature} was confirmed on-chain but REVERTED "
            f"(failed) - no funds moved. Check "
            f"https://solscan.io/tx/{signature} for the reason (e.g. "
            f"slippage tolerance exceeded)."
        )

    if status == "timeout":
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


# ==========================================================================
# FILL PARSING (Stage 10 Part 4, 30 Aug 2026)
#
# LIVE_EXECUTION_PLAN.md Gap 4/Stage 4: execute_swap() returns the PRE-trade
# quote, not what was actually received - real slippage means the two can
# differ. This parses the CONFIRMED transaction itself (getTransaction) to
# recover the real amounts, rather than diffing wallet balances before and
# after: balance-diffing is unsafe the moment more than one position can
# have a trade in flight at overlapping times (which Stage 10 Part 2's
# in-flight registry explicitly allows) - any other activity on the wallet
# between two balance snapshots would contaminate the diff. Parsing is
# scoped to exactly one signature and stays correct regardless of what else
# the wallet is doing.
#
# NOT WIRED INTO runner.py. Standalone, unit-tested against mocked
# getTransaction responses built from the documented RPC shape. See
# AUTONOMOUS_RUN_REPORT.md for what verification against a real signature
# was and was not possible this run.
# ==========================================================================


class FillParseError(Exception):
    """
    A confirmed transaction could not be parsed into real fill amounts -
    e.g. the transaction is not found, not yet finalised, reverted, or has
    no balance entry for our wallet/mint. Never guess a fill amount; raise
    instead.
    """


async def _fetch_transaction(session, signature):
    """
    One getTransaction call. maxSupportedTransactionVersion=0 is required
    for versioned (v0) transactions, which Jupiter swaps commonly are -
    without it, Helius rejects the request for any v0 tx. Split out, same
    reasoning as _fetch_signature_status(): lets tests mock exactly this
    call without faking aiohttp.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [signature, {"encoding": "json", "maxSupportedTransactionVersion": 0}],
    }
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    async with session.post(config.HELIUS_RPC_URL, json=payload,
                            timeout=timeout) as response:
        data = await response.json()
    return data.get("result")


async def parse_fill_from_transaction(signature, mint, owner_pubkey=None):
    """
    Parses a CONFIRMED transaction to recover what it ACTUALLY moved.

    Returns:
        {
            "real_token_delta": float,  # signed; +received (buy), -sent (sell)
            "real_sol_delta": float,    # signed SOL, already NET of the fee
                                         # (see below) - +received, -spent
            "fee_sol": float,           # reported separately for logging;
                                         # already reflected in real_sol_delta,
                                         # do not subtract it again
            "decimals": int,            # the mint's decimals, as reported by
                                         # this transaction's own balances -
                                         # not assumed, not hardcoded
        }

    owner_pubkey defaults to wallet.get_public_key() (our own wallet) -
    overridable for testing without needing a real configured wallet.

    ATA resolution: token balances (preTokenBalances/postTokenBalances) are
    self-describing - each entry already carries its own owner, mint, and
    decimals, so no separate account-lookup step is needed to find "our"
    token account; just filter by owner and mint. An entry absent from
    preTokenBalances means the account did not exist before this tx (e.g.
    an ATA created by this exact swap - true "pre" balance is 0); absent
    from postTokenBalances means it was fully drained (true "post" balance
    is 0). Both are handled as 0, not skipped.

    SOL balance: unlike token balances, preBalances/postBalances are
    indexed positionally against the FULL account list, which - for a
    versioned transaction using Jupiter's address lookup tables - is the
    static message accountKeys PLUS meta.loadedAddresses.writable PLUS
    meta.loadedAddresses.readonly, in that order. Using only the static
    accountKeys (skipping loadedAddresses) would find the WRONG index, or
    none, for any transaction that used a lookup table - silently wrong,
    not a crash. This is handled by concatenating all three lists before
    searching for our pubkey.

    Fee netting: preBalances/postBalances already reflect the ACTUAL
    lamport change including the network fee for whichever account paid it
    (always account index 0, the fee payer - our own wallet, since we are
    the one submitting). So real_sol_delta = (post - pre) / 1e9 is already
    fee-inclusive; fee_sol is reported alongside for visibility only, never
    added or subtracted again.

    Raises FillParseError if the transaction cannot be found, is not yet
    finalised, reverted (meta.err is non-null - see Part 1's confirm_transaction()
    fix; a reverted tx moved nothing, so there is nothing to parse), or has
    no matching balance entries.
    """
    owner = owner_pubkey if owner_pubkey is not None else str(wallet.get_public_key())

    async with aiohttp.ClientSession() as session:
        result = await _fetch_transaction(session, signature)

    if result is None:
        raise FillParseError(
            f"getTransaction returned no result for {signature} - not "
            f"found, or not yet finalised. Try again once confirmed."
        )

    meta = result.get("meta") or {}
    if meta.get("err") is not None:
        raise FillParseError(
            f"transaction {signature} reverted (err={meta['err']}) - no "
            f"funds moved, nothing to parse."
        )

    # --- Token leg -----------------------------------------------------
    pre_token = _find_token_balance(meta.get("preTokenBalances") or [], owner, mint)
    post_token = _find_token_balance(meta.get("postTokenBalances") or [], owner, mint)

    if pre_token is None and post_token is None:
        raise FillParseError(
            f"no {mint} token balance for owner {owner} in either "
            f"preTokenBalances or postTokenBalances of {signature} - "
            f"wrong mint, wrong owner, or this transaction did not touch "
            f"that token at all."
        )

    decimals = (post_token or pre_token)["uiTokenAmount"]["decimals"]
    pre_amount = int(pre_token["uiTokenAmount"]["amount"]) if pre_token else 0
    post_amount = int(post_token["uiTokenAmount"]["amount"]) if post_token else 0
    real_token_delta = (post_amount - pre_amount) / (10 ** decimals)

    # --- SOL leg ---------------------------------------------------------
    account_keys = list(result["transaction"]["message"].get("accountKeys") or [])
    loaded = meta.get("loadedAddresses") or {}
    account_keys += list(loaded.get("writable") or [])
    account_keys += list(loaded.get("readonly") or [])

    try:
        our_index = account_keys.index(owner)
    except ValueError:
        raise FillParseError(
            f"owner {owner} not found in the account list (static + "
            f"loaded) of {signature} - cannot determine the SOL delta."
        )

    pre_balances = meta.get("preBalances") or []
    post_balances = meta.get("postBalances") or []
    if our_index >= len(pre_balances) or our_index >= len(post_balances):
        raise FillParseError(
            f"account index {our_index} out of range for pre/postBalances "
            f"of {signature} - malformed or unexpected transaction shape."
        )

    real_sol_delta = (post_balances[our_index] - pre_balances[our_index]) / 1_000_000_000
    fee_sol = meta.get("fee", 0) / 1_000_000_000

    return {
        "real_token_delta": real_token_delta,
        "real_sol_delta": real_sol_delta,
        "fee_sol": fee_sol,
        "decimals": decimals,
    }


def _find_token_balance(entries, owner, mint):
    """Returns the first entry in a preTokenBalances/postTokenBalances list
    matching this owner and mint, or None. Self-contained lookup - each
    entry already carries owner/mint/decimals, no separate account-lookup
    step needed (see parse_fill_from_transaction()'s docstring)."""
    for entry in entries:
        if entry.get("owner") == owner and entry.get("mint") == mint:
            return entry
    return None


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
