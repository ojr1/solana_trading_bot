# src/pcr_analysis.py
"""
Does any entry input actually predict the outcome of a trade?

The PCR accepts roughly 96% of calls, and the highest-conviction call of the
10 Aug overnight run (MEME, PCR 0.621) lost 68%. If conviction does not track
outcome then the PCR is sizing positions on noise, and the fix is not a better
weighting - it is a different set of inputs.

WHAT THIS DOES NOT DO: fit a regression. With a few dozen trades and fifteen
candidate inputs, a regression would produce impressive-looking coefficients
that are almost entirely noise. This measures each input against outcome one
at a time, reports how strong the relationship is, and is explicit about how
much of what it finds could be luck.

    python src/pcr_analysis.py

TWO CHANGES MADE 15 Aug 2026
---------------------------

1. THE VERDICT SECTION WAS WRONG AND IS FIXED.

   The 10 Aug run printed "inverting it would beat using it" about the PCR,
   while the correlation table three sections earlier showed the PCR at
   +0.067 - a slightly POSITIVE relationship. Two numbers in the same report
   pointing opposite ways, and the stronger-sounding one was the weaker
   measure.

   The cause: the verdict split trades at the median PCR and compared the
   MEAN return of each half. A mean is dragged around by outliers, and in a
   dataset where single trades range from -90% to +240% a handful of large
   losses landing in the high-conviction half is enough to flip the sign.
   The rank correlation is not fooled that way, which is exactly why it is
   the measure used everywhere else in this file.

   The section now reports the rank correlation FIRST, shows both the median
   and the mean for each half, and says so explicitly when the two disagree.
   The verdict wording is driven by the correlation, not the mean gap. The
   honest reading of the 10 Aug data is "the PCR shows no relationship to
   outcome", which is a different and less actionable finding than "the PCR
   is inverted" - it argues for a flat lot size, not a reversed one.

2. THE TEST FAMILY IS SPLIT IN TWO.

   The Jupiter detail fields added on 15 Aug are exploratory: nobody
   predicted they would matter, they are being tested because they happen to
   be available. The nine original inputs were chosen deliberately before any
   data existed.

   Testing all of them as one family would tighten the multiple-comparison
   threshold from p<0.0056 to p<0.0033, and bundled % - the only input with
   any signal, at p=0.0060 - would miss by twice as much as before. Adding
   fields you have no prior about should not weaken the evidence for a field
   you do. So the two families are corrected separately and reported
   separately, with the exploratory one labelled as the fishing expedition
   it is.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from data_loader import load_closed_trades, coverage

# ---------------------------------------------------------------------------
# The two test families
# ---------------------------------------------------------------------------

# PRE-REGISTERED: chosen deliberately before any data existed, either because
# the PCR uses them or because there was a stated reason to suspect them.
PRIMARY_INPUTS = [
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

# EXPLORATORY: Jupiter fields captured from 15 Aug 2026 because they were
# available, not because anything predicted they would matter. Held to the
# same statistical bar within their own family, but a hit here needs
# replication on fresh nights before it means anything - finding one
# relationship among six you had no prior about is what chance looks like.
EXPLORATORY_INPUTS = [
    ("top_holders_pct", "Top holders %", "supply held by the largest holders"),
    ("organic_score", "Organic score", "Jupiter's own quality score"),
    ("dev_migrations", "Dev migrations", "tokens this dev previously migrated"),
    ("dev_mints", "Dev mints", "tokens this dev previously minted"),
    ("liquidity", "Liquidity $", "pool depth at entry"),
    ("live_holder_count", "Live holders", "Jupiter's holder count at entry"),
]

# Text, not numbers - cannot be rank-correlated, so they get a category table
# instead. Ranking "pump.fun" against "bonk" would be meaningless.
CATEGORICAL_INPUTS = [
    ("launchpad", "Launchpad", "which platform the token launched on"),
]

# The outcome measure. return_pct rather than net_sol, because net_sol is
# partly a function of lot size - and lot size is set by the PCR. Correlating
# the PCR against net_sol would partly be correlating the PCR with itself.
OUTCOME_COLUMN = "return_pct"

# Below this many trades, nothing here should be acted on at all.
MIN_TRADES_TO_INTERPRET = 60

# Below this many NON-EMPTY values, an individual input is not tested at all.
# Added 15 Aug 2026 alongside the Jupiter fields: those columns are blank for
# every trade taken before that date, and a correlation computed on four rows
# would print a large, confident, meaningless number.
MIN_SAMPLE_FOR_INPUT = 20

# Reshuffles used to work out how often chance reproduces a correlation.
PERMUTATION_RUNS = 10000

# Number of buckets each input is split into for the win-rate table.
BUCKET_COUNT = 3
BUCKET_LABELS = ["low", "mid", "high"]

# A rank correlation smaller than this is treated as no relationship at all,
# whatever its sign. Used by the verdict section so a +0.067 is never
# described as "higher conviction is better".
NEGLIGIBLE_CORRELATION = 0.15


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
    if size < NEGLIGIBLE_CORRELATION:
        return "essentially none"
    if size < 0.30:
        return f"weak ({direction})"
    if size < 0.50:
        return f"moderate ({direction})"
    return f"strong ({direction})"


def test_input(trades, column):
    """Run one input against outcome. Returns (r, p, n, skip_reason).

    skip_reason is a string when the input was not tested at all, and None
    when it was. Separating "we tested it and found nothing" from "we could
    not test it" matters: the first is evidence, the second is not.
    """
    if column not in trades.columns:
        return None, None, 0, "column not present in the data"

    usable = trades[[column, OUTCOME_COLUMN]].dropna()
    n = len(usable)

    if n == 0:
        return None, None, 0, "no values recorded yet"
    if n < MIN_SAMPLE_FOR_INPUT:
        return None, None, n, f"only {n} values, need {MIN_SAMPLE_FOR_INPUT}"

    r = spearman(usable[column], usable[OUTCOME_COLUMN])
    if r is None:
        return None, None, n, "every value identical - nothing to correlate"

    p = permutation_p_value(usable[column], usable[OUTCOME_COLUMN], r)
    return r, p, n, None


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

    print(f"Trades analysed: {count}")
    print(f"Dates covered:   {trades['entry_date'].nunique()}")
    print(f"Inputs tested:   {len(PRIMARY_INPUTS)} pre-registered, "
          f"{len(EXPLORATORY_INPUTS)} exploratory")

    print("\nThe two groups are corrected separately. Testing many inputs at")
    print("the usual p<0.05 threshold means some will look significant purely")
    print("by chance, so the threshold is divided by the number of tests in")
    print("its own group:")
    print(f"  pre-registered ({len(PRIMARY_INPUTS)} inputs): "
          f"p<{0.05 / len(PRIMARY_INPUTS):.4f}")
    print(f"  exploratory    ({len(EXPLORATORY_INPUTS)} inputs): "
          f"p<{0.05 / len(EXPLORATORY_INPUTS):.4f}")
    print("\nPooling the two would tighten the bar on the pre-registered")
    print("inputs, penalising them for questions they were not asked. A hit")
    print("in the exploratory group needs replication regardless of p-value.")

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

    # Coverage of the newer fields, so it is obvious why they may be skipped.
    exploratory_columns = [c for c, _l, _m in EXPLORATORY_INPUTS]
    exploratory_columns += [c for c, _l, _m in CATEGORICAL_INPUTS]
    counts = coverage(trades, exploratory_columns)
    best = max(counts.values()) if counts else 0

    if best == 0:
        print("\nNOTE: no trade yet carries the Jupiter detail fields. They")
        print("began recording 15 Aug 2026, so they populate from the next")
        print("session onward. The exploratory section will stay empty until")
        print(f"at least {MIN_SAMPLE_FOR_INPUT} trades have them.")
    elif best < MIN_SAMPLE_FOR_INPUT:
        print(f"\nNOTE: the Jupiter detail fields are populated on at most "
              f"{best} of")
        print(f"{count} trades. Testing starts at {MIN_SAMPLE_FOR_INPUT}.")


def print_correlation_table(trades, inputs, title, note=None):
    """One family's inputs against outcome, ranked by strength."""
    header(title)
    if note:
        print(note + "\n")

    results = []
    for column, label, meaning in inputs:
        r, p, n, skip = test_input(trades, column)
        results.append((label, meaning, r, p, n, skip))

    # Strongest relationship first - that is the one worth arguing about.
    results.sort(key=lambda row: abs(row[2]) if row[2] is not None else -1,
                 reverse=True)

    print(f"{'Input':<18}{'n':>5}{'Corr':>9}{'p-value':>10}  {'Strength':<24}")
    print("-" * 78)

    adjusted = 0.05 / len(inputs)

    for label, meaning, r, p, n, skip in results:
        if skip is not None:
            print(f"{label:<18}{n:>5}{'-':>9}{'-':>10}  not tested: {skip}")
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
          f"{len(inputs)} tests")
    print("Corr runs -1 to +1. Positive means a higher input value went with a")
    print(f"better return. Anything under {NEGLIGIBLE_CORRELATION} either way "
          f"is noise.")

    return results, adjusted


