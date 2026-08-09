"""
parser.py - Stage 2 of the Solana memecoin signal trading bot.

Turns raw GemTools message text into structured Python dictionaries.

Three message types are recognised:
  - call              : a new coin call, containing a contract address
  - multiplier_update : a milestone such as "$RATIONAL x4"
  - whale_update      : a whale purchase notification
  - unknown           : anything that matches none of the above

This module performs no trading and holds no state. It can be tested entirely
offline by running it directly:  python src/parser.py
"""

import re

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

# Suffix multipliers used in market cap values, e.g. "$38.4K" -> 38400.
_MONEY_MULTIPLIERS = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}

# Suffix multipliers used in token age values, converting everything to minutes.
_AGE_MULTIPLIERS = {"m": 1, "h": 60, "d": 1_440}

# The filled star character used by the GTscore rating.
#
# Note: the channel sometimes emits this character followed by an invisible
# "variation selector" (U+FE0F), which renders identically but is a different
# byte sequence. Counting the base character below matches both forms, which
# is why we do not compare against a literal "⭐️" string.
_FILLED_STAR = "\u2b50"

# Solana addresses are base58 encoded, which excludes the visually ambiguous
# characters 0 (zero), O, I and l. Anchoring to a full line avoids matching
# fragments of surrounding text.
_CONTRACT_PATTERN = re.compile(r"^([1-9A-HJ-NP-Za-km-z]{32,44})$", re.MULTILINE)

# Fields a call must contain before the bot is allowed to act on it.
_REQUIRED_CALL_FIELDS = ("ticker", "contract_address", "gt_score", "market_cap")


# --------------------------------------------------------------------------
# Helper functions
# --------------------------------------------------------------------------


def _search(pattern, text):
    """Returns the first capture group of a pattern, or None if not found."""
    match = re.search(pattern, text)
    return match.group(1) if match else None


def _to_float(value):
    """Converts a captured string to a float, tolerating a missing value."""
    return float(value) if value is not None else None


def _money_to_number(amount, suffix):
    """
    Converts a market cap such as ("38.4", "K") into a plain number: 38400.

    Exit logic compares market caps arithmetically against 2x thresholds, so
    values are normalised here rather than left as display text.
    """
    if amount is None:
        return None
    multiplier = _MONEY_MULTIPLIERS.get(suffix.upper(), 1) if suffix else 1
    return int(float(amount) * multiplier)


def _parse_money(text, label):
    """
    Extracts a labelled money value, e.g. _parse_money(text, "MC") for
    "MC: $38.4K", returning 38400.
    """
    match = re.search(rf"{label}:\s*\$([\d.]+)\s*([KMB])?", text)
    if not match:
        return None
    return _money_to_number(match.group(1), match.group(2))


def _parse_percent(text, label):
    """Extracts a labelled percentage, e.g. "Top10: 20%" -> 20.0."""
    return _to_float(_search(rf"{label}:\s*([\d.]+)\s*%", text))


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


def classify_message(text):
    """
    Determines which kind of message this is, based on its content.

    Note that this is content-based classification only. The listener also
    knows whether a message was a reply, which is a stronger signal - calls
    are always standalone, updates are always replies. The two checks are
    complementary: this function can be used on saved text with no Telegram
    context attached.
    """
    if not text:
        return "unknown"

    if "whale bought" in text:
        return "whale_update"

    # A call is identified by carrying a contract address. The GTscore check
    # guards against a stray address appearing in some other message type.
    if _CONTRACT_PATTERN.search(text) and "GTscore" in text:
        return "call"

    # Multiplier updates look like "$RATIONAL x4" followed by a market cap
    # transition using an arrow.
    if re.search(r"\$\w+\s+x\d+", text) and "→" in text:
        return "multiplier_update"

    return "unknown"


# --------------------------------------------------------------------------
# Call parsing
# --------------------------------------------------------------------------


def parse_call(text):
    """
    Extracts every field from a call message.

    All fields are captured, not only the four the Proprietary Conviction
    Rating currently uses. Fields that look irrelevant today may turn out to
    be predictive once there is enough logged data to run a regression, and
    they cannot be recovered retrospectively if they were never stored.
    """
    result = {"message_type": "call", "raw_text": text}

    # Ticker and display name, e.g. "$THESIS (I like the coin)".
    # The name can contain spaces, so it is captured non-greedily.
    name_match = re.search(r"\$(\w+)\s*\((.+?)\)", text)
    result["ticker"] = name_match.group(1) if name_match else None
    result["token_name"] = name_match.group(2) if name_match else None

    # Contract address - the trade target.
    contract_match = _CONTRACT_PATTERN.search(text)
    result["contract_address"] = contract_match.group(1) if contract_match else None

    # GTscore: count filled stars, but only within the GTscore line, so that
    # a star appearing elsewhere in the message cannot inflate the score.
    gt_line = _search(r"GTscore:\s*(.+)", text)
    result["gt_score"] = gt_line.count(_FILLED_STAR) if gt_line else None

    # Market cap, normalised to a plain number.
    result["market_cap"] = _parse_money(text, "MC")

    # Age, normalised to minutes regardless of the unit used.
    age_match = re.search(r"Age:\s*(\d+)\s*([mhd])", text)
    if age_match:
        unit = age_match.group(2)
        result["age_minutes"] = int(age_match.group(1)) * _AGE_MULTIPLIERS.get(unit, 1)
    else:
        result["age_minutes"] = None

    result["holders"] = int(_search(r"Holders:\s*(\d+)", text) or 0) or None

    # Distribution and behaviour metrics.
    result["top10_pct"] = _parse_percent(text, "Top10")
    result["bundled_pct"] = _parse_percent(text, "Bundled")
    result["first50_pct"] = _parse_percent(text, "First50")
    result["jeeters_pct"] = _parse_percent(text, "Jeeters")
    result["quickflip_pct"] = _parse_percent(text, "Quickflip")
    result["snipers_pct"] = _parse_percent(text, "Snipers")
    result["insiders_pct"] = _parse_percent(text, "Insiders")
    result["dev_pct"] = _parse_percent(text, "Dev")
    result["safe_pct"] = _parse_percent(text, "Safe")
    result["poor_pct"] = _parse_percent(text, "Poor")

    # The "8C · 6W · 6.0%" line. Its meaning is not yet confirmed, so it is
    # stored verbatim rather than interpreted. Capturing it now means it is
    # available for analysis later without guessing at a structure that may
    # turn out to be wrong.
    result["unknown_metric_raw"] = _search(r"🕸\s*(.+)", text)

    # Validation. The caller must check parse_ok before acting on this data.
    missing = [f for f in _REQUIRED_CALL_FIELDS if result.get(f) is None]
    result["parse_ok"] = not missing
    result["missing_fields"] = missing

    return result


