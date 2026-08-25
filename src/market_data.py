"""
market_data.py - fetches live market cap data for tokens.

ENDPOINT (updated 10 Aug 2026): the keyed api.jup.ag endpoint is primary,
matching trade_execution.py. Jupiter is deprecating the no-key lite-api
domain, and this module is the bot's most safety-critical data feed - every
stop-loss, trailing stop and floor exit depends on the prices it returns.
If lite-api died while the bot held positions, the monitor would silently
receive nothing and every open position would go unmanaged, which is the
exact downtime-overshoot failure seen on Ratatouille, BEAR and RODRI.

If JUPITER_API_KEY is missing from .env the module still works, falling back
to lite-api with a loud warning at import time - degraded, not broken.

Requests are BATCHED. Both tiers share a rate-limit bucket over a 60-second
window, so polling ten positions individually every five seconds would risk
being throttled. Asking for every open position in one request keeps usage
flat regardless of how many positions are open.

TWO PUBLIC FUNCTIONS (extra fields added 15 Aug 2026)
-----------------------------------------------------
    fetch_market_caps(session, mints)   -> {mint: market_cap}
    fetch_token_details(session, mints) -> {mint: {...every captured field}}

They share one HTTP path, so the endpoint, headers and batching live in a
single place. The split is deliberate:

  - fetch_market_caps is called by the monitor loop every 5 seconds and its
    return value is consumed directly as a number. Changing that shape would
    have touched the exit path - stop-loss, trailing stop, absolute floor -
    which is the one part of this bot that must not break. It is therefore
    IDENTICAL to before: same name, same arguments, same {mint: float}.
  - fetch_token_details is the new one, called once per entry, returning
    everything the API exposes so the extra fields start accumulating.

Excel analogy: fetch_market_caps is a VLOOKUP returning one column.
fetch_token_details returns the whole matched row.

To verify against the live API:      python src/market_data.py <mint address>
To run the offline self-test:        python src/market_data.py
"""

import asyncio
import logging
import os
import sys

import aiohttp
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("market_data")

# Primary keyed endpoint - same host trade_execution.py uses for swaps, so
# execution and monitoring share one dependency that is actively maintained.
JUPITER_API_URL = "https://api.jup.ag/tokens/v2/search"

# Deprecated no-key endpoint, kept only as a fallback so a missing key
# degrades the bot rather than stopping it.
JUPITER_LITE_URL = "https://lite-api.jup.ag/tokens/v2/search"

JUPITER_API_KEY = os.getenv("JUPITER_API_KEY")

if JUPITER_API_KEY:
    SEARCH_URL = JUPITER_API_URL
    _HEADERS = {"x-api-key": JUPITER_API_KEY}
else:
    SEARCH_URL = JUPITER_LITE_URL
    _HEADERS = {}
    log.warning(
        "JUPITER_API_KEY missing from .env - falling back to the deprecated "
        "lite-api.jup.ag endpoint. Position monitoring will stop working "
        "when Jupiter retires that domain. Add the key used by "
        "trade_execution.py to .env."
    )

# Jupiter accepts multiple comma-separated mints per query.
MAX_MINTS_PER_REQUEST = 90

REQUEST_TIMEOUT_SECONDS = 10

# Field names are probed in order rather than hardcoded, because the exact key
# used for market cap has varied across Jupiter API versions. Probing means a
# rename degrades to "no data" rather than a crash.
_MINT_FIELDS = ("id", "address", "mint")
_MCAP_FIELDS = ("mcap", "marketCap", "market_cap", "fdv")

