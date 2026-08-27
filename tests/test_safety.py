"""
tests/test_safety.py - Stage 1 minimal safety-guard tests, pytest.

Covers exactly the five checks brief_stage1.md Step 4 asks for - no broader
suite. This is the ONLY pytest file in the project; everything else under
tests/ uses the plain assert-based style in tests/run_all.py. Run this file
by itself, not bare `pytest` or `pytest tests/`, or pytest will also try to
COLLECT (import) the other test_*.py files here, which are not written for
pytest (some os.chdir() into a scratch folder at import time) and are not
meant to be run this way:

    pytest tests/test_safety.py -v

No real network call, no real Telegram, no real wallet signing anywhere in
this file - the one real network call this suite WOULD otherwise make
(wallet.get_balance(), a genuine Helius RPC call) is mocked in every test
that touches the reserve check. Every test also isolates runner.POSITIONS
and data_logger's output paths, so nothing here can touch the real
logs/positions.json or data/*.jsonl - same discipline as
tests/test_reject_paths.py's isolated_state().
"""

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import config          # noqa: E402
import data_logger     # noqa: E402
import runner          # noqa: E402
import trade_execution  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures and small helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_positions(monkeypatch, tmp_path):
    """
    Fresh POSITIONS dict, a no-op save_positions, and data_logger output
    redirected to a pytest tmp_path - so nothing a test does can reach the
    real position store or the real trading history files.

    Yields (positions_dict, calls_jsonl_path).
    """
    fresh = {}
    monkeypatch.setattr(runner, "POSITIONS", fresh)
    monkeypatch.setattr(runner, "save_positions", lambda positions: None)
    monkeypatch.setattr(data_logger, "DATA_DIR", tmp_path)
    monkeypatch.setattr(data_logger, "CALLS_FILE", tmp_path / "calls.jsonl")
    monkeypatch.setattr(data_logger, "FILLS_FILE", tmp_path / "fills.jsonl")
    monkeypatch.setattr(data_logger, "SNAPSHOTS_FILE", tmp_path / "snapshots.jsonl")
    monkeypatch.setattr(runner.trading_window, "window_status",
                        lambda *a, **k: (True, "test - always open"))
    return fresh, tmp_path / "calls.jsonl"


def read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class _FakeMessage:
    def __init__(self, raw_text, date):
        self.raw_text = raw_text
        self.date = date


class _FakeEvent:
    """Stands in for a Telethon NewMessage event."""
    def __init__(self, raw_text, date):
        self.message = _FakeMessage(raw_text, date)


def _make_call_text(ticker, contract):
    """Minimal message text parser.py will classify and parse as a call."""
    stars = "⭐"
    return (
        f"${ticker} (test token)\n"
        f"{contract}\n"
        f"GTscore: {stars}\n"
        f"MC: $20K  Age: 5m  Holders: 300\n"
        f"Bundled: 8%\n"
    )


# ---------------------------------------------------------------------------
# 1. Reserve check blocks below the floor, allows at or above it
# ---------------------------------------------------------------------------


def test_reserve_check_blocks_when_below_floor(isolated_positions, monkeypatch):
    """balance(0.20) - trade_size(0.16) = 0.04, below the 0.05 reserve -> blocked."""
    monkeypatch.setattr(runner.wallet, "get_balance", AsyncMock(return_value=0.20))
    monkeypatch.setattr(config, "MIN_SOL_RESERVE", 0.05)

    result = asyncio.run(runner.check_reserve_ok(0.16))

    assert result is False


def test_reserve_check_allows_when_exactly_at_floor(isolated_positions, monkeypatch):
    """balance(0.20) - trade_size(0.15) = 0.05, EQUAL to the reserve -> allowed."""
    monkeypatch.setattr(runner.wallet, "get_balance", AsyncMock(return_value=0.20))
    monkeypatch.setattr(config, "MIN_SOL_RESERVE", 0.05)

    result = asyncio.run(runner.check_reserve_ok(0.15))

    assert result is True


def test_reserve_check_allows_when_above_floor(isolated_positions, monkeypatch):
    """balance(0.20) - trade_size(0.10) = 0.10, above the 0.05 reserve -> allowed."""
    monkeypatch.setattr(runner.wallet, "get_balance", AsyncMock(return_value=0.20))
    monkeypatch.setattr(config, "MIN_SOL_RESERVE", 0.05)

    result = asyncio.run(runner.check_reserve_ok(0.10))

    assert result is True