def print_bucket_tables(trades, inputs, title):
    """Win rate and average return by input band - the intuitive view.

    A correlation compresses a relationship into one number and hides its
    shape. A relationship can be real but non-monotonic - middling values
    best, extremes bad - and a correlation near zero would miss it entirely.
    """
    header(title)
    print("Each input split into three equal-sized groups by value.")
    print("A correlation near zero with very different bands means the")
    print("relationship exists but is not a straight line.\n")

    printed = 0

    for column, label, meaning in inputs:
        if column not in trades.columns:
            continue

        usable = trades[[column, OUTCOME_COLUMN, "is_win", "net_sol"]].dropna()
        if len(usable) < MIN_SAMPLE_FOR_INPUT:
            print(f"{label} - {len(usable)} usable trades, need "
                  f"{MIN_SAMPLE_FOR_INPUT}\n")
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
        printed += 1

    if printed == 0:
        print("(nothing with enough data to band yet)\n")


def print_categorical_tables(trades):
    """Text inputs get a category table - they cannot be rank-correlated."""
    header("PERFORMANCE BY CATEGORY")
    print("Text fields cannot be ranked, so they are grouped instead.\n")

    printed = 0

    for column, label, meaning in CATEGORICAL_INPUTS:
        if column not in trades.columns:
            continue

        usable = trades[[column, OUTCOME_COLUMN, "is_win", "net_sol"]].dropna()
        if len(usable) < MIN_SAMPLE_FOR_INPUT:
            print(f"{label} - {len(usable)} usable trades, need "
                  f"{MIN_SAMPLE_FOR_INPUT}\n")
            continue

        print(f"{label}  ({meaning})")
        print(f"  {'Value':<22}{'Trades':>8}{'Win %':>8}"
              f"{'Avg return':>13}{'Net SOL':>10}")

        for value, group in usable.groupby(column):
            flag = "" if len(group) >= 5 else "  thin"
            print(f"  {str(value):<22}{len(group):>8}"
                  f"{group['is_win'].mean() * 100:>7.0f}%"
                  f"{group[OUTCOME_COLUMN].mean():>+12.1f}%"
                  f"{group['net_sol'].sum():>+10.3f}{flag}")
        print()
        printed += 1

    if printed == 0:
        print("(nothing with enough data to group yet)\n")


