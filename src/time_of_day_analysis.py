# src/time_of_day_analysis.py
"""
Trading performance by time of day, day of week, and window membership.

Rescoped 10 Aug 2026 after the trading window was set by decision rather
than by data. The old hardcoded 01:00-04:00 test is gone - that question
is settled. This script now answers the questions that are still live:

  1. Within the chosen window, are some hours materially worse?
  2. Are weekends actually better than weekdays?
  3. Was the Monday 08:00-09:00 extension a good idea?
  4. (Only until the gate goes live) Does in-window beat out-of-window?

IMPORTANT: once the entry gate is wired into runner.py, out-of-window calls
are logged but never bought, so they have no outcome. Question 4 becomes
permanently unanswerable from this data unless shadow price-tracking of
rejected calls is built. The report says so where relevant.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from data_loader import load_closed_trades
from trading_window import is_trading_window, DAY_NAMES

# Below this many trades, a bucket is noise rather than signal.
MIN_TRADES_FOR_SIGNAL = 5

# Reshuffles used by the permutation tests.
PERMUTATION_RUNS = 10000


# ---------------------------------------------------------------------------
# Preparation
# ---------------------------------------------------------------------------

def prepare(trades):
    """Add window membership and weekday columns to the loaded trades.

    Built with plain list comprehensions rather than pandas datetime
    accessors, so mixed GMT/BST offsets in the data cannot cause a dtype
    problem. Slower, but this runs on a few dozen rows.
    """
    moments = list(trades["opened_at_local"])

    trades = trades.copy()
    trades["in_window"] = [is_trading_window(m) for m in moments]
    trades["weekday"] = [m.weekday() for m in moments]
    trades["day_name"] = [DAY_NAMES[m.weekday()] for m in moments]
    # Saturday=5, Sunday=6
    trades["is_weekend"] = [m.weekday() >= 5 for m in moments]
    return trades


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def permutation_test(pnl, mask, runs=PERMUTATION_RUNS, seed=42):
    """Test whether a split of trades beats what random chance produces.

    Keeps every trade's P&L exactly as it happened but reshuffles which side
    of the split each one lands on, thousands of times. If the real gap is
    routinely matched by random shuffles, the split explains nothing.

    Excel analogy: RAND() shuffling one column against another, 10,000
    times, then counting how often the shuffle beats reality.

    Returns (gap, p_value, group_size). Gap is average SOL inside the group
    minus average SOL outside it. p_value is the share of shuffles that did
    at least as well - lower means harder to explain by luck.
    """
    pnl = np.asarray(pnl, dtype=float)
    mask = np.asarray(mask, dtype=bool)

    inside = int(mask.sum())
    outside = int((~mask).sum())
    if inside == 0 or outside == 0:
        return None, None, inside

    observed_gap = pnl[mask].mean() - pnl[~mask].mean()

    rng = np.random.default_rng(seed)  # fixed seed = repeatable
    at_least_as_extreme = 0
    for _ in range(runs):
        shuffled = rng.permutation(pnl)
        gap = shuffled[:inside].mean() - shuffled[inside:].mean()
        if gap >= observed_gap:
            at_least_as_extreme += 1

    return observed_gap, at_least_as_extreme / runs, inside


def verdict(p_value):
    """Plain-language reading of a p-value."""
    if p_value is None:
        return "not testable - one side of the split is empty"
    if p_value < 0.05:
        return "hard to explain by chance alone"
    if p_value < 0.20:
        return "suggestive, not conclusive"
    return "nothing here - random shuffles reproduce this routinely"


def summarise(group):
    """Return the standard stat block for a set of trades."""
    count = len(group)
    if count == 0:
        return None
    wins = int(group["is_win"].sum())
    net = group["net_sol"].sum()
    invested = group["sol_invested"].sum()
    return {
        "trades": count,
        "wins": wins,
        "win_rate": wins / count * 100,
        "net_sol": net,
        "avg_sol": net / count,
        "return_pct": (net / invested * 100) if invested else 0.0,
    }


# ---------------------------------------------------------------------------
# Report sections
# ---------------------------------------------------------------------------

def header(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def print_coverage(trades):
    """Distinct dates is the number that governs everything else here."""
    header("DATA COVERAGE")

    nights = trades["entry_date"].nunique()
    print(f"Closed trades: {len(trades)}")
    print(f"Distinct dates: {nights}")

    per_date = trades.groupby("entry_date").agg(
        trades=("net_sol", "size"),
        net_sol=("net_sol", "sum"),
    )
    for date, row in per_date.iterrows():
        day = DAY_NAMES[pd.Timestamp(date).weekday()][:3]
        print(f"  {date} ({day}): {int(row['trades']):>3} trades, "
              f"{row['net_sol']:>+7.3f} SOL")

    # Concentration check - many trades on one date is still one date.
    biggest = per_date["trades"].max()
    share = biggest / len(trades) * 100
    if share > 60:
        print(f"\nCAUTION: {share:.0f}% of all trades come from a single date.")
        print("This dataset is effectively one session. Hour and day splits")
        print("below describe that session, not a repeatable pattern.")
    elif nights < 5:
        print(f"\nCAUTION: {nights} dates is thin for day-of-week comparisons.")


def print_hourly_table(trades):
    """24-hour table, marking which hours the window actually permits."""
    header("PERFORMANCE BY ENTRY HOUR (London time)")

    print(f"{'Hour':<7}{'Gate':>6}{'Trades':>8}{'Wins':>6}{'Win %':>8}"
          f"{'Net SOL':>11}{'Avg SOL':>10}{'Return %':>11}{'':>7}")
    print("-" * 78)

    for hour in range(24):
        group = trades[trades["entry_hour"] == hour]
        stats = summarise(group)

        # Window membership is day-dependent, so report what share of this
        # hour's trades the gate would now allow.
        if len(group) > 0:
            allowed = group["in_window"].mean()
            gate = "open" if allowed == 1 else ("shut" if allowed == 0 else "part")
        else:
            gate = "-"

        label = f"{hour:02d}:00"

        if stats is None:
            print(f"{label:<7}{gate:>6}{'-':>8}{'-':>6}{'-':>8}"
                  f"{'-':>11}{'-':>10}{'-':>11}")
            continue

        flag = "" if stats["trades"] >= MIN_TRADES_FOR_SIGNAL else "  thin"
        print(f"{label:<7}{gate:>6}{stats['trades']:>8}{stats['wins']:>6}"
              f"{stats['win_rate']:>7.0f}%{stats['net_sol']:>+11.3f}"
              f"{stats['avg_sol']:>+10.3f}{stats['return_pct']:>+10.1f}%{flag:>7}")

    print("-" * 78)
    print(f"Gate: open / shut / part (hour is open on some weekdays only).")
    print(f"'thin' marks fewer than {MIN_TRADES_FOR_SIGNAL} trades - too few to read.")


def print_day_table(trades):
    """Day-of-week table - tests the 'weekends are better' claim."""
    header("PERFORMANCE BY DAY OF WEEK")

    print(f"{'Day':<12}{'Trades':>8}{'Wins':>6}{'Win %':>8}"
          f"{'Net SOL':>11}{'Avg SOL':>10}{'Return %':>11}{'':>7}")
    print("-" * 78)

    for weekday in range(7):
        group = trades[trades["weekday"] == weekday]
        stats = summarise(group)
        name = DAY_NAMES[weekday]

        if stats is None:
            print(f"{name:<12}{'-':>8}{'-':>6}{'-':>8}{'-':>11}{'-':>10}{'-':>11}")
            continue

        flag = "" if stats["trades"] >= MIN_TRADES_FOR_SIGNAL else "  thin"
        print(f"{name:<12}{stats['trades']:>8}{stats['wins']:>6}"
              f"{stats['win_rate']:>7.0f}%{stats['net_sol']:>+11.3f}"
              f"{stats['avg_sol']:>+10.3f}{stats['return_pct']:>+10.1f}%{flag:>7}")

    print("-" * 78)


def print_split(trades, mask, label_in, label_out, title, note=None):
    """Run and report one two-group comparison."""
    header(title)
    if note:
        print(note + "\n")

    inside = trades[mask]
    outside = trades[~mask]

    for name, group in ((label_in, inside), (label_out, outside)):
        stats = summarise(group)
        if stats is None:
            print(f"{name:<22} no trades")
            continue
        print(f"{name:<22} {stats['trades']:>3} trades, "
              f"{stats['win_rate']:>3.0f}% win rate, "
              f"{stats['net_sol']:>+7.3f} SOL, "
              f"{stats['avg_sol']:>+7.4f} SOL/trade")

    gap, p_value, size = permutation_test(trades["net_sol"], mask)
    if gap is None:
        print("\nCannot test - one side is empty.")
        return

    print(f"\nGap: {gap:+.4f} SOL per trade in favour of '{label_in}'")
    print(f"p-value: {p_value:.4f} - {verdict(p_value)}")


def print_monday_extension(trades):
    """Did the Monday 06:00-09:00 extension earn its place?"""
    header("MONDAY EXTENSION CHECK (06:00-09:00)")
    print("This window was added by decision, against the only evidence")
    print("available at the time. This section is the running scorecard.\n")

    mask = ((trades["weekday"] == 0) &
            (trades["entry_hour"] >= 6) &
            (trades["entry_hour"] < 9))

    group = trades[mask]
    stats = summarise(group)

    if stats is None or stats["trades"] == 0:
        print("No trades yet in Monday 06:00-09:00. Nothing to judge.")
        return

    print(f"Trades: {stats['trades']}   Wins: {stats['wins']} "
          f"({stats['win_rate']:.0f}%)")
    print(f"Net: {stats['net_sol']:+.3f} SOL "
          f"({stats['avg_sol']:+.4f} per trade)")

    if stats["trades"] < MIN_TRADES_FOR_SIGNAL:
        print(f"\nFewer than {MIN_TRADES_FOR_SIGNAL} trades - too early to judge.")


def main():
    trades = load_closed_trades()

    if trades.empty:
        print("No closed trades found. Nothing to analyse.")
        return

    trades = prepare(trades)

    print_coverage(trades)
    print_hourly_table(trades)
    print_day_table(trades)

    print_split(
        trades,
        trades["is_weekend"],
        "Weekend (Sat/Sun)",
        "Weekday (Mon-Fri)",
        "WEEKEND vs WEEKDAY",
        note="Tests the claim that weekends perform better.",
    )

    print_split(
        trades,
        trades["in_window"],
        "Inside window",
        "Outside window",
        "INSIDE vs OUTSIDE THE TRADING WINDOW",
        note=("Only meaningful for trades taken BEFORE the entry gate went\n"
              "live. Once gated, out-of-window calls are never bought, so\n"
              "this comparison stops accumulating new evidence."),
    )

    print_monday_extension(trades)

    header("ANALYSIS COMPLETE")


if __name__ == "__main__":
    main()