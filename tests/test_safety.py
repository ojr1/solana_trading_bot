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
import importlib
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
import wallet           # noqa: E402


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


# ---------------------------------------------------------------------------
# STAGE 4 - RESIZE FOR A 2-3 SOL WALLET, brief_stage4_resize.md Step 3
#
# Updated in place from stage 3's 0.3 SOL / MAX_CONCURRENT_POSITIONS=3 /
# MAX_POSITION_SOL=0.15 tests, per instruction ("Update them for the new
# values", not "add alongside them"). The stage 1 tests above this section
# are untouched, as instructed separately.
#
# entry_logic.decide_entry() is still mocked to a fixed decision - the
# guards under test (concurrency cap, reserve check, the DCA-time
# position-size cap) are runner.py's, not entry_logic's, and a fixed
# decision makes the wallet arithmetic exact and independent of the PCR
# formula. Unlike stage 3, a single-tranche decision here is a choice, not
# a forced consequence: at MAX_LOT_SOL=0.25 two-stage DCA IS reachable
# again for a high enough PCR (needs lot >= 0.1875 SOL) - see
# RESIZE_REPORT.md Step 2. These guard tests still use MIN_LOT_SOL-sized,
# single-tranche fixtures deliberately, to keep the wallet arithmetic exact.
# ---------------------------------------------------------------------------


def _fixed_decision(total_lot_sol):
    """A decide_entry() stand-in that always approves a single-tranche buy."""
    def fake_decide_entry(call):
        return {
            "ticker": call["ticker"],
            "contract_address": call["contract_address"],
            "market_cap": call["market_cap"],
            "action": "buy",
            "reason": "test fixture",
            "pcr": 0.05,
            "total_lot_sol": total_lot_sol,
            "tranches": [{
                "stage": 1, "sol": total_lot_sol,
                "trigger": "immediate", "drop_pct_from_previous_fill": 0,
            }],
            "breakdown": {},
        }
    return fake_decide_entry


def _stable_price_stub(mc):
    async def fake_fetch_token_details(session, mints):
        return {mint: {"market_cap": mc} for mint in mints}
    return fake_fetch_token_details


def test_ten_positions_at_min_lot_fit_a_2_5_sol_wallet_eleventh_refused(
    isolated_positions, monkeypatch,
):
    """
    Combined scenario, matching stage 3's approach: on a simulated 2.5 SOL
    wallet, MIN_LOT_SOL=0.075 entries:
      - ten concurrent (0.75 SOL total) are allowed
      - an eleventh is refused - and by WHICH guard is asserted explicitly
      - the 0.05 SOL reserve is never breached by any of the ten allowed
    """
    positions, calls_path = isolated_positions
    monkeypatch.setattr(config, "MIN_LOT_SOL", 0.075)
    monkeypatch.setattr(config, "MAX_CONCURRENT_POSITIONS", 10)
    monkeypatch.setattr(config, "MIN_SOL_RESERVE", 0.05)
    monkeypatch.setattr(runner.entry_logic, "decide_entry", _fixed_decision(0.075))
    monkeypatch.setattr(runner.market_data, "fetch_token_details", _stable_price_stub(20_000))

    # Wallet starts at 2.5 SOL, MIN_LOT_SOL (0.075) each. Balances the ten
    # allowed entries will observe: 2.500, 2.425, ... down to 1.750. If the
    # 11th entry's reserve check is ever reached at all, get_balance() is
    # called an 11th time and this mock raises (StopIteration/IndexError) -
    # itself proof the wrong guard bound first, not just a missing assert.
    balances = [round(2.500 - 0.075 * i, 4) for i in range(10)]
    get_balance = AsyncMock(side_effect=balances)
    monkeypatch.setattr(runner.wallet, "get_balance", get_balance)

    # Letter suffixes, not digits: a trailing "0" would break the base58
    # contract-address regex in parser.py (0/O/I/l are excluded from
    # base58), which very nearly cost this test a false failure at stage 3.
    suffixes = "ABCDEFGHJK"  # ten letters, skipping I (excluded from base58 too)
    for suffix in suffixes:
        ticker, contract = f"MINLOT{suffix}", f"TestMinLot{suffix}" + "9" * 32
        event = _FakeEvent(_make_call_text(ticker, contract), datetime.now(timezone.utc))
        asyncio.run(runner.on_message(event))
        assert contract in positions, f"entry {suffix} of 10 should have been allowed"
        assert positions[contract]["sol_invested"] >= config.MIN_LOT_SOL

    assert len(positions) == 10
    assert get_balance.call_count == 10, \
        "reserve check should have run exactly 10 times, once per allowed entry"

    # Reserve invariant: after each allowed entry, remaining balance never
    # dropped below MIN_SOL_RESERVE.
    remaining_after_each = [b - 0.075 for b in balances]
    assert all(r >= config.MIN_SOL_RESERVE for r in remaining_after_each)

    # The eleventh: concurrency cap must fire BEFORE open_position() ever
    # calls get_balance() again (proven above by call_count staying at 10).
    ticker11, contract11 = "MINLOTM", "TestMinLotM" + "9" * 32
    event11 = _FakeEvent(_make_call_text(ticker11, contract11), datetime.now(timezone.utc))
    asyncio.run(runner.on_message(event11))

    assert contract11 not in positions, "an 11th concurrent position should be refused"
    assert get_balance.call_count == 10, \
        "the 11th attempt must be refused before the reserve check ever runs"
    records = read_jsonl(calls_path)
    refusal = [r for r in records if r["contract_address"] == contract11]
    assert refusal, "no record was logged for the refused 11th entry"
    assert refusal[0]["event"] == "rejected_concurrency_cap", (
        f"expected the CONCURRENCY CAP to refuse the 11th entry (it runs "
        f"before open_position()'s reserve check in on_message()), got "
        f"{refusal[0]['event']!r} instead"
    )


