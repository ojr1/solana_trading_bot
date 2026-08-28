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

import alerting         # noqa: E402
import config          # noqa: E402
import data_logger     # noqa: E402
import exit_logic      # noqa: E402
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
    monkeypatch.setattr(data_logger, "PRICE_HISTORY_FILE", tmp_path / "price_history.jsonl")
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
                        AsyncMock(return_value=(b"fake-signed-tx", "fake-local-sig")))
    monkeypatch.setattr(trade_execution, "submit_transaction",
                        AsyncMock(return_value="fake-signature"))
    monkeypatch.setattr(trade_execution, "confirm_transaction",
                        AsyncMock(return_value="timeout"))  # never confirms

    with pytest.raises(trade_execution.FillNotConfirmedError):
        asyncio.run(trade_execution.execute_swap(
            "TestMintUnconfirmed11111111111111111111111", 0.05
        ))


# ---------------------------------------------------------------------------
# STAGE 10 - execute_swap()'s three-way confirm_transaction() outcome,
# brief_stage10_autonomous.md Part 1
# ---------------------------------------------------------------------------


def test_reverted_fill_raises_transaction_reverted_not_treated_as_filled(monkeypatch):
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(trade_execution, "get_quote",
                        AsyncMock(return_value={"outAmount": "12345"}))
    monkeypatch.setattr(trade_execution, "build_signed_transaction",
                        AsyncMock(return_value=(b"fake-signed-tx", "fake-local-sig")))
    monkeypatch.setattr(trade_execution, "submit_transaction",
                        AsyncMock(return_value="fake-signature"))
    monkeypatch.setattr(trade_execution, "confirm_transaction",
                        AsyncMock(return_value="failed"))  # confirmed but reverted

    with pytest.raises(trade_execution.TransactionRevertedError):
        asyncio.run(trade_execution.execute_swap(
            "TestMintReverted111111111111111111111111", 0.05
        ))


def test_confirmed_fill_returns_success(monkeypatch):
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(trade_execution, "get_quote",
                        AsyncMock(return_value={"outAmount": "12345"}))
    monkeypatch.setattr(trade_execution, "build_signed_transaction",
                        AsyncMock(return_value=(b"fake-signed-tx", "fake-local-sig")))
    monkeypatch.setattr(trade_execution, "submit_transaction",
                        AsyncMock(return_value="fake-signature"))
    monkeypatch.setattr(trade_execution, "confirm_transaction",
                        AsyncMock(return_value="confirmed"))

    result = asyncio.run(trade_execution.execute_swap(
        "TestMintConfirmed11111111111111111111111", 0.05
    ))

    assert result["confirmed"] is True
    assert result["signature"] == "fake-signature"


def test_submission_failure_carries_local_signature_forward(monkeypatch):
    """
    A network-level failure at submit_transaction() must not lose the one
    thing that lets a later poll figure out what actually happened: the
    signature captured locally right after signing, before submission was
    ever attempted.
    """
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(trade_execution, "get_quote",
                        AsyncMock(return_value={"outAmount": "12345"}))
    monkeypatch.setattr(trade_execution, "build_signed_transaction",
                        AsyncMock(return_value=(b"fake-signed-tx", "fake-local-sig-999")))
    monkeypatch.setattr(trade_execution, "submit_transaction",
                        AsyncMock(side_effect=RuntimeError("network drop")))

    with pytest.raises(trade_execution.TransactionSubmissionError) as excinfo:
        asyncio.run(trade_execution.execute_swap(
            "TestMintSubmitFail1111111111111111111111", 0.05
        ))

    assert excinfo.value.local_signature == "fake-local-sig-999"


# ---------------------------------------------------------------------------
# STAGE 10 - confirm_transaction()'s own err-field handling, mocked at
# _fetch_signature_status (one HTTP poll) rather than faking aiohttp itself.
# ---------------------------------------------------------------------------


