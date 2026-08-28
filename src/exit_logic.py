"""
exit_logic.py - Stage 3 of the Solana memecoin signal trading bot.

Decides when to sell an open position, and how much.

Four independent exit mechanisms, which never overlap:

  BEFORE initials are taken
    1. Stop-loss      - market cap falls 55% below average entry -> sell all
    2. Initials       - market cap rises 95% above entry         -> sell half

  AFTER initials are taken
    3. Trailing stop  - market cap falls 70% below its peak      -> sell all
    4. Ladder clips   - each $50K level crossed                  -> sell 15%

This module is pure logic. It performs no trading and makes no network calls,
so it can be tested offline against simulated price paths:

    python src/exit_logic.py
"""

import math

import config

# ==========================================================================
# TUNABLE PARAMETERS
# ==========================================================================

# --- Take profit -----------------------------------------------------------
# Gain at which half the position is sold. Deliberately below a true 2x: a
# coin lingering at +95% is worth banking rather than waiting for a round
# number it may never reach.
INITIALS_TRIGGER_GAIN = 0.95

# INITIALS_SELL_FRACTION lives in config.py / .env (Stage 8) rather than
# being hardcoded here, so it can be re-tuned without a code change -
# consistent with the Stage 3 entry-sizing values (see entry_logic.py).
# Kept as a module-level alias so every reference below is unchanged.
INITIALS_SELL_FRACTION = config.INITIALS_SELL_FRACTION  # fraction of the position sold when initials are taken

# Fraction of the REMAINING position sold at each ladder level. Because this
# compounds against a shrinking balance, the ladder never fully closes a
# position on its own - the trailing stop is what eventually exits it.
LADDER_CLIP_FRACTION = 0.15

# --- Ladder spacing --------------------------------------------------------
# Clip levels are multiples of LADDER_STEP above the initials level, widening
# to LADDER_STEP_LARGE once past LADDER_WIDEN_ABOVE, so that a coin which runs
# a long way is not clipped an impractical number of times.
LADDER_STEP = 50_000
LADDER_WIDEN_ABOVE = 500_000
LADDER_STEP_LARGE = 100_000

# --- Minimum gap between sells ---------------------------------------------
# A ladder clip only fires if the market cap has risen at least this far above
# the price of the PREVIOUS sell - not above the previous ladder level.
#
# Nominal levels are a poor guide to whether two sells are meaningfully apart.
# If initials fill on a gap at $98K, clipping again at $100K sells twice at
# effectively the same price and wastes a rung of the ladder. Measuring from
# the actual fill is what prevents that.
#
# One consequence worth knowing: above roughly $1M the $100K ladder steps are
# themselves less than 10% apart, so this rule quietly takes over and the
# ladder becomes "every 10% move" rather than "every $100K". That is the
# intended behaviour - clipping every 5% at a $2M market cap is over-trading.
MIN_GAP_BETWEEN_SELLS = 0.10

# --- Absolute floor ----------------------------------------------------
# Below this market cap, a position is treated as effectively dead regardless
# of entry price, average cost, or how much DCA has occurred. This exists
# because the ordinary stop-loss is measured against average entry - after a
# multi-stage DCA fill, average entry falls with each buy, which pulls the
# stop-loss trigger down too. A coin DCA'd to a $15K average entry has its
# 55% stop-loss sitting around $6,750, meaning the position could keep
# bleeding well past the point it is realistically dead. This floor overrides
# that calculation entirely: any open position, in any state, is closed
# immediately if market cap falls below it.
#
# Checked BEFORE stop-loss and trailing-stop logic, and applies regardless of
# whether initials have been taken.
ABSOLUTE_FLOOR_MC = 9_000

# --- Loss limits -----------------------------------------------------------
# Applies only before initials are taken, measured against average entry.
STOP_LOSS_DRAWDOWN = 0.55

# Applies only after initials are taken, measured against the highest market
# cap seen since entry. Wider than the stop-loss because the original stake
# has already been recovered.
TRAILING_STOP_DRAWDOWN = 0.60

# --- Spike confirmation ----------------------------------------------------
# A market cap can gap through several levels in a second. Selling on the
# first observation risks exiting mid-move at the worst price of the spike.
#
# On crossing a take-profit threshold the bot waits briefly and re-checks. If
# the market cap is still climbing it keeps waiting, up to a hard cap.
#
# Loss exits are deliberately exempt: on the way down, waiting costs money.
CONFIRM_DELAY_SECONDS = 3.0
MAX_CONFIRM_WAIT_SECONDS = 15.0

