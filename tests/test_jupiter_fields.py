"""
tests/test_jupiter_fields.py

Drives runner.open_position() end to end with NO network and NO Telegram,
proving the Jupiter detail fields actually reach both destinations:

    positions.json   -> read by data_loader -> read by pcr_analysis
    data/calls.jsonl -> the reject control group

Why an integration test rather than more unit tests: every unit here already
passes its own self-test. What has broken twice on this project is the JOIN
between units - a field renamed at one end, a file that never reached disk.
This test exercises the actual seam.

The Jupiter API is replaced with a stub function, so this runs anywhere,
costs nothing, and cannot be affected by the API being slow or down.

RENAMED 25 Aug 2026 from tests/test_stage2_integration.py during the
recovery merge, to resolve a filename collision with the reject-path tests
(now tests/test_reject_paths.py) - the two files test different things and
both were called test_stage2_integration.py in their own history.

    python tests/test_jupiter_fields.py

Success marker: "STAGE 2 INTEGRATION TEST PASSED" and a zero exit code.
"""

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# Work in a scratch folder so a test run can never touch real trade history.
SCRATCH = PROJECT_ROOT / "_test_scratch"
if SCRATCH.exists():
    shutil.rmtree(SCRATCH)
SCRATCH.mkdir()
os.chdir(SCRATCH)

import market_data          # noqa: E402
import data_logger          # noqa: E402
import runner               # noqa: E402

FAILURES = []


def check(condition, description):
    """Record a pass or a failure and print it."""
    if condition:
        print(f"   [PASS] {description}")
    else:
        print(f"   [FAIL] {description}")
        FAILURES.append(description)


# ---------------------------------------------------------------------------
# Stub Jupiter. Each scenario swaps in a different canned response.
# ---------------------------------------------------------------------------

_STUB_RESPONSE = {}


async def _stub_fetch_token_details(session, mints):
    """Stands in for the real network call. Returns whatever the test set."""
    return dict(_STUB_RESPONSE)


runner.market_data.fetch_token_details = _stub_fetch_token_details

# Stage 1 safety guards (added 27 Aug 2026, after this file) are not what
# this file tests - it is about Jupiter field plumbing. Stubbed out the same
# way as fetch_token_details above, rather than depending on the real
# (small) wallet balance or being incidentally blocked by MAX_POSITION_SOL.
# Both guards have their own dedicated tests in tests/test_safety.py.


async def _stub_get_balance():
    return 100.0


runner.wallet.get_balance = _stub_get_balance
runner.config.MAX_POSITION_SOL = 999.0

FULL_DETAILS = {
    "market_cap": 24_500.0,
    "top_holders_pct": 21.7,
    "organic_score": 63.2,
    "dev_migrations": 3.0,
    "dev_mints": 11.0,
    "liquidity": 18_400.5,
    "launchpad": "pump.fun",
    "live_holder_count": 412.0,
}


def make_call(contract, ticker="TESTA", market_cap=25_000):
    return {
        "message_type": "call",
        "ticker": ticker,
        "token_name": "a test token",
        "contract_address": contract,
        "market_cap": market_cap,
        "gt_score": 4,
        "holders": 180,
        "age_minutes": 12,
        "bundled_pct": 8.5,
        "parse_ok": True,
    }


def make_decision(contract, ticker="TESTA"):
    return {
        "action": "buy",
        "ticker": ticker,
        "contract_address": contract,
        "pcr": 0.612,
        "total_lot_sol": 0.40,
        "tranches": [
            {"stage": 1, "sol": 0.180, "drop_pct_from_previous_fill": 0},
            {"stage": 2, "sol": 0.120, "drop_pct_from_previous_fill": 10},
            {"stage": 3, "sol": 0.100, "drop_pct_from_previous_fill": 10},
        ],
    }


def read_calls():
    """Every record written to data/calls.jsonl so far."""
    path = Path("data/calls.jsonl")
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def reset_state():
    """Clear positions and log files between scenarios."""
    runner.POSITIONS.clear()
    for path in (Path("data/calls.jsonl"), Path("logs/positions.json")):
        if path.exists():
            path.unlink()


