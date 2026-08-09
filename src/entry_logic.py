"""
entry_logic.py - Stage 3 of the Solana memecoin signal trading bot.

Implements the Proprietary Conviction Rating (PCR) and turns a parsed call
into an entry decision: whether to buy, how much in total, and how that total
splits across multiple buys.

This module is pure logic. It performs no trading, touches no wallet, and
makes no network calls, so it can be tested offline:  python src/entry_logic.py

Structure of the PCR:

    base  = weighted sum of GTscore, holder velocity and bundled %
    mult  = market cap multiplier (suppresses size as market cap rises)
    PCR   = base * mult
    lot   = MIN_LOT + PCR * (MAX_LOT - MIN_LOT)
"""

# ==========================================================================
# TUNABLE PARAMETERS
#
# Everything in this block is expected to change as the strategy is refined.
# Keeping it together means the logic below can stay untouched while the
# numbers are calibrated against logged results.
# ==========================================================================

# --- Position sizing -------------------------------------------------------
MIN_LOT_SOL = 0.2          # total committed to a call at PCR = 0
MAX_LOT_SOL = 0.5          # total committed to a call at PCR = 1
MIN_BUY_SOL = 0.10         # smallest permitted individual buy transaction
MAX_TRANCHES = 3           # most buys a single call may be split across

# --- Weights (must sum to 1.0) ---------------------------------------------
# Market cap is deliberately absent: it acts as a multiplier, not a component.
WEIGHT_GTSCORE = 0.40
WEIGHT_VELOCITY = 0.40
WEIGHT_BUNDLED = 0.20

# --- Market cap thresholds -------------------------------------------------
MC_FLOOR = 15_000          # at or below this, market cap scores a full 1.0
MC_SOFT_CEILING = 50_000   # preference boundary; the steep taper begins here
MC_HARD_CUT = 75_000       # at or above this, the call is rejected outright
MC_SOFT_SCORE = 0.15       # score at the soft ceiling, where the taper starts

# Controls how punitive a high market cap is.
#   0.0 -> pure multiplier: a poor market cap crushes the whole score
#   0.5 -> softened:        a poor market cap halves it at worst
MC_MULTIPLIER_SOFTENING = 0.3

# --- Holder velocity -------------------------------------------------------
# Holders per minute at which velocity scores a full 1.0.
VELOCITY_CEILING = 60

# --- GTscore ---------------------------------------------------------------
# Maps star rating onto 0..1 by RARITY rather than linear position.
#
# A linear mapping ((stars-1)/4) assumes stars are evenly distributed. They are
# not: roughly three quarters of observed calls are 1 or 2 stars, so a linear
# scale hands the lowest scores to most of the population and wastes the
# factor's weight suppressing everything rather than discriminating between
# calls. These values reflect approximate observed frequency, so a 4-star call
# scores highly because it is genuinely uncommon.
#
# Recalibrate against logged data once enough calls have accumulated.
GTSCORE_SCALE = {1: 0.00, 2: 0.45, 3: 0.77, 4: 0.92, 5: 1.00}

# --- Bundled percentage ----------------------------------------------------
# At or above this, bundled scores 0.0.
BUNDLED_CEILING = 40

# Shifts the bundled curve leftward so that TYPICAL bundling (around 14%) sits
# near the middle of the scale rather than near the top. Without it the factor
# awards ~0.7 to almost every call, consuming range without differentiating.
#
# Values below 1.0 shift the steep section earlier. The ceiling above is
# unaffected: 40% still scores exactly 0.0.
BUNDLED_CURVE_GAMMA = 0.65

# --- PCR range stretching --------------------------------------------------
# The raw PCR does not span 0..1 in practice - it clusters, because a call
# rarely scores well on every factor at once. Mapping that clustered range
# directly onto the lot range wastes most of it: without stretching, roughly
# 80% of calls land within 0.12 SOL of each other.
#
# These two values define the PCR band that maps onto the full lot range.
# Anything at or below PCR_STRETCH_LO sizes at MIN_LOT_SOL; anything at or
# above PCR_STRETCH_HI sizes at MAX_LOT_SOL.
#
# IMPORTANT: these are calibrated against a simulated distribution, not real
# logged calls. Recalibrate from the dry-run logs - set LO near the 10th
# percentile of observed PCR values and HI near the 90th.
PCR_STRETCH_LO = 0.10
PCR_STRETCH_HI = 0.60