def test_confirm_transaction_null_err_is_success(monkeypatch):
    monkeypatch.setattr(
        trade_execution, "_fetch_signature_status",
        AsyncMock(return_value={"confirmationStatus": "confirmed", "err": None}),
    )
    result = asyncio.run(trade_execution.confirm_transaction("sig", timeout_seconds=5))
    assert result == "confirmed"


def test_confirm_transaction_absent_err_is_success(monkeypatch):
    """status.get('err') must treat a missing key the same as an explicit
    null - the field is not always present on a genuinely successful tx."""
    monkeypatch.setattr(
        trade_execution, "_fetch_signature_status",
        AsyncMock(return_value={"confirmationStatus": "finalized"}),  # no 'err' key at all
    )
    result = asyncio.run(trade_execution.confirm_transaction("sig", timeout_seconds=5))
    assert result == "confirmed"


def test_confirm_transaction_non_null_err_is_failed(monkeypatch):
    monkeypatch.setattr(
        trade_execution, "_fetch_signature_status",
        AsyncMock(return_value={
            "confirmationStatus": "confirmed",
            "err": {"InstructionError": [0, "slippage tolerance exceeded"]},
        }),
    )
    result = asyncio.run(trade_execution.confirm_transaction("sig", timeout_seconds=5))
    assert result == "failed"


def test_confirm_transaction_never_resolves_is_timeout(monkeypatch):
    monkeypatch.setattr(
        trade_execution, "_fetch_signature_status",
        AsyncMock(return_value=None),  # signature never seen by the RPC node
    )
    result = asyncio.run(trade_execution.confirm_transaction("sig", timeout_seconds=2))
    assert result == "timeout"


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


# ---------------------------------------------------------------------------
# STAGE 7 - PRICE HISTORY LOGGING, brief_stage7_price_history.md Step 2
#
# _monitor_once() is exercised directly (not on_message()) since that is the
# actual call site data_logger.log_price_point() was added to. market_data
# .fetch_market_caps() is mocked to a fixed mc for every contract in one
# call, matching its real (session, mints) -> {contract: mc} shape; session
# itself is never touched by the mock, so None stands in for it.
#
# Positions are built to sit exactly AT peak with plenty of room above
# every exit threshold (entry 20,000, peak/current 40,000: trailing stop
# only fires at peak*0.40=16,000; no ladder step is crossed since 40,000 is
# below the first $50,000 rung) so check_exit_conditions() never fires and
# the position stays open across the call - keeping these tests about the
# price-history write alone, not an incidental exit.
# ---------------------------------------------------------------------------


def _open_position(ticker, contract, initials_taken, peak_mc=40_000, current_mc=40_000):
    return {
        "ticker": ticker, "contract_address": contract, "closed": False,
        "entry_mc": 20_000, "reference_mc": 20_000, "peak_mc": peak_mc,
        "last_fill_mc": 20_000, "call_mc": 20_000,
        "sol_invested": 0.2, "realised_sol": 0.0,
        "total_tokens_bought": 1.0, "tokens_remaining": 1.0, "original_tokens": 1.0,
        "initials_taken": initials_taken, "initials_mc": 20_000 * 1.95, "pcr": 0.5,
        "pending_tranches": [], "fills": [
            {"stage": 1, "sol": 0.2, "mc": 20_000, "at": "2026-01-01T00:00:00+00:00"},
        ],
    }, current_mc


def _mock_fetch_market_caps(mc_by_contract):
    async def fake_fetch_market_caps(session, mints):
        return {m: mc_by_contract[m] for m in mints if m in mc_by_contract}
    return fake_fetch_market_caps