# ---------------------------------------------------------------------------
# 4. A second tranche taking one coin past MAX_POSITION_SOL is refused
#
# Exercises the NEW guard check_dca_fills() gained this stage (see
# SIZING_REPORT.md): under normal operation tranches can never sum past
# MAX_POSITION_SOL, since the upfront on_message() gate already bounds the
# planned total. This tests the guard directly against a position shaped
# like a LEGACY one - opened under an older, larger sizing regime - whose
# pending tranche would now breach a newer, smaller cap.
# ---------------------------------------------------------------------------


def test_dca_fill_refused_if_it_would_exceed_max_position_sol(monkeypatch):
    monkeypatch.setattr(config, "MAX_POSITION_SOL", 0.25)

    position = {
        "ticker": "LEGACY", "contract_address": "TestLegacy1" + "9" * 32,
        "sol_invested": 0.15, "total_tokens_bought": 1.0, "tokens_remaining": 1.0,
        "original_tokens": 1.0, "reference_mc": 20_000, "entry_mc": 20_000,
        "last_fill_mc": 20_000, "initials_taken": False,
        "pending_tranches": [{
            "stage": 2, "sol": 0.15,  # 0.15 + 0.15 = 0.30 > 0.25 cap
            "drop_pct_from_previous_fill": 10,
        }],
        "fills": [{"stage": 1, "sol": 0.15, "mc": 20_000, "at": "2026-01-01T00:00:00+00:00"}],
    }

    # Price has dropped enough to trigger the pending tranche on its own terms.
    current_mc = 20_000 * 0.85  # well past the 10% drop trigger

    result = asyncio.run(runner.check_dca_fills(position, current_mc))

    assert result is None, "the tranche must not fill"
    assert position["sol_invested"] == 0.15, "sol_invested must be unchanged"
    assert position["pending_tranches"] == [], \
        "the over-cap tranche should be abandoned (popped), not left to retry forever"


# ---------------------------------------------------------------------------
# 5. No lot below MIN_LOT_SOL, none above MAX_LOT_SOL
# ---------------------------------------------------------------------------


def test_no_lot_below_min_lot_sol(monkeypatch):
    monkeypatch.setattr(config, "MIN_LOT_SOL", 0.075)
    monkeypatch.setattr(config, "MAX_LOT_SOL", 0.25)

    import entry_logic
    # entry_logic.MIN_LOT_SOL/MAX_LOT_SOL are aliases assigned at import
    # time; re-point them at the monkeypatched config values for this test.
    monkeypatch.setattr(entry_logic, "MIN_LOT_SOL", config.MIN_LOT_SOL)
    monkeypatch.setattr(entry_logic, "MAX_LOT_SOL", config.MAX_LOT_SOL)

    # PCR is clamped to 0..1 by stretch_pcr() before sizing, so even a
    # nonsensical negative or huge PCR must never size below MIN_LOT_SOL.
    for pcr in (-5.0, -0.01, 0.0, 0.001, 0.5, 1.0, 5.0):
        lot = entry_logic.pcr_to_lot_size(pcr)
        assert lot >= config.MIN_LOT_SOL - 1e-9, f"pcr={pcr} produced lot={lot}"
        assert lot <= config.MAX_LOT_SOL + 1e-9, f"pcr={pcr} produced lot={lot}"