# --- DCA / multi-buy -------------------------------------------------------
# Price drop, measured from the PREVIOUS fill (not from the first buy), that
# triggers the next tranche. Buy 3 therefore fires roughly 19% below buy 1,
# not 20%.
DCA_DROP_STEP_PCT = 10

# Share of the total lot committed at each stage.
#
# The shape is deliberate: buy 1 is a probe made before the coin has proved
# anything, buy 2 is the largest commitment because the price has improved
# while the thesis is still intact, and buy 3 is slightly smaller again
# because a second consecutive drop raises the odds the call is simply wrong.
#
# Both sets must sum to 1.0.
DCA_WEIGHTS_THREE = (0.30, 0.38, 0.32)
DCA_WEIGHTS_TWO = (0.45, 0.55)


# ==========================================================================
# Normalisation helpers
# ==========================================================================


def smoothstep(x):
    """
    Smooth S-curve mapping any input onto 0..1, clamped at both ends.

    Chosen over a straight line because the extremes of these metrics carry
    little information (3% and 6% bundled are both "clean"; 36% and 39% are
    both "avoid"), while the middle of the range is where calls genuinely
    differ. An S-curve puts its steepest gradient exactly there.
    """
    x = max(0.0, min(1.0, x))
    return 3 * x**2 - 2 * x**3


def score_gtscore(stars):
    """
    Normalises the GTscore star rating onto 0..1 using the rarity scale.

    See GTSCORE_SCALE for why this is a lookup rather than a linear formula.
    An unrecognised or missing rating scores 0.0 rather than being assumed
    average, so a malformed message cannot inflate position size.
    """
    if stars is None:
        return 0.0
    return GTSCORE_SCALE.get(stars, 0.0)


def score_holder_velocity(holders, age_minutes):
    """
    Normalises holder growth rate onto 0..1.

    A raw holder count is ambiguous - 300 holders means something very
    different at five minutes old than at fifty. Dividing by age converts it
    into a rate, which is far more discriminating across real calls.

    Age is floored at one minute to avoid dividing by zero on calls that
    arrive within seconds of a token launching.
    """
    if holders is None or age_minutes is None:
        return 0.0
    velocity = holders / max(1, age_minutes)
    return smoothstep(velocity / VELOCITY_CEILING)


def score_bundled(bundled_pct):
    """
    Normalises bundled percentage onto 0..1, inverted so that lower is better.

    Bundling indicates coordinated buying at launch, which carries the risk of
    an equally coordinated exit. A missing value is treated as maximum risk
    rather than assumed harmless.
    """
    if bundled_pct is None:
        return 0.0
    ratio = max(0.0, min(1.0, bundled_pct / BUNDLED_CEILING))
    return 1 - smoothstep(ratio ** BUNDLED_CURVE_GAMMA)


def score_market_cap(market_cap):
    """
    Normalises market cap onto 0..1, or returns None if the call is rejected.

    Three regions:
      - at or below MC_FLOOR          : full score, lower offers no extra credit
      - MC_FLOOR to MC_SOFT_CEILING   : smoothstep decline to MC_SOFT_SCORE
      - MC_SOFT_CEILING to MC_HARD_CUT: steep quadratic taper to zero
      - at or above MC_HARD_CUT       : rejected, returns None

    The taper exists so that a call marginally above the preferred ceiling is
    penalised rather than discarded. A hard cliff at the soft ceiling would
    have rejected a real call that went on to run 4x.
    """
    if market_cap is None:
        return None
    if market_cap >= MC_HARD_CUT:
        return None

    if market_cap <= MC_SOFT_CEILING:
        span = MC_SOFT_CEILING - MC_FLOOR
        decline = smoothstep((market_cap - MC_FLOOR) / span)
        return MC_SOFT_SCORE + (1 - MC_SOFT_SCORE) * (1 - decline)

    # Above the soft ceiling: quadratic decay, steeper than the band below it.
    overshoot = (market_cap - MC_SOFT_CEILING) / (MC_HARD_CUT - MC_SOFT_CEILING)
    return MC_SOFT_SCORE * (1 - overshoot) ** 2