def test_price_history_written_for_each_open_position_each_cycle(
    isolated_positions, monkeypatch,
):
    positions, _ = isolated_positions
    price_history_path = data_logger.PRICE_HISTORY_FILE

    p1, mc1 = _open_position("ALPHA", "TestAlpha1" + "9" * 33, initials_taken=True)
    p2, mc2 = _open_position("BETA", "TestBeta1" + "9" * 34, initials_taken=True)
    positions[p1["contract_address"]] = p1
    positions[p2["contract_address"]] = p2

    monkeypatch.setattr(runner.market_data, "fetch_market_caps",
                         _mock_fetch_market_caps({p1["contract_address"]: mc1,
                                                   p2["contract_address"]: mc2}))

    asyncio.run(runner._monitor_once(session=None))

    records = read_jsonl(price_history_path)
    assert len(records) == 2, f"expected one row per open position, got {len(records)}"
    logged_contracts = {r["contract_address"] for r in records}
    assert logged_contracts == {p1["contract_address"], p2["contract_address"]}


def test_price_history_skips_positions_before_initials(isolated_positions, monkeypatch):
    """The Step 0 scope decision: no trailing stop is active before initials,
    so a pre-initials cycle is not logged at all - not sampled, skipped."""
    positions, _ = isolated_positions
    price_history_path = data_logger.PRICE_HISTORY_FILE

    pre, mc_pre = _open_position("PRE", "TestPreInit1" + "9" * 31, initials_taken=False)
    post, mc_post = _open_position("POST", "TestPostInit1" + "9" * 30, initials_taken=True)
    positions[pre["contract_address"]] = pre
    positions[post["contract_address"]] = post

    monkeypatch.setattr(runner.market_data, "fetch_market_caps",
                         _mock_fetch_market_caps({pre["contract_address"]: mc_pre,
                                                   post["contract_address"]: mc_post}))

    asyncio.run(runner._monitor_once(session=None))

    records = read_jsonl(price_history_path)
    assert len(records) == 1, "only the post-initials position should be logged"
    assert records[0]["contract_address"] == post["contract_address"]


def test_price_history_not_written_when_no_positions_open(isolated_positions, monkeypatch):
    price_history_path = data_logger.PRICE_HISTORY_FILE
    fetch_mock = AsyncMock(side_effect=AssertionError(
        "fetch_market_caps must not be called when there are no open positions"
    ))
    monkeypatch.setattr(runner.market_data, "fetch_market_caps", fetch_mock)

    asyncio.run(runner._monitor_once(session=None))

    assert not price_history_path.exists(), \
        "no price_history.jsonl row (or file) should be produced with nothing open"
    fetch_mock.assert_not_called()


def test_price_history_record_contains_every_specified_field(
    isolated_positions, monkeypatch,
):
    positions, _ = isolated_positions
    price_history_path = data_logger.PRICE_HISTORY_FILE

    p, mc = _open_position("FIELDS", "TestFields1" + "9" * 32, initials_taken=True,
                            peak_mc=35_000, current_mc=42_000)  # current_mc > peak_mc
    positions[p["contract_address"]] = p
    monkeypatch.setattr(runner.market_data, "fetch_market_caps",
                         _mock_fetch_market_caps({p["contract_address"]: mc}))

    asyncio.run(runner._monitor_once(session=None))

    records = read_jsonl(price_history_path)
    assert len(records) == 1
    record = records[0]

    for field in ("ts", "schema_version", "contract_address", "mc", "peak_mc",
                  "initials_taken"):
        assert field in record, f"missing required field: {field}"

    assert record["contract_address"] == p["contract_address"]
    assert record["mc"] == 42_000
    assert record["initials_taken"] is True
    # peak-lag fix: current_mc (42,000) exceeds the position's stored peak_mc
    # (35,000) as of this cycle - the logged peak must reflect the new high,
    # not the stale stored value, since exit_logic hasn't updated it yet at
    # the point log_price_point() is called.
    assert record["peak_mc"] == 42_000, (
        "peak_mc must be max(stored peak_mc, current_mc), not the stale stored "
        "value from before this cycle's exit_logic.check_exit_conditions() runs"
    )

    # Fields the brief explicitly said to drop from this file specifically.
    assert "run_id" not in record
    assert "event" not in record
    assert "ticker" not in record


