# src/data_loader.py
"""
Shared trade-history loader for analysis scripts.

Reads logs/positions.json, keeps only CLOSED positions, and returns one
tidy row per closed trade. Both time_of_day_analysis.py and pcr_analysis.py
import from here so they can never drift apart.

Excel analogy: this file is the hidden "helper table" tab that every pivot
in the workbook points at. Fix a calculation here, every report updates.

UPDATED 15 Aug 2026: the Jupiter detail fields (top_holders_pct,
organic_score, dev_migrations, dev_mints, liquidity, launchpad,
live_holder_count) are now surfaced as columns.

These will be EMPTY for every trade taken before 15 Aug 2026, because they
were not being recorded then. That is not a bug and must not be patched over
with a default value - a zero would be read by the analysis as a real
measurement of zero. They stay blank, the analysis counts how many rows
actually have a value, and it refuses to correlate a column that does not yet
have enough of them. See MIN_SAMPLE_FOR_INPUT in pcr_analysis.py.
"""

import json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# __file__ is this file's own location. .parent goes up one folder.
# src/data_loader.py -> src/ -> project root. This means the script works
# no matter which folder your terminal happens to be sitting in.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
POSITIONS_PATH = PROJECT_ROOT / "logs" / "positions.json"

# Europe/London, not a fixed +1. This handles the GMT/BST switch on its own,
# so October's clock change won't silently shift every hour bucket by one.
LOCAL_TZ = ZoneInfo("Europe/London")

# Floating-point noise absorber. A trade landing within +/- 0.001 SOL of flat
# is called breakeven rather than a 0.0000001 SOL "win".
BREAKEVEN_TOLERANCE_SOL = 0.001

