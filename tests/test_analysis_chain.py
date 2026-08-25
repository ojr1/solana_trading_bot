"""
tests/test_analysis_chain.py

Runs the whole analysis chain against each fixture dataset and asserts on
what the reports actually say.

    data_loader -> time_of_day_analysis
                -> pcr_analysis

Three datasets, three different jobs:

  legacy  no Jupiter detail fields at all, and a PCR column built to
          reproduce the 10 Aug contradiction. Checks the verdict fix and
          checks the exploratory section degrades cleanly instead of
          printing a correlation computed on nothing.

  mixed   some trades with the fields, some without. Checks the
          MIN_SAMPLE_FOR_INPUT guard fires on the right columns.

  full    everything populated, with a strong relationship deliberately
          planted in top_holders_pct. Checks the analysis can still FIND
          something - a report that never finds anything is not cautious,
          it is broken.

Run tests/build_fixtures.py first.

    python tests/test_analysis_chain.py

Success marker: "ANALYSIS CHAIN TEST PASSED" and a zero exit code.
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
FIXTURES = PROJECT_ROOT / "tests" / "fixtures"

FAILURES = []


def check(condition, description):
    if condition:
        print(f"   [PASS] {description}")
    else:
        print(f"   [FAIL] {description}")
        FAILURES.append(description)


def run(script, dataset):
    """Run one analysis script against one fixture, return its stdout.

    data_loader works out where logs/positions.json lives from its OWN file
    location, not from the working directory, so the scripts have to sit
    beside the fixture's logs folder. That is done in a throwaway temp
    directory rather than inside tests/fixtures, so the repo keeps only the
    fixture data itself and never a stale copy of src/.
    """
    workdir = Path(tempfile.mkdtemp(prefix=f"chain_{dataset}_"))
    (workdir / "src").mkdir()
    (workdir / "logs").mkdir()

    shutil.copy(FIXTURES / dataset / "logs" / "positions.json",
                workdir / "logs" / "positions.json")

    # Copy the CURRENT source in, so the test always exercises the live files.
    for name in ("data_loader.py", "pcr_analysis.py", "time_of_day_analysis.py",
                 "trading_window.py", "market_data.py"):
        shutil.copy(SRC / name, workdir / "src" / name)

    result = subprocess.run(
        [sys.executable, f"src/{script}"],
        cwd=workdir, capture_output=True, text=True, timeout=300,
    )
    shutil.rmtree(workdir, ignore_errors=True)
    if result.returncode != 0:
        print(f"   [FAIL] {script} on '{dataset}' exited {result.returncode}")
        print(result.stderr[-1500:])
        FAILURES.append(f"{script} crashed on {dataset}")
    return result.stdout


def test_legacy():
    print("\n1. LEGACY dataset - the 10 Aug contradiction, no detail fields")

    out = run("data_loader.py", "legacy")
    check("LOADER SELF-TEST PASSED" in out, "data_loader runs and self-tests")
    check("None populated yet" in out,
          "loader reports the detail fields as not yet populated")

    out = run("pcr_analysis.py", "legacy")

    # --- the verdict fix -----------------------------------------------
    check("RANK CORRELATION (the robust measure)" in out,
          "verdict leads with the rank correlation")
    check("Gap on medians" in out and "Gap on means" in out,
          "verdict reports BOTH the median and the mean gap")
    check("DISAGREEMENT between measures" in out,
          "verdict flags that the two measures disagree")
    check("NO RELATIONSHIP to outcome" in out,
          "verdict concludes 'no relationship', matching the correlation")
    check("FLAT lot size, not a reversed one" in out,
          "verdict recommends a flat lot size, not an inverted PCR")

    # --- the bug must be gone -------------------------------------------
    check("Inverting it would beat using it" not in out,
          "the old overstated wording is gone")
    check("is actively harmful" not in out,
          "the old 'actively harmful' claim is gone")

    # --- the exploratory family -----------------------------------------
    check("not tested: no values recorded yet" in out,
          "exploratory inputs are skipped, not correlated on nothing")
    check("p<0.0056 adjusted for 9 tests" in out,
          "pre-registered bar stayed at p<0.0056 for 9 tests")
    check("p<0.0083 adjusted for 6 tests" in out,
          "exploratory bar is corrected separately at p<0.0083")

    out = run("time_of_day_analysis.py", "legacy")
    check("ANALYSIS COMPLETE" in out, "time_of_day_analysis runs to completion")
    check("PERFORMANCE BY ENTRY HOUR" in out, "hourly table rendered")


def test_mixed():
    print("\n2. MIXED dataset - some trades carry the fields, some do not")

    out = run("data_loader.py", "mixed")
    check("LOADER SELF-TEST PASSED" in out, "data_loader handles a mixed file")
    check("(expected - new field)" in out,
          "loader labels the new-field gaps as expected rather than as errors")

    out = run("pcr_analysis.py", "mixed")
    check("ANALYSIS COMPLETE" in out, "pcr_analysis runs to completion")
    check("Top holders %" in out, "exploratory inputs appear in the table")
    # 25 of 70 rows carry the fields, which clears MIN_SAMPLE_FOR_INPUT (20).
    check("not tested: only" in out or "     25" in out,
          "partial coverage is either tested or explicitly skipped with a count")
    check("PERFORMANCE BY CATEGORY" in out, "categorical section rendered")


def test_full():
    print("\n3. FULL dataset - planted signal must actually be detected")

    out = run("pcr_analysis.py", "full")
    check("ANALYSIS COMPLETE" in out, "pcr_analysis runs to completion")

    # The planted relationship: lower top-holder concentration goes with
    # better returns, so the correlation must be strongly NEGATIVE.
    line = next((l for l in out.splitlines()
                 if l.startswith("Top holders %")), "")
    check(line != "", "Top holders % row present in the exploratory table")
    if line:
        print(f"          -> {line.strip()}")
        # The table is FIXED-WIDTH, not whitespace-delimited: the label field
        # is 18 chars and labels contain spaces ("Top holders %"), so
        # line.split() would mis-index. Slice by column instead.
        #   label 0-17 | n 18-22 | corr 23-31 | p 32-41
        try:
            corr = float(line[23:32])
        except ValueError:
            corr = 0.0
        check(corr < -0.5,
              f"planted signal detected as a strong negative ({corr:+.3f})")

    check("EXPLORATORY inputs clearing the adjusted bar" in out,
          "next-steps section reports the exploratory hit")
    check("TREAT WITH MORE SUSPICION" in out,
          "exploratory hit carries the replication warning")
    check("probably the same finding twice" in out,
          "collinearity with bundled % is flagged")

    out = run("time_of_day_analysis.py", "full")
    check("ANALYSIS COMPLETE" in out, "time_of_day_analysis runs on full data")


def main():
    print("=" * 78)
    print("ANALYSIS CHAIN TEST - data_loader -> time_of_day -> pcr_analysis")
    print("=" * 78)

    if not (FIXTURES / "legacy" / "logs" / "positions.json").exists():
        raise SystemExit("Fixtures missing. Run: python tests/build_fixtures.py")

    test_legacy()
    test_mixed()
    test_full()

    print("\n" + "=" * 78)
    if not FAILURES:
        print("ANALYSIS CHAIN TEST PASSED")
    else:
        print(f"ANALYSIS CHAIN TEST FAILED - {len(FAILURES)} problem(s):")
        for item in FAILURES:
            print(f"  - {item}")
    print("=" * 78)
    return len(FAILURES)


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)