def test_price_history_write_failure_does_not_propagate(
    isolated_positions, monkeypatch, caplog,
):
    positions, _ = isolated_positions
    # Point the file at a path whose parent directory cannot exist (a file,
    # not a directory, in the path) so the write raises OSError/NotADirectoryError.
    bad_dir = data_logger.DATA_DIR / "not_a_directory"
    bad_dir.parent.mkdir(parents=True, exist_ok=True)
    bad_dir.write_text("blocking file, not a directory")
    monkeypatch.setattr(data_logger, "PRICE_HISTORY_FILE", bad_dir / "price_history.jsonl")

    p, mc = _open_position("FAILWRITE", "TestFailWrite1" + "9" * 29, initials_taken=True)
    positions[p["contract_address"]] = p
    monkeypatch.setattr(runner.market_data, "fetch_market_caps",
                         _mock_fetch_market_caps({p["contract_address"]: mc}))

    with caplog.at_level("WARNING", logger="data_logger"):
        asyncio.run(runner._monitor_once(session=None))  # must not raise

    assert any(
        record.levelname == "WARNING" and "could not write" in record.message
        for record in caplog.records
    ), "a write failure must be logged as a WARNING, not silently lost or raised"
    # The rest of the cycle must still have completed - proven by the position
    # remaining exactly as it was (not closed, unmodified), not by a crash.
    assert positions[p["contract_address"]]["closed"] is False


# ---------------------------------------------------------------------------
# STAGE 8 - INITIALS_SELL_FRACTION moved to config.py / .env, set to 0.33,
# brief_stage8_initials_fraction.md
#
# exit_logic.check_exit_conditions() is exercised directly, against the real
# configured value (config.INITIALS_SELL_FRACTION via exit_logic's module-
# level alias) rather than a monkeypatched one - the point is to prove the
# actual .env value now in effect fires at 33%, not 50%, end to end through
# the real firing path (including spike confirmation), not just that the
# constant equals 0.33 in isolation.
# ---------------------------------------------------------------------------


def test_initials_sells_33_percent_not_50():
    entry_mc = 20_000
    position = {
        "closed": False, "entry_mc": entry_mc, "peak_mc": entry_mc,
        "initials_taken": False, "tokens_remaining": 1.0, "original_tokens": 1.0,
        "last_sell_mc": None, "fired_levels": [], "pending": None,
    }
    trigger_mc = entry_mc * (1 + exit_logic.INITIALS_TRIGGER_GAIN)
    now = 1_000_000.0

    # First sighting only arms the spike-confirmation timer (_confirm()) -
    # it never fires on the first call.
    actions = exit_logic.check_exit_conditions(position, trigger_mc, now)
    assert actions == [], "initials must not fire on the first sighting of the trigger"

    # Second call, price held steady, past CONFIRM_DELAY_SECONDS - fires.
    actions = exit_logic.check_exit_conditions(
        position, trigger_mc, now + exit_logic.CONFIRM_DELAY_SECONDS + 0.1,
    )

    assert len(actions) == 1
    action = actions[0]
    assert action["exit_type"] == "initials"
    assert action["fraction_of_remaining"] == pytest.approx(0.33), (
        "must sell the configured 0.33, not the old hardcoded 0.50"
    )
    assert action["pct_of_original_position"] == pytest.approx(33.0, abs=0.01)
    assert position["tokens_remaining"] == pytest.approx(0.67, abs=1e-9), (
        "67% of the original position must remain, not 50%"
    )
    assert position["closed"] is False, \
        "67% remains after initials - the position must stay open"


# ---------------------------------------------------------------------------
# STAGE 10 PART 1 - MAX_DAILY_LOSS_SOL / check_daily_loss_ok(),
# brief_stage10_autonomous.md
# ---------------------------------------------------------------------------


