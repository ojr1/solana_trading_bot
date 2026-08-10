"""
trade_execution.py

Trade execution engine: gets swap quotes, and builds/signs/submits swaps
via Jupiter. Roadmap Step 2, all points now implemented:
  1. get_quote() — interface + quote fetching (read-only)
  2. build_signed_transaction() — gets unsigned tx, signs locally (SENSITIVE)
  3. submit_transaction() — broadcasts via Helius RPC (SENSITIVE)
  4. confirm_transaction() — polls until finalised on-chain
  5. execute_swap() — orchestrates all of the above

execute_swap() is NOT wired into main() — calling it with a real mint and
amount executes a REAL trade. main() below only runs the read-only quote
test and never touches signing or submission.
"""

import os
import asyncio
import base64
import aiohttp
from dotenv import load_dotenv
from solders.transaction import VersionedTransaction
from solders import message as solders_message
from wallet import get_keypair, public_key as wallet_public_key

load_dotenv()

JUPITER_API_KEY = os.getenv("JUPITER_API_KEY")
JUPITER_QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL = "https://api.jup.ag/swap/v1/swap"
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL")

SOL_MINT = "So11111111111111111111111111111111111111112"
# mint = the unique on-chain address identifying a specific token (SOL's is this fixed constant)


async def get_quote(output_mint: str, amount_sol: float, slippage_bps: int = 100) -> dict:
    """
    Asks Jupiter for a swap quote: SOL -> output_mint.

    output_mint: the target token's mint address (e.g. a meme coin contract address)
    amount_sol: how much SOL to swap, in SOL (not lamports)
    slippage_bps: slippage tolerance in basis points [bps = 1/100th of a percent; 100 bps = 1%]

    Returns Jupiter's raw quote response — expected output amount, price impact,
    and route. Read-only: no funds move, nothing is signed.
    """
    amount_lamports = int(amount_sol * 1_000_000_000)

    params = {
        "inputMint": SOL_MINT,
        "outputMint": output_mint,
        "amount": amount_lamports,
        "slippageBps": slippage_bps,
    }
    headers = {"x-api-key": JUPITER_API_KEY} if JUPITER_API_KEY else {}

    async with aiohttp.ClientSession() as session:
        async with session.get(JUPITER_QUOTE_URL, params=params, headers=headers) as response:
            return await response.json()


async def build_signed_transaction(quote: dict) -> bytes:
    """
    Gets the unsigned swap transaction from Jupiter and signs it locally.
    SENSITIVE — this is the moment the private key authorises a transaction.
    Returns signed transaction bytes. Nothing has been broadcast yet.
    """
    payload = {
        "quoteResponse": quote,
        "userPublicKey": str(wallet_public_key),
        "wrapAndUnwrapSol": True,  # auto-converts SOL <-> WSOL, since SOL itself isn't an SPL token
    }
    headers = {"x-api-key": JUPITER_API_KEY} if JUPITER_API_KEY else {}

    async with aiohttp.ClientSession() as session:
        async with session.post(JUPITER_SWAP_URL, json=payload, headers=headers) as response:
            data = await response.json()

    unsigned_tx_b64 = data["swapTransaction"]
    raw_tx = VersionedTransaction.from_bytes(base64.b64decode(unsigned_tx_b64))

    keypair = get_keypair()
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
        async with session.post(HELIUS_RPC_URL, json=payload) as response:
            data = await response.json()

    if "result" not in data:
        raise RuntimeError(f"Transaction submission failed: {data}")

    return data["result"]


async def confirm_transaction(signature: str, timeout_seconds: int = 60) -> bool:
    """
    Polls Helius until the transaction is confirmed on-chain, or times out.
    [confirmed = the blockchain has processed and finalised it, not just accepted it]
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignatureStatuses",
        "params": [[signature], {"searchTransactionHistory": True}],
    }

    elapsed = 0
    async with aiohttp.ClientSession() as session:
        while elapsed < timeout_seconds:
            async with session.post(HELIUS_RPC_URL, json=payload) as response:
                data = await response.json()

            status = data["result"]["value"][0]
            if status is not None and status.get("confirmationStatus") in ("confirmed", "finalized"):
                return True

            await asyncio.sleep(2)
            elapsed += 2

    return False


async def execute_swap(output_mint: str, amount_sol: float, slippage_bps: int = 100) -> dict:
    """
    Full pipeline: quote -> build+sign -> submit -> confirm.
    SENSITIVE — calling this with a real mint and amount executes a REAL trade.

    Returns the transaction signature and the quote used. Note: this does not
    yet parse the actual realised fill amount from the confirmed transaction —
    cross-check the signature on Solscan to see exactly what was received.
    """
    quote = await get_quote(output_mint, amount_sol, slippage_bps)
    if "outAmount" not in quote:
        raise RuntimeError(f"Quote failed, aborting before any signing: {quote}")

    signed_tx = await build_signed_transaction(quote)
    signature = await submit_transaction(signed_tx)
    confirmed = await confirm_transaction(signature)

    return {
        "signature": signature,
        "confirmed": confirmed,
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