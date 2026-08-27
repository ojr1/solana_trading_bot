"""
entry_analysis.py - Stage 2b: does anything recorded at entry distinguish
positions that moved (peak_mc/entry_mc >= 1.5x) from ones that never did?

READ-ONLY. Reads logs/positions.json and data/calls.jsonl directly, and
imports exit_analysis.py's load_positions()/load_fills()/build_sample() and
its slippage constants rather than re-deriving them, so the P&L model used
in Step 2's cut-off sweep is GUARANTEED identical to exit_analysis.py's, not
just similarly-written. Writes nothing anywhere. Prints its report to
stdout; ENTRY_ANALYSIS.md is a separate, hand-written document built from
this script's real output.

    python src/entry_analysis.py
"""

import hashlib
import json
import random
import statistics
from datetime import datetime
from pathlib import Path

from exit_analysis import (  # read-only: only ever reads these
    load_positions, load_fills, build_sample,
    median, percentile, SLIPPAGE_BPS_CURRENT, PRIORITY_FEE_SOL,
)

CALLS_PATH = Path("data/calls.jsonl")
HASH_PATHS = [
    Path("logs/positions.json"), Path("data/calls.jsonl"),
    Path("data/fills.jsonl"), Path("data/snapshots.jsonl"),
]

FIELDS = ["pcr", "gt_score", "holders", "age_minutes", "bundled_pct"]

PERMUTATIONS = 10_000
RNG_SEED = 20260828  # fixed, so this report is reproducible on re-run


