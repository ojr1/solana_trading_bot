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

import data_logger
import entry_logic
import exit_logic
import market_data
import parser as message_parser
import trading_window

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

# How often a status line is written for each open position. Without this the
# monitor is silent unless something fails, so there is no way to distinguish
# "nothing is happening" from "pricing has silently stopped working".
STATUS_INTERVAL_SECONDS = 300

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

    target = ticker.strip().lower()
    now = datetime.now(timezone.utc)
    longest = None

    for position in POSITIONS.values():
        if not position.get("closed"):
            continue
        if (position.get("ticker") or "").strip().lower() != target:
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
    indexed), the call figure is used as a fallback and flagged in the log.
    """
    contract = decision["contract_address"]
    tranches = decision["tranches"]
    first = tranches[0]
    call_mc = call["market_cap"]

    live_mc = None
    try:
        async with aiohttp.ClientSession() as session:
            caps = await market_data.fetch_market_caps(session, [contract])
        live_mc = caps.get(contract)
    except aiohttp.ClientError as exc:
        log.warning("Live price fetch failed for %s: %s", decision["ticker"], exc)

    if live_mc is not None:
        if live_mc >= entry_logic.MC_HARD_CUT:
            log.info(
                "REJECT %-9s live $%s (call said $%s)  | above hard cut at fill time",
                decision["ticker"], f"{live_mc:,.0f}", f"{call_mc:,.0f}",
            )
            data_logger.log_call(
                "rejected_fill", call, decision, live_mc=live_mc,
                reason="above hard cut at fill time",
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
            )
            return

        entry_mc = live_mc
        log.info(
            "FILL  %-10s live $%s vs call $%s  (gap %+.1f%%)",
            decision["ticker"], f"{live_mc:,.0f}", f"{call_mc:,.0f}", gap_pct,
        )
    else:
        entry_mc = call_mc
        log.warning(
            "FILL  %-10s no live price - falling back to call figure $%s",
            decision["ticker"], f"{call_mc:,.0f}",
        )

    # GUARD 2: the entry price is already at or below the level at which the
    # exit logic would immediately sell. Buying here means paying a swap fee
    # to open a position that closes on the next monitor cycle. Applied to
    # whichever figure became entry_mc above, so the call-figure fallback is
    # covered too.
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

    data_logger.log_call("bought", call, decision, live_mc=live_mc)
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


def check_dca_fills(position, current_mc):
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


# Timestamp of the last status heartbeat.
_last_status_at = 0.0


async def _monitor_once(session):
    global _last_status_at

    open_positions = {k: v for k, v in POSITIONS.items() if not v["closed"]}
    if not open_positions:
        return

    market_caps = await market_data.fetch_market_caps(session, list(open_positions))
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

    # Guards against running with a mismatched entry_logic version - the
    # exact failure mode that produced a monitor-loop crash on every cycle.
    if getattr(entry_logic, "MIN_BUY_SOL", None) != 0.10:
        log.warning(
            "src/entry_logic.py looks OUTDATED (MIN_BUY_SOL=%s, expected 0.1). "
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
    log.info("DRY RUN - no wallet loaded, no trades executed")
    log.info("channel: %s | polling every %ss", CHANNEL, POLL_INTERVAL_SECONDS)
    open_count = sum(1 for p in POSITIONS.values() if not p["closed"])
    log.info("restored %d position(s), %d still open", len(POSITIONS), open_count)

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
        raise SystemExit("Live execution is not implemented. Keep DRY_RUN = True.")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped by user. %d position(s) saved.", len(POSITIONS))