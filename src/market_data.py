"""
market_data.py - fetches live market cap data for tokens.

Uses Jupiter's public lite-api endpoint, which requires no API key and is
intended for exactly this kind of low-volume use. See spec section 7: Jupiter
is the primary source because the bot already integrates with it for
execution, with DexScreener available as a fallback if rate limits or
reliability become a problem in practice.

Requests are BATCHED. The free tier shares a single rate-limit bucket over a
60-second window, so polling ten positions individually every five seconds
would risk being throttled. Asking for every open position in one request
keeps usage flat regardless of how many positions are open.

To verify the response format against the live API:

    python src/market_data.py <a token mint address>
"""

import asyncio
import sys

import aiohttp

# Public, no-key endpoint. The paid equivalent is api.jup.ag, which would need
# a key in .env - not required at this volume.
JUPITER_SEARCH_URL = "https://lite-api.jup.ag/tokens/v2/search"

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

    async with session.get(JUPITER_SEARCH_URL, params=params, timeout=timeout) as response:
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

    async with aiohttp.ClientSession() as session:
        params = {"query": mint}
        async with session.get(JUPITER_SEARCH_URL, params=params) as response:
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