def print_pcr_verdict(trades):
    """The specific question: is high conviction better than low conviction?

    REWRITTEN 15 Aug 2026. The previous version compared the MEAN return of
    the high- and low-conviction halves and drew its conclusion from that gap
    alone. On the 10 Aug data that produced "inverting it would beat using
    it", while the correlation table in the same report showed +0.067 - the
    two sections contradicted each other and the weaker measure won.

    A mean is outlier-sensitive. With trades ranging from -90% to +240%, two
    or three large losses landing on one side of the split are enough to
    reverse the sign of the gap without any real relationship existing. The
    rank correlation is immune to that, which is why it now leads.
    """
    header("PCR VERDICT - does conviction track outcome?")

    usable = trades[["pcr", OUTCOME_COLUMN, "is_win", "net_sol",
                     "sol_invested"]].dropna()

    if len(usable) < 10:
        print("Not enough trades with a recorded PCR to judge.")
        return

    # --- The robust measure, reported first -------------------------------
    r, p, n, skip = test_input(trades, "pcr")

    print("1. RANK CORRELATION (the robust measure)\n")
    if skip is not None:
        print(f"   Not measurable: {skip}")
    else:
        print(f"   PCR vs {OUTCOME_COLUMN}: {r:+.3f}  (p={p:.4f}, n={n})")
        print(f"   Reading: {strength_label(r)}")

    # --- The median split, reported second, with BOTH averages ------------
    midpoint = usable["pcr"].median()
    high = usable[usable["pcr"] >= midpoint]
    low = usable[usable["pcr"] < midpoint]

    print(f"\n2. MEDIAN SPLIT at PCR {midpoint:.3f}\n")
    print(f"   {'Half':<18}{'Trades':>7}{'Win %':>8}{'Median ret':>13}"
          f"{'Mean ret':>11}{'Net SOL':>10}")
    for name, group in (("High conviction", high), ("Low conviction", low)):
        print(f"   {name:<18}{len(group):>7}"
              f"{group['is_win'].mean() * 100:>7.0f}%"
              f"{group[OUTCOME_COLUMN].median():>+12.1f}%"
              f"{group[OUTCOME_COLUMN].mean():>+10.1f}%"
              f"{group['net_sol'].sum():>+10.3f}")

    median_gap = high[OUTCOME_COLUMN].median() - low[OUTCOME_COLUMN].median()
    mean_gap = high[OUTCOME_COLUMN].mean() - low[OUTCOME_COLUMN].mean()

    print(f"\n   Gap on medians: {median_gap:+.1f} pp     "
          f"Gap on means: {mean_gap:+.1f} pp")
    print("   (pp = percentage points. The median gap is the one to trust -")
    print("    a single -90% trade moves a mean and barely moves a median.)")

    # --- Reconciliation: say it out loud when the measures disagree -------
    if r is not None:
        signs = [np.sign(r), np.sign(median_gap), np.sign(mean_gap)]
        nonzero = [s for s in signs if s != 0]
        if len(set(nonzero)) > 1:
            print("\n   DISAGREEMENT between measures. This is expected when a")
            print("   few extreme trades sit on one side of the split, and it")
            print("   is itself the finding: a relationship that reverses")
            print("   depending on which average you pick is not a")
            print("   relationship. The rank correlation is the tiebreak.")

    # --- The verdict, driven by the correlation ---------------------------
    print("\n3. WHAT THIS MEANS FOR SIZING\n")

    if r is None:
        print("   Cannot judge - see above.")
    elif abs(r) < NEGLIGIBLE_CORRELATION:
        print("   The PCR shows NO RELATIONSHIP to outcome. High and low")
        print("   conviction perform about the same once outliers are")
        print("   discounted. If that holds as data accumulates, the PCR is")
        print("   sizing on noise - committing more capital to calls that are")
        print("   not actually better.")
        print("\n   The implication is a FLAT lot size, not a reversed one.")
        print("   Inverting a coefficient that carries no information just")
        print("   produces noise pointing the other way, with the same")
        print("   variance and no more edge.")
    elif r > 0:
        strong_enough = p is not None and p < 0.05 / len(PRIMARY_INPUTS)
        print("   Higher conviction is performing better, which is what the")
        print("   PCR is designed to do.")
        if not strong_enough:
            print("   Not yet clearing the adjusted significance bar, so treat")
            print("   it as a direction to watch rather than confirmation.")
    else:
        strong_enough = (p is not None
                         and p < 0.05 / len(PRIMARY_INPUTS)
                         and abs(r) >= 0.30)
        print("   Higher conviction is performing WORSE.")
        if strong_enough:
            print("   This clears the adjusted bar and is large enough to act")
            print("   on: the PCR is committing more capital to worse calls.")
            print("   Confirm on one more night, then either rebuild the")
            print("   inputs or drop to a flat lot size.")
        else:
            print("   But the relationship is too weak or too uncertain to")
            print("   call the PCR inverted. 'Worse' at this size is closer to")
            print("   'no relationship' than to a usable reversed signal - a")
            print("   flat lot size remains the safe response, not an")
            print("   inverted PCR.")

    if len(usable) < MIN_TRADES_TO_INTERPRET:
        print(f"\n   Sample is {len(usable)} trades. Read this as a direction")
        print("   to watch, not a conclusion.")