# ==========================================================================
# Proprietary Conviction Rating
# ==========================================================================


def calculate_pcr(call):
    """
    Calculates the PCR for a parsed call.

    Returns a dictionary containing the final rating plus every intermediate
    score. The breakdown is deliberately exposed rather than hidden: when a
    call sizes unexpectedly, being able to see which factor caused it is the
    difference between debugging and guessing.

    Returns None for market_cap_score if the call is disqualified.
    """
    mc_score = score_market_cap(call.get("market_cap"))

    breakdown = {
        "gtscore_score": score_gtscore(call.get("gt_score")),
        "velocity_score": score_holder_velocity(
            call.get("holders"), call.get("age_minutes")
        ),
        "bundled_score": score_bundled(call.get("bundled_pct")),
        "market_cap_score": mc_score,
    }

    # Disqualified on market cap - no rating is produced.
    if mc_score is None:
        breakdown["base_score"] = None
        breakdown["mc_multiplier"] = None
        breakdown["pcr"] = None
        return breakdown

    base = (
        WEIGHT_GTSCORE * breakdown["gtscore_score"]
        + WEIGHT_VELOCITY * breakdown["velocity_score"]
        + WEIGHT_BUNDLED * breakdown["bundled_score"]
    )

    # Market cap applies multiplicatively, so a poor market cap suppresses the
    # whole rating rather than merely failing to lift it.
    multiplier = MC_MULTIPLIER_SOFTENING + (1 - MC_MULTIPLIER_SOFTENING) * mc_score

    breakdown["base_score"] = base
    breakdown["mc_multiplier"] = multiplier
    breakdown["pcr"] = base * multiplier
    return breakdown


def stretch_pcr(pcr):
    """
    Rescales a raw PCR onto 0..1 using the expected operating band.

    Raw PCR values cluster in the lower part of the theoretical 0..1 range,
    so mapping them directly onto the lot range leaves most of that range
    unused. Stretching the band where calls actually fall onto the full range
    restores meaningful differentiation between a weak call and a strong one.
    """
    span = PCR_STRETCH_HI - PCR_STRETCH_LO
    return max(0.0, min(1.0, (pcr - PCR_STRETCH_LO) / span))


def pcr_to_lot_size(pcr):
    """Maps a PCR onto the permitted total position size in SOL."""
    return MIN_LOT_SOL + stretch_pcr(pcr) * (MAX_LOT_SOL - MIN_LOT_SOL)


# ==========================================================================
# Tranche splitting (DCA)
# ==========================================================================


def split_into_tranches(total_sol):
    """
    Splits a total position size into staged buys.

    Tries a three-stage split first, falling back to two stages and then to a
    single buy. The deciding constraint is MIN_BUY_SOL: a split is only used if
    EVERY tranche it produces clears that floor, because a plan whose first buy
    is too small to execute is not a plan.

    Consequence worth understanding: low-conviction calls naturally end up as a
    single buy, because their total is too small to divide. Staged entry is
    therefore something the strategy earns through conviction rather than
    applies uniformly.
    """
    total_sol = round(total_sol, 4)

    for weights in (DCA_WEIGHTS_THREE, DCA_WEIGHTS_TWO):
        amounts = [total_sol * w for w in weights]

        # Tolerance guards against floating point noise at the boundary.
        if all(a >= MIN_BUY_SOL - 1e-9 for a in amounts):
            tranches = []
            for i, amount in enumerate(amounts):
                if i == 0:
                    trigger = "immediate"
                    drop = 0
                else:
                    drop = DCA_DROP_STEP_PCT
                    trigger = f"{drop}% below buy {i} fill price"
                tranches.append(
                    {
                        "stage": i + 1,
                        "sol": round(amount, 4),
                        "trigger": trigger,
                        "drop_pct_from_previous_fill": drop,
                    }
                )

            # Rounding can leave a few lamports unallocated; the residue goes on
            # the final tranche so the stages always sum to the intended total.
            residue = total_sol - sum(t["sol"] for t in tranches)
            tranches[-1]["sol"] = round(tranches[-1]["sol"] + residue, 4)
            return tranches

    return [
        {
            "stage": 1,
            "sol": total_sol,
            "trigger": "immediate",
            "drop_pct_from_previous_fill": 0,
        }
    ]


