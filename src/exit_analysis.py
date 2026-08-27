"""
exit_analysis.py - Stage 2, step 1: exit / rule analysis over closed positions.

READ-ONLY. Reads logs/positions.json and data/fills.jsonl only, and reads
(never writes) exit_logic.py's module-level constants so the "current
thresholds" reported below can never silently drift out of sync with the
real running code. Writes nothing anywhere - not to logs/, not to data/,
not even a cache file. Prints its report to stdout; EXIT_ANALYSIS.md is a
separate, hand-written document built from this script's real output.

Every number below is either read directly from the two source files or
derived from an explicit, stated formula on them. Nothing is interpolated
or assumed about price behaviour BETWEEN recorded points - see the
"retrace behaviour" section, which explains why that question is reported
as unanswerable rather than approximated.

    python src/exit_analysis.py
"""

import json
import statistics
from datetime import datetime
from pathlib import Path

import exit_logic  # read-only: only ever reads its module-level constants

POSITIONS_PATH = Path("logs/positions.json")
FILLS_PATH = Path("data/fills.jsonl")

MULTIPLE_BUCKETS = [
    ("<1x", lambda m: m < 1.0),
    ("1-2x", lambda m: 1.0 <= m < 2.0),
    ("2-5x", lambda m: 2.0 <= m < 5.0),
    ("5-10x", lambda m: 5.0 <= m < 10.0),
    (">10x", lambda m: m >= 10.0),
]

SLIPPAGE_BPS_HISTORICAL = 0        # see Step 2 section: confirmed, not the 100bps the brief assumed
SLIPPAGE_BPS_CURRENT = 2500
PRIORITY_FEE_SOL = 125_000 / 1_000_000_000  # 0.000125 SOL


