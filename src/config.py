"""
config.py - Stage 1 configuration layer.

Loads every setting the bot needs from .env, casts each to its correct type,
and validates it once here, at import time. The point is to fail loudly and
specifically at startup rather than letting a missing or malformed value
surface later as a cryptic KeyError or TypeError deep inside runner.py, or
worse, silently do the wrong thing (e.g. a blank SLIPPAGE_BPS being read as
0 and accepting any price).

Excel analogy: this is Data Validation applied to a whole tab, run once when
the workbook opens, rather than trusting every cell was filled in correctly.

    python src/config.py        prints the resolved config, secrets masked
"""

import logging
import os

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("config")


class ConfigError(Exception):
    """A .env value is missing, unparseable, or out of range."""


# ---------------------------------------------------------------------------
# Loading and casting helpers
# ---------------------------------------------------------------------------


def _require(name):
    """Returns the raw string value, or fails loudly if it is missing."""
    value = os.getenv(name)
    if value is None or value.strip() == "":
        raise ConfigError(
            f"{name} is missing from .env. Check .env exists in the project "
            f"root and contains a line '{name}=...'. See .env.example for "
            f"the full list of required keys."
        )
    return value.strip()


def _optional(name):
    """Returns the raw string value, or None if unset. Never fails."""
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return None
    return value.strip()


def _check_range(name, value, min_value, max_value):
    if min_value is not None and value < min_value:
        raise ConfigError(
            f"{name}={value} is below the minimum allowed ({min_value})."
        )
    if max_value is not None and value > max_value:
        raise ConfigError(
            f"{name}={value} is above the maximum allowed ({max_value})."
        )


def _as_int(name, raw, min_value=None, max_value=None):
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not a valid whole number.")
    _check_range(name, value, min_value, max_value)
    return value


def _as_float(name, raw, min_value=None, max_value=None):
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not a valid number.")
    _check_range(name, value, min_value, max_value)
    return value


def _as_bool(name, raw):
    lowered = raw.strip().lower()
    if lowered in ("true", "1", "yes"):
        return True
    if lowered in ("false", "0", "no"):
        return False
    raise ConfigError(
        f"{name}={raw!r} is not a valid boolean. Use 'true' or 'false'."
    )


# ---------------------------------------------------------------------------
# DRY_RUN - loaded first (Stage 5, 28 Aug 2026)
#
# Whether WALLET_PRIVATE_KEY is required below depends on this value, so it
# has to be known before that check runs. Moved up from "Trading safety"
# rather than adding an if in place, so the dependency is obvious from the
# file's order rather than hidden inside a later block.
# ---------------------------------------------------------------------------

DRY_RUN = _as_bool("DRY_RUN", _require("DRY_RUN"))

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

TELEGRAM_API_ID = _as_int("TELEGRAM_API_ID", _require("TELEGRAM_API_ID"))
TELEGRAM_API_HASH = _require("TELEGRAM_API_HASH")
TELEGRAM_CHANNEL = _require("TELEGRAM_CHANNEL")

# ---------------------------------------------------------------------------
# Wallet / RPC / Jupiter
#
# SENSITIVE: WALLET_PRIVATE_KEY is read here as a plain string and never
# logged, printed or included in log_resolved_config() except masked. It is
# handed to wallet.py, which is the only place it is turned into a Keypair.
#
# STAGE 5 (28 Aug 2026): required only when DRY_RUN is false. A dry run has
# no real balance to protect and no transaction to sign, so a private key
# serves no purpose there - wallet.py constructs its Keypair lazily, on
# first use, and runner.check_reserve_ok() handles the no-wallet case
# explicitly rather than assuming a key exists. When DRY_RUN is false this
# still fails exactly as before: loudly, at startup, same message.
# ---------------------------------------------------------------------------

if DRY_RUN:
    WALLET_PRIVATE_KEY = _optional("WALLET_PRIVATE_KEY")
else:
    WALLET_PRIVATE_KEY = _require("WALLET_PRIVATE_KEY")

HELIUS_RPC_URL = _require("HELIUS_RPC_URL")
if not HELIUS_RPC_URL.startswith("http"):
    raise ConfigError(
        f"HELIUS_RPC_URL does not look like a URL (starts with "
        f"{HELIUS_RPC_URL[:8]!r}). Check .env."
    )

# Optional: market_data.py and trade_execution.py both already degrade
# gracefully (to the lite-api endpoint, or to an unauthenticated request)
# when this is missing, so it is not required here either.
JUPITER_API_KEY = _optional("JUPITER_API_KEY")

# ---------------------------------------------------------------------------
# Trading safety
#
# DRY_RUN itself is loaded earlier in this file (see above) - required
# before WALLET_PRIVATE_KEY's conditional requirement can be evaluated.
# ---------------------------------------------------------------------------

# Total SOL committed to one coin across ALL DCA tranches combined, not one
# tranche. See runner.py's use of this for the aggregate-position-size cap.
MAX_POSITION_SOL = _as_float(
    "MAX_POSITION_SOL", _require("MAX_POSITION_SOL"), min_value=0.0001
)

MAX_CONCURRENT_POSITIONS = _as_int(
    "MAX_CONCURRENT_POSITIONS", _require("MAX_CONCURRENT_POSITIONS"), min_value=1
)

# The wallet must always be able to afford to sell what it holds. See the
# reserve check in runner.py for the arithmetic behind the 0.05 default.
MIN_SOL_RESERVE = _as_float(
    "MIN_SOL_RESERVE", _require("MIN_SOL_RESERVE"), min_value=0.0
)

