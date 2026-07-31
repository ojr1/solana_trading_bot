"""
listener.py - Stage 1 of the Solana memecoin signal trading bot.

Purpose: connect to Telegram, listen to the GemTools calls channel, and print
every incoming message to the console.

This stage deliberately does NOT parse messages or execute any trades. It
exists purely to prove the connection works end to end before anything is
built on top of it.
"""

import os

from dotenv import load_dotenv
from telethon import TelegramClient, events

# Load the values stored in .env into the environment so they can be read
# below. Keeping credentials in .env rather than in this file means this
# script can be committed to GitHub without exposing anything confidential.
load_dotenv()

API_ID = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")
CHANNEL = os.getenv("TELEGRAM_CHANNEL")

# Fail early and loudly if anything is missing. Without this check, a missing
# value would produce a confusing error deep inside the Telethon library
# rather than telling us plainly what is wrong.
if not API_ID or not API_HASH or not CHANNEL:
    raise SystemExit(
        "Missing credentials.\n"
        "Check that .env exists in the project root and contains "
        "TELEGRAM_API_ID, TELEGRAM_API_HASH and TELEGRAM_CHANNEL."
    )

# Values read from .env are always text, but Telethon requires the API ID as
# a number, so it is converted here.
try:
    API_ID = int(API_ID)
except ValueError:
    raise SystemExit(
        "TELEGRAM_API_ID must be a number.\n"
        "Check .env - the placeholder text may not have been replaced."
    )

# "bot_session" is the name of the session file Telethon creates on first
# login. It keeps you signed in so you are not asked to re-authenticate on
# every run.
#
# CONFIDENTIAL: anyone holding this file has ongoing access to your Telegram
# account. It is already covered by .gitignore via the *.session rule.
client = TelegramClient("bot_session", API_ID, API_HASH)


@client.on(events.NewMessage(chats=CHANNEL))
async def handle_new_message(event):
    """
    Runs automatically every time a new message is posted to the channel.

    Telethon holds a persistent connection open and Telegram pushes messages
    to us as they happen. There is no polling loop, so no API calls are wasted
    while the channel is quiet.
    """
    message = event.message

    # message.reply_to is populated only when a message is a reply to another
    # message. This is the mechanism we will use later to separate 'calls'
    # (standalone messages) from 'updates' such as 2x milestones and whale
    # buys, which are always posted as replies to the original call.
    is_reply = message.reply_to is not None

    print("=" * 70)
    print(f"Message ID  : {message.id}")
    print(f"Timestamp   : {message.date}")
    print(f"Is a reply  : {is_reply}")
    if is_reply:
        print(f"Replying to : {message.reply_to.reply_to_msg_id}")
    print("-" * 70)

    # raw_text strips Telegram's markdown formatting (the ** bold markers),
    # returning plain text. message.text would keep them, which would make
    # parsing in Stage 2 unnecessarily fragile.
    print(message.raw_text)

    print("=" * 70)
    print()


def main():
    print(f"Connecting to Telegram. Listening to channel: {CHANNEL}")
    print("Waiting for messages. Press Ctrl+C to stop.\n")

    # Using the client as a context manager handles logging in on entry and
    # disconnecting cleanly on exit, including when stopped with Ctrl+C.
    with client:
        client.run_until_disconnected()


if __name__ == "__main__":
    main()