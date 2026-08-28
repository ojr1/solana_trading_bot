"""
data_logger.py - structured JSONL logging for later analysis.

The dated .log file in logs/ is written for a human reading a terminal. This
module writes the same events in a form a computer can analyse: one JSON
object per line, appended, never rewritten.

    data/calls.jsonl        every call the bot saw and what it decided
    data/fills.jsonl        every buy and every sell
    data/snapshots.jsonl    periodic price observations on open positions
    data/price_history.jsonl  per-cycle price observations, post-initials only

Why JSONL rather than one big JSON file: a JSON file must be complete to be
readable, so a process killed mid-write corrupts the lot. JSONL appends one
self-contained line at a time, so a crash costs at most the final line.

Excel analogy: a CSV you only ever add rows to. Reading it back later is one
line of pandas - pd.read_json("data/calls.jsonl", lines=True) - which gives
you a worksheet-shaped table.

RULE: logging must never crash the bot. Every write is wrapped, and a failure
is reported to the console then swallowed. A lost log line is an inconvenience;
a crashed process holding open positions is not.

    python src/data_logger.py        runs the self-test

SCHEMA HISTORY
--------------
  v1  (03 Aug 2026)  original three files
  v2  (15 Aug 2026)  call records gained the Jupiter detail fields:
                     top_holders_pct, organic_score, dev_migrations,
                     dev_mints, liquidity, launchpad, live_holder_count.
  v3  (28 Aug 2026)  data/price_history.jsonl added (Stage 7). One row per
                     open position per monitor cycle, but ONLY once initials
                     have been taken for that position - before initials, no
                     trailing stop is active at any drawdown setting, so a
                     pre-initials price point cannot inform a stepped-stop
                     comparison and is not worth the extra volume. This is a
                     deliberate scope decision, not a sampling shortcut: it
                     means price_history.jsonl can never be used to analyse
                     entry-side behaviour (e.g. how close a position got to
                     stop-loss or the absolute floor before ever reaching
                     initials) - only post-initials exit behaviour. This file
                     also intentionally omits run_id, event and ticker (kept
                     on the other three files) - see log_price_point()'s
                     docstring for why.

Why the version number matters: records written before 15 Aug have no such
keys at all, and records written after have them but often as null. Analysis
that pools the two without checking schema_version will read "field absent"
and "field present but empty" as the same thing, which they are not.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

# Bump this when a schema changes, so analysis can tell record shapes apart.
SCHEMA_VERSION = 3

DATA_DIR = Path("data")
CALLS_FILE = DATA_DIR / "calls.jsonl"
FILLS_FILE = DATA_DIR / "fills.jsonl"
SNAPSHOTS_FILE = DATA_DIR / "snapshots.jsonl"
PRICE_HISTORY_FILE = DATA_DIR / "price_history.jsonl"

log = logging.getLogger("data_logger")

# Identifies one continuous run of the bot. Every record written by this
# process carries it, so a session can be isolated during analysis - useful
# when downtime means a gap in coverage rather than a gap in the market.
RUN_ID = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{os.getpid()}"

# The Jupiter detail fields carried on a call record. Imported from
# market_data so there is one definition rather than two that drift apart.
# Falls back to a literal list if market_data cannot be imported, because
# logging must never be the reason the bot fails to start.
try:
    from market_data import DETAIL_COLUMNS
except ImportError:  # pragma: no cover - defensive only
    DETAIL_COLUMNS = (
        "top_holders_pct", "organic_score", "dev_migrations",
        "dev_mints", "liquidity", "launchpad", "live_holder_count",
    )
    log.warning("data_logger could not import market_data; using a static "
                "copy of the detail field list. Check src/market_data.py.")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _write(path, record):
    """
    Appends one record as a single JSON line.

    Opened and closed per write rather than held open. At this volume - tens
    of calls a day, one snapshot per position every five minutes - the cost is
    irrelevant, and it means the file on disk is always complete and readable
    even while the bot is running.
    """
    record = {
        "ts": _now(),
        "run_id": RUN_ID,
        "schema_version": SCHEMA_VERSION,
        **record,
    }
    try:
        DATA_DIR.mkdir(exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except OSError as exc:
        log.error("data_logger could not write to %s: %s", path.name, exc)
    except (TypeError, ValueError) as exc:
        # A value that will not serialise. default=str catches most cases;
        # this is the backstop so a bad field never takes the bot down.
        log.error("data_logger could not serialise a %s record: %s", path.stem, exc)


# ==========================================================================
# CALLS
#
# One record per call the bot saw, whatever it decided. Rejects matter as much
# as buys: without them there is no control group, and any later analysis of
# "which calls were good" is measuring only the ones already selected.
# ==========================================================================


def log_call(event, parsed=None, decision=None, live_mc=None, reason=None,
             raw_text=None, token_details=None):
    """
    Records a call and the decision taken on it.

    event is one of:
        bought          entry accepted, position opened
        rejected        entry logic declined it
        rejected_fill   accepted, then declined once the live price was seen
        parse_fail      recognised as a call but fields were missing
        duplicate       contract already held or previously traded
        rejected_time_window   call arrived outside the trading window
        rejected_cooldown      ticker closed at a loss within the cooldown
        rejected_no_price      no live price available at fill time
        rejected_stale_call    call message older than the staleness limit
        rejected_ticker_open   ticker already has an OPEN position (different
                                contract)

    token_details (added 15 Aug 2026) is the dictionary returned by
    market_data.fetch_token_details() for this contract. It is only available
    once the live price has been fetched, which happens at fill time - so
    calls rejected BEFORE that point (time window, cooldown, PCR reject,
    duplicate) carry these fields as null. That is expected: fetching Jupiter
    data for every call the bot will never touch would triple API usage for
    a control group that has no outcome to correlate against anyway.

    Every detail key is written on every call record whether or not a value
    was found, so the column exists in the data from day one and pandas reads
    it back as a proper column rather than a ragged table.
    """
    parsed = parsed or {}
    decision = decision or {}
    token_details = token_details or {}

    record = {
        "event": event,
        "ticker": parsed.get("ticker"),
        "contract_address": parsed.get("contract_address"),
        "token_name": parsed.get("token_name"),
        # Entry inputs, kept so the PCR can be re-derived or re-fitted later.
        "call_mc": parsed.get("market_cap"),
        "live_mc": live_mc,
        "gt_score": parsed.get("gt_score"),
        "holders": parsed.get("holders"),
        "age_minutes": parsed.get("age_minutes"),
        "bundled_pct": parsed.get("bundled_pct"),
        # What the entry logic concluded.
        "pcr": decision.get("pcr"),
        "action": decision.get("action"),
        "total_lot_sol": decision.get("total_lot_sol"),
        "tranche_count": len(decision.get("tranches", [])) or None,
        "reason": reason or decision.get("reason"),
    }

    # Jupiter detail fields. Written unconditionally so the schema is stable.
    for column in DETAIL_COLUMNS:
        record[column] = token_details.get(column)

    # Only kept for parse failures, and truncated - the point is to see the
    # message format that broke, not to archive the channel.
    if raw_text is not None:
        record["raw_text"] = raw_text[:300]

    _write(CALLS_FILE, record)


# ==========================================================================
# FILLS
#
# Every buy and every sell. In the dry run a fill is simulated and always
# succeeds; in production it is a chain transaction that can fail, cost more
# than quoted, or return a different quantity than expected. The schema is
# extended at that point - see SCHEMA_VERSION.
# ==========================================================================


def log_fill(event, position, sol_amount, mc, **extra):
    """
    Records a buy or a sell.

    event is "buy" or "sell". extra carries the fields specific to each:
    stage for buys; exit_type, pct_of_original, proceeds_sol, reason and
    position_closed for sells.
    """
    record = {
        "event": event,
        "ticker": position.get("ticker"),
        "contract_address": position.get("contract_address"),
        "sol_amount": sol_amount,
        "mc_at_fill": mc,
        # Position state after the fill, so each row stands alone and analysis
        # does not have to replay the whole sequence to know where things stood.
        "entry_mc": position.get("entry_mc"),
        "call_mc": position.get("call_mc"),
        "peak_mc": position.get("peak_mc"),
        "sol_invested": position.get("sol_invested"),
        "realised_sol": position.get("realised_sol"),
        "tokens_remaining": position.get("tokens_remaining"),
        "initials_taken": position.get("initials_taken"),
        "pcr": position.get("pcr"),
        **extra,
    }
    _write(FILLS_FILE, record)


# ==========================================================================
# SNAPSHOTS
#
# Written on the status heartbeat, not on every poll. At a 5 second poll and
# several open positions this file would otherwise grow by roughly 17,000 rows
# a day per position, almost all of it noise.
# ==========================================================================


def log_snapshot(position, current_mc, value_sol, pnl_sol):
    """One periodic observation of an open position."""
    record = {
        "event": "snapshot",
        "ticker": position.get("ticker"),
        "contract_address": position.get("contract_address"),
        "mc": current_mc,
        "entry_mc": position.get("entry_mc"),
        "peak_mc": position.get("peak_mc"),
        "value_sol": value_sol,
        "pnl_sol": pnl_sol,
        "sol_invested": position.get("sol_invested"),
        "realised_sol": position.get("realised_sol"),
        "initials_taken": position.get("initials_taken"),
        "pending_tranches": len(position.get("pending_tranches", [])),
    }
    _write(SNAPSHOTS_FILE, record)


# ==========================================================================
# PRICE HISTORY (Stage 7)
#
# Dense per-cycle price observations, added specifically to answer whether a
# stepped trailing stop would beat the current flat one - see
# EXIT_RULES_ANALYSIS.md. Deliberately narrower than the other three files:
#
#   - Post-initials only. Before initials, no trailing stop is active at any
#     drawdown setting, so a pre-initials row cannot inform that question and
#     is not logged - a stated scope reduction (see SCHEMA HISTORY v3 above),
#     not a volume shortcut applied indiscriminately.
#   - Trimmed record: no run_id (nothing else in this codebase keys off it
#     for price_history specifically, and every row already carries ts), no
#     event (this file only ever holds one kind of row), no ticker (derivable
#     from contract_address via positions.json - kept out to save the ~15
#     bytes/row it would otherwise cost across a much higher row count than
#     the other three files).
#   - Written with its own inline try/except, not the shared _write() helper,
#     so a lost row here logs at WARNING rather than _write()'s ERROR - this
#     is a diagnostic for future analysis, not an accounting record; losing
#     one is a shrug, not an incident.
# ==========================================================================


def log_price_point(position, current_mc):
    """
    One row per open position per monitor cycle, once initials have been
    taken (see module docstring for why not before). Call with the mc value
    already fetched this cycle - never fetches its own.

    peak_mc is written as max(position['peak_mc'], current_mc) rather than
    the stored field verbatim: exit_logic.check_exit_conditions() is what
    actually updates position['peak_mc'], and runner.py calls this before
    that happens each cycle, so the stored value alone would lag one cycle
    behind on any row where current_mc is itself a new high.

    Never raises. A failure here must not be able to take down the monitor
    loop it is diagnosing - it is logged as a WARNING and swallowed.
    """
    if not position.get("initials_taken"):
        return

    record = {
        "ts": _now(),
        "schema_version": SCHEMA_VERSION,
        "contract_address": position.get("contract_address"),
        "mc": current_mc,
        "peak_mc": max(position.get("peak_mc", current_mc), current_mc),
        "initials_taken": position.get("initials_taken"),
    }
    try:
        DATA_DIR.mkdir(exist_ok=True)
        with open(PRICE_HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except OSError as exc:
        log.warning("data_logger could not write to %s: %s",
                    PRICE_HISTORY_FILE.name, exc)
    except (TypeError, ValueError) as exc:
        log.warning("data_logger could not serialise a price_history record: %s", exc)


# ==========================================================================
# SELF-TEST
# ==========================================================================


def _run_self_test():
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    fake_parsed = {
        "ticker": "TESTCOIN",
        "contract_address": "TestContract1111111111111111111111111111111",
        "token_name": "a test token",
        "market_cap": 24_000,
        "gt_score": 7,
        "holders": 180,
        "age_minutes": 12,
        "bundled_pct": 8.5,
    }
    fake_decision = {
        "action": "buy",
        "pcr": 0.612,
        "total_lot_sol": 0.38,
        "tranches": [{"stage": 1}, {"stage": 2}, {"stage": 3}],
    }
    fake_details = {
        "top_holders_pct": 21.7,
        "organic_score": 63.2,
        "dev_migrations": 3.0,
        "dev_mints": 11.0,
        "liquidity": 18_400.5,
        "launchpad": "pump.fun",
        "live_holder_count": 412.0,
        "market_cap": 24_000.0,
    }
    fake_position = {
        "ticker": "TESTCOIN",
        "contract_address": fake_parsed["contract_address"],
        "entry_mc": 24_000,
        "call_mc": 26_500,
        "peak_mc": 51_000,
        "sol_invested": 0.114,
        "realised_sol": 0.0,
        "tokens_remaining": 0.114,
        "initials_taken": False,
        "pcr": 0.612,
        "pending_tranches": [{"stage": 2}, {"stage": 3}],
    }

    print("Writing test records to data/ ...\n")

    # A buy, carrying the full Jupiter detail set.
    log_call("bought", fake_parsed, fake_decision, live_mc=24_000,
             token_details=fake_details)
    # A reject that happened before any Jupiter fetch - details are null.
    log_call("rejected", fake_parsed, {"action": "reject",
                                       "reason": "market cap above hard cut"})
    # A reject at fill time - details ARE available here.
    log_call("rejected_fill", fake_parsed, fake_decision, live_mc=5_100,
             reason="below the -35% entry gap limit",
             token_details=fake_details)
    log_call("parse_fail", raw_text="Just did x3 - some message with no ticker")

    log_fill("buy", fake_position, 0.114, 24_000, stage=1)
    log_fill("sell", fake_position, None, 47_000, exit_type="initials",
             pct_of_original=50.0, proceeds_sol=0.223,
             reason="initials: up 96% from entry", position_closed=False)

    log_snapshot(fake_position, 33_500, 0.159, 0.045)

    failures = 0

    for path in (CALLS_FILE, FILLS_FILE, SNAPSHOTS_FILE):
        if not path.exists():
            print(f"  MISSING  {path}")
            failures += 1
            continue
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        # Every line must parse on its own - that is the whole point of JSONL.
        for i, line in enumerate(lines, 1):
            try:
                json.loads(line)
            except json.JSONDecodeError:
                print(f"  BAD LINE {i} in {path.name}")
                failures += 1
                break
        else:
            print(f"  OK  {path}  ({len(lines)} line(s), all valid JSON)")

    # Schema check: every call record must carry every detail column, whether
    # or not a value was found. A missing key is a schema bug, not a blank.
    print("\nSchema check on the call records written just now:")
    with open(CALLS_FILE, encoding="utf-8") as f:
        recent = [json.loads(line) for line in f][-4:]

    for record in recent:
        missing = [c for c in DETAIL_COLUMNS if c not in record]
        if missing:
            print(f"  FAIL  {record['event']:<16} missing keys: {missing}")
            failures += 1
        else:
            filled = sum(1 for c in DETAIL_COLUMNS
                         if record.get(c) is not None)
            print(f"  OK    {record['event']:<16} all "
                  f"{len(DETAIL_COLUMNS)} detail keys present, "
                  f"{filled} populated")

    print(f"\nrun_id for this session: {RUN_ID}")
    print(f"schema_version written:  {SCHEMA_VERSION}")

    if failures == 0:
        print("\nDATA_LOGGER SELF-TEST PASSED")
    else:
        print(f"\nDATA_LOGGER SELF-TEST FAILED - {failures} problem(s) above")
    return failures


if __name__ == "__main__":
    raise SystemExit(1 if _run_self_test() else 0)