# Basis points: 1 bps = 0.01%. 10,000 bps would be 100% slippage tolerance,
# which is the practical ceiling - anything at or above that is not a
# slippage setting any more, it is "accept any price".
SLIPPAGE_BPS = _as_int(
    "SLIPPAGE_BPS", _require("SLIPPAGE_BPS"), min_value=1, max_value=10_000
)

PRIORITY_FEE_LAMPORTS = _as_int(
    "PRIORITY_FEE_LAMPORTS", _require("PRIORITY_FEE_LAMPORTS"), min_value=0
)

# --- Entry sizing (Stage 3, moved from entry_logic.py) ---------------------
# entry_logic.py's pcr_to_lot_size() interpolates between these two for the
# total lot committed to one call; split_into_tranches() will not use a
# stage whose amount would fall below MIN_BUY_SOL. Kept here rather than
# hardcoded in entry_logic.py so wallet size can be re-tuned via .env alone,
# consistent with every other setting in this file.
MIN_LOT_SOL = _as_float(
    "MIN_LOT_SOL", _require("MIN_LOT_SOL"), min_value=0.0001
)
MAX_LOT_SOL = _as_float(
    "MAX_LOT_SOL", _require("MAX_LOT_SOL"), min_value=0.0001
)
if MAX_LOT_SOL < MIN_LOT_SOL:
    raise ConfigError(
        f"MAX_LOT_SOL ({MAX_LOT_SOL}) is below MIN_LOT_SOL ({MIN_LOT_SOL}) - "
        f"the PCR interpolation in entry_logic.py requires MAX_LOT_SOL >= MIN_LOT_SOL."
    )

MIN_BUY_SOL = _as_float(
    "MIN_BUY_SOL", _require("MIN_BUY_SOL"), min_value=0.0001
)

# --- Exit strategy (Stage 8, moved from exit_logic.py) ---------------------
# Fraction of the position sold when initials are taken. Kept here rather
# than hardcoded in exit_logic.py so it can be re-tuned via .env alone,
# consistent with the Stage 3 entry-sizing values above. min_value=0.0001 is
# the same "must be positive" convention used for MIN_LOT_SOL/MAX_LOT_SOL/
# MIN_BUY_SOL above; max_value=1.0 because selling more than the whole
# position is meaningless.
INITIALS_SELL_FRACTION = _as_float(
    "INITIALS_SELL_FRACTION", _require("INITIALS_SELL_FRACTION"),
    min_value=0.0001, max_value=1.0,
)


# ---------------------------------------------------------------------------
# Startup logging - every secret masked
# ---------------------------------------------------------------------------


def _mask(value):
    """
    Keeps a value's shape recognisable without exposing it: at most 2
    characters front and back, e.g. 'Ab...78'. Values of 8 characters or
    fewer are fully starred out instead, since a short secret (a Telegram
    API ID is often only 7-8 digits) has too little length to hide behind
    even a light mask - showing 4 of 8 characters is barely masked at all.
    """
    if value is None:
        return None
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:2]}...{value[-2:]}"


def log_resolved_config():
    """
    Logs every setting now in use, secrets masked, so a session's log file
    records exactly which configuration it ran under - the same reasoning
    as runner.py's existing startup guard-value logging.
    """
    log.info("config: DRY_RUN=%s", DRY_RUN)
    log.info("config: MAX_POSITION_SOL=%s SOL (aggregate per coin, all tranches)",
              MAX_POSITION_SOL)
    log.info("config: MAX_CONCURRENT_POSITIONS=%s", MAX_CONCURRENT_POSITIONS)
    log.info("config: MIN_SOL_RESERVE=%s SOL", MIN_SOL_RESERVE)
    log.info("config: SLIPPAGE_BPS=%s (%.2f%%)", SLIPPAGE_BPS, SLIPPAGE_BPS / 100)
    log.info("config: PRIORITY_FEE_LAMPORTS=%s", PRIORITY_FEE_LAMPORTS)
    log.info("config: MIN_LOT_SOL=%s SOL", MIN_LOT_SOL)
    log.info("config: MAX_LOT_SOL=%s SOL", MAX_LOT_SOL)
    log.info("config: MIN_BUY_SOL=%s SOL", MIN_BUY_SOL)
    log.info("config: INITIALS_SELL_FRACTION=%s", INITIALS_SELL_FRACTION)
    log.info("config: TELEGRAM_API_ID=%s", _mask(str(TELEGRAM_API_ID)))
    log.info("config: TELEGRAM_API_HASH=%s", _mask(TELEGRAM_API_HASH))
    log.info("config: TELEGRAM_CHANNEL=%s", TELEGRAM_CHANNEL)
    # SENSITIVE: masked, never logged in full.
    log.info(
        "config: WALLET_PRIVATE_KEY=%s",
        _mask(WALLET_PRIVATE_KEY) if WALLET_PRIVATE_KEY
        else "(not set - dry run only)",
    )
    log.info("config: HELIUS_RPC_URL=%s", _mask(HELIUS_RPC_URL))
    log.info(
        "config: JUPITER_API_KEY=%s",
        _mask(JUPITER_API_KEY) if JUPITER_API_KEY
        else "(not set - falling back to lite-api.jup.ag)",
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print("Config loaded and validated OK.\n")
    log_resolved_config()
