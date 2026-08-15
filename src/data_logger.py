"""
data_logger.py - structured JSONL logging for later analysis.

The dated .log file in logs/ is written for a human reading a terminal. This
module writes the same events in a form a computer can analyse: one JSON
object per line, appended, never rewritten.

    data/calls.jsonl        every call the bot saw and what it decided
    data/fills.jsonl        every buy and every sell
    data/snapshots.jsonl    periodic price observations on open positions

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
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

# Bump this when a schema changes, so analysis can tell record shapes apart.
SCHEMA_VERSION = 1

DATA_DIR = Path("data")
CALLS_FILE = DATA_DIR / "calls.jsonl"
FILLS_FILE = DATA_DIR / "fills.jsonl"
SNAPSHOTS_FILE = DATA_DIR / "snapshots.jsonl"

log = logging.getLogger("data_logger")

# Identifies one continuous run of the bot. Every record written by this
# process carries it, so a session can be isolated during analysis - useful
# when downtime means a gap in coverage rather than a gap in the market.
RUN_ID = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{os.getpid()}"


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
             raw_text=None):
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
    """
    parsed = parsed or {}
    decision = decision or {}

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

    log_call("bought", fake_parsed, fake_decision, live_mc=24_000)
    log_call("rejected", fake_parsed, {"action": "reject",
                                       "reason": "market cap above hard cut"})
    log_call("parse_fail", raw_text="Just did x3 - some message with no ticker")

    log_fill("buy", fake_position, 0.114, 24_000, stage=1)
    log_fill("sell", fake_position, None, 47_000, exit_type="initials",
             pct_of_original=50.0, proceeds_sol=0.223,
             reason="initials: up 96% from entry", position_closed=False)

    log_snapshot(fake_position, 33_500, 0.159, 0.045)

    for path in (CALLS_FILE, FILLS_FILE, SNAPSHOTS_FILE):
        if not path.exists():
            print(f"  MISSING  {path}")
            continue
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        # Every line must parse on its own - that is the whole point of JSONL.
        for i, line in enumerate(lines, 1):
            try:
                json.loads(line)
            except json.JSONDecodeError:
                print(f"  BAD LINE {i} in {path.name}")
                break
        else:
            print(f"  OK  {path}  ({len(lines)} line(s), all valid JSON)")

    print(f"\nrun_id for this session: {RUN_ID}")
    print("Self-test complete. Inspect the files in data/ to see the shape.")


if __name__ == "__main__":
    _run_self_test()