# ==========================================================================
# Entry decision
# ==========================================================================


def decide_entry(call):
    """
    Turns a parsed call into an entry decision.

    Returns a dictionary with "action" set to either "buy" or "reject", plus
    the reasoning and full PCR breakdown either way. Rejections are described
    rather than silently dropped, so the log records why a call was skipped.

    This function does not check wallet balance or the fee reserve - those are
    execution concerns and belong in the code that actually places the trade.
    """
    decision = {
        "ticker": call.get("ticker"),
        "contract_address": call.get("contract_address"),
        "market_cap": call.get("market_cap"),
    }

    # A partially parsed call must never reach sizing logic. Without a market
    # cap there is no 2x exit threshold; without a contract address there is
    # nothing to buy.
    if not call.get("parse_ok"):
        decision["action"] = "reject"
        decision["reason"] = (
            f"incomplete parse, missing: {call.get('missing_fields')}"
        )
        return decision

    breakdown = calculate_pcr(call)
    decision["breakdown"] = breakdown

    if breakdown["pcr"] is None:
        decision["action"] = "reject"
        decision["reason"] = (
            f"market cap ${call['market_cap']:,} at or above hard cut "
            f"${MC_HARD_CUT:,}"
        )
        return decision

    total = pcr_to_lot_size(breakdown["pcr"])

    decision["action"] = "buy"
    decision["reason"] = "passed all entry criteria"
    decision["pcr"] = round(breakdown["pcr"], 4)
    decision["total_lot_sol"] = round(total, 4)
    decision["tranches"] = split_into_tranches(total)
    return decision


# ==========================================================================
# Self test
# ==========================================================================

_TEST_CALLS = [
    {
        "label": "THESIS (real call)",
        "ticker": "THESIS", "contract_address": "2PJX...pump",
        "gt_score": 1, "market_cap": 38400, "holders": 286,
        "age_minutes": 5, "bundled_pct": 18.0, "parse_ok": True,
    },
    {
        "label": "VIBECAT (real call)",
        "ticker": "VIBECAT", "contract_address": "77YU...pump",
        "gt_score": 2, "market_cap": 43500, "holders": 335,
        "age_minutes": 49, "bundled_pct": 11.0, "parse_ok": True,
    },
    {
        "label": "Low MC, strong signal",
        "ticker": "LOWMC", "contract_address": "aaaa...pump",
        "gt_score": 3, "market_cap": 22000, "holders": 350,
        "age_minutes": 5, "bundled_pct": 8.0, "parse_ok": True,
    },
    {
        "label": "Above hard cut - should reject",
        "ticker": "TOOBIG", "contract_address": "bbbb...pump",
        "gt_score": 5, "market_cap": 90000, "holders": 800,
        "age_minutes": 4, "bundled_pct": 2.0, "parse_ok": True,
    },
    {
        "label": "Incomplete parse - should reject",
        "ticker": "BROKEN", "contract_address": "cccc...pump",
        "gt_score": 3, "market_cap": None, "holders": 200,
        "age_minutes": 5, "bundled_pct": 10.0,
        "parse_ok": False, "missing_fields": ["market_cap"],
    },
]


def _run_self_test():
    for call in _TEST_CALLS:
        label = call.pop("label")
        print("=" * 70)
        print(label)
        print("-" * 70)

        d = decide_entry(call)
        print(f"  action        : {d['action'].upper()}")
        print(f"  reason        : {d['reason']}")

        if d.get("breakdown") and d["breakdown"].get("pcr") is not None:
            b = d["breakdown"]
            print(f"  gtscore       : {b['gtscore_score']:.3f}")
            print(f"  velocity      : {b['velocity_score']:.3f}")
            print(f"  bundled       : {b['bundled_score']:.3f}")
            print(f"  base score    : {b['base_score']:.3f}")
            print(f"  mc score      : {b['market_cap_score']:.3f}")
            print(f"  mc multiplier : {b['mc_multiplier']:.3f}")
            print(f"  PCR           : {d['pcr']:.3f}")
            print(f"  TOTAL LOT     : {d['total_lot_sol']} SOL")
            for i, t in enumerate(d["tranches"], 1):
                print(f"     buy {i}: {t['sol']} SOL  ({t['trigger']})")
        print()


if __name__ == "__main__":
    _run_self_test()