DETAIL_COLUMNS = market_data.DETAIL_COLUMNS


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

async def scenario_normal_buy():
    global _STUB_RESPONSE
    print("\n1. Normal buy, Jupiter returns every field")
    reset_state()

    contract = "MintNormal1111111111111111111111111111111111"
    _STUB_RESPONSE = {contract: dict(FULL_DETAILS)}

    await runner.open_position(make_decision(contract), make_call(contract))

    check(contract in runner.POSITIONS, "position was opened")
    position = runner.POSITIONS.get(contract, {})

    missing = [c for c in DETAIL_COLUMNS if c not in position]
    check(not missing, f"position carries all {len(DETAIL_COLUMNS)} detail keys")
    check(position.get("top_holders_pct") == 21.7,
          "top_holders_pct stored on the position (21.7)")
    check(position.get("launchpad") == "pump.fun",
          "launchpad stored as text ('pump.fun')")
    check(position.get("entry_mc") == 24_500.0,
          "entry_mc came from the live figure, not the call figure")

    # And it must survive the round trip to disk - the failure mode that has
    # bitten this project twice is data that exists in memory but not on disk.
    saved = json.loads(Path("logs/positions.json").read_text(encoding="utf-8"))
    check(saved[contract].get("organic_score") == 63.2,
          "detail fields survived the write to positions.json")

    records = read_calls()
    bought = [r for r in records if r["event"] == "bought"]
    check(len(bought) == 1, "one 'bought' record written to calls.jsonl")
    if bought:
        check(bought[0].get("dev_mints") == 11.0,
              "calls.jsonl 'bought' record carries the detail fields")
        check(bought[0].get("schema_version") == 2,
              "record stamped schema_version 2")


async def scenario_gap_reject():
    global _STUB_RESPONSE
    print("\n2. Entry gap reject - details must still be logged")
    reset_state()

    contract = "MintGapReject11111111111111111111111111111111"
    # Call said $25,000; live is $12,000 = -52%, past the -35% limit.
    details = dict(FULL_DETAILS, market_cap=12_000.0)
    _STUB_RESPONSE = {contract: details}

    await runner.open_position(make_decision(contract, "GAPPY"),
                               make_call(contract, "GAPPY", 25_000))

    check(contract not in runner.POSITIONS,
          "position correctly NOT opened (gap guard fired)")

    records = read_calls()
    rejects = [r for r in records if r["event"] == "rejected_fill"]
    check(len(rejects) == 1, "one 'rejected_fill' record written")
    if rejects:
        check(rejects[0].get("top_holders_pct") == 21.7,
              "reject record carries the detail fields (the control group)")
        check("entry gap limit" in (rejects[0].get("reason") or ""),
              "reject reason names the gap guard")


async def scenario_floor_reject():
    global _STUB_RESPONSE
    print("\n3. Absolute floor reject - details must still be logged")
    reset_state()

    contract = "MintFloorReject111111111111111111111111111111"
    # Live $8,000 is below the $9,000 floor, and within the -35% gap limit
    # of a $10,000 call, so the FLOOR guard is the one that must fire.
    details = dict(FULL_DETAILS, market_cap=8_000.0)
    _STUB_RESPONSE = {contract: details}

    await runner.open_position(make_decision(contract, "FLOORY"),
                               make_call(contract, "FLOORY", 10_000))

    check(contract not in runner.POSITIONS,
          "position correctly NOT opened (floor guard fired)")

    records = read_calls()
    rejects = [r for r in records if r["event"] == "rejected_fill"]
    check(len(rejects) == 1, "one 'rejected_fill' record written")
    if rejects:
        check("absolute floor" in (rejects[0].get("reason") or ""),
              "reject reason names the floor guard, not the gap guard")
        check(rejects[0].get("liquidity") == 18_400.5,
              "reject record carries the detail fields")


