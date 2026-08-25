"""
tests/run_all.py

Runs every check in the project in one command, so there is a single thing to
type before committing.

    python tests/run_all.py

Success marker: "ALL CHECKS PASSED" and a zero exit code. Anything else means
do not commit yet.

WHY THIS EXISTS
---------------
Twice on this project (9 Aug and 10 Aug 2026) work was committed that had
never reached disk, and the bot then ran a full overnight session on rules
nobody had verified. One command that exercises every file makes that state
loud rather than silent.

This does NOT replace `git show HEAD:src/runner.py`. This proves the code on
disk works; git show proves the code in the commit is the code on disk. Both,
every time.

ISOLATION NOTE (added 25 Aug 2026, recovery merge)
----------------------------------------------------
src/data_logger.py's own self-test (`python src/data_logger.py`) writes real
records to data/*.jsonl relative to whatever directory it is run from. Unlike
every test file in this folder, it has no scratch-folder isolation of its
own - it was written as a standalone diagnostic, not as part of this suite.
Running it the same way as the checks below, from PROJECT_ROOT, would append
test junk to the real trading history on every single run of this file.
It is therefore run separately, in run_isolated_data_logger_check() below,
inside a throwaway temp directory instead of PROJECT_ROOT. Do not move it
back into the CHECKS list below without adding the same isolation.
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# (label, command, marker_that_must_appear_in_output_or_None)
CHECKS = [
    ("compile - every source file",
     [sys.executable, "-m", "py_compile"] +
     [str(p) for p in sorted((PROJECT_ROOT / "src").glob("*.py"))],
     None),

    ("self-test - parser",
     [sys.executable, "src/parser.py"], None),
    ("self-test - entry_logic",
     [sys.executable, "src/entry_logic.py"], None),
    ("self-test - exit_logic",
     [sys.executable, "src/exit_logic.py"], None),
    ("self-test - trading_window",
     [sys.executable, "src/trading_window.py"],
     "TRADING WINDOW SELF-TEST PASSED"),
    ("self-test - market_data",
     [sys.executable, "src/market_data.py"],
     "MARKET_DATA SELF-TEST PASSED"),
    # self-test - data_logger is NOT here - see the ISOLATION NOTE above and
    # run_isolated_data_logger_check() below.

    ("build analysis fixtures",
     [sys.executable, "tests/build_fixtures.py"], None),

    ("integration - reject paths (entry guards)",
     [sys.executable, "tests/test_reject_paths.py"],
     "REJECT PATH TESTS PASSED"),
    ("integration - stage 2 field plumbing",
     [sys.executable, "tests/test_jupiter_fields.py"],
     "STAGE 2 INTEGRATION TEST PASSED"),
    ("integration - analysis chain",
     [sys.executable, "tests/test_analysis_chain.py"],
     "ANALYSIS CHAIN TEST PASSED"),
    ("integration - end to end",
     [sys.executable, "tests/test_end_to_end.py"],
     "END-TO-END TEST PASSED"),
]


def _evaluate(result, marker):
    """Shared pass/fail logic: bad exit code, or the expected marker missing."""
    if result.returncode != 0:
        return f"exit code {result.returncode}"
    if marker and marker not in result.stdout:
        return f"expected marker not found: {marker!r}"
    return None


def run_isolated_data_logger_check():
    """
    Runs src/data_logger.py's self-test in a throwaway temp directory.

    data_logger.py writes to Path("data") relative to whatever directory the
    process is running in - it has no isolation of its own, unlike every
    other check in this file. Running it from PROJECT_ROOT like the checks
    above would append test records to the REAL data/*.jsonl files, which is
    exactly the kind of silent corruption of live trade history this whole
    suite exists to catch, not cause.

    Passing the script by ABSOLUTE path and setting cwd to a scratch folder
    fixes this: Python still puts the script's own folder (src/) on
    sys.path[0], so data_logger's "from market_data import DETAIL_COLUMNS"
    resolves exactly as it does when run from PROJECT_ROOT, but
    Path("data") now resolves inside the scratch folder instead of the real
    project's data/ directory. The scratch folder is deleted afterwards
    either way.
    """
    script = PROJECT_ROOT / "src" / "data_logger.py"
    scratch = Path(tempfile.mkdtemp(prefix="data_logger_selftest_"))
    try:
        result = subprocess.run([sys.executable, str(script)], cwd=scratch,
                                capture_output=True, text=True, timeout=900)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return result


def main():
    print("=" * 78)
    print("RUNNING ALL CHECKS")
    print("=" * 78)

    failures = []

    for label, command, marker in CHECKS:
        print(f"\n{label}")
        result = subprocess.run(command, cwd=PROJECT_ROOT,
                                capture_output=True, text=True, timeout=900)

        problem = _evaluate(result, marker)
        if problem:
            print(f"   FAILED - {problem}")
            tail = (result.stdout + result.stderr).strip().splitlines()[-15:]
            for line in tail:
                print(f"      | {line}")
            failures.append(label)
        else:
            print("   OK")

    label = "self-test - data_logger (isolated - see module docstring)"
    print(f"\n{label}")
    result = run_isolated_data_logger_check()
    problem = _evaluate(result, "DATA_LOGGER SELF-TEST PASSED")
    if problem:
        print(f"   FAILED - {problem}")
        tail = (result.stdout + result.stderr).strip().splitlines()[-15:]
        for line in tail:
            print(f"      | {line}")
        failures.append(label)
    else:
        print("   OK")

    print("\n" + "=" * 78)
    if not failures:
        print("ALL CHECKS PASSED")
        print("\nNext: commit, then verify with")
        print("  git show HEAD:src/runner.py | Select-String MAX_ENTRY_GAP_PCT")
    else:
        print(f"{len(failures)} CHECK(S) FAILED - do not commit:")
        for label in failures:
            print(f"  - {label}")
    print("=" * 78)
    return len(failures)


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)
