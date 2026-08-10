# src/trading_window.py
"""
Entry time gate.

Decides whether a call arriving NOW is inside the permitted trading window.
Gates ENTRIES ONLY - the bot must keep running 24/7 so open positions stay
monitored for stop-loss and trailing-stop exits. Shutting the process down
outside the window would recreate the 81-94% overshoot losses seen on
Ratatouille, BEAR and RODRI.

Window (Europe/London local time):
    Saturday, Sunday  - open all day
    Monday            - open 00:00-09:00 and 18:00-24:00
    Tuesday-Friday    - open 00:00-06:00 and 18:00-24:00

Which produces one continuous block from Friday 18:00 to Monday 09:00.

Timezone note: all comparisons use Europe/London via zoneinfo, so the window
follows the UK clock through the GMT/BST changeover automatically. The US
equivalent of each boundary shifts by an hour during the two weeks per year
when the UK and US clock changes are out of step. That is expected, not a bug.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Configuration - change the window here, nowhere else
# ---------------------------------------------------------------------------

LOCAL_TZ = ZoneInfo("Europe/London")

# Evening open time, same on every day that has a closed period.
WINDOW_OPEN_HOUR = 18

# Morning close time. Monday runs later to cover past midnight US Pacific.
DEFAULT_CLOSE_HOUR = 6
MONDAY_CLOSE_HOUR = 9

# Python's weekday(): Monday=0, Tuesday=1 ... Saturday=5, Sunday=6.
MONDAY = 0
FULL_DAY_WEEKDAYS = {5, 6}  # Saturday and Sunday - no closed period at all

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday",
             "Friday", "Saturday", "Sunday"]


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _to_local(moment):
    """Convert an aware datetime to London local time.

    Rejects naive datetimes (ones with no timezone attached) on purpose. A
    naive timestamp silently assumed to be local is a classic source of
    off-by-one-hour bugs, and here that would mean trading an hour that
    should be closed.
    """
    if moment.tzinfo is None:
        raise ValueError(
            "trading_window received a naive datetime (no timezone). "
            "Pass an aware datetime, e.g. datetime.now(timezone.utc)."
        )
    return moment.astimezone(LOCAL_TZ)


def close_hour_for(weekday):
    """Return the morning close hour for a given weekday number."""
    return MONDAY_CLOSE_HOUR if weekday == MONDAY else DEFAULT_CLOSE_HOUR


def window_status(moment=None):
    """Return (is_open, reason) for the given moment.

    The reason string is written for the log file, so a rejected call carries
    an explanation you can read back later.

    Excel analogy: this is one IF formula with a few nested conditions, but
    it also returns the text of which branch it took.
    """
    if moment is None:
        moment = datetime.now(timezone.utc)

    local = _to_local(moment)
    weekday = local.weekday()
    hour = local.hour
    day_name = DAY_NAMES[weekday]
    stamp = local.strftime("%H:%M %Z")

    # Weekends have no closed period at all.
    if weekday in FULL_DAY_WEEKDAYS:
        return True, f"{day_name} {stamp} - weekend, window open all day"

    close_hour = close_hour_for(weekday)

    if hour >= WINDOW_OPEN_HOUR:
        return True, f"{day_name} {stamp} - inside evening window (from {WINDOW_OPEN_HOUR:02d}:00)"

    if hour < close_hour:
        return True, f"{day_name} {stamp} - inside overnight window (until {close_hour:02d}:00)"

    return False, (f"{day_name} {stamp} - outside window "
                   f"(closed {close_hour:02d}:00-{WINDOW_OPEN_HOUR:02d}:00)")


def is_trading_window(moment=None):
    """True if entries are permitted at this moment. Convenience wrapper."""
    is_open, _reason = window_status(moment)
    return is_open


# ---------------------------------------------------------------------------
# Self-test - runs only when you execute this file directly
# ---------------------------------------------------------------------------

def _print_week_grid():
    """Print a 7 x 24 on/off grid so the window can be checked by eye."""
    # Any known Monday works - this is a fixed reference week, not live data.
    base = datetime(2026, 8, 10, 0, 0, tzinfo=LOCAL_TZ)  # Monday 10 Aug 2026

    print("\n" + "=" * 78)
    print("TRADING WINDOW - FULL WEEK (Europe/London)")
    print("=" * 78)
    print("Hour:      " + "".join(f"{h:>3}" for h in range(24)))
    print("-" * 78)

    for offset in range(7):
        day = base.replace(day=base.day + offset)
        marks = []
        for hour in range(24):
            moment = day.replace(hour=hour)
            marks.append("  #" if is_trading_window(moment) else "  .")
        print(f"{DAY_NAMES[day.weekday()]:<11}" + "".join(marks))

    print("-" * 78)
    print("# = entries open    . = entries closed")


def _run_boundary_checks():
    """Assert the exact edges of the window behave as specified."""
    def moment(day, hour, minute=0):
        return datetime(2026, 8, day, hour, minute, tzinfo=LOCAL_TZ)

    # 10 Aug 2026 is a Monday, so: 10=Mon 11=Tue 14=Fri 15=Sat 16=Sun 17=Mon
    cases = [
        # (datetime, expected_open, description)
        (moment(10, 8, 59), True,  "Monday 08:59 - last minute of extended close"),
        (moment(10, 9, 0),  False, "Monday 09:00 - window shuts"),
        (moment(10, 17, 59), False, "Monday 17:59 - still shut"),
        (moment(10, 18, 0), True,  "Monday 18:00 - evening open"),
        (moment(11, 5, 59), True,  "Tuesday 05:59 - last minute of standard close"),
        (moment(11, 6, 0),  False, "Tuesday 06:00 - window shuts (not 09:00)"),
        (moment(14, 5, 59), True,  "Friday 05:59 - open"),
        (moment(14, 6, 0),  False, "Friday 06:00 - last closed period of the week"),
        (moment(14, 18, 0), True,  "Friday 18:00 - weekend block starts"),
        (moment(15, 12, 0), True,  "Saturday midday - open"),
        (moment(16, 12, 0), True,  "Sunday midday - open"),
        (moment(17, 8, 59), True,  "Monday 08:59 - weekend block ends"),
        (moment(17, 9, 0),  False, "Monday 09:00 - weekend block over"),
    ]

    print("\n" + "=" * 78)
    print("BOUNDARY CHECKS")
    print("=" * 78)

    failures = 0
    for when, expected, description in cases:
        actual = is_trading_window(when)
        ok = (actual == expected)
        if not ok:
            failures += 1
        symbol = "PASS" if ok else "FAIL"
        state = "OPEN" if actual else "SHUT"
        print(f"  [{symbol}] {description:<52} -> {state}")

    return failures


def _check_continuous_block():
    """Confirm Friday 18:00 to Monday 09:00 has no gaps."""
    start = datetime(2026, 8, 14, 18, 0, tzinfo=LOCAL_TZ)  # Friday
    gaps = []

    # 63 hours from Friday 18:00 to Monday 09:00.
    for step in range(63):
        moment = start.replace(day=start.day) + __import__("datetime").timedelta(hours=step)
        if not is_trading_window(moment):
            gaps.append(moment.strftime("%a %H:%M"))

    print("\n" + "=" * 78)
    print("CONTINUOUS WEEKEND BLOCK - Friday 18:00 to Monday 09:00")
    print("=" * 78)
    if gaps:
        print(f"  [FAIL] {len(gaps)} closed hours found inside the block: {gaps[:5]}")
        return 1
    print("  [PASS] 63 consecutive hours, no gaps")
    return 0


if __name__ == "__main__":
    _print_week_grid()

    failures = _run_boundary_checks()
    failures += _check_continuous_block()

    print("\n" + "=" * 78)
    if failures == 0:
        print("TRADING WINDOW SELF-TEST PASSED")
    else:
        print(f"TRADING WINDOW SELF-TEST FAILED - {failures} problem(s) above")
    print("=" * 78)

    # Show the live status too, so you can see what the bot would do right now.
    is_open, reason = window_status()
    print(f"\nRight now: {'OPEN' if is_open else 'SHUT'} - {reason}")