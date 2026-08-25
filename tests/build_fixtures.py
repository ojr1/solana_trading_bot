"""
tests/build_fixtures.py

Builds synthetic logs/positions.json files for testing the analysis chain,
so data_loader / time_of_day_analysis / pcr_analysis can be exercised without
touching real trade history.

Three datasets, matching the three states the analysis will actually meet:

  legacy   45 trades, none carrying the Jupiter detail fields.
           This is the shape of the current 41-trade dataset. The exploratory
           section must degrade cleanly rather than printing a correlation
           computed on nothing.

  mixed    45 old trades plus 25 new ones with the fields populated.
           The shape after a few nights on the updated code. Some columns
           cross MIN_SAMPLE_FOR_INPUT and some do not.

  full     70 trades, all fields populated, with a KNOWN relationship
           planted in top_holders_pct so the exploratory section can be
           checked for false negatives as well as false positives.

THE POINT OF THE 'legacy' SET
-----------------------------
Its PCR column is built deliberately to reproduce the 10 Aug 2026 bug: a
slightly POSITIVE rank correlation against return alongside a strongly
NEGATIVE gap between the mean returns of the high- and low-conviction halves.
That combination is what made the old verdict section print "inverting it
would beat using it" while the correlation table said +0.067.

The numbers are found by search rather than hand-written, because the
combination is fiddly to hit by eye. Seed is fixed, so the fixture is
identical on every run.

    python tests/build_fixtures.py
"""

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures"

LAUNCHPADS = ["pump.fun", "bonk", "moonshot"]


def spearman(x, y):
    """Same rank correlation the analysis uses, so targets are comparable."""
    xr = pd.Series(x).rank()
    yr = pd.Series(y).rank()
    return float(np.corrcoef(xr, yr)[0, 1])


def find_contradictory_pcr_set(n=45, seed=0):
    """Search for a (pcr, return_pct) pairing that reproduces the 10 Aug bug.

    Target: rank correlation slightly positive, mean-of-halves gap strongly
    negative. The real run was r=+0.067 with a -9.8pp mean gap.

    Returns (pcr_list, return_list).
    """
    rng = random.Random(seed)

    # A realistic return distribution: mostly losses clustered in the
    # stop-loss / trailing-stop bands, a few large winners.
    returns = (
        [-95.0, -92.0, -88.0]                       # total wipeouts
        + [round(rng.uniform(-72, -55), 1) for _ in range(18)]   # stop bands
        + [round(rng.uniform(-40, -5), 1) for _ in range(9)]     # small losses
        + [round(rng.uniform(2, 60), 1) for _ in range(10)]      # small wins
        + [104.0, 117.0, 174.0, 239.0, 108.0]       # the big winners
    )
    returns = returns[:n]

    pcrs = [round(0.20 + 0.60 * i / (n - 1), 3) for i in range(n)]

    best = None
    for attempt in range(200_000):
        rng.shuffle(returns)

        r = spearman(pcrs, returns)
        if not (0.03 <= r <= 0.10):
            continue

        midpoint = float(np.median(pcrs))
        high = [ret for p, ret in zip(pcrs, returns) if p >= midpoint]
        low = [ret for p, ret in zip(pcrs, returns) if p < midpoint]
        gap = float(np.mean(high) - np.mean(low))

        if gap <= -8.0:
            best = (list(pcrs), list(returns), r, gap, attempt)
            break

    if best is None:
        raise SystemExit(
            "Could not find a contradictory pairing - widen the search bounds."
        )

    pcrs, returns, r, gap, attempt = best
    print(f"  contradiction found after {attempt:,} shuffles:")
    print(f"    rank correlation : {r:+.3f}  (positive)")
    print(f"    mean-of-halves gap: {gap:+.1f} pp  (negative)")
    print(f"    -> the exact shape that broke the old verdict section")
    return pcrs, returns