# ---------------------------------------------------------------------------
# EXTRA FIELDS (added 15 Aug 2026)
#
# Confirmed present in the live response on 10 Aug 2026. NONE of these feeds
# any trading decision. They are captured only so they accumulate on disk and
# can be tested against outcome once enough nights exist. A field that was
# never recorded cannot be recovered retrospectively, which is the entire
# reason for adding them now rather than when they are wanted.
#
# Each entry is (our_column_name, tuple_of_paths_to_probe).
#
# A "path" is a tuple of keys walked in order, so ("audit", "devMints") means
# payload["audit"]["devMints"]. Several paths per field because Jupiter has
# moved keys between the top level and the audit object across versions, and
# a probe list degrades to None rather than raising.
# ---------------------------------------------------------------------------

_DETAIL_FIELDS = (
    # Share of supply held by the largest holders. Conceptually adjacent to
    # the call's bundled_pct, currently the only entry input showing any
    # signal - so this is the highest-prior candidate of the six.
    ("top_holders_pct", (("audit", "topHoldersPercentage"),
                         ("topHoldersPercentage",))),
    # Jupiter's own composite quality score.
    ("organic_score", (("organicScore",), ("audit", "organicScore"))),
    # How many previous tokens this developer migrated / minted. A high count
    # is the serial-launcher pattern.
    ("dev_migrations", (("audit", "devMigrations"), ("devMigrations",))),
    ("dev_mints", (("audit", "devMints"), ("devMints",))),
    # Pool depth in dollars. Thin liquidity means slippage on the way out,
    # which the dry run cannot simulate but live trading will feel.
    ("liquidity", (("liquidity",), ("stats", "liquidity"))),
    # Which launchpad the token came from (pump.fun, bonk, etc). Text, not a
    # number - handled separately below.
    ("launchpad", (("launchpad",), ("audit", "launchpad"))),
    # EXTRA - not on the original list of six. Jupiter's live holder count,
    # free to capture and directly comparable to the call's `holders` figure,
    # so it measures how stale the call was: the same idea as entry_gap_pct,
    # which is already in the analysis. Delete this one line if unwanted.
    ("live_holder_count", (("holderCount",), ("stats", "holderCount"))),
)

# Fields that are text rather than numbers, so no float() conversion.
_TEXT_DETAIL_FIELDS = {"launchpad"}

# The column names, exported so data_logger, runner and data_loader can all
# refer to one definition instead of three lists that drift apart.
DETAIL_COLUMNS = tuple(column for column, _paths in _DETAIL_FIELDS)


def _first_present(record, field_names):
    """Returns the first field in field_names that exists and is not empty."""
    for name in field_names:
        value = record.get(name)
        if value not in (None, "", 0):
            return value
    return None


def _walk(record, path):
    """Follows a tuple of keys down into nested dictionaries.

    Returns None the moment anything on the way is missing or is not a
    dictionary, so a changed response shape produces a blank rather than an
    exception. Nothing in this module is allowed to take the bot down.

    Excel analogy: IFERROR wrapped around a chain of lookups.
    """
    current = record
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
        if current is None:
            return None
    return current


def _extract_details(record):
    """Pulls every captured extra field out of one token record.

    Returns a dictionary. Missing fields are present as None rather than
    absent, so every logged row carries the same set of keys and pandas reads
    them back as proper columns instead of a ragged table.
    """
    details = {}

    for column, paths in _DETAIL_FIELDS:
        value = None
        for path in paths:
            value = _walk(record, path)
            if value is not None:
                break

        if value is None:
            details[column] = None
        elif column in _TEXT_DETAIL_FIELDS:
            details[column] = str(value)
        else:
            try:
                details[column] = float(value)
            except (TypeError, ValueError):
                # A field that arrived as something unexpected is recorded as
                # blank rather than guessed at.
                details[column] = None

    return details


def _extract(record):
    """Pulls (mint, market_cap) out of one token record, or (None, None)."""
    mint = _first_present(record, _MINT_FIELDS)
    mcap = _first_present(record, _MCAP_FIELDS)
    if mint is None or mcap is None:
        return None, None
    try:
        return str(mint), float(mcap)
    except (TypeError, ValueError):
        return None, None