# Further rise between checks that counts as "still climbing".
STILL_RISING_THRESHOLD = 0.02

# Fall from the highest market cap seen while waiting that counts as the spike
# having topped out. Once this triggers the sale fires immediately: there is no
# value in waiting out a move that has already turned.
PULLBACK_THRESHOLD = 0.03


# ==========================================================================
# Position state
# ==========================================================================


def new_position(ticker, contract_address, entry_mc, sol_invested, tokens):
    """
    Creates the state record for a newly opened position.

    entry_mc is the AVERAGE entry market cap. Where a position was built over
    several DCA tranches, the caller is responsible for weighting that average
    by the SOL committed at each fill - every threshold in this module is
    measured against it.
    """
    return {
        "ticker": ticker,
        "contract_address": contract_address,
        "entry_mc": entry_mc,
        "sol_invested": sol_invested,
        "original_tokens": tokens,
        "tokens_remaining": tokens,
        "peak_mc": entry_mc,
        "initials_taken": False,
        "last_sell_mc": None,  # market cap at the most recent sell, if any
        "fired_levels": [],
        "closed": False,
        "pending": None,  # in-flight spike confirmation, if any
    }


# ==========================================================================
# Ladder
# ==========================================================================


def ladder_levels(initials_mc, up_to_mc):
    """
    Generates the clip levels that apply above a given initials level.

    Levels are round multiples of the step size rather than offsets from the
    entry price, so that two coins trading at the same market cap clip at the
    same points regardless of where they were called.

    Worked examples:
        entry $30K -> initials $58.5K  -> first level $100K
        entry $90K -> initials $175.5K -> first level $200K
        entry $15K -> initials $29.3K  -> first level $50K
    """
    levels = []

    # Standard spacing up to the widening point.
    level = math.floor(initials_mc / LADDER_STEP) * LADDER_STEP + LADDER_STEP
    while level <= min(up_to_mc, LADDER_WIDEN_ABOVE):
        levels.append(level)
        level += LADDER_STEP

    # Wider spacing beyond it.
    level = max(level, LADDER_WIDEN_ABOVE + LADDER_STEP_LARGE)
    level = math.floor(level / LADDER_STEP_LARGE) * LADDER_STEP_LARGE
    while level <= up_to_mc:
        if level > LADDER_WIDEN_ABOVE and level > initials_mc:
            levels.append(level)
        level += LADDER_STEP_LARGE

    return levels


# ==========================================================================
# Spike confirmation
# ==========================================================================


def _confirm(position, trigger_id, current_mc, now):
    """
    Holds a take-profit trigger briefly to avoid selling into a live spike.

    Returns True when the trigger should fire.

    The wait extends while the market cap keeps climbing, but the original
    sighting time still governs the hard cap, so a steadily rising coin cannot
    defer a sale indefinitely.
    """
    pending = position.get("pending")

    # A different trigger fired first - replace whatever was pending.
    if pending and pending["trigger_id"] != trigger_id:
        pending = None

    if pending is None:
        position["pending"] = {
            "trigger_id": trigger_id,
            "first_seen": now,
            "last_mc": current_mc,
        }
        return False

    elapsed = now - pending["first_seen"]

    # Hard cap reached - fire regardless of what the price is doing.
    if elapsed >= MAX_CONFIRM_WAIT_SECONDS:
        return True

    # The move has turned over - fire now rather than following it down.
    if current_mc < pending["last_mc"] * (1 - PULLBACK_THRESHOLD):
        return True

    # Still climbing - keep waiting, but do not reset the clock.
    if current_mc > pending["last_mc"] * (1 + STILL_RISING_THRESHOLD):
        pending["last_mc"] = current_mc
        return False

    return elapsed >= CONFIRM_DELAY_SECONDS


def _clear_pending(position):
    position["pending"] = None


# ==========================================================================
# Exit checks
# ==========================================================================


def _sell(position, fraction_of_remaining, reason, current_mc, exit_type):
    """Builds a sell instruction and updates the position's holdings."""
    tokens = position["tokens_remaining"] * fraction_of_remaining
    position["tokens_remaining"] -= tokens
    position["last_sell_mc"] = current_mc

    # Anything at or beyond a full exit closes the position outright.
    if fraction_of_remaining >= 1.0 or position["tokens_remaining"] <= 0:
        position["tokens_remaining"] = 0.0
        position["closed"] = True

    pct_of_original = tokens / position["original_tokens"] * 100

    return {
        "action": "sell",
        "exit_type": exit_type,
        "reason": reason,
        "at_mc": current_mc,
        "tokens": tokens,
        "fraction_of_remaining": fraction_of_remaining,
        "pct_of_original_position": round(pct_of_original, 2),
        "tokens_left": position["tokens_remaining"],
        "position_closed": position["closed"],
    }


