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

To verify the response format against the live API:

    python src/market_data.py <a token mint address>
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


def _first_present(record, field_names):
    """Returns the first field in field_names that exists and is not empty."""
    for name in field_names:
        value = record.get(name)
        if value not in (None, "", 0):
            return value
    return None


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


async def _fetch_batch(session, mints):
    """Fetches one batch of mints. Returns {mint: market_cap}."""
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

    results = {}
    for record in payload:
        if not isinstance(record, dict):
            continue
        mint, mcap = _extract(record)
        if mint:
            results[mint] = mcap
    return results


async def fetch_market_caps(session, mints):
    """
    Fetches current market caps for a list of mint addresses.

    Returns {mint: market_cap}. Mints the API does not recognise are simply
    absent from the result rather than raising - a token that has been rugged
    or delisted should not bring down the polling loop for every other
    position.

    Network and API errors are allowed to propagate so the caller can decide
    how to handle them; the runner logs and retries on the next cycle.
    """
    mints = list(dict.fromkeys(mints))  # de-duplicate, preserve order
    if not mints:
        return {}

    results = {}
    for i in range(0, len(mints), MAX_MINTS_PER_REQUEST):
        batch = mints[i : i + MAX_MINTS_PER_REQUEST]
        results.update(await _fetch_batch(session, batch))
    return results


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

    print("\n--- PARSED ---")
    async with aiohttp.ClientSession() as session:
        print(await fetch_market_caps(session, [mint]))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python src/market_data.py <mint address>")
        print("Example mint from a real call:")
        print("  2PJXBNNp4qqcQM5cNdTJPhJ2cojif1HruSSCTyDLpump")
        sys.exit(1)
    asyncio.run(_verify(sys.argv[1]))