"""
test_swap.py

SENSITIVE — deliberate, one-off test of execute_swap(). Running this
signs and submits a REAL transaction using REAL SOL from the wallet in
.env. Requires typed confirmation before anything is signed or submitted.
"""

import asyncio
from trade_execution import execute_swap

# Test parameters — confirmed with the user before this script was written
TEST_MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"  # BONK
TEST_AMOUNT_SOL = 0.02


async def main():
    print("=" * 60)
    print("REAL TRADE TEST")
    print("=" * 60)
    print(f"About to swap {TEST_AMOUNT_SOL} SOL -> BONK")
    print("This is a REAL transaction. Real SOL will be spent.")
    print("=" * 60)

    confirm = input("Type EXACTLY 'confirm' to proceed, anything else cancels: ")
    if confirm.strip() != "confirm":
        print("Cancelled. Nothing was signed or submitted.")
        return

    print("\nFetching quote, signing, and submitting...\n")
    result = await execute_swap(TEST_MINT, TEST_AMOUNT_SOL)

    print("=" * 60)
    print("RESULT")
    print("=" * 60)
    print(f"Signature: {result['signature']}")
    print(f"Confirmed on-chain: {result['confirmed']}")
    print(f"Check it here: {result['solscan_url']}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())