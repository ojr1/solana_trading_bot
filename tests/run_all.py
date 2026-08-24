"""
run_all.py - runs the full test suite and prints ALL CHECKS PASSED on success.

Add new test modules to MODULES below. Each must expose a run() function that
prints PASS/FAIL per case and returns a list of (name, exception) failures.

    python tests\run_all.py
"""

import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import test_stage2_integration

MODULES = [test_stage2_integration]


def main():
    total_failures = []
    for module in MODULES:
        print("=" * 70)
        print(module.__name__)
        print("=" * 70)
        total_failures.extend(module.run())
        print()

    if total_failures:
        print(f"{len(total_failures)} check(s) FAILED:")
        for name, exc in total_failures:
            print(f"  - {name}: {exc}")
        sys.exit(1)

    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
