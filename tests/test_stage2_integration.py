"""
test_stage2_integration.py - integration tests for runner.py's entry guards.

Exercises open_position() and on_message() directly, using fakes for the
Telethon event and mocks for the Jupiter price fetch and the trading-window
clock. No real network call, no Telegram credential, and no write to the
real logs/positions.json or data/*.jsonl is needed or made - every test
isolates POSITIONS and the data_logger output paths first.

Run standalone:   python tests\test_stage2_integration.py
Run full suite:   python tests\run_all.py
"""

import asyncio
import contextlib
import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
SRC_DIR = TESTS_DIR.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import data_logger
import entry_logic
import runner


# ==========================================================================
# Fakes and fixtures
# ==========================================================================


class FakeMessage:
    def __init__(self, raw_text, date):
        self.raw_text = raw_text
        self.date = date


class FakeEvent:
    """Stands in for a Telethon NewMessage event - only .message.raw_text and
    .message.date are ever read by on_message()."""

    def __init__(self, raw_text, date):
        self.message = FakeMessage(raw_text, date)


def fake_contract(tag):
    """A syntactically valid (fake) base58-looking contract address."""
    safe_tag = "".join(c for c in tag if c.isalnum() and c not in "0OIl")
    body = f"Test{safe_tag}"
    return (body + "9" * 44)[:43]


def make_call_text(ticker, contract, mc_str="38.4K", stars=1, holders=286,
                    age="5m", bundled="18"):
    """Builds message text that parser.py will classify and parse as a call."""
    stars_str = "⭐" * stars
    return (
        f"${ticker} (test token)\n"
        f"{contract}\n"
        f"GTscore: {stars_str}\n"
        f"MC: ${mc_str}  Age: {age}  Holders: {holders}\n"
        f"Bundled: {bundled}%\n"
    )


def read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


@contextlib.contextmanager
def isolated_state():
    """
    Isolates one test from real state: a fresh POSITIONS dict (never written
    to logs/positions.json), and data_logger output redirected to a scratch
    directory (never written to data/*.jsonl). Also fixes the trading window
    open, so tests do not depend on the real time of day.

    Yields (positions_dict, calls_jsonl_path).
    """
    tmpdir = tempfile.mkdtemp(prefix="sol_bot_test_")
    tmp_path = Path(tmpdir)
    fresh_positions = {}

    patches = [
        mock.patch.object(runner, "POSITIONS", fresh_positions),
        mock.patch.object(runner, "save_positions", lambda positions: None),
        mock.patch.object(data_logger, "DATA_DIR", tmp_path),
        mock.patch.object(data_logger, "CALLS_FILE", tmp_path / "calls.jsonl"),
        mock.patch.object(data_logger, "FILLS_FILE", tmp_path / "fills.jsonl"),
        mock.patch.object(data_logger, "SNAPSHOTS_FILE", tmp_path / "snapshots.jsonl"),
        mock.patch.object(runner.trading_window, "window_status",
                           lambda *a, **k: (True, "test - always open")),
    ]
    for p in patches:
        p.start()
    try:
        yield fresh_positions, tmp_path / "calls.jsonl"
    finally:
        for p in patches:
            p.stop()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ==========================================================================
# Checks
# ==========================================================================


def check_no_live_price_rejects():
    """Bug 1: no live price -> position NOT opened, rejected_no_price logged."""
    ticker, contract = "NOPRICE", fake_contract("NoPrice")
    call = {
        "ticker": ticker, "contract_address": contract, "token_name": "test",
        "market_cap": 30_000, "gt_score": 3, "holders": 300,
        "age_minutes": 5, "bundled_pct": 10.0, "parse_ok": True,
    }
    decision = entry_logic.decide_entry(call)
    assert decision["action"] == "buy", "fixture call must be a buy for this test to isolate bug 1"

    async def no_price(session, mints):
        return {}  # Jupiter returned nothing for this mint

    with isolated_state() as (positions, calls_path):
        with mock.patch.object(runner.market_data, "fetch_market_caps", no_price):
            asyncio.run(runner.open_position(decision, call))

        assert contract not in positions, "a position was opened with no live price"
        matches = [r for r in read_jsonl(calls_path)
                   if r["event"] == "rejected_no_price" and r["contract_address"] == contract]
        assert matches, "no rejected_no_price record was logged"
        assert matches[0]["live_mc"] is None


def check_stale_call_rejected():
    """Bug 2: a call older than MAX_CALL_AGE_SECONDS -> rejected_stale_call."""
    ticker, contract = "STALE1", fake_contract("Stale1")
    text = make_call_text(ticker, contract)
    old_date = datetime.now(timezone.utc) - timedelta(seconds=400)
    event = FakeEvent(text, old_date)

    with isolated_state() as (positions, calls_path):
        asyncio.run(runner.on_message(event))

        assert contract not in positions, "a stale call should not open a position"
        matches = [r for r in read_jsonl(calls_path)
                   if r["event"] == "rejected_stale_call" and r["ticker"] == ticker]
        assert matches, "no rejected_stale_call record was logged"


def check_fresh_call_accepted():
    """Bug 2: a call only 30s old is NOT treated as stale (guard isn't too aggressive)."""
    ticker, contract = "FRESH1", fake_contract("Fresh1")
    text = make_call_text(ticker, contract, mc_str="20K", stars=3, holders=350,
                           age="5m", bundled="8")
    fresh_date = datetime.now(timezone.utc) - timedelta(seconds=30)
    event = FakeEvent(text, fresh_date)

    async def stable_price(session, mints):
        return {contract: 20_000}  # matches the call figure - 0% gap

    with isolated_state() as (positions, calls_path):
        with mock.patch.object(runner.market_data, "fetch_market_caps", stable_price):
            asyncio.run(runner.on_message(event))

        records = read_jsonl(calls_path)
        assert not [r for r in records if r["event"] == "rejected_stale_call"], \
            "a 30s-old call was wrongly treated as stale"
        assert contract in positions, "a fresh, otherwise-valid call should have opened a position"
        assert [r for r in records if r["event"] == "bought" and r["contract_address"] == contract]


