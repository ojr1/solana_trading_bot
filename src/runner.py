"""
runner.py - Stage 3 dry run. Connects every component into a working pipeline.

    Telegram message
        -> parser          classify and extract
        -> entry_logic     buy or reject, and how much
        -> position        opened in simulation, never on chain
        -> market_data     market cap polled on a timer
        -> exit_logic      DCA fills, take profit, stop loss
        -> logs

NOTHING IS TRADED and no transaction is ever built or signed here. Every fill
below is simulated at the market cap observed at that moment. DRY_RUN exists
as a switch so that live execution can be added later without restructuring,
but it is not yet wired to anything that could spend.

SENSITIVE (added 27 Aug 2026, Stage 1 safety): this file imports wallet.py
for the reserve check in check_reserve_ok(), purely to derive the public key
that get_balance() reads from Helius. Nothing in this file ever calls
anything that signs with it. UPDATED 28 Aug 2026 (Stage 5): wallet.py's
Keypair is now built lazily, on first use, not at import time - so while
DRY_RUN is true and no wallet is configured, importing wallet here does NOT
require or parse a private key at all. check_reserve_ok() handles that case
explicitly; see its docstring.

Two things run at once: the Telegram listener, which reacts to messages as
they arrive, and the position monitor, which polls on a timer. Python's
asyncio runs both in a single process, each pausing while it waits so the
other can proceed.

    python src/runner.py
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import aiohttp
from telethon import TelegramClient, events

import config
import data_logger
import entry_logic
import exit_logic
import market_data
import parser as message_parser
import trading_window
import wallet

# ==========================================================================
# CONFIGURATION
# ==========================================================================

# Hard safety switch. While True the bot only ever writes to logs. Sourced
# from .env via config.py (Stage 1, 27 Aug 2026) rather than hardcoded, so
# flipping it is a deliberate .env edit rather than a code change - but the
# value is still validated at import time, so a missing or malformed
# DRY_RUN in .env fails loudly before this line is ever reached.
DRY_RUN = config.DRY_RUN

# How often open positions are re-priced, in seconds. One batched request
# covers every position, so this cost does not grow as positions accumulate.
POLL_INTERVAL_SECONDS = 5

# Where simulated state and output are kept. No database yet - see spec
# section 12; that arrives at Stage 7 alongside the backtesting work.
LOGS_DIR = Path("logs")
POSITIONS_FILE = LOGS_DIR / "positions.json"

# How often a status line is written for each open position. Without this the
# monitor is silent unless something fails, so there is no way to distinguish
# "nothing is happening" from "pricing has silently stopped working".
STATUS_INTERVAL_SECONDS = 300

# If the wall-clock gap between monitor cycles exceeds this, the process was
# suspended (e.g. the machine slept) rather than merely running slow. Uses
# time.time(), not time.monotonic() - monotonic does not advance during a
# Windows sleep, which is exactly why the 16 Aug stall left nothing in the
# log until the next reconnect. Log-only: never alters trading behaviour.
SUSPENSION_THRESHOLD_SECONDS = 30

# If there are open positions and no market data fetch has SUCCEEDED for
# longer than this, something is wrong even though the loop is still ticking
# - without this a wedged loop looks identical to a dead network. Log-only.
STALE_FETCH_WARNING_SECONDS = 180

# ---------------------------------------------------------------------------
# ENTRY GUARDS (added 10 Aug 2026 after the first full overnight run)
#
# The hard cut at $75K protects against buying a coin that has already run.
# These three protect against the opposite and adjacent failures, each of
# which cost real money in the 10 Aug session:
#
#   Shaboingdog  opened at $5,221 against a call figure of $20,400 - a 74%
#                gap, and already below the absolute floor. It was bought and
#                floor-sold within the same cycle.
#   ALING        opened and floor-closed three times in eight minutes for a
#                combined -0.665 SOL. Duplicate protection is keyed on
#                contract address, so a relaunch under the same ticker is a
#                different contract and slipped straight through.
# ---------------------------------------------------------------------------

# Reject a fill if the live market cap has fallen more than this far below the
# figure quoted in the call. Mirrors the $75K hard cut on the downside: a coin
# that has collapsed since the message was composed is not the coin that was
# called, whatever the PCR scored it at.
MAX_ENTRY_GAP_PCT = 35

# After a position in a ticker closes at a loss, block new entries in that
# same ticker name for this long. Keyed on TICKER deliberately, because a
# relaunch has a new contract address and would otherwise defeat the existing
# contract-keyed duplicate check.
LOSS_COOLDOWN_MINUTES = 60

# Reject a call whose Telegram message is older than this by the time it is
# processed. Telethon redelivers a whole backlog of queued messages in the
# same second after a reconnect, and the bot has no other way to tell a
# fresh call from an hours-old one - this is what let bih, PUMPTOWN and
# HALLU fill blind on 16 Aug 2026. Five minutes is generous: normal
# call-to-fill time in the log is seconds.
MAX_CALL_AGE_SECONDS = 300

# Positions that have closed are kept in the file for later analysis rather
# than deleted, but are skipped by the monitor.

# Sourced from config.py (Stage 1, 27 Aug 2026), which validates these at
# import time - by the time this line runs, import config above has already
# either succeeded or crashed with a specific "X is missing from .env"
# message, so these are never None here in practice.
API_ID = config.TELEGRAM_API_ID
API_HASH = config.TELEGRAM_API_HASH
CHANNEL = config.TELEGRAM_CHANNEL


# ==========================================================================
# LOGGING
# ==========================================================================


def setup_logging():
    """
    Logs to the console and to a rotating file, so nothing is lost on a
    multi-day run.

    Rotates at local midnight and keeps 14 days of history. Previously the
    filename was computed once at import from the START date, so a run
    spanning midnight (like 15-16 Aug) kept writing into the file named for
    the day it started, and an unattended VPS run would grow one unbounded
    file. NOTE: this changes the filename convention - today's file is now
    dryrun.log, and rotated files are dryrun.log.YYYY-MM-DD, not
    dryrun_YYYY-MM-DD.log.
    """
    LOGS_DIR.mkdir(exist_ok=True)
    logfile = LOGS_DIR / "dryrun.log"

    file_handler = TimedRotatingFileHandler(
        logfile, when="midnight", backupCount=14, encoding="utf-8",
    )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=[file_handler, logging.StreamHandler()],
    )
    return logging.getLogger("runner")


log = setup_logging()

# Telethon logs every connection detail at INFO, which buries the trading
# output. Warnings and errors still come through.
logging.getLogger("telethon").setLevel(logging.WARNING)


# ==========================================================================
# POSITION STORE
#
# Positions persist to JSON so a restart does not lose open trades, and so
# duplicate-buy protection survives the process dying. Keyed by contract
# address because that is the only identifier guaranteed unique per token.
# ==========================================================================


def _migrate_tranches(positions):
    """
    Normalises pending tranches saved by an older entry_logic version.

    The tranche key was renamed from drop_pct to drop_pct_from_previous_fill
    when DCA was reworked. Positions stored under the old name would crash the
    monitor loop on every cycle, so they are converted here at load time.
    """
    for position in positions.values():
        for tranche in position.get("pending_tranches", []):
            if "drop_pct_from_previous_fill" not in tranche:
                tranche["drop_pct_from_previous_fill"] = tranche.get("drop_pct", 10)
    return positions


def load_positions():
    if not POSITIONS_FILE.exists():
        return {}
    try:
        with open(POSITIONS_FILE, encoding="utf-8") as f:
            return _migrate_tranches(json.load(f))
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


def _normalise_ticker(ticker):
    """Whitespace-stripped, lowercased ticker, for case-insensitive comparison."""
    return (ticker or "").strip().lower()


def ticker_cooldown_remaining(ticker):
    """
    Minutes left on the loss cooldown for this ticker, or None if clear.

    Scans closed positions for the same ticker name that ended at a net loss
    within the cooldown period.

    NOTE ON THE RULE: the original design said "after a stop-loss or floor
    close". This implementation triggers on any close that ended at a net
    LOSS, which is a superset - it catches stop-loss and floor closes by
    definition, plus a trailing stop that still ended underwater. It is also
    robust to the exit_type strings in exit_logic changing, which a hardcoded
    list of names would not be. If you want the narrower rule, the change is
    one condition below.

    Ticker comparison is case-insensitive because relaunches frequently
    differ only in capitalisation.
    """
    if not ticker:
        return None

    target = _normalise_ticker(ticker)
    now = datetime.now(timezone.utc)
    longest = None

    for position in POSITIONS.values():
        if not position.get("closed"):
            continue
        if _normalise_ticker(position.get("ticker")) != target:
            continue

        # Net loss check. A position with no realised figure is skipped
        # rather than guessed at.
        invested = position.get("sol_invested")
        realised = position.get("realised_sol")
        if invested is None or realised is None or realised >= invested:
            continue

        closed_at = position.get("closed_at")
        if not closed_at:
            # Positions closed before closed_at was recorded cannot be timed,
            # so they cannot hold a cooldown open. Nothing to do.
            continue

        try:
            closed_time = datetime.fromisoformat(closed_at)
        except (TypeError, ValueError):
            continue

        elapsed_minutes = (now - closed_time).total_seconds() / 60.0
        if elapsed_minutes < LOSS_COOLDOWN_MINUTES:
            remaining = LOSS_COOLDOWN_MINUTES - elapsed_minutes
            if longest is None or remaining > longest:
                longest = remaining

    return longest


def open_position_for_ticker(ticker):
    """
    Contract address of any OPEN position sharing this ticker, or None.

    The duplicate check above is keyed on contract address, so a second
    contract launched under the same ticker sails straight through it - that
    is how PANDA held two simultaneous positions on 16 Aug 2026, seven
    minutes apart on different contracts. Reuses the same normalisation as
    ticker_cooldown_remaining() so the two checks can never disagree about
    what counts as "the same ticker".
    """
    if not ticker:
        return None

    target = _normalise_ticker(ticker)
    for contract, position in POSITIONS.items():
        if position.get("closed"):
            continue
        if _normalise_ticker(position.get("ticker")) == target:
            return contract

    return None


# ==========================================================================
# CALL FRESHNESS
#
# A reconnect makes Telethon redeliver every message it missed while
# disconnected, all in the same second. Nothing about that redelivery marks
# a message as old, so without this check the bot scores and fills a call
# that may be hours stale as if it had just arrived.
# ==========================================================================


def call_age_seconds(message_date, now=None):
    """
    Seconds between a Telegram message's timestamp and now.

    Both must be aware datetimes (carrying timezone info) - message_date is
    event.message.date from Telethon, which always is. Kept standalone and
    synchronous, with no Telethon dependency, so it can be unit-tested with
    plain datetimes instead of a fake event object.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    return (now - message_date).total_seconds()