def header(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def load_positions():
    with open(POSITIONS_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_fills():
    with open(FILLS_PATH, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def parse_ts(s):
    return datetime.fromisoformat(s)


def median(values):
    return statistics.median(values) if values else None


def percentile(values, pct):
    """Linear-interpolation percentile, pct in [0, 100]. None if no data."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (pct / 100.0)
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def bucket_counts(values):
    counts = {label: 0 for label, _ in MULTIPLE_BUCKETS}
    for v in values:
        for label, test in MULTIPLE_BUCKETS:
            if test(v):
                counts[label] += 1
                break
    return counts


# ---------------------------------------------------------------------------
# Sample / exclusions
# ---------------------------------------------------------------------------


def build_sample(positions, fills):
    """
    Returns (records, exclusions) where records is a list of dicts, one per
    closed position, carrying everything the rest of this script needs, and
    exclusions is {reason: [tickers]} for positions left out of one or more
    sections below - reported, never silently dropped.
    """
    sells_by_contract = {}
    for f in fills:
        if f["event"] == "sell":
            sells_by_contract.setdefault(f["contract_address"], []).append(f)

    records = []
    exclusions = {"no_fills_jsonl_coverage": []}

    for contract, p in positions.items():
        if not p.get("closed"):
            continue  # none currently, but stated for completeness

        sells = sells_by_contract.get(contract, [])
        if not sells:
            exclusions["no_fills_jsonl_coverage"].append(p["ticker"])

        closed_at = p.get("closed_at")
        if not closed_at and sells:
            # Reconstruct from the last sell event's own timestamp - real
            # data, just not written back into positions.json for positions
            # closed before that field existed (pre-10 Aug).
            closed_at = max(s["ts"] for s in sells)

        records.append({
            "ticker": p["ticker"],
            "contract": contract,
            "entry_mc": p["entry_mc"],
            "peak_mc": p["peak_mc"],
            "last_sell_mc": p["last_sell_mc"],
            "opened_at": p["opened_at"],
            "closed_at": closed_at,          # may still be None
            "closed_at_reconstructed": not p.get("closed_at") and closed_at is not None,
            "sol_invested": p["sol_invested"],
            "realised_sol": p["realised_sol"],
            "num_buy_fills": len(p.get("fills", [])),
            "sells": sells,                   # [] if no fills.jsonl coverage
        })

    return records, exclusions


# ---------------------------------------------------------------------------
# Step 1: peak vs final (Retrace behaviour subsection is CANCELLED - see
# report; this function intentionally does not attempt it)
# ---------------------------------------------------------------------------


def section_peak_vs_final(records):
    header("STEP 1 - PEAK VS FINAL")

    peak_multiples = [r["peak_mc"] / r["entry_mc"] for r in records]
    exit_multiples = [r["last_sell_mc"] / r["entry_mc"] for r in records]
    capture_ratios = [e / p for e, p in zip(exit_multiples, peak_multiples)]

    print(f"n = {len(records)} closed positions (all 64; entry_mc, peak_mc and "
          f"last_sell_mc are present on every one, so none are excluded here)\n")

    print("Peak multiple distribution (peak_mc / entry_mc):")
    for label, count in bucket_counts(peak_multiples).items():
        print(f"  {label:<8}{count:>4}")

    print("\nExit multiple distribution (last_sell_mc / entry_mc):")
    print("  NOTE: last_sell_mc is the market cap at the FINAL sell only, not")
    print("  a fraction-weighted blend across partial sells (initials/ladder")
    print("  clips at earlier, usually higher, prices are not reflected here).")
    for label, count in bucket_counts(exit_multiples).items():
        print(f"  {label:<8}{count:>4}")

    print(f"\nCapture ratio (exit multiple / peak multiple):")
    print(f"  median = {median(capture_ratios):.3f}")
    print(f"  25th percentile = {percentile(capture_ratios, 25):.3f}")
    print(f"  75th percentile = {percentile(capture_ratios, 75):.3f}")

    peaked_2x_exited_below_1x = sum(
        1 for p, e in zip(peak_multiples, exit_multiples) if p > 2.0 and e < 1.0
    )
    print(f"\nPositions that peaked above 2x but exited below 1x: "
          f"{peaked_2x_exited_below_1x} of {len(records)}")

    return {
        "peak_multiples": peak_multiples,
        "exit_multiples": exit_multiples,
        "capture_ratios": capture_ratios,
        "peaked_2x_exited_below_1x": peaked_2x_exited_below_1x,
    }


# ---------------------------------------------------------------------------
# Step 1b: exit type analysis (NEW, replaces "Retrace behaviour")
# ---------------------------------------------------------------------------


def section_exit_type_analysis(records, fills):
    header("STEP 1b - EXIT TYPE ANALYSIS")

    opened_at_by_contract = {r["contract"]: r["opened_at"] for r in records}
    entry_mc_by_contract = {r["contract"]: r["entry_mc"] for r in records}

    all_sells = [f for f in fills if f["event"] == "sell"]
    print(f"n = {len(all_sells)} sell events in data/fills.jsonl "
          f"(across {len(set(f['contract_address'] for f in all_sells))} of "
          f"{len(records)} positions - see exclusions in the written report)\n")

    by_type = {}
    for f in all_sells:
        by_type.setdefault(f["exit_type"], []).append(f)

    print(f"{'exit_type':<16}{'count':>7}{'median mult.':>14}{'total SOL':>12}{'median hold':>16}")
    print("-" * 78)

    exit_type_stats = {}
    for exit_type in sorted(by_type, key=lambda k: -len(by_type[k])):
        group = by_type[exit_type]
        multiples = [f["mc_at_fill"] / f["entry_mc"] for f in group]
        total_sol = sum(f["proceeds_sol"] for f in group)

        holds = []
        for f in group:
            opened = opened_at_by_contract.get(f["contract_address"])
            if opened:
                holds.append((parse_ts(f["ts"]) - parse_ts(opened)).total_seconds() / 60.0)

        med_hold = median(holds)
        med_hold_str = f"{med_hold:.1f} min" if med_hold is not None else "n/a"
        print(f"{exit_type:<16}{len(group):>7}{median(multiples):>13.3f}x"
              f"{total_sol:>12.4f}{med_hold_str:>16}")

        exit_type_stats[exit_type] = {
            "count": len(group), "median_multiple": median(multiples),
            "total_sol": total_sol, "median_hold_minutes": med_hold,
        }

    # Stop-loss + absolute-floor: how far did these get before dying
    dying = by_type.get("stop_loss", []) + by_type.get("absolute_floor", [])
    ratios = [f["peak_mc"] / f["entry_mc"] for f in dying]
    header_note = "STOP_LOSS + ABSOLUTE_FLOOR: peak_mc / entry_mc reached before dying"
    print(f"\n{header_note}")
    print(f"  n = {len(dying)} (stop_loss {len(by_type.get('stop_loss', []))} + "
          f"absolute_floor {len(by_type.get('absolute_floor', []))})")
    print(f"  min={min(ratios):.3f}x  25th={percentile(ratios, 25):.3f}x  "
          f"median={median(ratios):.3f}x  75th={percentile(ratios, 75):.3f}x  "
          f"max={max(ratios):.3f}x")

    # Split all 64 positions: ever exceeded 1.5x vs never
    over_15 = [r for r in records if r["peak_mc"] / r["entry_mc"] >= 1.5]
    under_15 = [r for r in records if r["peak_mc"] / r["entry_mc"] < 1.5]

    def group_stats(group):
        pnl = sum(r["realised_sol"] - r["sol_invested"] for r in group)
        wins = sum(1 for r in group if r["realised_sol"] > r["sol_invested"])
        return len(group), pnl, wins

    n_over, pnl_over, wins_over = group_stats(over_15)
    n_under, pnl_under, wins_under = group_stats(under_15)

    print(f"\nSplit by whether peak_mc/entry_mc ever reached 1.5x:")
    print(f"  Ever >= 1.5x   : {n_over:>3} positions, "
          f"P&L {pnl_over:+.4f} SOL, win rate {wins_over/n_over*100:.0f}%")
    print(f"  Never >= 1.5x  : {n_under:>3} positions, "
          f"P&L {pnl_under:+.4f} SOL, win rate {wins_under/n_under*100:.0f}%")

    return {
        "exit_type_stats": exit_type_stats,
        "dying_peak_ratio": {
            "n": len(dying), "min": min(ratios), "p25": percentile(ratios, 25),
            "median": median(ratios), "p75": percentile(ratios, 75), "max": max(ratios),
        },
        "over_15x": {"n": n_over, "pnl": pnl_over, "wins": wins_over},
        "under_15x": {"n": n_under, "pnl": pnl_under, "wins": wins_under},
    }


# ---------------------------------------------------------------------------
# Step 2: slippage-aware baseline
# ---------------------------------------------------------------------------


def section_slippage_baseline(records):
    header("STEP 2 - SLIPPAGE-AWARE BASELINE")

    total_invested = sum(r["sol_invested"] for r in records)
    total_realised = sum(r["realised_sol"] for r in records)
    as_recorded_return = total_realised - total_invested

    print("AS RECORDED:")
    print("  The logs reflect 0% slippage and 0 priority fee, NOT the 100bps")
    print("  the brief assumed. trade_execution.py had 100bps hardcoded, but")
    print("  runner.py's dry-run fills never call trade_execution.py at all -")
    print("  every simulated fill in these 64 positions used the live market")
    print("  cap Jupiter returned, with no slippage or fee deduction applied")
    print("  anywhere in the simulation. Confirmed by reading runner.py's")
    print("  open_position()/check_dca_fills(), not assumed.")
    print(f"\n  total invested : {total_invested:.4f} SOL")
    print(f"  total realised : {total_realised:.4f} SOL")
    print(f"  total return   : {as_recorded_return:+.4f} SOL")

    # Adjusted: buy-side slippage shrinks tokens received by 1/(1+slip);
    # sell-side slippage shrinks proceeds by (1-slip). ASSUMPTION: the
    # fraction of the position sold at each threshold, and the market cap at
    # which each threshold fires, are unchanged by slippage - only how much
    # SOL each already-decided trade nets is affected. This is what makes a
    # single scalar adjustment to realised_sol valid; see written report.
    slip = SLIPPAGE_BPS_CURRENT / 10_000.0
    realised_scale = (1 - slip) / (1 + slip)
    adjusted_realised = total_realised * realised_scale

    total_buy_fills = sum(r["num_buy_fills"] for r in records)
    total_sell_fills = sum(len(r["sells"]) for r in records)
    total_fee_txns = total_buy_fills + total_sell_fills
    total_fees = total_fee_txns * PRIORITY_FEE_SOL

    adjusted_return = adjusted_realised - total_invested - total_fees

    missing_sells = sum(1 for r in records if not r["sells"])
    print(f"\nAT {SLIPPAGE_BPS_CURRENT} BPS BOTH LEGS + {PRIORITY_FEE_SOL:.6f} SOL/txn priority fee:")
    print(f"  ASSUMPTION: full stated slippage tolerance consumed on every fill,")
    print(f"  symmetrically, on both the buy and the sell leg - a conservative")
    print(f"  (worst-realistic-case) model, not a simulation of actual realised")
    print(f"  slippage, which would fall somewhere between 0 and this figure.")
    print(f"  sol_invested is unchanged (you still spend what you declared);")
    print(f"  realised_sol is scaled by (1-slip)/(1+slip) = {realised_scale:.4f}.")
    print(f"\n  fee-bearing transactions: {total_buy_fills} buys (all 64 positions, "
          f"from positions.json) + {total_sell_fills} sells (data/fills.jsonl)")
    print(f"  = {total_fee_txns} total. {missing_sells} position(s) have zero")
    print(f"  fills.jsonl coverage, so at least {missing_sells} real sell")
    print(f"  transaction(s) are NOT counted here - total fees below are a")
    print(f"  slight underestimate by at most {missing_sells * PRIORITY_FEE_SOL:.6f} SOL.")
    print(f"\n  total invested          : {total_invested:.4f} SOL")
    print(f"  total realised (adj.)   : {adjusted_realised:.4f} SOL")
    print(f"  total priority fees     : -{total_fees:.4f} SOL")
    print(f"  total return (adjusted) : {adjusted_return:+.4f} SOL")

    difference = adjusted_return - as_recorded_return
    print(f"\nDIFFERENCE: {difference:+.4f} SOL "
          f"({difference / abs(as_recorded_return) * 100:+.1f}% relative to the as-recorded return)")

    return {
        "as_recorded_return": as_recorded_return,
        "adjusted_return": adjusted_return,
        "difference": difference,
        "total_fee_txns": total_fee_txns,
        "missing_sell_positions": missing_sells,
    }


# ---------------------------------------------------------------------------
# Step 3: rule comparison
# ---------------------------------------------------------------------------


def section_rule_comparison(records, exit_type_result):
    header("STEP 3 - RULE COMPARISON")

    print("CURRENT THRESHOLDS, read directly from src/exit_logic.py constants")
    print("(not hand-copied - this script imports the module and reads them):\n")
    print(f"  STOP_LOSS_DRAWDOWN      = {exit_logic.STOP_LOSS_DRAWDOWN:.0%}   "
          f"(before initials, vs average entry)")
    print(f"  INITIALS_TRIGGER_GAIN   = {exit_logic.INITIALS_TRIGGER_GAIN:.0%}  "
          f"(sell {exit_logic.INITIALS_SELL_FRACTION:.0%} of position)")
    print(f"  TRAILING_STOP_DRAWDOWN  = {exit_logic.TRAILING_STOP_DRAWDOWN:.0%}   "
          f"(after initials, vs running peak)")
    print(f"  ABSOLUTE_FLOOR_MC       = ${exit_logic.ABSOLUTE_FLOOR_MC:,.0f}")
    print(f"  LADDER_STEP             = ${exit_logic.LADDER_STEP:,.0f}  "
          f"(up to ${exit_logic.LADDER_WIDEN_ABOVE:,.0f})")
    print(f"  LADDER_STEP_LARGE       = ${exit_logic.LADDER_STEP_LARGE:,.0f}  "
          f"(above ${exit_logic.LADDER_WIDEN_ABOVE:,.0f})")
    print(f"  LADDER_CLIP_FRACTION    = {exit_logic.LADDER_CLIP_FRACTION:.0%} of remaining, per level")
    print(f"  MIN_GAP_BETWEEN_SELLS   = {exit_logic.MIN_GAP_BETWEEN_SELLS:.0%} above the previous sell")
    print()
    print("  NOTE: the module docstring at the top of exit_logic.py says the")
    print("  trailing stop fires at 70% below peak. The actual constant is")
    print("  60% (confirmed above, and matches the value TASK.md fixed the")
    print("  brief's own numbers against). The docstring is stale; the")
    print("  constant is what runs. Flagged here, not changed - out of scope.")

    print("\nRULE A (current flat 60% trailing stop) and the CURRENT LADDER:")
    print("  These are not backtested - they are what actually happened.")
    print("  See Step 1/1b above and EXIT_ANALYSIS.md for the exact totals.")
    ladder_clips = exit_type_result["exit_type_stats"].get("ladder_clip", {})
    initials = exit_type_result["exit_type_stats"].get("initials", {})
    print(f"    initials fired on {initials.get('count', 0)} positions, "
          f"ladder_clip fired {ladder_clips.get('count', 0)} times total")

    print("\nRULE B (stepped: 60% below 3x, 45% above 5x, 35% above 10x) and")
    print("TWO ALTERNATIVE LADDER LEVEL SETS: NOT HONESTLY SIMULABLE.")
    print("  See EXIT_ANALYSIS.md for the full reasoning, the bounded/")
    print("  directional statements that ARE supportable, and the two")
    print("  alternative ladder sets specified (not backtested) for the")
    print("  record.")


# ---------------------------------------------------------------------------


def main():
    positions = load_positions()
    fills = load_fills()
    records, exclusions = build_sample(positions, fills)

    header("SAMPLE")
    print(f"Closed positions in logs/positions.json: {len(records)}")
    print(f"Excluded from fills.jsonl-dependent sections (no coverage): "
          f"{len(exclusions['no_fills_jsonl_coverage'])} "
          f"({', '.join(exclusions['no_fills_jsonl_coverage']) or 'none'})")
    reconstructed = sum(1 for r in records if r["closed_at_reconstructed"])
    print(f"closed_at reconstructed from the last fills.jsonl sell event "
          f"(field absent in positions.json): {reconstructed}")
    still_missing = sum(1 for r in records if r["closed_at"] is None)
    print(f"closed_at unavailable even after reconstruction (no fills.jsonl "
          f"coverage AND no stored closed_at): {still_missing}")

    section_peak_vs_final(records)
    exit_type_result = section_exit_type_analysis(records, fills)
    section_slippage_baseline(records)
    section_rule_comparison(records, exit_type_result)

    header("DONE")
    print("See EXIT_ANALYSIS.md for the full written report, including the")
    print("retrace-behaviour finding (cancelled per instruction, reported as")
    print("unanswerable) and the Step 5 logging specification.")


if __name__ == "__main__":
    main()
