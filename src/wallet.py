"""
wallet.py

Loads the bot's wallet from .env and checks its SOL balance via Helius RPC.
SENSITIVE — this module handles the private key in memory. Never log or
print the private key from anywhere in this file except generate_wallet.py.

STAGE 1 SAFETY (added 27 Aug 2026): get_balance() now has an explicit
timeout and bounded retries with backoff, matching trade_execution.py -
previously this was the one Helius call in the codebase with no timeout at
all, and it is now called on every buy via runner.py's reserve check, not
just when this module is run standalone.
"""

import asyncio
import logging

import aiohttp
from solders.keypair import Keypair

import config

log = logging.getLogger("wallet")

# Same policy as trade_execution.py: a timeout per attempt, bounded retries
# with backoff, then abandoned and logged rather than an unbounded wait.
REQUEST_TIMEOUT_SECONDS = 10
MAX_RETRIES = 3
RETRY_BACKOFF_BASE_SECONDS = 1.0

# Loaded once when this module is imported elsewhere in the bot.
# SENSITIVE - config.WALLET_PRIVATE_KEY is consumed here and only here to
# build the Keypair; nothing downstream needs the raw key again.
keypair = Keypair.from_base58_string(config.WALLET_PRIVATE_KEY)
public_key = keypair.pubkey()


def get_keypair() -> Keypair:
    """Returns the loaded Keypair, used later to sign transactions."""
    return keypair


async def get_balance() -> float:
    """
    Fetches the wallet's current SOL balance via Helius RPC.
    [RPC = Remote Procedure Call — the API a bot uses to talk to the blockchain]
    Returns the balance in SOL (Helius returns lamports; 1 SOL = 1,000,000,000 lamports,
    lamports being the smallest unit — like pence to pounds).
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getBalance",
        "params": [str(public_key)],
    }
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)

    last_exc = None
    async with aiohttp.ClientSession() as session:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with session.post(config.HELIUS_RPC_URL, json=payload,
                                        timeout=timeout) as response:
                    data = await response.json()
                    lamports = data["result"]["value"]
                    return lamports / 1_000_000_000
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                if attempt == MAX_RETRIES:
                    break
                backoff = RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                log.warning(
                    "get_balance failed (attempt %d/%d): %s - retrying in %.1fs",
                    attempt, MAX_RETRIES, exc, backoff,
                )
                await asyncio.sleep(backoff)

    log.error("get_balance abandoned after %d attempts: %s", MAX_RETRIES, last_exc)
    raise RuntimeError(f"get_balance failed after {MAX_RETRIES} attempts: {last_exc}")


async def main():
    balance = await get_balance()
    print(f"Wallet address: {public_key}")
    print(f"Balance: {balance:.4f} SOL")


if __name__ == "__main__":
    asyncio.run(main())