async def _fetch_raw_batch(session, mints):
    """Fetches one batch and returns {mint: raw_record}.

    The single HTTP path shared by both public functions, so endpoint,
    headers, timeout and response-shape handling exist in exactly one place.
    """
    params = {"query": ",".join(mints)}
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)

    async with session.get(SEARCH_URL, params=params, headers=_HEADERS,
                           timeout=timeout) as response:
        response.raise_for_status()
        payload = await response.json()

    # The endpoint has returned both a bare list and a wrapped object across
    # versions, so both shapes are handled.
    if isinstance(payload, dict):
        payload = payload.get("data") or payload.get("tokens") or []

    records = {}
    for record in payload:
        if not isinstance(record, dict):
            continue
        mint = _first_present(record, _MINT_FIELDS)
        if mint:
            records[str(mint)] = record
    return records


async def _fetch_all_raw(session, mints):
    """Runs _fetch_raw_batch across as many batches as the mint list needs."""
    mints = list(dict.fromkeys(mints))  # de-duplicate, preserve order
    if not mints:
        return {}

    records = {}
    for i in range(0, len(mints), MAX_MINTS_PER_REQUEST):
        batch = mints[i : i + MAX_MINTS_PER_REQUEST]
        records.update(await _fetch_raw_batch(session, batch))
    return records


async def fetch_market_caps(session, mints):
    """
    Fetches current market caps for a list of mint addresses.

    UNCHANGED CONTRACT: returns {mint: market_cap} exactly as it always has.
    The monitor loop calls this every 5 seconds and treats each value as a
    number, and every exit rule depends on it.

    Mints the API does not recognise are simply absent from the result rather
    than raising - a token that has been rugged or delisted should not bring
    down the polling loop for every other position.

    Network and API errors are allowed to propagate so the caller can decide
    how to handle them; the runner logs and retries on the next cycle.
    """
    records = await _fetch_all_raw(session, mints)

    results = {}
    for _mint, record in records.items():
        parsed_mint, mcap = _extract(record)
        if parsed_mint:
            results[parsed_mint] = mcap
    return results


async def fetch_token_details(session, mints):
    """
    Fetches market cap PLUS every extra field listed in _DETAIL_FIELDS.

    Returns {mint: {"market_cap": float, "top_holders_pct": ..., ...}}.

    Called once per entry rather than on the 5-second monitor cycle, so it
    adds no ongoing rate-limit pressure: one extra field set per call the bot
    considers filling, against roughly 50 calls a day.

    None of these fields feeds a trading decision. They are recorded so that
    in five nights' time there is something to test.
    """
    records = await _fetch_all_raw(session, mints)

    results = {}
    for _mint, record in records.items():
        parsed_mint, mcap = _extract(record)
        if not parsed_mint:
            continue
        details = _extract_details(record)
        details["market_cap"] = mcap
        results[parsed_mint] = details
    return results


# ---------------------------------------------------------------------------
# Offline self-test - no network, runs anywhere
# ---------------------------------------------------------------------------