def is_call_stale(message_date, now=None):
    """True if a call's message is older than MAX_CALL_AGE_SECONDS."""
    return call_age_seconds(message_date, now) > MAX_CALL_AGE_SECONDS


# ==========================================================================
# WALLET RESERVE (Stage 1 safety, 27 Aug 2026)
#
# The reserve exists so the bot can always afford to sell what it holds.
# Its claims are token account rent of roughly 0.002 SOL per open position
# and a priority fee of roughly 0.000125 SOL per exit transaction. At
# MAX_CONCURRENT_POSITIONS (6) that is about 6 * (0.002 + 0.000125) =
# 0.0128 SOL - comfortably inside MIN_SOL_RESERVE's default of 0.05 SOL, so
# the reserve floor is headroom above what closing every open position would
# actually cost, not a tight estimate of it.
# ==========================================================================


async def check_reserve_ok(trade_size_sol):
    """
    True if the wallet can afford trade_size_sol AND keep the safety reserve.

    Calls wallet.get_balance() - a real, read-only Helius RPC call using only
    the wallet's PUBLIC key. Runs even while DRY_RUN is true and no real
    trade will follow, so the guard's logic is proven correct against the
    real balance before it is ever relied on for a real trade.

    FAILS CLOSED: if the balance cannot be fetched at all (Helius down,
    timed out after retries), this blocks the trade rather than crashing the
    bot or letting it through unchecked - an unknown balance is treated the
    same as an insufficient one, consistent with 2b: never assume, infer, or
    proceed on missing information.

    STAGE 5 (28 Aug 2026): while DRY_RUN is true, no wallet may be
    configured at all - config.py only allows that while DRY_RUN is true,
    and this is expected to be a TEMPORARY state, until the wallet is
    funded for real trading. With no wallet there is no balance to read, so
    the reserve check cannot run at all - not "fails closed" (there is
    nothing to check), and never logged as though a check passed. It logs a
    loud WARNING naming that explicitly and allows the entry through. This
    path must never be reachable when DRY_RUN is false: config.py already
    guarantees WALLET_PRIVATE_KEY is present in that case, so
    wallet.NoWalletConfiguredError should never surface here outside dry run.
    """
    try:
        balance = await wallet.get_balance()
    except wallet.NoWalletConfiguredError:
        log.warning(
            "RESERVE CHECK UNAVAILABLE - no wallet is configured (dry run "
            "only, temporary until the wallet is funded). Allowing entry "
            "WITHOUT a reserve check, not because one passed.",
        )
        return True
    except RuntimeError as exc:
        log.warning(
            "RESERVE BLOCK could not fetch wallet balance, refusing to buy "
            "blind: %s", exc,
        )
        return False

    open_count = sum(1 for p in POSITIONS.values() if not p["closed"])

    if balance - trade_size_sol < config.MIN_SOL_RESERVE:
        log.warning(
            "RESERVE BLOCK balance=%.4f SOL  trade_size=%.4f SOL  "
            "reserve_floor=%.4f SOL  open_positions=%d",
            balance, trade_size_sol, config.MIN_SOL_RESERVE, open_count,
        )
        return False
    return True


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

    THE FILL PRICE IS THE LIVE MARKET CAP, NOT THE ONE IN THE MESSAGE. The
    figure GemTools prints was measured before the message was composed,
    delivered and parsed; a real buy happens at whatever the price is now.
    Observed gaps have exceeded 25% within a second of a call. Using the
    stale figure inflates P&L and misplaces every exit threshold, so:

      - the PCR is still scored on the GemTools snapshot (the judgement is
        about the call as made), but
      - the position's entry is the live price, and
      - the live price is re-checked against the hard cut, so a coin that
        has already run past $75K by the time the message arrives cannot
        slip in under its stale figure.

    If no live price is available (very new tokens are sometimes not yet
    indexed, or the API request failed), the fill is REJECTED rather than
    using the stale call figure. The entire entry model above is "fill at
    the live price"; without one, the gap guard is comparing the call figure
    to itself and can never fire. That gap is exactly how bih, PUMPTOWN and
    HALLU filled blind on 16 Aug 2026 for a combined -0.517 SOL - the bot
    woke from a network outage, got no live price, and bought three
    hours-dead coins at their stale call figures. Missing a trade costs
    nothing; filling blind does not.
    """
    contract = decision["contract_address"]
    tranches = decision["tranches"]
    first = tranches[0]
    call_mc = call["market_cap"]

    # RESERVE CHECK (Stage 1 safety, 27 Aug 2026). Checked before the price
    # fetch below - trade_size does not depend on price, so there is no
    # reason to spend a Jupiter call finding out the reserve was already
    # going to block this fill.
    if not await check_reserve_ok(first["sol"]):
        log.info(
            "REJECT %-9s reserve floor would be breached  | trade %.3f SOL",
            decision["ticker"], first["sol"],
        )
        data_logger.log_call(
            "rejected_reserve", call, decision,
            reason="buying this would breach MIN_SOL_RESERVE",
        )
        return

    # ONE fetch, returning the live price AND the extra Jupiter fields added
    # 15 Aug 2026. This is the only place they are collected: the 5-second
    # monitor loop still calls fetch_market_caps and is untouched, so nothing
    # about the exit path changes.
    #
    # None of these fields feeds a decision below. They are captured purely so
    # they accumulate on disk and can be tested once enough nights exist - a
    # field never recorded cannot be recovered retrospectively.
    live_mc = None
    token_details = {}
    try:
        async with aiohttp.ClientSession() as session:
            details = await market_data.fetch_token_details(session, [contract])
        token_details = details.get(contract) or {}
        live_mc = token_details.get("market_cap")
    except aiohttp.ClientError as exc:
        log.warning("Live price fetch failed for %s: %s", decision["ticker"], exc)

    if live_mc is None:
        log.info(
            "REJECT %-9s no live price available  | call figure was $%s",
            decision["ticker"], f"{call_mc:,.0f}",
        )
        data_logger.log_call(
            "rejected_no_price", call, decision, live_mc=None,
            reason="no live price available at fill time - refusing to fill blind",
            token_details=token_details,
        )
        return

    if live_mc >= entry_logic.MC_HARD_CUT:
        log.info(
            "REJECT %-9s live $%s (call said $%s)  | above hard cut at fill time",
            decision["ticker"], f"{live_mc:,.0f}", f"{call_mc:,.0f}",
        )
        data_logger.log_call(
            "rejected_fill", call, decision, live_mc=live_mc,
            reason="above hard cut at fill time",
            token_details=token_details,
        )
        return

    gap_pct = (live_mc / call_mc - 1) * 100

    # GUARD 1: the price has collapsed since the call was composed.
    if gap_pct < -MAX_ENTRY_GAP_PCT:
        log.info(
            "REJECT %-9s live $%s vs call $%s (gap %+.1f%%)  | "
            "below the -%d%% entry gap limit",
            decision["ticker"], f"{live_mc:,.0f}", f"{call_mc:,.0f}",
            gap_pct, MAX_ENTRY_GAP_PCT,
        )
        data_logger.log_call(
            "rejected_fill", call, decision, live_mc=live_mc,
            reason=(f"live price {gap_pct:+.1f}% vs call, beyond the "
                    f"-{MAX_ENTRY_GAP_PCT}% entry gap limit"),
            token_details=token_details,
        )
        return

    entry_mc = live_mc
    log.info(
        "FILL  %-10s live $%s vs call $%s  (gap %+.1f%%)",
        decision["ticker"], f"{live_mc:,.0f}", f"{call_mc:,.0f}", gap_pct,
    )

    # GUARD 2: the entry price is already at or below the level at which the
    # exit logic would immediately sell. Buying here means paying a swap fee
    # to open a position that closes on the next monitor cycle.
    if entry_mc <= exit_logic.ABSOLUTE_FLOOR_MC:
        log.info(
            "REJECT %-9s $%s  | at or below the $%s absolute floor at entry",
            decision["ticker"], f"{entry_mc:,.0f}",
            f"{exit_logic.ABSOLUTE_FLOOR_MC:,.0f}",
        )
        data_logger.log_call(
            "rejected_fill", call, decision, live_mc=live_mc,
            reason=(f"entry price ${entry_mc:,.0f} is at or below the "
                    f"${exit_logic.ABSOLUTE_FLOOR_MC:,.0f} absolute floor"),
            token_details=token_details,
        )
        return

    tokens = tokens_for(first["sol"], entry_mc, entry_mc)

    position = {
        "ticker": decision["ticker"],
        "contract_address": contract,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        # Accounting
        "reference_mc": entry_mc,
        "entry_mc": entry_mc,
        "call_mc": call_mc,
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
        # Jupiter detail fields captured at entry (added 15 Aug 2026). Stored
        # on the position so data_loader can read them straight out of
        # positions.json alongside the outcome, with no join to calls.jsonl.
        # Written whether or not a value came back, so the column exists from
        # day one rather than appearing halfway through the dataset.
        **{column: token_details.get(column)
           for column in market_data.DETAIL_COLUMNS},
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

    data_logger.log_call("bought", call, decision, live_mc=live_mc,
                         token_details=token_details)
    data_logger.log_fill("buy", position, first["sol"], entry_mc, stage=1)

    log.info(
        "OPEN  %-10s $%s  PCR %.3f  buy 1/%d: %.3f SOL of %.3f planned",
        decision["ticker"], f"{entry_mc:,.0f}", decision["pcr"],
        len(tranches), first["sol"], decision["total_lot_sol"],
    )
    if tranches[1:]:
        upcoming = ", ".join(
            f"{t['sol']:.3f} SOL at "
            f"-{t.get('drop_pct_from_previous_fill', t.get('drop_pct', '?'))}%"
            for t in tranches[1:]
        )
        log.info("      pending tranches: %s", upcoming)


# ==========================================================================
# DCA FILLS
# ==========================================================================


async def check_dca_fills(position, current_mc):
    """
    Fills the next pending tranche if the price has dropped far enough.

    Each drop is measured from the PREVIOUS fill rather than cumulatively from
    the first buy, so a three-stage entry completes around 19% below buy 1
    rather than 20%.

    DCA stops once initials have been taken. Past that point the position is
    being wound down, and adding to it would contradict the exit that is
    already in progress.

    DCA also stops at the absolute floor. The floor check inside exit_logic
    runs first within that function, but this function is called BEFORE it in
    the monitor cycle - so without the guard below, a position sitting under
    the floor would take one more tranche and then be sold at the same price
    microseconds later. In a dry run that only distorts the log; live it is
    real capital committed to a position already being closed, plus a wasted
    swap fee. The floor value is read from exit_logic so there is one
    definition of "dead" rather than two that can drift apart.
    """
    if position["initials_taken"] or not position["pending_tranches"]:
        return None

    if current_mc <= exit_logic.ABSOLUTE_FLOOR_MC:
        log.info(
            "DCA   %-10s skipped: $%s is at or below the $%s floor, "
            "position is closing",
            position["ticker"], f"{current_mc:,.0f}",
            f"{exit_logic.ABSOLUTE_FLOOR_MC:,.0f}",
        )
        return None

    next_tranche = position["pending_tranches"][0]
    drop_pct = next_tranche.get(
        "drop_pct_from_previous_fill", next_tranche.get("drop_pct")
    )
    if drop_pct is None:
        # An unrecognised tranche format - refuse to guess a trigger for it.
        log.warning(
            "DCA tranche for %s has no drop percentage - skipping it. "
            "src/entry_logic.py is likely an outdated version.",
            position["ticker"],
        )
        position["pending_tranches"].pop(0)
        return None
    drop = drop_pct / 100.0
    trigger_mc = position["last_fill_mc"] * (1 - drop)

    if current_mc > trigger_mc:
        return None

    # POSITION SIZE CAP (Stage 3 safety, 28 Aug 2026). Under normal operation
    # a position's tranches can never sum past MAX_POSITION_SOL - the
    # upfront on_message() gate already rejects any call whose planned
    # aggregate exceeds it, and tranches are just fixed fractions of that
    # already-approved total. But a position opened under an OLDER, larger
    # sizing regime can still have pending tranches on disk sized for that
    # regime - if MAX_POSITION_SOL is later lowered (as this stage does,
    # 0.4 -> 0.15), those tranches could take the position over the NEW cap.
    # Abandoned rather than retried: nothing about the wallet's state can
    # ever make an already-too-large tranche fit under a fixed cap, so
    # leaving it pending would just repeat this log line every cycle forever.
    prospective_total = position["sol_invested"] + next_tranche["sol"]
    if prospective_total > config.MAX_POSITION_SOL:
        log.warning(
            "DCA   %-10s tranche %d ABANDONED: would take position to "
            "%.3f SOL, over the %.3f SOL cap - likely opened under an "
            "older, larger sizing regime. Not retried.",
            position["ticker"], next_tranche["stage"], prospective_total,
            config.MAX_POSITION_SOL,
        )
        position["pending_tranches"].pop(0)
        return None

    # RESERVE CHECK (Stage 1 safety, 27 Aug 2026). "Before any buy" applies
    # to every DCA tranche, not just the first. The trigger condition above
    # is left intact (not popped) so a blocked tranche is simply retried on
    # the next cycle once the wallet has room again, rather than being lost.
    if not await check_reserve_ok(next_tranche["sol"]):
        log.info(
            "DCA   %-10s tranche %d skipped: reserve floor would be breached "
            "| trade %.3f SOL",
            position["ticker"], next_tranche["stage"], next_tranche["sol"],
        )
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

    data_logger.log_fill(
        "buy", position, next_tranche["sol"], current_mc,
        stage=next_tranche["stage"],
    )

    return {
        "stage": next_tranche["stage"],
        "sol": next_tranche["sol"],
        "mc": current_mc,
        "new_entry_mc": position["entry_mc"],
    }


# ==========================================================================
# MONITOR LOOP
#
# Two watchdogs below are log-only diagnostics added after the 16 Aug
# overnight run, where a 2.5-hour Windows sleep went completely unnoticed in
# the log. Both are pure functions of a timestamp so they can be
# unit-tested without running the asyncio loop, and neither ever raises,
# exits, or changes what gets traded.
# ==========================================================================


def suspended_gap_seconds(previous_wall_clock, now=None,
                           threshold=SUSPENSION_THRESHOLD_SECONDS):
    """
    Gap in seconds since the last monitor cycle, if it looks like a
    suspend/sleep (exceeds threshold) rather than normal jitter; else None.
    """
    if previous_wall_clock is None:
        return None
    if now is None:
        now = time.time()
    gap = now - previous_wall_clock
    return gap if gap > threshold else None


def stale_fetch_gap_seconds(last_success_at, now=None, has_open_positions=True,
                             threshold=STALE_FETCH_WARNING_SECONDS):
    """
    Gap in seconds since the last successful market data fetch, if there are
    open positions and it exceeds threshold; else None.
    """
    if not has_open_positions or last_success_at is None:
        return None
    if now is None:
        now = time.time()
    gap = now - last_success_at
    return gap if gap > threshold else None


# Wall-clock time.time() of the previous monitor cycle, and of the last
# market data fetch that succeeded. Seeded at import so a fresh start does
# not immediately look suspended or stale.
_last_cycle_wall_clock = None
_last_successful_fetch_at = time.time()


async def monitor_positions():
    """Re-prices every open position on a timer and applies the exit rules."""
    global _last_cycle_wall_clock

    async with aiohttp.ClientSession() as session:
        while True:
            now = time.time()

            gap = suspended_gap_seconds(_last_cycle_wall_clock, now)
            if gap is not None:
                log.warning(
                    "PROCESS SUSPENDED for %.0fs (%.1fh) - machine likely "
                    "slept; open positions were unmanaged",
                    gap, gap / 3600,
                )
            _last_cycle_wall_clock = now

            has_open_positions = any(not p["closed"] for p in POSITIONS.values())
            stale_gap = stale_fetch_gap_seconds(
                _last_successful_fetch_at, now, has_open_positions,
            )
            if stale_gap is not None:
                log.warning(
                    "NO SUCCESSFUL MARKET DATA FETCH for %.0fs (%.1fmin) with "
                    "open positions - wedged loop or dead network",
                    stale_gap, stale_gap / 60,
                )

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


# Timestamp of the last status heartbeat.
_last_status_at = 0.0


async def _monitor_once(session):
    global _last_status_at, _last_successful_fetch_at

    open_positions = {k: v for k, v in POSITIONS.items() if not v["closed"]}
    if not open_positions:
        return

    market_caps = await market_data.fetch_market_caps(session, list(open_positions))
    _last_successful_fetch_at = time.time()
    now = time.time()
    changed = False

    # Periodic proof of life. Also surfaces positions the API has stopped
    # returning data for, which is itself a meaningful signal - a token that
    # has vanished from Jupiter is usually one with no liquidity left.
    if now - _last_status_at >= STATUS_INTERVAL_SECONDS:
        _last_status_at = now
        for contract, position in open_positions.items():
            mc = market_caps.get(contract)
            if mc is None:
                log.warning("STATUS %-10s NO PRICE DATA returned", position["ticker"])
                continue
            value = position_value_sol(position, mc)
            pnl = value + position["realised_sol"] - position["sol_invested"]
            log.info(
                "STATUS %-10s $%s  (entry $%s)  holding %.4f SOL  P&L %+.4f SOL",
                position["ticker"], f"{mc:,.0f}", f"{position['entry_mc']:,.0f}",
                value, pnl,
            )
            data_logger.log_snapshot(position, mc, value, pnl)

    for contract, position in open_positions.items():
        current_mc = market_caps.get(contract)
        if current_mc is None:
            continue

        fill = await check_dca_fills(position, current_mc)
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
            data_logger.log_fill(
                "sell", position, None, current_mc,
                exit_type=action["exit_type"],
                pct_of_original=action["pct_of_original_position"],
                proceeds_sol=proceeds,
                reason=action["reason"],
                position_closed=action["position_closed"],
            )

            if action["position_closed"]:
                # Recorded so the loss cooldown can time itself, and so later
                # analysis can group closes by how they ended without having
                # to replay the fill history.
                position["closed_at"] = datetime.now(timezone.utc).isoformat()
                position["last_exit_type"] = action["exit_type"]

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
        #
        # TEMPORARY DEBUG: some multiplier updates ("Just did xN" format) have
        # no ticker, and the parser currently returns nothing for them. This
        # branch captures the raw text of any such case so parser.py can be
        # fixed against the real format instead of a guess. Remove once fixed.
        if kind != "unknown":
            if not parsed.get("ticker"):
                log.warning(
                    "UPDATE MISSING TICKER | raw: %s",
                    text[:150].replace("\n", " "),
                )
            else:
                log.info("UPDATE %-9s %s", parsed.get("ticker"), kind)
        return

    if not parsed["parse_ok"]:
        log.warning("PARSE FAIL  missing %s | %s",
                    parsed["missing_fields"], text[:60].replace("\n", " "))
        data_logger.log_call(
            "parse_fail", parsed,
            reason=f"missing {parsed['missing_fields']}", raw_text=text,
        )
        return

    # Staleness gate. A reconnect makes Telethon redeliver every message it
    # missed while disconnected, all in the same second, with nothing marking
    # them as old. This is what let a backlog flush fill three hours-dead
    # coins on 16 Aug 2026 as if they had just arrived.
    age_seconds = call_age_seconds(event.message.date)
    if is_call_stale(event.message.date):
        log.info(
            "REJECT %-9s call is %.0fs old  | older than the %ds staleness limit",
            parsed["ticker"], age_seconds, MAX_CALL_AGE_SECONDS,
        )
        data_logger.log_call(
            "rejected_stale_call", parsed,
            reason=(f"call message is {age_seconds:.0f}s old, older than the "
                    f"{MAX_CALL_AGE_SECONDS}s staleness limit"),
        )
        return

    contract = parsed["contract_address"]

    # Duplicate protection. A channel repost or a reconnect that redelivers a
    # message must not open a second position in the same token.
    if contract in POSITIONS:
        log.info("SKIP  %-10s already held or previously traded", parsed["ticker"])
        data_logger.log_call(
            "duplicate", parsed,
            reason="contract already held or previously traded",
        )
        return

    # Same-ticker-open guard. Duplicate protection above is keyed on contract
    # address, so a relaunch under the same ticker (a different contract) is
    # invisible to it - which is how PANDA held two simultaneous positions on
    # 16 Aug 2026, seven minutes apart on different contracts, combined
    # -0.357 SOL.
    open_contract = open_position_for_ticker(parsed["ticker"])
    if open_contract is not None:
        log.info(
            "SKIP  %-10s already open under contract %s",
            parsed["ticker"], open_contract,
        )
        data_logger.log_call(
            "rejected_ticker_open", parsed,
            reason=f"ticker already open under contract {open_contract}",
        )
        return

    # Concurrency cap (Stage 1 safety, 27 Aug 2026). A wallet has finite SOL
    # to fund exits from, so the number of positions open at once is bounded
    # regardless of how many good calls arrive - see MAX_POSITION_SOL below
    # for the matching per-coin cap.
    open_count = sum(1 for p in POSITIONS.values() if not p["closed"])
    if open_count >= config.MAX_CONCURRENT_POSITIONS:
        log.info(
            "SKIP  %-10s concurrency cap reached (%d/%d open positions)",
            parsed["ticker"], open_count, config.MAX_CONCURRENT_POSITIONS,
        )
        data_logger.log_call(
            "rejected_concurrency_cap", parsed,
            reason=(f"{open_count} of {config.MAX_CONCURRENT_POSITIONS} "
                    f"concurrent positions already open"),
        )
        return

    # Time-of-day gate. Checked before the PCR is scored, because there is no
    # point valuing a call that cannot be acted on.
    #
    # This gates ENTRIES ONLY. The monitor loop keeps running whatever the
    # clock says, so open positions are still managed for stop-loss, trailing
    # stop and floor exits outside the window. Stopping the process instead
    # is what produced the 81-94% trailing-stop overshoots on Ratatouille,
    # BEAR and RODRI.
    window_open, window_reason = trading_window.window_status()
    if not window_open:
        log.info("SKIP  %-10s outside trading window | %s",
                 parsed["ticker"], window_reason)
        data_logger.log_call(
            "rejected_time_window", parsed,
            {"action": "reject_time_window"},
            reason=window_reason,
        )
        return

    # Loss cooldown, keyed on ticker rather than contract. A relaunched token
    # carries a new contract address, so the duplicate check above cannot see
    # it - which is how ALING opened and floor-closed three times in eight
    # minutes on 10 Aug for a combined -0.665 SOL.
    cooldown_left = ticker_cooldown_remaining(parsed["ticker"])
    if cooldown_left is not None:
        log.info(
            "SKIP  %-10s loss cooldown, %.0f min remaining of %d",
            parsed["ticker"], cooldown_left, LOSS_COOLDOWN_MINUTES,
        )
        data_logger.log_call(
            "rejected_cooldown", parsed,
            {"action": "reject_cooldown"},
            reason=(f"ticker closed at a loss within the last "
                    f"{LOSS_COOLDOWN_MINUTES} minutes "
                    f"({cooldown_left:.0f} min remaining)"),
        )
        return

    decision = entry_logic.decide_entry(parsed)

    if decision["action"] == "reject":
        log.info("REJECT %-9s $%s  | %s", parsed["ticker"],
                 f"{parsed['market_cap']:,.0f}", decision["reason"])
        data_logger.log_call("rejected", parsed, decision)
        return

    # Position size cap (Stage 1 safety, 27 Aug 2026). total_lot_sol is the
    # AGGREGATE planned commitment across every DCA tranche, not one tranche
    # - entry_logic.split_into_tranches divides exactly this figure - so
    # checking it once, here, before the first tranche fills, enforces the
    # cap across the whole position rather than per-buy.
    if decision["total_lot_sol"] > config.MAX_POSITION_SOL:
        log.info(
            "REJECT %-9s planned lot %.3f SOL exceeds MAX_POSITION_SOL %.3f SOL",
            parsed["ticker"], decision["total_lot_sol"], config.MAX_POSITION_SOL,
        )
        data_logger.log_call(
            "rejected_position_size_cap", parsed, decision,
            reason=(f"planned lot {decision['total_lot_sol']:.3f} SOL exceeds "
                    f"the {config.MAX_POSITION_SOL:.3f} SOL cap"),
        )
        return

    await open_position(decision, parsed)


# ==========================================================================
# ENTRY POINT
# ==========================================================================


async def main():
    # Credential presence/shape is already validated by `import config` above
    # (it fails loudly there, before this function is even reached), so the
    # old runtime check that used to live here is redundant and was removed
    # rather than duplicating config.py's own validation.

    # Guards against running with a mismatched entry_logic version - the
    # exact failure mode that produced a monitor-loop crash on every cycle.
    #
    # Since Stage 3, MIN_BUY_SOL is an alias for config.MIN_BUY_SOL rather
    # than an independent hardcoded value, so this now also fires if
    # MIN_BUY_SOL is deliberately changed via .env, not only if the file
    # itself is stale - update the expected value below alongside .env if
    # that is ever done on purpose.
    if getattr(entry_logic, "MIN_BUY_SOL", None) != 0.075:
        log.warning(
            "src/entry_logic.py looks OUTDATED (MIN_BUY_SOL=%s, expected 0.075). "
            "DCA tranching will not behave as designed.",
            getattr(entry_logic, "MIN_BUY_SOL", "missing"),
        )

    # Same idea for the strategy constants set on 10 Aug 2026. Commit 666267d
    # claimed all three files but only entry_logic.py reached disk, so the bot
    # ran a full session on the old trailing stop with no entry guards and
    # nothing in the log said so. These checks make that state loud.
    _expected = [
        (entry_logic, "DCA_WEIGHTS_THREE", (0.45, 0.30, 0.25)),
        (exit_logic, "TRAILING_STOP_DRAWDOWN", 0.60),
    ]
    for module, name, expected in _expected:
        actual = getattr(module, name, "missing")
        if actual != expected:
            log.warning(
                "%s.%s is %s, expected %s - that file is an OUTDATED version "
                "and this session will not run the intended strategy.",
                module.__name__, name, actual, expected,
            )

    log.info("=" * 66)
    log.info("DRY RUN - simulated fills only, no transaction is ever signed or submitted")
    log.info("channel: %s | polling every %ss", CHANNEL, POLL_INTERVAL_SECONDS)
    open_count = sum(1 for p in POSITIONS.values() if not p["closed"])
    log.info("restored %d position(s), %d still open", len(POSITIONS), open_count)

    log.info("-" * 66)
    config.log_resolved_config()

    # Print the guards at startup so the log itself records which version of
    # the rules produced a session's trades. The 10 Aug optimisation was
    # committed but never reached runner.py, and nothing in the log would
    # have revealed that.
    log.info("-" * 66)
    log.info("entry guards: max gap -%d%% | floor $%s | loss cooldown %d min",
             MAX_ENTRY_GAP_PCT, f"{exit_logic.ABSOLUTE_FLOOR_MC:,.0f}",
             LOSS_COOLDOWN_MINUTES)
    is_open, reason = trading_window.window_status()
    log.info("trading window: %s - %s", "OPEN" if is_open else "SHUT", reason)
    if not is_open:
        log.info("  entries are gated off; exits on open positions still run")
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
        raise SystemExit(
            "Live execution is not implemented. Set DRY_RUN=true in .env."
        )
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped by user. %d position(s) saved.", len(POSITIONS))