def check_exit_conditions(position, current_mc, now):
    """
    Evaluates a position against the current market cap.

    Returns a list of sell instructions - usually empty, occasionally one, and
    never more than one per call. Firing a single action at a time prevents a
    price gap from cascading through several ladder levels and dumping most of
    the position at one price.

    'now' is a timestamp in seconds, used only for spike confirmation. Pass
    time.time() in production, or a simulated value in tests.
    """
    if position["closed"]:
        return []

    # Absolute floor. Checked before anything else, and independent of
    # whether initials have been taken - this is a hard override, not part
    # of the normal stop-loss/trailing-stop decision tree. No confirmation
    # delay, for the same reason as the stop-loss: on the way down, waiting
    # only costs money.
    if current_mc <= ABSOLUTE_FLOOR_MC:
        _clear_pending(position)
        return [
            _sell(
                position, 1.0,
                f"absolute floor: market cap ${current_mc:,.0f} at or below "
                f"the ${ABSOLUTE_FLOOR_MC:,.0f} dead-coin threshold",
                current_mc, "absolute_floor",
            )
        ]

    # The peak drives the trailing stop, so it is tracked on every check
    # regardless of whether anything else triggers.
    if current_mc > position["peak_mc"]:
        position["peak_mc"] = current_mc

    entry = position["entry_mc"]

    # ---- Before initials -------------------------------------------------
    if not position["initials_taken"]:

        # Stop-loss. Checked first and fires immediately: on the way down,
        # confirmation delay only costs money.
        stop_level = entry * (1 - STOP_LOSS_DRAWDOWN)
        if current_mc <= stop_level:
            _clear_pending(position)
            return [
                _sell(
                    position, 1.0,
                    f"stop-loss: market cap ${current_mc:,.0f} is "
                    f"{(1 - current_mc / entry) * 100:.0f}% below entry "
                    f"${entry:,.0f}",
                    current_mc, "stop_loss",
                )
            ]

        # Initials.
        initials_level = entry * (1 + INITIALS_TRIGGER_GAIN)
        if current_mc >= initials_level:
            if not _confirm(position, "initials", current_mc, now):
                return []
            _clear_pending(position)
            position["initials_taken"] = True
            position["initials_mc"] = initials_level
            return [
                _sell(
                    position, INITIALS_SELL_FRACTION,
                    f"initials: up {(current_mc / entry - 1) * 100:.0f}% "
                    f"from entry ${entry:,.0f}",
                    current_mc, "initials",
                )
            ]

        return []

    # ---- After initials --------------------------------------------------

    # Trailing stop. Fires immediately, for the same reason as the stop-loss.
    trail_level = position["peak_mc"] * (1 - TRAILING_STOP_DRAWDOWN)
    if current_mc <= trail_level:
        _clear_pending(position)
        return [
            _sell(
                position, 1.0,
                f"trailing stop: market cap ${current_mc:,.0f} is "
                f"{(1 - current_mc / position['peak_mc']) * 100:.0f}% below "
                f"peak ${position['peak_mc']:,.0f}",
                current_mc, "trailing_stop",
            )
        ]

    # Ladder clips.
    levels = ladder_levels(position["initials_mc"], current_mc)
    unfired = [lv for lv in levels if lv not in position["fired_levels"]]
    if not unfired:
        return []

    # Enforce the minimum gap from the last sell. Levels failing this test are
    # left unfired rather than consumed: they are absorbed later when a clip
    # does fire, since every unfired level below the current price is marked
    # at that point.
    if position["last_sell_mc"] is not None:
        required = position["last_sell_mc"] * (1 + MIN_GAP_BETWEEN_SELLS)
        if current_mc < required:
            # Clear any pending ladder confirmation so the clock starts fresh
            # once the level genuinely becomes eligible.
            if position.get("pending", {}) and position["pending"]["trigger_id"] == "ladder":
                _clear_pending(position)
            return []

    # Only the highest crossed level fires. Lower ones are marked as spent so
    # a gap through several levels results in one sale, not several.
    highest = max(unfired)
    if not _confirm(position, "ladder", current_mc, now):
        return []

    # Recompute after the wait: the price may have climbed through further
    # levels while the confirmation was pending.
    levels = ladder_levels(position["initials_mc"], current_mc)
    unfired = [lv for lv in levels if lv not in position["fired_levels"]]
    if not unfired:
        _clear_pending(position)
        return []
    highest = max(unfired)

    _clear_pending(position)
    position["fired_levels"].extend(unfired)
    skipped = len(unfired) - 1
    note = f" (gapped through {skipped} lower level(s))" if skipped else ""

    return [
        _sell(
            position, LADDER_CLIP_FRACTION,
            f"ladder clip at ${highest:,.0f}{note}",
            current_mc, "ladder_clip",
        )
    ]