# ---------------------------------------------------------------------------
# 2. The reserve block emits a WARNING
# ---------------------------------------------------------------------------


def test_reserve_block_emits_warning(isolated_positions, monkeypatch, caplog):
    monkeypatch.setattr(runner.wallet, "get_balance", AsyncMock(return_value=0.10))
    monkeypatch.setattr(config, "MIN_SOL_RESERVE", 0.05)

    with caplog.at_level("WARNING", logger="runner"):
        result = asyncio.run(runner.check_reserve_ok(1.0))  # way below floor

    assert result is False
    assert any(
        record.levelname == "WARNING" and "RESERVE BLOCK" in record.message
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# 3. Same-ticker lock prevents a duplicate concurrent entry
# ---------------------------------------------------------------------------


def test_same_ticker_lock_blocks_duplicate_concurrent_entry(isolated_positions):
    positions, calls_path = isolated_positions

    ticker = "DUPETEST"
    contract_open = "TestDupeOld" + "9" * 32
    contract_new = "TestDupeNew" + "9" * 32

    # An OPEN position already exists under this ticker, different contract.
    positions[contract_open] = {
        "ticker": ticker, "contract_address": contract_open, "closed": False,
    }

    text = _make_call_text(ticker, contract_new)
    event = _FakeEvent(text, datetime.now(timezone.utc))

    asyncio.run(runner.on_message(event))

    assert contract_new not in positions, \
        "a second contract under an already-open ticker must not open a position"
    records = read_jsonl(calls_path)
    assert any(r["event"] == "rejected_ticker_open" and r["contract_address"] == contract_new
               for r in records), "no rejected_ticker_open record was logged"


# ---------------------------------------------------------------------------
# 4. An unconfirmed fill results in no position being opened
#
# open_position() in runner.py simulates fills directly and does not (yet)
# call execute_swap() - execute_swap() is the function whose job is to
# guarantee this once it IS wired in. So this is tested at that seam: if
# confirm_transaction() comes back False, execute_swap() must raise rather
# than return anything a caller could mistake for a filled position.
# ---------------------------------------------------------------------------


def test_unconfirmed_fill_is_never_treated_as_filled(monkeypatch):
    monkeypatch.setattr(config, "DRY_RUN", False)  # exercise the real (non-DRY_RUN) path
    monkeypatch.setattr(trade_execution, "get_quote",
                        AsyncMock(return_value={"outAmount": "12345"}))
    monkeypatch.setattr(trade_execution, "build_signed_transaction",
                        AsyncMock(return_value=b"fake-signed-tx"))
    monkeypatch.setattr(trade_execution, "submit_transaction",
                        AsyncMock(return_value="fake-signature"))
    monkeypatch.setattr(trade_execution, "confirm_transaction",
                        AsyncMock(return_value=False))  # never confirms

    with pytest.raises(trade_execution.FillNotConfirmedError):
        asyncio.run(trade_execution.execute_swap(
            "TestMintUnconfirmed11111111111111111111111", 0.05
        ))


# ---------------------------------------------------------------------------
# 5. DRY_RUN true never reaches the submission function
# ---------------------------------------------------------------------------


def test_dry_run_never_reaches_submission(monkeypatch):
    monkeypatch.setattr(config, "DRY_RUN", True)
    monkeypatch.setattr(trade_execution, "get_quote",
                        AsyncMock(return_value={"outAmount": "99999"}))
    build_mock = AsyncMock(
        side_effect=AssertionError("build_signed_transaction must not be called in DRY_RUN")
    )
    submit_mock = AsyncMock(
        side_effect=AssertionError("submit_transaction must not be called in DRY_RUN")
    )
    monkeypatch.setattr(trade_execution, "build_signed_transaction", build_mock)
    monkeypatch.setattr(trade_execution, "submit_transaction", submit_mock)

    result = asyncio.run(trade_execution.execute_swap(
        "TestMintDryRun2222222222222222222222222222", 0.05
    ))

    assert result["dry_run"] is True
    assert result["confirmed"] is False
    assert result["signature"] is None
    build_mock.assert_not_called()
    submit_mock.assert_not_called()
