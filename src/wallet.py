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

STAGE 5 (28 Aug 2026): the Keypair is now built lazily, on first use,
rather than at import time. Importing wallet.py no longer requires
WALLET_PRIVATE_KEY at all - config.py only allows it to be unset while
DRY_RUN is true, and get_keypair()/get_public_key()/get_balance() all
raise NoWalletConfiguredError if something calls into them anyway with no
key set. runner.check_reserve_ok() is the one call site that expects and
handles that error.
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

# Cache for the lazily-built Keypair - see _load_keypair(). Never accessed
# directly outside this module; use get_keypair()/get_public_key().
_keypair = None


class NoWalletConfiguredError(RuntimeError):
    """
    No WALLET_PRIVATE_KEY is set in .env. Only valid while DRY_RUN is true -
    config.py requires the key unconditionally otherwise, so reaching this
    with DRY_RUN false would mean config.py itself has a bug, not that this
    is a normal condition to handle further up.
    """


def _load_keypair() -> Keypair:
    """
    Builds and caches the Keypair from config.WALLET_PRIVATE_KEY on first
    use. SENSITIVE - the only place the raw private key is turned into a
    signing object; nothing downstream needs the raw key again.

    Raises NoWalletConfiguredError if no key is set, rather than letting
    Keypair.from_base58_string(None) fail with an unrelated, confusing
    TypeError.
    """
    global _keypair
    if _keypair is None:
        if not config.WALLET_PRIVATE_KEY:
            raise NoWalletConfiguredError(
                "No wallet is configured (WALLET_PRIVATE_KEY is not set in "
                ".env). This is only ever valid while DRY_RUN is true."
            )
        _keypair = Keypair.from_base58_string(config.WALLET_PRIVATE_KEY)
    return _keypair


def get_keypair() -> Keypair:
    """Returns the loaded Keypair, used later to sign transactions."""
    return _load_keypair()


def get_public_key():
    """Returns the wallet's public key, building the Keypair on first use."""
    return _load_keypair().pubkey()


async def get_balance() -> float:
    """
    Fetches the wallet's current SOL balance via Helius RPC.
    [RPC = Remote Procedure Call — the API a bot uses to talk to the blockchain]
    Returns the balance in SOL (Helius returns lamports; 1 SOL = 1,000,000,000 lamports,
    lamports being the smallest unit — like pence to pounds).

    Raises NoWalletConfiguredError if no wallet is set up at all (see
    _load_keypair()), or RuntimeError if the RPC call itself fails after
    retries. Callers that want to tell these apart - see
    runner.check_reserve_ok() - must catch NoWalletConfiguredError first,
    since it is a RuntimeError subclass.
    """
    public_key = get_public_key()
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
    print(f"Wallet address: {get_public_key()}")
    print(f"Balance: {balance:.4f} SOL")


if __name__ == "__main__":
    asyncio.run(main())