# ---------------------------------------------------------------------------
# STAGE 5 - LAZY WALLET (dry run without a private key),
# brief_stage5_lazy_wallet.md Step 3
#
# The first two tests reload config.py against a mutated environment to
# observe its real startup behaviour, rather than mocking config.py's own
# logic - that is the actual thing being tested. config.py is a module
# already imported (and cached) by runner.py, wallet.py, trade_execution.py
# and entry_logic.py, so a stray reload leaking a fake DRY_RUN/
# WALLET_PRIVATE_KEY into later tests would be a real hazard. config_env
# guards against that - but note the ordering: pytest tears fixtures down
# LIFO (reverse of setup order), so as a fixture that DEPENDS on
# monkeypatch, config_env's own post-yield code runs BEFORE monkeypatch's
# built-in finalizer restores the environment, not after. Reloading
# config.py at that point would still see the mutated (test) environment,
# not the real one - the opposite of the intended effect. config_env calls
# monkeypatch.undo() itself, explicitly, before reloading, rather than
# relying on teardown order to get this right.
#
# SECURITY NOTE, learned the hard way while writing this: simulating "the
# key is absent" with monkeypatch.delenv() is NOT safe here. config.py
# calls load_dotenv() on every reload, and python-dotenv defaults to
# override=False - it only skips a variable that is already PRESENT in
# os.environ. Deleting it makes it absent again, so the reload promptly
# re-reads the REAL key straight off disk and repopulates it, which then
# printed the actual private key in plaintext into a failed assertion's
# output during development of this exact test. Setting it to an EMPTY
# STRING instead keeps the name present-but-blank in os.environ, which
# load_dotenv() will not override, and which config.py's own
# _require()/_optional() already treat as "missing" - so it behaves
# identically for what's being tested, without ever touching the real
# value. Do not change this back to delenv().
# ---------------------------------------------------------------------------


@pytest.fixture
def config_env(monkeypatch):
    yield monkeypatch
    monkeypatch.undo()  # restore the real env vars BEFORE reloading against them
    importlib.reload(config)


def test_config_loads_with_dry_run_true_and_no_wallet_key(config_env):
    config_env.setenv("WALLET_PRIVATE_KEY", "")  # not delenv() - see note above
    config_env.setenv("DRY_RUN", "true")

    importlib.reload(config)

    assert config.DRY_RUN is True
    assert config.WALLET_PRIVATE_KEY is None


def test_config_raises_when_dry_run_false_and_no_wallet_key(config_env):
    config_env.setenv("WALLET_PRIVATE_KEY", "")  # not delenv() - see note above
    config_env.setenv("DRY_RUN", "false")

    # Not pytest.raises(config.ConfigError, ...): that expression captures
    # today's ConfigError class BEFORE the reload runs, but the reload
    # re-executes "class ConfigError(Exception): ..." and so raises using
    # a freshly-created class object of the same name - a different class
    # by identity, which pytest.raises would then fail to match. Checking
    # the exception's type NAME (a plain string, stable across reloads)
    # sidesteps that entirely.
    try:
        importlib.reload(config)
    except Exception as exc:
        assert type(exc).__name__ == "ConfigError", \
            f"expected ConfigError, got {type(exc).__name__}: {exc}"
        assert "WALLET_PRIVATE_KEY" in str(exc)
    else:
        pytest.fail(
            "expected config.py to raise ConfigError when DRY_RUN is false "
            "and WALLET_PRIVATE_KEY is absent"
        )


def test_importing_wallet_does_not_construct_a_keypair():
    """
    A fresh import (simulated here with reload, since wallet.py is already
    imported) must not build a Keypair - only get_keypair()/get_public_key()/
    get_balance() do, on first use. This holds regardless of whether a real
    key is configured (it is, in this repo's real .env); the point is that
    import alone never touches it.
    """
    importlib.reload(wallet)
    assert wallet._keypair is None


def test_reserve_check_allows_entry_when_no_wallet_configured(
    isolated_positions, monkeypatch, caplog,
):
    async def raise_no_wallet():
        raise wallet.NoWalletConfiguredError("no key set - test")
    monkeypatch.setattr(runner.wallet, "get_balance", raise_no_wallet)

    with caplog.at_level("WARNING", logger="runner"):
        result = asyncio.run(runner.check_reserve_ok(0.1))

    assert result is True, \
        "with no wallet configured in dry run, the entry must be allowed"
    assert any(
        record.levelname == "WARNING" and "RESERVE CHECK UNAVAILABLE" in record.message
        for record in caplog.records
    ), "the log must name explicitly that no check was actually performed"
    # And must NOT read as though a check passed - "RESERVE BLOCK" is the
    # marker used when a check ran and failed; it must not appear here.
    assert not any("RESERVE BLOCK" in record.message for record in caplog.records)
