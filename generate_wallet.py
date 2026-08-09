"""
generate_wallet.py

SENSITIVE — creates a brand-new Solana wallet (keypair) and prints the
private key to the terminal. Run this ONCE. Copy the output into .env
straight away, then clear the terminal (Windows: type 'cls' and press Enter).

Never share the private key. Never commit it to Git. Never paste it
anywhere except your local .env file.
"""

from solders.keypair import Keypair
# Keypair = the linked public/private key pair that controls a Solana wallet


def main():
    new_wallet = Keypair()

    public_key = str(new_wallet.pubkey())
    private_key = str(new_wallet)  # base58 string — the format Phantom/Solflare use for "Import Private Key"

    print("=" * 60)
    print("NEW WALLET GENERATED")
    print("=" * 60)
    print(f"Public address (safe to share):\n{public_key}\n")
    print(f"Private key (SENSITIVE — copy into .env now):\n{private_key}\n")
    print("=" * 60)
    print("Next steps:")
    print("1. Copy the private key above into .env as:")
    print("   WALLET_PRIVATE_KEY=<paste_here>")
    print("2. Run 'cls' to clear this terminal.")
    print("3. Do not save this output anywhere else (no notes app, no screenshot).")
    print("=" * 60)


if __name__ == "__main__":
    main()