# The Jupiter detail columns, imported from market_data so there is exactly
# one definition of the list. If market_data cannot be imported (it needs
# aiohttp, which an analysis-only environment may not have), fall back to a
# static copy rather than failing - analysis must never depend on the bot's
# network libraries being installed.
try:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from market_data import DETAIL_COLUMNS
except ImportError:
    DETAIL_COLUMNS = (
        "top_holders_pct", "organic_score", "dev_migrations",
        "dev_mints", "liquidity", "launchpad", "live_holder_count",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_divide(numerator, denominator):
    """Return None instead of crashing on a divide-by-zero or missing value.

    Excel analogy: this is IFERROR(a/b, "") wrapped in a function.
    """
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def _to_local_datetime(iso_string):
    """Convert a stored UTC timestamp string into London local time."""
    if not iso_string:
        return None
    # fromisoformat reads the "2026-08-07T17:12:07.388095+00:00" format
    # that data_logger.py writes, including the +00:00 UTC marker.
    parsed = datetime.fromisoformat(iso_string)
    return parsed.astimezone(LOCAL_TZ)


def _classify_outcome(net_sol):
    """Label a trade win / loss / breakeven."""
    if net_sol > BREAKEVEN_TOLERANCE_SOL:
        return "win"
    if net_sol < -BREAKEVEN_TOLERANCE_SOL:
        return "loss"
    return "breakeven"


def coverage(trades, columns=DETAIL_COLUMNS):
    """How many rows actually carry a value for each of the given columns.

    Returns {column: count_of_non_empty}. Used by the analysis scripts to
    decide whether a column has accumulated enough data to be worth testing,
    and by the self-test below to show progress as nights are added.

    Excel analogy: COUNTA() down each column, ignoring blanks.
    """
    result = {}
    for column in columns:
        if column not in trades.columns:
            result[column] = 0
        else:
            result[column] = int(trades[column].notna().sum())
    return result


# ---------------------------------------------------------------------------
# Main loader
# ---------------------------------------------------------------------------

def load_closed_trades(positions_path=POSITIONS_PATH):
    """Read positions.json and return a DataFrame of closed trades.

    A DataFrame is a worksheet held in memory: named columns, one row per
    record, and you can filter, sort and group it like a pivot table.

    Returns an EMPTY DataFrame (not an error) if there are no closed trades,
    so the analysis scripts can print a clean message instead of crashing.
    """
    positions_path = Path(positions_path)

    if not positions_path.exists():
        raise FileNotFoundError(
            f"Could not find {positions_path}. "
            "Run this from the project folder, or check the file has not moved."
        )

    with open(positions_path, "r", encoding="utf-8") as handle:
        positions = json.load(handle)

    rows = []

    # positions.json is keyed by contract address, so .values() walks the
    # position records themselves. Dictionary analogy: the contract address
    # is the VLOOKUP key, the position record is the row it returns.
    for position in positions.values():
        if not position.get("closed"):
            continue  # still open - no outcome to measure yet

        sol_invested = position.get("sol_invested")
        realised_sol = position.get("realised_sol")

        # A closed position with no money figures is unusable for P&L work.
        if sol_invested is None or realised_sol is None or sol_invested == 0:
            continue

        net_sol = realised_sol - sol_invested

        opened_local = _to_local_datetime(position.get("opened_at"))
        if opened_local is None:
            continue  # no entry time means no hour bucket

        call_mc = position.get("call_mc")
        entry_mc = position.get("entry_mc")
        holders = position.get("holders")
        age_minutes = position.get("age_minutes")

        # How far live price had already moved away from the call figure at
        # entry. Negative means we bought into a drop.
        entry_gap_ratio = _safe_divide(
            (entry_mc - call_mc) if (entry_mc is not None and call_mc is not None) else None,
            call_mc,
        )

        # Holder velocity - the same "holders per minute" idea the PCR uses,
        # rebuilt here so it can be tested against outcome directly.
        holders_per_minute = _safe_divide(holders, age_minutes)

        row = {
            # --- identity ---
            "ticker": position.get("ticker"),
            "contract_address": position.get("contract_address"),
            # --- timing ---
            "opened_at_local": opened_local,
            "entry_hour": opened_local.hour,          # 0-23, London time
            "entry_date": opened_local.date(),
            # --- outcome ---
            "sol_invested": sol_invested,
            "realised_sol": realised_sol,
            "net_sol": net_sol,
            "return_pct": (net_sol / sol_invested) * 100,
            "outcome": _classify_outcome(net_sol),
            "is_win": net_sol > BREAKEVEN_TOLERANCE_SOL,
            # --- entry inputs, for the PCR analysis ---
            "pcr": position.get("pcr"),
            "gt_score": position.get("gt_score"),
            "holders": holders,
            "age_minutes": age_minutes,
            "bundled_pct": position.get("bundled_pct"),
            "call_mc": call_mc,
            "entry_mc": entry_mc,
            # --- derived inputs ---
            "entry_gap_pct": (entry_gap_ratio * 100) if entry_gap_ratio is not None else None,
            "holders_per_minute": holders_per_minute,
            # --- context ---
            "initials_taken": position.get("initials_taken"),
            "fill_count": len(position.get("fills") or []),
        }

        # Jupiter detail fields, added 15 Aug 2026. .get() returns None for
        # every trade taken before that date, which is exactly right - the
        # value was never measured, so it must stay blank rather than being
        # defaulted to a number the analysis would read as real.
        for column in DETAIL_COLUMNS:
            row[column] = position.get(column)

        rows.append(row)

    frame = pd.DataFrame(rows)

    if not frame.empty:
        frame = frame.sort_values("opened_at_local").reset_index(drop=True)

    return frame


# ---------------------------------------------------------------------------
# Self-test - runs only when you execute this file directly
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    trades = load_closed_trades()

    if trades.empty:
        print("No closed trades found in logs/positions.json.")
    else:
        print(f"Loaded {len(trades)} closed trades.")
        print(f"Date range: {trades['entry_date'].min()} to {trades['entry_date'].max()}")
        print(f"Wins: {int(trades['is_win'].sum())}  "
              f"Losses: {int((trades['outcome'] == 'loss').sum())}  "
              f"Breakeven: {int((trades['outcome'] == 'breakeven').sum())}")
        print(f"Total invested: {trades['sol_invested'].sum():.3f} SOL")
        print(f"Net P&L: {trades['net_sol'].sum():+.3f} SOL")

        # Jupiter detail field progress. These start at zero and climb as
        # nights accumulate; this is the number to watch before expecting the
        # exploratory section of pcr_analysis.py to say anything.
        print("\nJupiter detail fields (recorded from 15 Aug 2026 onward):")
        counts = coverage(trades)
        for column, count in counts.items():
            share = count / len(trades) * 100
            print(f"  {column:<20} {count:>4} of {len(trades)} rows "
                  f"({share:>5.1f}%)")

        if max(counts.values()) == 0:
            print("\n  None populated yet - expected until the bot next runs")
            print("  with the updated market_data.py and runner.py.")

        # Flag any column with gaps - matters because a column that is mostly
        # empty cannot be correlated against anything in the PCR script.
        missing = trades.isna().sum()
        gaps = missing[missing > 0]
        if len(gaps) > 0:
            print("\nColumns with missing values:")
            for column, count in gaps.items():
                note = "  (expected - new field)" if column in DETAIL_COLUMNS else ""
                print(f"  {column}: {count} of {len(trades)} rows empty{note}")
        else:
            print("\nNo missing values in any column.")

        print("\nLOADER SELF-TEST PASSED")