# ==========================================================================
# Self test
# ==========================================================================


def _run_path(label, entry_mc, path, tokens=1_000_000.0):
    """Runs a position through a sequence of (market cap, timestamp) points."""
    print("=" * 74)
    print(f"{label}   entry ${entry_mc:,.0f}")
    print("-" * 74)

    pos = new_position("TEST", "aaa...pump", entry_mc, 0.3, tokens)
    if pos["entry_mc"]:
        print(f"  initials trigger : ${entry_mc * (1 + INITIALS_TRIGGER_GAIN):,.0f}")
        print(f"  stop-loss        : ${entry_mc * (1 - STOP_LOSS_DRAWDOWN):,.0f}")
        preview = ladder_levels(entry_mc * (1 + INITIALS_TRIGGER_GAIN), 800_000)
        print(f"  ladder           : {', '.join(f'${l/1000:.0f}K' for l in preview[:8])}...")
    print()

    for mc, t in path:
        for action in check_exit_conditions(pos, mc, t):
            print(f"  [{action['exit_type']:<13}] ${mc:>10,.0f}  "
                  f"sold {action['pct_of_original_position']:>5.1f}% of original  "
                  f"| {action['reason']}")
        if pos["closed"]:
            print("  POSITION CLOSED")
            break

    if not pos["closed"]:
        held = pos["tokens_remaining"] / pos["original_tokens"] * 100
        print(f"  still open, holding {held:.1f}% of original position")
    print()


def _run_self_test():
    # A steady run upward, checked every 5 seconds so confirmations clear.
    _run_path(
        "STEADY RUN - $30K entry climbing to $400K",
        30_000,
        [(mc, i * 5.0) for i, mc in enumerate(
            [35_000, 50_000, 58_000, 60_000, 65_000, 100_000, 105_000,
             150_000, 155_000, 200_000, 210_000, 300_000, 310_000,
             400_000, 410_000]
        )],
    )

    # A single observation gapping through several ladder levels at once.
    _run_path(
        "INSTANT SPIKE - $50K entry gapping to $200K",
        50_000,
        [(52_000, 0.0), (200_000, 1.0), (205_000, 4.0), (208_000, 8.0),
         (210_000, 12.0), (212_000, 16.0), (214_000, 20.0)],
    )

    # The user's worked example: $40K entry, initials near $80K, then $100K.
    _run_path(
        "USER EXAMPLE - $40K entry, clean fill near $80K, then $100K",
        40_000,
        [(mc, i * 5.0) for i, mc in enumerate(
            [45_000, 78_500, 80_000, 82_000, 100_000, 102_000,
             110_000, 112_000, 150_000, 152_000]
        )],
    )

    # Initials fill on a gap just under a ladder level - that level must be
    # skipped rather than clipping twice at effectively the same price.
    _run_path(
        "NEAR-MISS - initials gap-fill at $98K, $100K level must be skipped",
        50_000,
        [(52_000, 0.0), (98_000, 1.0), (97_500, 5.0), (100_000, 10.0),
         (101_000, 15.0), (105_000, 20.0), (108_000, 25.0),
         (112_000, 30.0), (114_000, 35.0)],
    )

    # Never reaches the initials trigger, then falls through the stop.
    _run_path(
        "LOSER - $40K entry falling away",
        40_000,
        [(38_000, 0.0), (30_000, 5.0), (22_000, 10.0), (17_000, 15.0)],
    )

    # Runs, takes initials and clips, then gives it all back.
    _run_path(
        "RUN THEN COLLAPSE - $30K to $250K, back to $60K",
        30_000,
        [(mc, i * 5.0) for i, mc in enumerate(
            [60_000, 62_000, 100_000, 105_000, 150_000, 155_000,
             250_000, 255_000, 180_000, 120_000, 70_000, 60_000]
        )],
    )


if __name__ == "__main__":
    _run_self_test()