def test_daily_loss_cap_blocks_new_entries_when_breached(isolated_positions, monkeypatch):
    positions, _ = isolated_positions
    monkeypatch.setattr(config, "MAX_DAILY_LOSS_SOL", 0.5)
    now = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)

    positions["TestLossA" + "9" * 33] = {
        "closed": True, "closed_at": "2026-08-28T10:00:00+00:00",
        "sol_invested": 1.0, "realised_sol": 0.4,  # -0.6 SOL, breaches -0.5 cap
    }

    assert runner.check_daily_loss_ok(now=now) is False


def test_daily_loss_cap_allows_when_under_the_cap(isolated_positions, monkeypatch):
    positions, _ = isolated_positions
    monkeypatch.setattr(config, "MAX_DAILY_LOSS_SOL", 0.5)
    now = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)

    positions["TestLossB" + "9" * 33] = {
        "closed": True, "closed_at": "2026-08-28T10:00:00+00:00",
        "sol_invested": 1.0, "realised_sol": 0.7,  # -0.3 SOL, under the -0.5 cap
    }

    assert runner.check_daily_loss_ok(now=now) is True


def test_daily_loss_cap_boundary_just_before_and_after_utc_midnight(
    isolated_positions, monkeypatch,
):
    """A position closed at 23:59:59 UTC counts toward that day; one closed
    a second later (00:00:00 UTC the next day) counts toward the next - a
    hard date boundary, not a rolling 24h window."""
    positions, _ = isolated_positions
    monkeypatch.setattr(config, "MAX_DAILY_LOSS_SOL", 0.5)
    now = datetime(2026, 8, 28, 23, 59, 59, tzinfo=timezone.utc)

    # Closed one second before midnight on the 28th - counts toward "today" (the 28th).
    positions["TestBeforeMidnight" + "9" * 24] = {
        "closed": True, "closed_at": "2026-08-28T23:59:59+00:00",
        "sol_invested": 1.0, "realised_sol": 0.4,  # -0.6 SOL alone breaches -0.5
    }
    assert runner.check_daily_loss_ok(now=now) is False, \
        "a position closed at 23:59:59 UTC on the 28th must count toward the 28th"

    # Remove it, add one closed exactly at midnight the NEXT day instead -
    # must NOT count toward "today" (still the 28th per `now` above).
    positions.clear()
    positions["TestAfterMidnight" + "9" * 24] = {
        "closed": True, "closed_at": "2026-08-29T00:00:00+00:00",
        "sol_invested": 1.0, "realised_sol": 0.4,  # same -0.6 SOL loss
    }
    assert runner.check_daily_loss_ok(now=now) is True, \
        "a position closed at 00:00:00 UTC on the 29th must NOT count toward the 28th"


def test_daily_loss_cap_ignores_positions_without_closed_at(isolated_positions, monkeypatch):
    positions, _ = isolated_positions
    monkeypatch.setattr(config, "MAX_DAILY_LOSS_SOL", 0.5)
    now = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)

    # Legacy-shaped closed position with no closed_at - must be excluded,
    # not crash and not count.
    positions["TestLegacyNoClosedAt" + "9" * 20] = {
        "closed": True, "closed_at": None,
        "sol_invested": 1.0, "realised_sol": 0.0,  # would be -1.0 SOL if counted
    }
    assert runner.check_daily_loss_ok(now=now) is True


# ---------------------------------------------------------------------------
# STAGE 10 PART 1 - alerting.alert(), brief_stage10_autonomous.md
# ---------------------------------------------------------------------------


def test_alert_logs_at_error_with_distinctive_prefix(caplog):
    with caplog.at_level("ERROR", logger="alerting"):
        alerting.alert("daily_loss_cap", "today's P&L breached the cap")

    assert any(
        record.levelname == "ERROR"
        and "[ALERT]" in record.message
        and "daily_loss_cap" in record.message
        and "today's P&L breached the cap" in record.message
        for record in caplog.records
    )