def _run_self_test():
    """Checks field extraction against payload shapes, with no network.

    The cases below are the ones that matter: a full response, a response
    where Jupiter has dropped the audit object entirely, and a response with
    junk in the numeric fields. A live API test only ever exercises the first.
    """
    print("=" * 70)
    print("MARKET_DATA SELF-TEST (offline - no network calls)")
    print("=" * 70)

    failures = 0

    full = {
        "id": "MintAAA111111111111111111111111111111111111",
        "mcap": 24_500.0,
        "organicScore": 63.2,
        "liquidity": 18_400.5,
        "launchpad": "pump.fun",
        "holderCount": 412,
        "audit": {
            "topHoldersPercentage": 21.7,
            "devMigrations": 3,
            "devMints": 11,
        },
    }

    print("\n1. Full payload, every field present")
    details = _extract_details(full)
    expected = {
        "top_holders_pct": 21.7,
        "organic_score": 63.2,
        "dev_migrations": 3.0,
        "dev_mints": 11.0,
        "liquidity": 18_400.5,
        "launchpad": "pump.fun",
        "live_holder_count": 412.0,
    }
    for key, want in expected.items():
        got = details.get(key)
        ok = got == want
        failures += 0 if ok else 1
        print(f"   [{'PASS' if ok else 'FAIL'}] {key:<20} {got!r}")

    print("\n2. No audit object at all (Jupiter omits it on some tokens)")
    no_audit = {"id": "MintBBB", "mcap": 12_000.0, "launchpad": "bonk"}
    details = _extract_details(no_audit)
    ok = (details["top_holders_pct"] is None
          and details["dev_migrations"] is None
          and details["launchpad"] == "bonk")
    failures += 0 if ok else 1
    print(f"   [{'PASS' if ok else 'FAIL'}] missing fields are None, present "
          f"ones still read")
    ok = set(details) == set(DETAIL_COLUMNS)
    failures += 0 if ok else 1
    print(f"   [{'PASS' if ok else 'FAIL'}] every column key present anyway "
          f"({len(details)} keys)")

    print("\n3. Junk in the numeric fields")
    junk = {
        "id": "MintCCC",
        "mcap": 9_000.0,
        "organicScore": "not a number",
        "liquidity": None,
        "audit": "this should have been a dict",
    }
    try:
        details = _extract_details(junk)
        ok = (details["organic_score"] is None
              and details["liquidity"] is None
              and details["top_holders_pct"] is None)
        failures += 0 if ok else 1
        print(f"   [{'PASS' if ok else 'FAIL'}] junk degrades to None, no "
              f"exception raised")
    except Exception as exc:
        failures += 1
        print(f"   [FAIL] raised {type(exc).__name__}: {exc}")

    print("\n4. Market cap extraction unchanged (the monitor loop's path)")
    for label, record, want in (
        ("mcap key", full, 24_500.0),
        ("marketCap key", {"id": "X", "marketCap": 5_000}, 5_000.0),
        ("no mcap at all", {"id": "X"}, None),
    ):
        _mint, got = _extract(record)
        ok = got == want
        failures += 0 if ok else 1
        print(f"   [{'PASS' if ok else 'FAIL'}] {label:<20} {got!r}")

    print("\n" + "=" * 70)
    if failures == 0:
        print("MARKET_DATA SELF-TEST PASSED")
    else:
        print(f"MARKET_DATA SELF-TEST FAILED - {failures} problem(s) above")
    print("=" * 70)
    return failures


async def _verify(mint):
    """Prints the raw API response so the field names above can be checked."""
    import json

    print(f"Endpoint in use: {SEARCH_URL}")
    print(f"API key loaded : {'yes' if JUPITER_API_KEY else 'NO - using fallback'}\n")

    async with aiohttp.ClientSession() as session:
        params = {"query": mint}
        async with session.get(SEARCH_URL, params=params,
                               headers=_HEADERS) as response:
            print(f"HTTP {response.status}")
            payload = await response.json()

    print("\n--- RAW RESPONSE ---")
    print(json.dumps(payload, indent=2)[:3000])

    print("\n--- PARSED: market cap only (what the monitor loop uses) ---")
    async with aiohttp.ClientSession() as session:
        print(await fetch_market_caps(session, [mint]))

    print("\n--- PARSED: full details (what entries now record) ---")
    async with aiohttp.ClientSession() as session:
        details = await fetch_token_details(session, [mint])
    if not details:
        print("  (no record returned for this mint)")
    for token, fields in details.items():
        print(f"  {token}")
        for key, value in fields.items():
            marker = "" if value is not None else "   <- not returned by API"
            print(f"    {key:<20}: {value!r}{marker}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        # No mint given: run the offline self-test rather than printing usage.
        raise SystemExit(1 if _run_self_test() else 0)
    asyncio.run(_verify(sys.argv[1]))