def print_next_steps(primary, primary_bar, exploratory, exploratory_bar):
    """What to do with whatever came out, stated plainly."""
    header("WHAT TO DO WITH THIS")

    def survivors(results, bar):
        return [row for row in results
                if row[3] is not None and row[3] < bar]

    primary_hits = survivors(primary, primary_bar)
    exploratory_hits = survivors(exploratory, exploratory_bar)

    if not primary_hits:
        print("PRE-REGISTERED: no input cleared the adjusted bar.")
        print("\nThat is the expected result at this sample size and is not a")
        print("failure - it means the data cannot yet distinguish a real")
        print("predictor from chance. Do not re-weight the PCR on the basis of")
        print("the strongest number in the table above; that is how an")
        print("overfitted model gets built.")
    else:
        print("PRE-REGISTERED inputs clearing the adjusted bar:")
        for label, meaning, r, p, n, _skip in primary_hits:
            print(f"  {label}: correlation {r:+.3f}, p={p:.4f} ({meaning})")
        print("\nEven these deserve one more night of confirmation before")
        print("being used to re-weight the PCR - a result that appears once")
        print("and vanishes on more data was luck.")

    print()

    tested = [row for row in exploratory if row[5] is None]
    if not tested:
        print("EXPLORATORY: not enough data to test any of these yet.")
        print("They populate from the first session run after 15 Aug 2026.")
    elif not exploratory_hits:
        print("EXPLORATORY: nothing cleared the bar.")
        print("Nothing to do. These were tested because the fields exist, not")
        print("because anything predicted they would matter.")
    else:
        print("EXPLORATORY inputs clearing the adjusted bar:")
        for label, meaning, r, p, n, _skip in exploratory_hits:
            print(f"  {label}: correlation {r:+.3f}, p={p:.4f} ({meaning})")
        print("\nTREAT WITH MORE SUSPICION THAN THE ABOVE. These were not")
        print("predicted in advance, so finding one among several is what")
        print("chance looks like. The test is whether it survives on nights")
        print("that were not used to find it. Do not add it to the PCR on the")
        print("strength of this run.")
        print("\nOne to watch specifically: top holders % measures supply")
        print("concentration, and so does bundled %. If both show signal they")
        print("are probably the same finding twice, not two findings.")

    print("\nRe-run after each night. If an input is genuinely predictive, its")
    print("correlation will hold steady as n grows. A number that swings")
    print("around between runs was noise.")


