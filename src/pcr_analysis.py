# src/pcr_analysis.py
"""
Does any entry input actually predict the outcome of a trade?

The PCR accepts roughly 96% of calls, and the highest-conviction call of the
10 Aug overnight run (MEME, PCR 0.621) lost 68%. If conviction does not track
outcome then the PCR is sizing positions on noise, and the fix is not a better
weighting - it is a different set of inputs.

WHAT THIS DOES NOT DO: fit a regression. With a few dozen trades and nine
candidate inputs, a regression would produce impressive-looking coefficients
that are almost entirely noise. This measures each input against outcome one
at a time, reports how strong the relationship is, and is explicit about how
much of what it finds could be luck.

    python src/pcr_analysis.py
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from data_loader import load_closed_trades

# Inputs tested against outcome. Each is (column, label, what it means).
CANDIDATE_INPUTS = [
    ("pcr", "PCR score", "the final conviction coefficient"),
    ("gt_score", "GTscore", "GemTools' own rating"),
    ("holders", "Holders", "holder count at call time"),
    ("age_minutes", "Age (mins)", "how long the token had existed"),
    ("holders_per_minute", "Holder velocity", "holders divided by age"),
    ("bundled_pct", "Bundled %", "share of supply held by bundlers"),
    ("call_mc", "Call market cap", "market cap quoted in the message"),
    ("entry_mc", "Entry market cap", "live market cap actually paid"),
    ("entry_gap_pct", "Entry gap %", "how far live price had moved from the call"),
]

# The outcome measure. return_pct rather than net_sol, because net_sol is
# partly a function of lot size - and lot size is set by the PCR. Correlating
# the PCR against net_sol would partly be correlating the PCR with itself.
OUTCOME_COLUMN = "return_pct"

# Below this many trades, nothing here should be acted on at all.
MIN_TRADES_TO_INTERPRET = 60

# Reshuffles used to work out how often chance reproduces a correlation.
PERMUTATION_RUNS = 10000

# Number of buckets each input is split into for the win-rate table.
BUCKET_COUNT = 3
BUCKET_LABELS = ["low", "mid", "high"]


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def spearman(x, y):
    """Rank correlation between two columns, from -1 to +1.

    Rank-based rather than raw-value based (Spearman rather than Pearson)
    because memecoin returns are wildly skewed - one +240% winner would
    dominate a raw correlation and drown out the other 39 trades. Ranking
    first means a huge winner counts as "the best one", not as forty times
    more important than the second best.

    Excel analogy: RANK() both columns, then CORREL() the ranks.

    Returns None if either column is constant, since a column that never
    varies cannot explain anything that does.
    """
    x = pd.Series(x).rank()
    y = pd.Series(y).rank()
    if x.nunique() <= 1 or y.nunique() <= 1:
        return None
    return float(np.corrcoef(x, y)[0, 1])


def permutation_p_value(x, y, observed, runs=PERMUTATION_RUNS, seed=42):
    """How often does a random reshuffle produce a correlation this strong?

    Keeps both columns exactly as they are but scrambles which outcome is
    paired with which input value, thousands of times. If shuffled data
    routinely matches the real correlation, the real one means nothing.

    Two-sided: a strong negative relationship is as interesting as a strong
    positive one, so the comparison is on absolute size.
    """
    if observed is None:
        return None

    x_ranks = pd.Series(x).rank().to_numpy()
    y_ranks = pd.Series(y).rank().to_numpy()

    rng = np.random.default_rng(seed)  # fixed seed = repeatable output
    hits = 0
    for _ in range(runs):
        shuffled = rng.permutation(y_ranks)
        r = np.corrcoef(x_ranks, shuffled)[0, 1]
        if abs(r) >= abs(observed):
            hits += 1
    return hits / runs


def strength_label(r):
    """Plain-language reading of a correlation's size."""
    if r is None:
        return "not measurable"
    size = abs(r)
    direction = "higher is better" if r > 0 else "lower is better"
    if size < 0.15:
        return "essentially none"
    if size < 0.30:
        return f"weak ({direction})"
    if size < 0.50:
        return f"moderate ({direction})"
    return f"strong ({direction})"


# ---------------------------------------------------------------------------
# Report sections
# ---------------------------------------------------------------------------

