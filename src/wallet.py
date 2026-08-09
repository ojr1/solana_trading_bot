"""
wallet.py

Loads the bot's wallet from .env and checks its SOL balance via Helius RPC.
SENSITIVE — this module handles the private key in memory. Never log or
print the private key from anywhere in this file except generate_wallet.py.
"""

import os
import asyncio
import aiohttp
from solders.keypair import Keypair
from dotenv import load_dotenv

load_dotenv()

HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL")
WALLET_PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY")

if not HELIUS_RPC_URL:
    raise ValueError("HELIUS_RPC_URL is missing from .env")
if not WALLET_PRIVATE_KEY:
    raise ValueError("WALLET_PRIVATE_KEY is missing from .env")

# Loaded once when this module is imported elsewhere in the bot
keypair = Keypair.from_base58_string(WALLET_PRIVATE_KEY)
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

    async with aiohttp.ClientSession() as session:
        async with session.post(HELIUS_RPC_URL, json=payload) as response:
            data = await response.json()
            lamports = data["result"]["value"]
            return lamports / 1_000_000_000


async def main():
    balance = await get_balance()
    print(f"Wallet address: {public_key}")
    print(f"Balance: {balance:.4f} SOL")


if __name__ == "__main__":
    asyncio.run(main())