def main():
    trades = load_closed_trades()

    if trades.empty:
        print("No closed trades found. Nothing to analyse.")
        return

    print_preamble(trades)

    primary, primary_bar = print_correlation_table(
        trades, PRIMARY_INPUTS,
        f"PRE-REGISTERED ENTRY INPUTS vs OUTCOME ({OUTCOME_COLUMN})",
        note="Chosen before any data existed. This is the evidence that counts.",
    )

    exploratory, exploratory_bar = print_correlation_table(
        trades, EXPLORATORY_INPUTS,
        f"EXPLORATORY JUPITER FIELDS vs OUTCOME ({OUTCOME_COLUMN})",
        note=("Captured from 15 Aug 2026 because they were available, not\n"
              "because anything predicted them. A hit here is a hypothesis,\n"
              "not a result, and needs confirming on nights not used to find it."),
    )

    print_bucket_tables(trades, PRIMARY_INPUTS,
                        "PERFORMANCE BY INPUT BAND - PRE-REGISTERED")
    print_bucket_tables(trades, EXPLORATORY_INPUTS,
                        "PERFORMANCE BY INPUT BAND - EXPLORATORY")
    print_categorical_tables(trades)

    print_pcr_verdict(trades)
    print_next_steps(primary, primary_bar, exploratory, exploratory_bar)

    header("ANALYSIS COMPLETE")


if __name__ == "__main__":
    main()