def build_position(index, opened_at, pcr, return_pct, with_details,
                   rng, top_holders_override=None):
    """One synthetic closed position in the shape runner.py writes."""
    sol_invested = round(rng.uniform(0.18, 0.50), 3)
    realised_sol = round(sol_invested * (1 + return_pct / 100.0), 6)

    call_mc = int(rng.uniform(12_000, 70_000))
    entry_mc = int(call_mc * rng.uniform(0.72, 1.05))

    position = {
        "ticker": f"TKN{index:03d}",
        "contract_address": f"Mint{index:04d}" + "1" * 36,
        "opened_at": opened_at.isoformat(),
        "closed_at": (opened_at + timedelta(minutes=rng.randint(5, 300))).isoformat(),
        "reference_mc": entry_mc,
        "entry_mc": entry_mc,
        "call_mc": call_mc,
        "sol_invested": sol_invested,
        "realised_sol": max(realised_sol, 0.0),
        "total_tokens_bought": sol_invested,
        "tokens_remaining": 0.0,
        "original_tokens": sol_invested,
        "pcr": pcr,
        "planned_lot_sol": sol_invested,
        "gt_score": rng.randint(1, 5),
        "holders": rng.randint(40, 900),
        "age_minutes": rng.randint(2, 240),
        "bundled_pct": round(rng.uniform(0, 32), 1),
        "fills": [{"stage": 1, "sol": sol_invested, "mc": entry_mc,
                   "at": opened_at.isoformat()}],
        "pending_tranches": [],
        "last_fill_mc": entry_mc,
        "peak_mc": int(entry_mc * rng.uniform(1.0, 3.5)),
        "initials_taken": return_pct > 50,
        "last_sell_mc": None,
        "fired_levels": [],
        "closed": True,
        "last_exit_type": "trailing_stop" if return_pct > 0 else "stop_loss",
        "pending": None,
    }

    if with_details:
        # top_holders_pct can be forced to carry a planted relationship, so
        # the exploratory section can be tested for false negatives too.
        if top_holders_override is not None:
            top_holders = top_holders_override
        else:
            top_holders = round(rng.uniform(5, 45), 1)

        position.update({
            "top_holders_pct": top_holders,
            "organic_score": round(rng.uniform(10, 95), 1),
            "dev_migrations": float(rng.randint(0, 12)),
            "dev_mints": float(rng.randint(0, 40)),
            "liquidity": round(rng.uniform(3_000, 90_000), 1),
            "launchpad": rng.choice(LAUNCHPADS),
            "live_holder_count": float(rng.randint(40, 1200)),
        })

    return position


def write_dataset(name, positions):
    path = FIXTURE_DIR / name / "logs"
    path.mkdir(parents=True, exist_ok=True)
    target = path / "positions.json"
    with open(target, "w", encoding="utf-8") as f:
        json.dump(positions, f, indent=2)
    print(f"  wrote {target.relative_to(PROJECT_ROOT)} "
          f"({len(positions)} positions)")


def spread_over_nights(count, nights, rng, start_day=7):
    """Timestamps spread across several nights, weighted to trading hours."""
    moments = []
    for i in range(count):
        night = i % nights
        # Cluster inside the 18:00-06:00 window so the time-of-day script has
        # something realistic to bucket.
        hour = rng.choice([18, 19, 20, 21, 22, 23, 0, 1, 2, 3, 4, 5])
        day = start_day + night + (1 if hour < 12 else 0)
        moments.append(datetime(2026, 8, day, hour,
                                rng.randint(0, 59), tzinfo=timezone.utc))
    return moments


def main():
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    print("Building analysis fixtures...\n")

    # ---- legacy: reproduces the 10 Aug contradiction, no detail fields ----
    print("legacy (45 trades, no Jupiter detail fields)")
    pcrs, returns = find_contradictory_pcr_set(n=45, seed=0)

    rng = random.Random(1)
    moments = spread_over_nights(45, 3, rng)
    legacy = {}
    for i, (pcr, ret, moment) in enumerate(zip(pcrs, returns, moments)):
        p = build_position(i, moment, pcr, ret, with_details=False, rng=rng)
        legacy[p["contract_address"]] = p
    write_dataset("legacy", legacy)

    # ---- mixed: legacy plus newer trades that DO carry the fields --------
    print("\nmixed (45 legacy + 25 new)")
    rng = random.Random(2)
    mixed = dict(legacy)
    new_moments = spread_over_nights(25, 2, rng, start_day=16)
    for i, moment in enumerate(new_moments, start=100):
        ret = rng.choice(returns)
        p = build_position(i, moment, round(rng.uniform(0.2, 0.8), 3), ret,
                           with_details=True, rng=rng)
        mixed[p["contract_address"]] = p
    write_dataset("mixed", mixed)

    # ---- full: everything populated, with a planted relationship ---------
    print("\nfull (70 trades, all fields, planted top_holders_pct signal)")
    rng = random.Random(3)
    full_moments = spread_over_nights(70, 6, rng, start_day=16)
    full = {}
    for i, moment in enumerate(full_moments, start=200):
        ret = round(rng.uniform(-95, 240), 1)
        # PLANTED: low top-holder concentration goes with better returns.
        # Strong and deliberate, so a failure to detect it is a real bug in
        # the analysis rather than an ambiguous result.
        planted = round(45 - (ret + 95) / 335 * 38 + rng.uniform(-3, 3), 1)
        p = build_position(i, moment, round(rng.uniform(0.2, 0.8), 3), ret,
                           with_details=True, rng=rng,
                           top_holders_override=planted)
        full[p["contract_address"]] = p
    write_dataset("full", full)

    print("\nFixtures built.")


if __name__ == "__main__":
    main()
