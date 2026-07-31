"""
runner.py - Stage 3 dry run. Connects every component into a working pipeline.

    Telegram message
        -> parser          classify and extract
        -> entry_logic     buy or reject, and how much
        -> position        opened in simulation, never on chain
        -> market_data     market cap polled on a timer
        -> exit_logic      DCA fills, take profit, stop loss
        -> logs

NOTHING IS TRADED. No wallet is loaded, no key is read, no transaction is
built. Every fill below is simulated at the market cap observed at that
moment. DRY_RUN exists as a switch so that live execution can be added later
without restructuring, but it is not yet wired to anything that could spend.

Two things run at once: the Telegram listener, which reacts to messages as
they arrive, and the position monitor, which polls on a timer. Python's
asyncio runs both in a single process, each pausing while it waits so the
other can proceed.

    python src/runner.py
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from telethon import TelegramClient, events

import entry_logic
import exit_logic
import market_data
import parser as message_parser

# ==========================================================================
# CONFIGURATION
# ==========================================================================

# Hard safety switch. While True the bot only ever writes to logs.
DRY_RUN = True

# How often open positions are re-priced, in seconds. One batched request
# covers every position, so this cost does not grow as positions accumulate.
POLL_INTERVAL_SECONDS = 5

# Where simulated state and output are kept. No database yet - see spec
# section 12; that arrives at Stage 7 alongside the backtesting work.
LOGS_DIR = Path("logs")
POSITIONS_FILE = LOGS_DIR / "positions.json"

# Positions that have closed are kept in the file for later analysis rather
# than deleted, but are skipped by the monitor.
load_dotenv()

API_ID = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")
CHANNEL = os.getenv("TELEGRAM_CHANNEL")


# ==========================================================================
# LOGGING
# ==========================================================================


def setup_logging():
    """Logs to the console and to a dated file, so nothing is lost overnight."""
    LOGS_DIR.mkdir(exist_ok=True)
    logfile = LOGS_DIR / f"dryrun_{datetime.now(timezone.utc):%Y-%m-%d}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.FileHandler(logfile, encoding="utf-8"),
                  logging.StreamHandler()],
    )
    return logging.getLogger("runner")


log = setup_logging()
# Telethon logs connection detail at INFO, which drowns out the trading
# output. Warnings and errors still come through.
logging.getLogger("telethon").setLevel(logging.WARNING)

# ==========================================================================
# POSITION STORE
#
# Positions persist to JSON so a restart does not lose open trades, and so
# duplicate-buy protection survives the process dying. Keyed by contract
# address because that is the only identifier guaranteed unique per token.
# ==========================================================================


def load_positions():
    if not POSITIONS_FILE.exists():
        return {}
    try:
        with open(POSITIONS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Could not read positions file (%s). Starting empty.", exc)
        return {}


def save_positions(positions):
    LOGS_DIR.mkdir(exist_ok=True)
    try:
        with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(positions, f, indent=2)
    except OSError as exc:
        log.error("Could not save positions: %s", exc)


POSITIONS = load_positions()


# ==========================================================================
# NOTIONAL ACCOUNTING
#
# The dry run never receives a real fill, so there is no token quantity to
# record. Instead exposure is tracked in SOL terms against a fixed reference
# market cap, which works because market cap is proportional to price when
# supply is fixed:
#
#     value_in_sol = tokens * (current_mc / reference_mc)
#
# "tokens" is therefore a notional unit equal to one SOL of exposure at the
# reference market cap. Break-even is derived from it rather than assumed,
# which matters after a DCA fill: averaging down is harmonic, not arithmetic,
# so taking a simple mean of the fill prices would put the stop-loss in the
# wrong place.
# ==========================================================================


def tokens_for(sol_amount, fill_mc, reference_mc):
    """Notional token units acquired by spending sol_amount at fill_mc."""
    return sol_amount * (reference_mc / fill_mc)


def breakeven_mc(position):
    """The market cap at which the position is exactly flat."""
    if position["tokens_remaining"] <= 0:
        return position["entry_mc"]
    return (
        position["sol_invested"]
        * position["reference_mc"]
        / position["total_tokens_bought"]
    )


def position_value_sol(position, current_mc):
    """Current SOL value of what is still held."""
    return position["tokens_remaining"] * (current_mc / position["reference_mc"])


# ==========================================================================
# OPENING A POSITION
# ==========================================================================


async def open_position(decision, call):
    """
    Records a simulated entry and schedules the remaining DCA tranches.

    Only the first tranche fills here. Later stages are stored as pending and
    fill when the price drops far enough - see check_dca_fills.
    """
    contract = decision["contract_address"]
    tranches = decision["tranches"]
    first = tranches[0]
    entry_mc = call["market_cap"]

    tokens = tokens_for(first["sol"], entry_mc, entry_mc)

    position = {
        "ticker": decision["ticker"],
        "contract_address": contract,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        # Accounting
        "reference_mc": entry_mc,
        "entry_mc": entry_mc,
        "sol_invested": first["sol"],
        "total_tokens_bought": tokens,
        "tokens_remaining": tokens,
        "original_tokens": tokens,
        # Entry context, kept for later analysis
        "pcr": decision["pcr"],
        "planned_lot_sol": decision["total_lot_sol"],
        "gt_score": call.get("gt_score"),
        "holders": call.get("holders"),
        "age_minutes": call.get("age_minutes"),
        "bundled_pct": call.get("bundled_pct"),
        # DCA
        "fills": [
            {"stage": 1, "sol": first["sol"], "mc": entry_mc,
             "at": datetime.now(timezone.utc).isoformat()}
        ],
        "pending_tranches": tranches[1:],
        "last_fill_mc": entry_mc,
        # Exit state, matching what exit_logic expects
        "peak_mc": entry_mc,
        "initials_taken": False,
        "last_sell_mc": None,
        "fired_levels": [],
        "closed": False,
        "pending": None,
        "realised_sol": 0.0,
    }

    POSITIONS[contract] = position
    save_positions(POSITIONS)

    log.info(
        "OPEN  %-10s $%s  PCR %.3f  buy 1/%d: %.3f SOL of %.3f planned",
        decision["ticker"], f"{entry_mc:,.0f}", decision["pcr"],
        len(tranches), first["sol"], decision["total_lot_sol"],
    )
    if tranches[1:]:
        upcoming = ", ".join(
            f"{t['sol']:.3f} SOL at -{t['drop_pct_from_previous_fill']}%"
            for t in tranches[1:]
        )
        log.info("      pending tranches: %s", upcoming)


# ==========================================================================
# DCA FILLS
# ==========================================================================


def check_dca_fills(position, current_mc):
    """
    Fills the next pending tranche if the price has dropped far enough.

    Each drop is measured from the PREVIOUS fill rather than cumulatively from
    the first buy, so a three-stage entry completes around 19% below buy 1
    rather than 20%.

    DCA stops once initials have been taken. Past that point the position is
    being wound down, and adding to it would contradict the exit that is
    already in progress.
    """
    if position["initials_taken"] or not position["pending_tranches"]:
        return None

    next_tranche = position["pending_tranches"][0]
    drop = next_tranche["drop_pct_from_previous_fill"] / 100.0
    trigger_mc = position["last_fill_mc"] * (1 - drop)

    if current_mc > trigger_mc:
        return None

    # Fill it.
    position["pending_tranches"].pop(0)
    tokens = tokens_for(next_tranche["sol"], current_mc, position["reference_mc"])

    position["sol_invested"] += next_tranche["sol"]
    position["total_tokens_bought"] += tokens
    position["tokens_remaining"] += tokens
    position["original_tokens"] += tokens
    position["last_fill_mc"] = current_mc
    position["fills"].append(
        {"stage": next_tranche["stage"], "sol": next_tranche["sol"],
         "mc": current_mc, "at": datetime.now(timezone.utc).isoformat()}
    )

    # Averaging down moves break-even, which moves every exit threshold with it.
    position["entry_mc"] = breakeven_mc(position)

    return {
        "stage": next_tranche["stage"],
        "sol": next_tranche["sol"],
        "mc": current_mc,
        "new_entry_mc": position["entry_mc"],
    }


# ==========================================================================
# MONITOR LOOP
# ==========================================================================


async def monitor_positions():
    """Re-prices every open position on a timer and applies the exit rules."""
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await _monitor_once(session)
            except aiohttp.ClientError as exc:
                # Network problems are expected occasionally. Log and carry on:
                # the next cycle is only seconds away, and killing the loop
                # would silently stop managing live positions.
                log.warning("Market data request failed: %s", exc)
            except Exception:
                log.exception("Unexpected error in monitor loop")

            await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def _monitor_once(session):
    open_positions = {k: v for k, v in POSITIONS.items() if not v["closed"]}
    if not open_positions:
        return

    market_caps = await market_data.fetch_market_caps(session, list(open_positions))
    now = time.time()
    changed = False

    for contract, position in open_positions.items():
        current_mc = market_caps.get(contract)
        if current_mc is None:
            continue

        fill = check_dca_fills(position, current_mc)
        if fill:
            changed = True
            log.info(
                "DCA   %-10s buy %d: %.3f SOL at $%s  | avg entry now $%s",
                position["ticker"], fill["stage"], fill["sol"],
                f"{fill['mc']:,.0f}", f"{fill['new_entry_mc']:,.0f}",
            )

        for action in exit_logic.check_exit_conditions(position, current_mc, now):
            changed = True
            proceeds = action["tokens"] * (current_mc / position["reference_mc"])
            position["realised_sol"] += proceeds

            log.info(
                "SELL  %-10s %-13s %.1f%% at $%s  -> %.4f SOL  | %s",
                position["ticker"], action["exit_type"],
                action["pct_of_original_position"], f"{current_mc:,.0f}",
                proceeds, action["reason"],
            )

            if action["position_closed"]:
                pnl = position["realised_sol"] - position["sol_invested"]
                log.info(
                    "CLOSE %-10s invested %.3f SOL, returned %.3f SOL, P&L %+.4f SOL (%+.1f%%)",
                    position["ticker"], position["sol_invested"],
                    position["realised_sol"], pnl,
                    pnl / position["sol_invested"] * 100,
                )

    if changed:
        save_positions(POSITIONS)


# ==========================================================================
# TELEGRAM HANDLING
# ==========================================================================


client = TelegramClient("bot_session", int(API_ID or 0), API_HASH or "")


@client.on(events.NewMessage(chats=CHANNEL))
async def on_message(event):
    text = event.message.raw_text
    if not text:
        return

    parsed = message_parser.parse_message(text)
    kind = parsed["message_type"]

    if kind != "call":
        # Updates are logged only. They are the raw material for the whale-buy
        # regression described in spec section 13, so they are worth capturing
        # even though nothing acts on them yet.
        if kind != "unknown":
            log.info("UPDATE %-9s %s", parsed.get("ticker") or "", kind)
        return

    if not parsed["parse_ok"]:
        log.warning("PARSE FAIL  missing %s | %s",
                    parsed["missing_fields"], text[:60].replace("\n", " "))
        return

    contract = parsed["contract_address"]

    # Duplicate protection. A channel repost or a reconnect that redelivers a
    # message must not open a second position in the same token.
    if contract in POSITIONS:
        log.info("SKIP  %-10s already held or previously traded", parsed["ticker"])
        return

    decision = entry_logic.decide_entry(parsed)

    if decision["action"] == "reject":
        log.info("REJECT %-9s $%s  | %s", parsed["ticker"],
                 f"{parsed['market_cap']:,.0f}", decision["reason"])
        return

    await open_position(decision, parsed)


# ==========================================================================
# ENTRY POINT
# ==========================================================================


async def main():
    if not API_ID or not API_HASH or not CHANNEL:
        raise SystemExit(
            "Missing credentials. Check .env contains TELEGRAM_API_ID, "
            "TELEGRAM_API_HASH and TELEGRAM_CHANNEL."
        )

    log.info("=" * 66)
    log.info("DRY RUN - no wallet loaded, no trades executed")
    log.info("channel: %s | polling every %ss", CHANNEL, POLL_INTERVAL_SECONDS)
    open_count = sum(1 for p in POSITIONS.values() if not p["closed"])
    log.info("restored %d position(s), %d still open", len(POSITIONS), open_count)
    log.info("=" * 66)

    async with client:
        # Both tasks run concurrently: the listener reacts to messages while
        # the monitor polls on its own schedule.
        await asyncio.gather(
            client.run_until_disconnected(),
            monitor_positions(),
        )


if __name__ == "__main__":
    if not DRY_RUN:
        raise SystemExit("Live execution is not implemented. Keep DRY_RUN = True.")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped by user. %d position(s) saved.", len(POSITIONS))