# --------------------------------------------------------------------------
# Update parsing
# --------------------------------------------------------------------------


def parse_multiplier_update(text):
    """
    Extracts fields from a milestone message such as:
        🚀 $RATIONAL x4 🚀
        💵 MC: $50.1K → $204.7K in 16m
    """
    result = {"message_type": "multiplier_update", "raw_text": text}

    header = re.search(r"\$(\w+)\s+x(\d+)", text)
    result["ticker"] = header.group(1) if header else None
    result["multiplier"] = int(header.group(2)) if header else None

    # The two market caps either side of the arrow: starting and current.
    transition = re.search(
        r"MC:\s*\$([\d.]+)\s*([KMB])?\s*→\s*\$([\d.]+)\s*([KMB])?", text
    )
    if transition:
        result["mc_start"] = _money_to_number(transition.group(1), transition.group(2))
        result["mc_current"] = _money_to_number(transition.group(3), transition.group(4))
    else:
        result["mc_start"] = None
        result["mc_current"] = None

    # Elapsed time is kept as text for now ("34s", "16m") since it is not yet
    # used by any logic and the units vary.
    result["elapsed_raw"] = _search(r"in\s+(\S+)\s*$", text)

    result["parse_ok"] = result["ticker"] is not None
    return result


def parse_whale_update(text):
    """
    Extracts fields from a whale purchase message such as:
        🐋 $JIMOTHY whale bought 3.77 SOL @ $115.0K MC
    """
    result = {"message_type": "whale_update", "raw_text": text}

    match = re.search(
        r"\$(\w+)\s+whale bought\s+([\d.]+)\s*SOL\s*@\s*\$([\d.]+)\s*([KMB])?",
        text,
    )
    if match:
        result["ticker"] = match.group(1)
        result["sol_amount"] = float(match.group(2))
        result["mc_at_purchase"] = _money_to_number(match.group(3), match.group(4))
    else:
        result["ticker"] = None
        result["sol_amount"] = None
        result["mc_at_purchase"] = None

    result["parse_ok"] = result["ticker"] is not None
    return result


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def parse_message(text):
    """
    Classifies a message and routes it to the appropriate parser.

    Always returns a dictionary containing at least "message_type" and
    "parse_ok". The caller is responsible for checking parse_ok before acting
    on the contents - a partially parsed call must never reach trade logic.
    """
    message_type = classify_message(text)

    if message_type == "call":
        return parse_call(text)
    if message_type == "multiplier_update":
        return parse_multiplier_update(text)
    if message_type == "whale_update":
        return parse_whale_update(text)

    return {"message_type": "unknown", "raw_text": text, "parse_ok": False}


# --------------------------------------------------------------------------
# Self test - runs when this file is executed directly
# --------------------------------------------------------------------------

# Real messages captured from the channel, used as test fixtures.
_SAMPLE_CALL = """🚀 $THESIS (I like the coin)
2PJXBNNp4qqcQM5cNdTJPhJ2cojif1HruSSCTyDLpump
GTscore: ⭐☆☆☆☆
📊 MC: $38.4K · ⏱ Age: 5m · 👪 Holders: 286
🔟 Top10: 20% · 📦 Bundled: 18% · 🏁 First50: 4.8%
☢️ Jeeters: 16% · 🔄 Quickflip: 0.0% · 🎯 Snipers: 0.0%
🐁 Insiders: 0.0% · 👤 Dev: 0.0%
✅ Safe: 44% · ⚠️ Poor: 2.0%
🕸 8C · 6W · 6.0%
⚡ gemtools.fun
⚠️ GT Alerts are still warming up — treat every signal as risky."""

_SAMPLE_MULTIPLIER = """🚀 $RATIONAL x4 🚀
💵 MC: $50.1K → $204.7K in 16m"""

_SAMPLE_WHALE = "🐋 $JIMOTHY whale bought 3.77 SOL @ $115.0K MC"


def _run_self_test():
    """Parses each sample message and prints the result for inspection."""
    samples = [
        ("CALL", _SAMPLE_CALL),
        ("MULTIPLIER UPDATE", _SAMPLE_MULTIPLIER),
        ("WHALE UPDATE", _SAMPLE_WHALE),
    ]

    for label, sample in samples:
        print("=" * 70)
        print(f"{label}  ->  classified as: {classify_message(sample)}")
        print("-" * 70)

        parsed = parse_message(sample)
        for key, value in parsed.items():
            # raw_text is long and already known, so it is skipped here.
            if key == "raw_text":
                continue
            print(f"  {key:22}: {value}")
        print()


if __name__ == "__main__":
    _run_self_test()