def check_ticker_already_open_rejected():
    """Bug 3: same ticker already OPEN on a different contract -> rejected_ticker_open."""
    ticker = "TICKA"
    contract_open = fake_contract("TickAOld")
    contract_new = fake_contract("TickANew")

    with isolated_state() as (positions, calls_path):
        positions[contract_open] = {
            "ticker": ticker, "contract_address": contract_open, "closed": False,
        }
        text = make_call_text(ticker, contract_new)
        event = FakeEvent(text, datetime.now(timezone.utc))
        asyncio.run(runner.on_message(event))

        assert contract_new not in positions, \
            "a second contract under an already-open ticker should not open a position"
        matches = [r for r in read_jsonl(calls_path)
                   if r["event"] == "rejected_ticker_open" and r["contract_address"] == contract_new]
        assert matches, "no rejected_ticker_open record was logged"


def check_ticker_closed_at_profit_allowed():
    """Bug 3 must not become a permanent ban: a ticker CLOSED at a profit lets a new contract through."""
    ticker = "TICKB"
    contract_old = fake_contract("TickBOld")
    contract_new = fake_contract("TickBNew")

    with isolated_state() as (positions, calls_path):
        positions[contract_old] = {
            "ticker": ticker, "contract_address": contract_old, "closed": True,
            "sol_invested": 0.2, "realised_sol": 0.3,  # closed at a profit
            "closed_at": datetime.now(timezone.utc).isoformat(),
        }
        text = make_call_text(ticker, contract_new, mc_str="20K", stars=3, holders=350,
                               age="5m", bundled="8")
        event = FakeEvent(text, datetime.now(timezone.utc))

        async def stable_price(session, mints):
            return {contract_new: 20_000}

        with mock.patch.object(runner.market_data, "fetch_market_caps", stable_price):
            asyncio.run(runner.on_message(event))

        records = read_jsonl(calls_path)
        assert not [r for r in records if r["event"] == "rejected_ticker_open"], \
            "a ticker closed at a profit should not block a new entry"
        assert contract_new in positions, \
            "the call should have been allowed through to open a position"


def check_suspension_watchdog():
    """7b: suspension watchdog fires on a large clock jump, not on a normal 5s cycle."""
    t0 = 1_000_000.0

    assert runner.suspended_gap_seconds(t0, t0 + 5) is None, \
        "a normal 5s cycle should not be flagged as a suspension"

    gap = runner.suspended_gap_seconds(t0, t0 + 9109)  # the 16 Aug incident's own gap
    assert gap is not None and gap == 9109, \
        "a 9109s gap should be flagged as a suspension"

    assert runner.suspended_gap_seconds(None, t0) is None, \
        "no previous cycle yet should never be flagged"


def check_stale_fetch_watchdog():
    """7b: the staleness watchdog only fires when positions are open AND stale."""
    t0 = 1_000_000.0

    assert runner.stale_fetch_gap_seconds(t0, t0 + 30, has_open_positions=True) is None, \
        "a recent fetch should not be flagged as stale"

    gap = runner.stale_fetch_gap_seconds(t0, t0 + 200, has_open_positions=True)
    assert gap is not None, "an old fetch with open positions should be flagged as stale"

    assert runner.stale_fetch_gap_seconds(t0, t0 + 200, has_open_positions=False) is None, \
        "an old fetch with no open positions has nothing to protect - should not be flagged"


def check_call_age_helpers():
    """Bug 2 helpers directly: call_age_seconds() and is_call_stale() need no Telethon event."""
    now = datetime.now(timezone.utc)
    fresh = now - timedelta(seconds=30)
    stale = now - timedelta(seconds=400)

    assert not runner.is_call_stale(fresh, now=now)
    assert runner.is_call_stale(stale, now=now)
    assert abs(runner.call_age_seconds(fresh, now=now) - 30) < 0.001


# ==========================================================================
# Runner
# ==========================================================================

CHECKS = [
    ("Bug 1: no live price -> rejected_no_price, no fill", check_no_live_price_rejects),
    ("Bug 2: call >5min old -> rejected_stale_call", check_stale_call_rejected),
    ("Bug 2: call 30s old -> accepted, not treated as stale", check_fresh_call_accepted),
    ("Bug 3: ticker already OPEN -> rejected_ticker_open", check_ticker_already_open_rejected),
    ("Bug 3: ticker CLOSED at profit -> allowed through", check_ticker_closed_at_profit_allowed),
    ("7b: suspension watchdog gap detection", check_suspension_watchdog),
    ("7b: stale market-data-fetch watchdog", check_stale_fetch_watchdog),
    ("Bug 2: call_age_seconds / is_call_stale directly", check_call_age_helpers),
]


def run():
    """Runs every check, printing PASS/FAIL per case. Returns a list of failures."""
    failures = []
    for name, fn in CHECKS:
        try:
            fn()
        except Exception as exc:
            failures.append((name, exc))
            print(f"  [FAIL] {name}: {exc}")
        else:
            print(f"  [PASS] {name}")
    return failures


if __name__ == "__main__":
    print("=" * 70)
    print("STAGE 2 INTEGRATION TESTS - entry guards (runner.py)")
    print("=" * 70)
    failures = run()
    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED")
        sys.exit(1)
    print("All checks in this module passed")