async def scenario_no_jupiter_data():
    global _STUB_RESPONSE
    print("\n4. Jupiter returns nothing (unindexed new token)")
    reset_state()

    contract = "MintUnindexed11111111111111111111111111111111"
    _STUB_RESPONSE = {}   # token not found

    await runner.open_position(make_decision(contract, "NEWBIE"),
                               make_call(contract, "NEWBIE", 30_000))

    # FIXED 25 Aug 2026, during the recovery merge. This scenario predates
    # the bug-1 fix (see TASK.md): before that fix, no live price meant a
    # fallback to the call figure and the position opened anyway. The fix
    # replaced that with an outright reject (rejected_no_price), because a
    # fallback fill bypasses the entry-gap guard entirely - that is what let
    # three hours-dead coins fill blind on 16 Aug 2026. The assertions below
    # were rewritten to match the KEPT (fixed) behaviour; the old ones
    # asserted the fallback that no longer exists and would fail against the
    # current runner.py on purpose.
    check(contract not in runner.POSITIONS,
          "position correctly NOT opened - no live price means no fill")

    records = read_calls()
    rejects = [r for r in records if r["event"] == "rejected_no_price"]
    check(len(rejects) == 1, "one 'rejected_no_price' record written")
    if rejects:
        check(rejects[0].get("live_mc") is None,
              "reject record carries live_mc=None")
        missing = [c for c in DETAIL_COLUMNS if c not in rejects[0]]
        check(not missing,
              "reject record still carries every detail column key")
        check(all(rejects[0].get(c) is None for c in DETAIL_COLUMNS),
              "every detail field is None on the reject record, not absent")


async def scenario_partial_payload():
    global _STUB_RESPONSE
    print("\n5. Jupiter returns a partial payload (no audit object)")
    reset_state()

    contract = "MintPartial111111111111111111111111111111111"
    _STUB_RESPONSE = {contract: {
        "market_cap": 22_000.0,
        "top_holders_pct": None,      # audit block absent
        "organic_score": 44.0,
        "dev_migrations": None,
        "dev_mints": None,
        "liquidity": 9_100.0,
        "launchpad": "bonk",
        "live_holder_count": None,
    }}

    await runner.open_position(make_decision(contract, "PARTLY"),
                               make_call(contract, "PARTLY", 24_000))

    check(contract in runner.POSITIONS, "position opened despite missing fields")
    position = runner.POSITIONS.get(contract, {})
    check(position.get("organic_score") == 44.0, "present field stored")
    check(position.get("top_holders_pct") is None, "absent field stored as None")
    check(position.get("launchpad") == "bonk", "text field stored")


async def scenario_monitor_loop_untouched():
    print("\n6. Monitor loop's fetch_market_caps contract is unchanged")

    # The monitor loop calls fetch_market_caps and treats the value as a
    # number. This is the check that stage 2 did not quietly change that.
    import inspect
    signature = inspect.signature(market_data.fetch_market_caps)
    check(list(signature.parameters) == ["session", "mints"],
          "fetch_market_caps(session, mints) signature unchanged")

    source = inspect.getsource(runner._monitor_once)
    check("fetch_market_caps" in source,
          "monitor loop still calls fetch_market_caps, not the detail fetch")
    check("fetch_token_details" not in source,
          "monitor loop does NOT call the heavier detail fetch every 5s")


async def main():
    print("=" * 78)
    print("STAGE 2 INTEGRATION TEST - Jupiter detail fields end to end")
    print("(no network, no Telegram, scratch folder)")
    print("=" * 78)

    await scenario_normal_buy()
    await scenario_gap_reject()
    await scenario_floor_reject()
    await scenario_no_jupiter_data()
    await scenario_partial_payload()
    await scenario_monitor_loop_untouched()

    print("\n" + "=" * 78)
    if not FAILURES:
        print("STAGE 2 INTEGRATION TEST PASSED")
    else:
        print(f"STAGE 2 INTEGRATION TEST FAILED - {len(FAILURES)} problem(s):")
        for item in FAILURES:
            print(f"  - {item}")
    print("=" * 78)
    return len(FAILURES)


if __name__ == "__main__":
    code = asyncio.run(main())
    os.chdir(PROJECT_ROOT)
    shutil.rmtree(SCRATCH, ignore_errors=True)
    raise SystemExit(1 if code else 0)