def header(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def print_preamble(trades):
    """Sample size and the multiple-comparisons warning, before any numbers."""
    header("HOW TO READ THIS")

    count = len(trades)
    tested = len(CANDIDATE_INPUTS)

    print(f"Trades analysed: {count}")
    print(f"Inputs tested:   {tested}")
    print(f"Dates covered:   {trades['entry_date'].nunique()}")

    # With this many independent tests, at least one 'significant' result is
    # expected by chance alone. The adjusted threshold below accounts for it.
    naive = 0.05
    adjusted = naive / tested
    expected_false = tested * naive

    print(f"\nTesting {tested} inputs at the usual p<0.05 threshold means")
    print(f"roughly {expected_false:.1f} of them will look significant purely by")
    print(f"chance. The adjusted threshold for this many tests is p<{adjusted:.4f}.")
    print("Both are shown per input below. Treat the adjusted one as the bar.")

    if count < MIN_TRADES_TO_INTERPRET:
        print(f"\nCAUTION: {count} trades is below the {MIN_TRADES_TO_INTERPRET} "
              "needed for these")
        print("correlations to be worth acting on. Everything below is a record")
        print("to build on, not a result. Re-run as nights accumulate.")

    biggest_date = trades.groupby("entry_date").size().max()
    if biggest_date / count > 0.6:
        share = biggest_date / count * 100
        print(f"\nCAUTION: {share:.0f}% of trades come from a single date. These")
        print("inputs are being measured against one session's market conditions.")


def print_correlation_table(trades):
    """The headline: each input against outcome, ranked by strength."""
    header(f"ENTRY INPUT vs OUTCOME ({OUTCOME_COLUMN})")

    outcome = trades[OUTCOME_COLUMN]
    results = []

    for column, label, meaning in CANDIDATE_INPUTS:
        if column not in trades.columns:
            continue

        usable = trades[[column, OUTCOME_COLUMN]].dropna()
        if len(usable) < 5:
            results.append((label, meaning, None, None, len(usable)))
            continue

        r = spearman(usable[column], usable[OUTCOME_COLUMN])
        p = permutation_p_value(usable[column], usable[OUTCOME_COLUMN], r)
        results.append((label, meaning, r, p, len(usable)))

    # Strongest relationship first - that is the one worth arguing about.
    results.sort(key=lambda row: abs(row[2]) if row[2] is not None else -1,
                 reverse=True)

    print(f"{'Input':<18}{'n':>5}{'Corr':>9}{'p-value':>10}  {'Strength':<24}")
    print("-" * 78)

    adjusted = 0.05 / len(CANDIDATE_INPUTS)

    for label, meaning, r, p, n in results:
        if r is None:
            print(f"{label:<18}{n:>5}{'-':>9}{'-':>10}  not enough usable values")
            continue

        # Mark results that clear each bar.
        if p is not None and p < adjusted:
            mark = " **"
        elif p is not None and p < 0.05:
            mark = " *"
        else:
            mark = ""

        print(f"{label:<18}{n:>5}{r:>+9.3f}{p:>10.4f}  "
              f"{strength_label(r) + mark:<24}")

    print("-" * 78)
    print(f"*  p<0.05 unadjusted   ** p<{adjusted:.4f} adjusted for "
          f"{len(CANDIDATE_INPUTS)} tests")
    print("Corr runs -1 to +1. Positive means a higher input value went with a")
    print("better return. Anything under 0.15 either way is noise.")

    return results


def print_bucket_tables(trades):
    """Win rate and average return by input band - the intuitive view.

    A correlation compresses a relationship into one number and hides its
    shape. A relationship can be real but non-monotonic - middling values
    best, extremes bad - and a correlation near zero would miss it entirely.
    """
    header("PERFORMANCE BY INPUT BAND")
    print("Each input split into three equal-sized groups by value.")
    print("A correlation near zero with very different bands means the")
    print("relationship exists but is not a straight line.\n")

    for column, label, meaning in CANDIDATE_INPUTS:
        if column not in trades.columns:
            continue

        usable = trades[[column, OUTCOME_COLUMN, "is_win", "net_sol"]].dropna()
        if len(usable) < BUCKET_COUNT * 3:
            print(f"{label} - too few usable trades to band\n")
            continue

        # qcut splits into equal-COUNT groups rather than equal-width ranges,
        # so a skewed input still produces comparable group sizes.
        try:
            bands = pd.qcut(usable[column], BUCKET_COUNT,
                            labels=BUCKET_LABELS, duplicates="drop")
        except ValueError:
            print(f"{label} - too many repeated values to band\n")
            continue

        usable = usable.assign(band=bands)

        print(f"{label}  ({meaning})")
        print(f"  {'Band':<7}{'Range':>22}{'Trades':>8}{'Win %':>8}"
              f"{'Avg return':>13}{'Net SOL':>10}")

        for band in BUCKET_LABELS:
            group = usable[usable["band"] == band]
            if group.empty:
                continue
            low = group[column].min()
            high = group[column].max()
            span = f"{low:,.2f} to {high:,.2f}"
            print(f"  {band:<7}{span:>22}{len(group):>8}"
                  f"{group['is_win'].mean() * 100:>7.0f}%"
                  f"{group[OUTCOME_COLUMN].mean():>+12.1f}%"
                  f"{group['net_sol'].sum():>+10.3f}")
        print()


def print_pcr_verdict(trades):
    """The specific question: is high conviction better than low conviction?"""
    header("PCR VERDICT - does conviction track outcome?")

    usable = trades[["pcr", OUTCOME_COLUMN, "is_win", "net_sol",
                     "sol_invested"]].dropna()

    if len(usable) < 10:
        print("Not enough trades with a recorded PCR to judge.")
        return

    midpoint = usable["pcr"].median()
    high = usable[usable["pcr"] >= midpoint]
    low = usable[usable["pcr"] < midpoint]

    print(f"Split at the median PCR of {midpoint:.3f}.\n")
    for name, group in (("High conviction", high), ("Low conviction", low)):
        print(f"  {name:<18}{len(group):>3} trades, "
              f"{group['is_win'].mean() * 100:>3.0f}% win rate, "
              f"avg return {group[OUTCOME_COLUMN].mean():>+7.1f}%, "
              f"net {group['net_sol'].sum():>+7.3f} SOL")

    gap = high[OUTCOME_COLUMN].mean() - low[OUTCOME_COLUMN].mean()
    print(f"\n  Difference: {gap:+.1f} percentage points in favour of "
          f"{'high' if gap > 0 else 'low'} conviction")

    print("\nWhat this means for sizing:")
    if abs(gap) < 5:
        print("  High and low conviction perform about the same. If that holds")
        print("  as data accumulates, the PCR is sizing on noise - bigger bets")
        print("  on calls that are not actually better. A flat lot size would")
        print("  do the same job with less variance.")
    elif gap > 0:
        print("  Higher conviction is performing better, which is what the PCR")
        print("  is designed to do. Worth re-checking as data accumulates.")
    else:
        print("  Higher conviction is performing WORSE. If that holds, the PCR")
        print("  is actively harmful - it commits more capital to worse calls.")
        print("  Inverting it would beat using it.")

    if len(usable) < MIN_TRADES_TO_INTERPRET:
        print(f"\n  Sample is {len(usable)} trades. Read this as a direction to")
        print("  watch, not a conclusion.")


def print_next_steps(results):
    """What to do with whatever came out, stated plainly."""
    header("WHAT TO DO WITH THIS")

    adjusted = 0.05 / len(CANDIDATE_INPUTS)
    survivors = [row for row in results
                 if row[3] is not None and row[3] < adjusted]

    if not survivors:
        print("No input cleared the adjusted significance bar.")
        print("\nThat is the expected result at this sample size and is not a")
        print("failure - it means the data cannot yet distinguish a real")
        print("predictor from chance. Do not re-weight the PCR on the basis of")
        print("the strongest number in the table above; that is how an")
        print("overfitted model gets built.")
        print("\nRe-run after each night. If an input is genuinely predictive,")
        print("its correlation will hold steady as n grows. A number that")
        print("swings around between runs was noise.")
    else:
        print("Inputs clearing the adjusted bar:")
        for label, meaning, r, p, n in survivors:
            print(f"  {label}: correlation {r:+.3f}, p={p:.4f} ({meaning})")
        print("\nEven these deserve one more night of confirmation before")
        print("being used to re-weight the PCR - a result that appears once")
        print("and vanishes on more data was luck.")


def main():
    trades = load_closed_trades()

    if trades.empty:
        print("No closed trades found. Nothing to analyse.")
        return

    print_preamble(trades)
    results = print_correlation_table(trades)
    print_bucket_tables(trades)
    print_pcr_verdict(trades)
    print_next_steps(results)

    header("ANALYSIS COMPLETE")


if __name__ == "__main__":
    main()
    