def header(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def file_hashes():
    out = {}
    for p in HASH_PATHS:
        out[str(p)] = hashlib.md5(p.read_bytes()).hexdigest() if p.exists() else "MISSING"
    return out


def load_calls():
    with open(CALLS_PATH, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def iqr_overlap_fraction(a, b):
    """
    Fraction of the UNION of two groups' [25th,75th] boxes that is shared by
    both. 0 = boxes don't touch at all (clean separation). 1 = identical
    boxes (no separation). Used only as a descriptive summary, not a test.
    """
    a25, a75 = percentile(a, 25), percentile(a, 75)
    b25, b75 = percentile(b, 25), percentile(b, 75)
    lo, hi = max(a25, b25), min(a75, b75)
    overlap = max(0.0, hi - lo)
    union = max(a75, b75) - min(a25, b25)
    if union == 0:
        return 0.0 if overlap == 0 else 1.0
    return overlap / union


def permutation_test_median_gap(a, b, seed=RNG_SEED, n=PERMUTATIONS):
    """
    Two-sample permutation test on the difference in medians.

    Pools a and b, reshuffles the labels n times, and reports what fraction
    of reshuffles produce a |median gap| at least as large as the one
    actually observed. No distributional assumption - exactly the same
    logic as pcr_analysis.py's permutation approach elsewhere in this
    project, applied to a two-group median gap instead of a correlation.
    """
    observed = abs(median(a) - median(b))
    pooled = list(a) + list(b)
    na = len(a)
    rng = random.Random(seed)
    hits = 0
    for _ in range(n):
        rng.shuffle(pooled)
        shuffled_a, shuffled_b = pooled[:na], pooled[na:]
        if abs(median(shuffled_a) - median(shuffled_b)) >= observed:
            hits += 1
    return observed, hits / n


def label_positions(positions, cutoff_multiple):
    moved, dead = {}, {}
    for contract, p in positions.items():
        (moved if p["peak_mc"] / p["entry_mc"] >= cutoff_multiple else dead)[contract] = p
    return moved, dead


# ---------------------------------------------------------------------------


def section_group_counts(positions):
    header("GROUP COUNTS")
    m15, d15 = label_positions(positions, 1.5)
    m20, d20 = label_positions(positions, 2.0)
    print(f"At 1.5x: MOVED {len(m15)}, DEAD {len(d15)}  (expected 31/33)")
    print(f"At 2.0x: MOVED {len(m20)}, DEAD {len(d20)}  (secondary split)")
    if len(m15) != 31 or len(d15) != 33:
        print("\n*** MISMATCH from expected 31/33 - see instruction to stop and explain. ***")
    return m15, d15, m20, d20


def section_step1_fields(moved, dead):
    header("STEP 1 - CANDIDATE DISCRIMINATORS (1.5x split)")

    field_summaries = {}
    for field in FIELDS:
        moved_vals = [p[field] for p in moved.values() if p.get(field) is not None]
        dead_vals = [p[field] for p in dead.values() if p.get(field) is not None]
        moved_missing = len(moved) - len(moved_vals)
        dead_missing = len(dead) - len(dead_vals)

        print(f"\n{field}")
        print(f"  {'':<10}{'present':>9}{'missing':>9}{'median':>10}{'p25':>10}{'p75':>10}{'min':>10}{'max':>10}")
        print(f"  {'MOVED':<10}{len(moved_vals):>9}{moved_missing:>9}"
              f"{median(moved_vals):>10.3f}{percentile(moved_vals,25):>10.3f}"
              f"{percentile(moved_vals,75):>10.3f}{min(moved_vals):>10.3f}{max(moved_vals):>10.3f}")
        print(f"  {'DEAD':<10}{len(dead_vals):>9}{dead_missing:>9}"
              f"{median(dead_vals):>10.3f}{percentile(dead_vals,25):>10.3f}"
              f"{percentile(dead_vals,75):>10.3f}{min(dead_vals):>10.3f}{max(dead_vals):>10.3f}")

        overlap = iqr_overlap_fraction(moved_vals, dead_vals)
        gap = median(moved_vals) - median(dead_vals)
        direction = "higher in MOVED" if gap > 0 else ("higher in DEAD" if gap < 0 else "no difference")
        verdict = "separates" if overlap < 0.5 else "IQRs overlap heavily - little/no discriminating power"
        print(f"  median gap = {gap:+.3f} ({direction}); IQR overlap fraction = {overlap:.2f} -> {verdict}")

        field_summaries[field] = {
            "moved_vals": moved_vals, "dead_vals": dead_vals,
            "moved_missing": moved_missing, "dead_missing": dead_missing,
            "overlap": overlap, "gap": gap,
            "direction": ">= " if gap > 0 else "<=",  # sweep direction favouring MOVED
        }
    return field_summaries


def section_step2_sweep(positions, field_summaries):
    header("STEP 2 - CUT-OFF SWEEP (P&L at 2500bps + priority fee, same model as exit_analysis.py)")

    fills = load_fills()
    all_records, exclusions = build_sample(positions, fills)
    by_contract = {r["contract"]: r for r in all_records}
    slip = SLIPPAGE_BPS_CURRENT / 10_000.0
    realised_scale = (1 - slip) / (1 + slip)

    def pnl_for_subset(contracts):
        subset = [by_contract[c] for c in contracts if c in by_contract]
        total_invested = sum(r["sol_invested"] for r in subset)
        total_realised = sum(r["realised_sol"] for r in subset) * realised_scale
        fee_txns = sum(r["num_buy_fills"] + len(r["sells"]) for r in subset)
        fees = fee_txns * PRIORITY_FEE_SOL
        return total_realised - total_invested - fees

    baseline_pnl = pnl_for_subset(list(by_contract))
    print(f"Baseline (no filter, all {len(by_contract)} positions with fills.jsonl "
          f"coverage): {baseline_pnl:+.4f} SOL\n")

    sweep_results = {}
    for field in FIELDS:
        summary = field_summaries[field]
        direction = summary["direction"]  # ">= " keeps high values (favours MOVED median), "<= " keeps low

        contract_value = {}
        for contract, p in positions.items():
            v = p.get(field)
            if v is not None:
                contract_value[contract] = v
        values_sorted = sorted(set(contract_value.values()))
        # Decile cut-offs across the field's own observed values - grounded
        # in real data, not an arbitrary invented grid.
        cutoffs = sorted(set(
            percentile(values_sorted, q) for q in (10, 20, 30, 40, 50, 60, 70, 80, 90)
        ))

        print(f"\n{field}  (keeping positions where {field} {direction}cutoff, "
              f"the direction favouring MOVED's median)")
        print(f"  {'cutoff':>12}{'dead_removed':>14}{'movers_lost':>14}{'kept_pnl':>12}")

        rows = []
        for cutoff in cutoffs:
            if direction.strip() == ">=":
                kept = {c for c, v in contract_value.items() if v >= cutoff}
            else:
                kept = {c for c, v in contract_value.items() if v <= cutoff}

            moved_kept = sum(1 for c in kept if positions[c]["peak_mc"] / positions[c]["entry_mc"] >= 1.5)
            dead_kept = len(kept) - moved_kept
            moved_total = sum(1 for p in positions.values() if p["peak_mc"] / p["entry_mc"] >= 1.5)
            dead_total = len(positions) - moved_total
            dead_removed = dead_total - dead_kept
            movers_lost = moved_total - moved_kept

            kept_pnl = pnl_for_subset(kept) if kept & set(by_contract) else 0.0
            print(f"  {cutoff:>12.3f}{dead_removed:>14}{movers_lost:>14}{kept_pnl:>+12.4f}")
            rows.append({
                "cutoff": cutoff, "dead_removed": dead_removed,
                "movers_lost": movers_lost, "kept_pnl": kept_pnl,
            })

        missing_note = ""
        n_missing = (33 + 31) - len(contract_value)  # 64 - present
        if n_missing:
            missing_note = f" ({n_missing} position(s) missing this field, excluded from the sweep entirely)"
        if missing_note:
            print(f" {missing_note}")

        sweep_results[field] = rows

    return sweep_results, baseline_pnl


def section_step3_sample_honesty(field_summaries):
    header("STEP 3 - SAMPLE SIZE HONESTY")

    print("Group sizes: MOVED n=31, DEAD n=33. 64 positions total, spanning "
          "2026-08-07 to 2026-08-25 (~18 days).")
    print("This is a small sample by any standard for detecting a moderate effect.\n")

    # Identify the field with the least IQR overlap = "best separating"
    best_field = min(field_summaries, key=lambda f: field_summaries[f]["overlap"])
    s = field_summaries[best_field]
    observed_gap, p_value = permutation_test_median_gap(s["moved_vals"], s["dead_vals"])

    print(f"Best-separating field by IQR overlap: {best_field} "
          f"(overlap fraction {s['overlap']:.2f}, median gap {s['gap']:+.3f})")
    print(f"Permutation test on the median gap ({PERMUTATIONS:,} reshuffles, seed={RNG_SEED}):")
    print(f"  observed |median gap| = {observed_gap:.3f}")
    print(f"  p = {p_value:.4f}  (fraction of random relabellings producing a gap at least this large)")
    bonferroni_bar = 0.05 / len(FIELDS)
    print(f"  5 fields were examined, so the naive p<0.05 bar is not the right one to "
          f"read this against - divided by 5 (Bonferroni, same logic pcr_analysis.py "
          f"already uses elsewhere in this project) the bar is p<{bonferroni_bar:.3f}.")
    verdict = "clears" if p_value < bonferroni_bar else "does NOT clear"
    print(f"  This {verdict} that adjusted bar.")

    return {"best_field": best_field, "observed_gap": observed_gap, "p_value": p_value,
            "bonferroni_bar": bonferroni_bar}


def section_step4_call_source(positions):
    header("STEP 4 - CALL SOURCE FIELDS (data/calls.jsonl)")

    calls = load_calls()
    bought = {c["contract_address"]: c for c in calls if c["event"] == "bought"}
    covered = [contract for contract in positions if contract in bought]
    missing = [contract for contract in positions if contract not in bought]
    print(f"{len(covered)} of {len(positions)} positions have a 'bought' record in "
          f"calls.jsonl; {len(missing)} do not (pre-data_logger.py positions) and are "
          f"excluded from this section: "
          f"{', '.join(positions[c]['ticker'] for c in missing) or 'none'}")

    moved_contracts = {c for c in covered if positions[c]["peak_mc"] / positions[c]["entry_mc"] >= 1.5}
    dead_contracts = set(covered) - moved_contracts

    def group_field(contracts, extractor):
        vals = []
        for c in contracts:
            v = extractor(bought[c])
            if v is not None:
                vals.append(v)
        return vals

    print(f"\nMOVED (covered) n={len(moved_contracts)}, DEAD (covered) n={len(dead_contracts)}\n")

    # call_mc
    for label, extractor in [
        ("call_mc", lambda r: r.get("call_mc")),
        ("hour_of_day (UTC)", lambda r: datetime.fromisoformat(r["ts"]).hour),
        ("day_of_week (0=Mon)", lambda r: datetime.fromisoformat(r["ts"]).weekday()),
    ]:
        mv = group_field(moved_contracts, extractor)
        dv = group_field(dead_contracts, extractor)
        print(f"{label}:")
        print(f"  MOVED median={median(mv):.2f}  p25={percentile(mv,25):.2f}  p75={percentile(mv,75):.2f}")
        print(f"  DEAD  median={median(dv):.2f}  p25={percentile(dv,25):.2f}  p75={percentile(dv,75):.2f}")

    print("\nJupiter detail fields (top_holders_pct, organic_score, etc.): null on "
          "all 70 calls.jsonl records. Not treated as data, per instruction.")


def main():
    print("Hashes BEFORE:")
    before = file_hashes()
    for k, v in before.items():
        print(f"  {k}: {v}")

    positions = load_positions()
    m15, d15, m20, d20 = section_group_counts(positions)

    if len(m15) == 31 and len(d15) == 33:
        field_summaries = section_step1_fields(m15, d15)
        section_step2_sweep(positions, field_summaries)
        section_step3_sample_honesty(field_summaries)
        section_step4_call_source(positions)
    else:
        print("\nSTOPPED: group counts did not match 31/33 - see instruction.")

    header("HASHES AFTER (must match BEFORE)")
    after = file_hashes()
    for k, v in after.items():
        match = "OK" if after[k] == before[k] else "MISMATCH!!"
        print(f"  {k}: {v}  [{match}]")


if __name__ == "__main__":
    main()
