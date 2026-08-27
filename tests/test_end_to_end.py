"""
tests/test_end_to_end.py

The full seam, exercised with real code at every step and no network:

    runner.open_position()      real entry path, real guards
      -> runner._monitor_once() real monitor loop, real DCA
      -> exit_logic             real stop-loss / trailing stop / floor
      -> logs/positions.json    real file on disk
      -> data_loader            real loader
      -> pcr_analysis           real analysis, exploratory section populated

Only two things are faked: Jupiter (a stub returning a scripted price path)
and Telegram (never involved - open_position is called directly).

This is the test that would have caught the 10 Aug failure, where three
files were committed but two never reached disk and the bot ran a whole
session on rules nobody had checked.

    python tests/test_end_to_end.py

Success marker: "END-TO-END TEST PASSED" and a zero exit code.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC))

SCRATCH = PROJECT_ROOT / "_e2e_scratch"
if SCRATCH.exists():
    shutil.rmtree(SCRATCH)
(SCRATCH / "src").mkdir(parents=True)
for path in SRC.glob("*.py"):
    shutil.copy(path, SCRATCH / "src" / path.name)
os.chdir(SCRATCH)

import market_data          # noqa: E402
import runner               # noqa: E402

FAILURES = []


def check(condition, description):
    if condition:
        print(f"   [PASS] {description}")
    else:
        print(f"   [FAIL] {description}")
        FAILURES.append(description)


# ---------------------------------------------------------------------------
# Fake Jupiter: a scripted price path per contract, stepped by the monitor.
# ---------------------------------------------------------------------------

PRICE_PATHS = {}   # contract -> list of market caps, consumed left to right
CURRENT_MC = {}    # contract -> the price this cycle


async def stub_fetch_token_details(session, mints):
    """Entry-time fetch: live price plus the full detail set."""
    out = {}
    for mint in mints:
        if mint not in PRICE_PATHS:
            continue
        index = int(mint[-2:])
        out[mint] = {
            "market_cap": float(PRICE_PATHS[mint][0]),
            # Deliberately varied so the analysis has something to correlate.
            "top_holders_pct": round(8.0 + (index % 17) * 2.1, 1),
            "organic_score": round(20.0 + (index % 13) * 5.5, 1),
            "dev_migrations": float(index % 7),
            "dev_mints": float(index % 23),
            "liquidity": round(4_000 + (index % 19) * 3_100, 1),
            "launchpad": ["pump.fun", "bonk", "moonshot"][index % 3],
            "live_holder_count": float(90 + (index % 29) * 37),
        }
    return out


async def stub_fetch_market_caps(session, mints):
    """Monitor-loop fetch: market cap only, exactly as the real one returns."""
    return {m: CURRENT_MC[m] for m in mints if m in CURRENT_MC}


runner.market_data.fetch_token_details = stub_fetch_token_details
runner.market_data.fetch_market_caps = stub_fetch_market_caps

# Stage 1 safety guards (added 27 Aug 2026, after this file) are not what
# this file tests - it is the full entry -> monitor -> exit -> disk seam.
# Stubbed the same way as the market_data functions above, rather than
# depending on the real (small) wallet balance or being incidentally
# blocked by MAX_POSITION_SOL across 40 simulated positions. Both guards
# have their own dedicated tests in tests/test_safety.py.


async def _stub_get_balance():
    return 100.0


runner.wallet.get_balance = _stub_get_balance
runner.config.MAX_POSITION_SOL = 999.0


def make_call(contract, ticker, market_cap, index):
    return {
        "message_type": "call", "ticker": ticker, "token_name": f"token {index}",
        "contract_address": contract, "market_cap": market_cap,
        "gt_score": (index % 5) + 1,
        "holders": 60 + (index % 31) * 24,
        "age_minutes": 3 + (index % 17) * 8,
        "bundled_pct": round((index % 19) * 1.7, 1),
        "parse_ok": True,
    }


def make_decision(contract, ticker, index):
    total = 0.20 + (index % 7) * 0.05
    return {
        "action": "buy", "ticker": ticker, "contract_address": contract,
        "pcr": round(0.15 + (index % 11) * 0.06, 3),
        "total_lot_sol": round(total, 3),
        "tranches": [
            {"stage": 1, "sol": round(total * 0.45, 3),
             "drop_pct_from_previous_fill": 0},
            {"stage": 2, "sol": round(total * 0.30, 3),
             "drop_pct_from_previous_fill": 10},
            {"stage": 3, "sol": round(total * 0.25, 3),
             "drop_pct_from_previous_fill": 10},
        ],
    }


def build_price_path(entry_mc, index):
    """A plausible post-entry path: some run, most collapse."""
    if index % 3 == 0:                       # runner: up then trail off
        peak = entry_mc * (2.5 + (index % 5) * 0.4)
        return [entry_mc, entry_mc * 1.4, peak * 0.8, peak,
                peak * 0.55, peak * 0.32, peak * 0.30]
    if index % 3 == 1:                       # slow bleed into the stop
        return [entry_mc, entry_mc * 0.88, entry_mc * 0.74,
                entry_mc * 0.58, entry_mc * 0.40, entry_mc * 0.38]
    # straight to the floor
    return [entry_mc, entry_mc * 0.55, entry_mc * 0.20,
            entry_mc * 0.06, entry_mc * 0.05]


async def run_session(count=40):
    """Open `count` positions, then run the monitor until all are closed."""
    print(f"\n1. Opening {count} positions through the real entry path")

    for i in range(count):
        contract = f"Mint{i:04d}" + "1" * 34 + f"{i % 100:02d}"
        entry_mc = 14_000 + (i % 23) * 2_400
        PRICE_PATHS[contract] = build_price_path(entry_mc, i)
        CURRENT_MC[contract] = PRICE_PATHS[contract][0]
        await runner.open_position(
            make_decision(contract, f"TKN{i:02d}", i),
            make_call(contract, f"TKN{i:02d}", int(entry_mc * 1.05), i),
        )

    opened = sum(1 for p in runner.POSITIONS.values() if not p["closed"])
    check(opened > count * 0.7,
          f"{opened} of {count} calls opened (rest correctly rejected by guards)")

    print("\n2. Running the real monitor loop until every position closes")
    for cycle in range(1, 40):
        # Step every open position one point along its price path.
        for contract, path in PRICE_PATHS.items():
            position = runner.POSITIONS.get(contract)
            if not position or position["closed"]:
                CURRENT_MC.pop(contract, None)
                continue
            step = min(cycle, len(path) - 1)
            CURRENT_MC[contract] = float(path[step])

        await runner._monitor_once(None)

        still_open = sum(1 for p in runner.POSITIONS.values()
                         if not p["closed"])
        if still_open == 0:
            print(f"   all positions closed after {cycle} monitor cycles")
            break

    closed = [p for p in runner.POSITIONS.values() if p["closed"]]
    check(len(closed) > count * 0.7, f"{len(closed)} positions closed")

    exit_types = {p.get("last_exit_type") for p in closed}
    check(len(exit_types) > 1,
          f"multiple exit types exercised: {sorted(t for t in exit_types if t)}")

    # The detail fields must have survived entry -> DCA -> exit -> disk.
    saved = json.loads(Path("logs/positions.json").read_text(encoding="utf-8"))
    with_details = [p for p in saved.values()
                    if p.get("top_holders_pct") is not None]
    check(len(with_details) > count * 0.7,
          f"{len(with_details)} positions still carry the detail fields on disk")

    dca_used = sum(1 for p in saved.values() if len(p.get("fills", [])) > 1)
    check(dca_used > 0, f"DCA fired on {dca_used} positions (tranches still work)")


def run_analysis():
    print("\n3. Feeding the result into the analysis chain")

    for script in ("data_loader.py", "pcr_analysis.py",
                   "time_of_day_analysis.py"):
        result = subprocess.run([sys.executable, f"src/{script}"],
                                capture_output=True, text=True, timeout=300)
        ok = result.returncode == 0
        check(ok, f"{script} ran on the freshly generated positions.json")
        if not ok:
            print(result.stderr[-1200:])
            continue

        if script == "data_loader.py":
            check("LOADER SELF-TEST PASSED" in result.stdout,
                  "loader self-test passed on real generated data")
            # Every detail column should now be populated on most rows.
            populated = [line for line in result.stdout.splitlines()
                         if "top_holders_pct" in line]
            if populated:
                print(f"          -> {populated[0].strip()}")
            check(any("100.0%" in line or "9" in line for line in populated),
                  "top_holders_pct is populated in the loaded frame")

        if script == "pcr_analysis.py":
            check("EXPLORATORY JUPITER FIELDS" in result.stdout,
                  "exploratory section present")
            check("not tested: no values recorded yet" not in result.stdout,
                  "exploratory inputs are now actually TESTED, not skipped")
            check("RANK CORRELATION (the robust measure)" in result.stdout,
                  "fixed verdict section present")
            check("Inverting it would beat using it" not in result.stdout,
                  "old overstated wording absent")
            for line in result.stdout.splitlines():
                if line.startswith(("Top holders %", "Organic score",
                                    "Liquidity $")):
                    print(f"          -> {line.strip()}")


async def main():
    print("=" * 78)
    print("END-TO-END TEST - entry -> monitor -> exit -> disk -> analysis")
    print("(real code throughout; only Jupiter and Telegram are stubbed)")
    print("=" * 78)

    await run_session()
    run_analysis()

    print("\n" + "=" * 78)
    if not FAILURES:
        print("END-TO-END TEST PASSED")
    else:
        print(f"END-TO-END TEST FAILED - {len(FAILURES)} problem(s):")
        for item in FAILURES:
            print(f"  - {item}")
    print("=" * 78)
    return len(FAILURES)


if __name__ == "__main__":
    code = asyncio.run(main())
    os.chdir(PROJECT_ROOT)
    shutil.rmtree(SCRATCH, ignore_errors=True)
    raise SystemExit(1 if code else 0)
