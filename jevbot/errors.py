"""Turn exceptions into messages a person can act on."""
from __future__ import annotations

import urllib.error


def friendly_error(e: BaseException) -> str:
    name = type(e).__name__
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 404:
            return "Ticker not found by the data source. Check the spelling (US tickers, like AAPL or BRK-B)."
        if e.code == 429:
            return "The free data source is rate limiting us. Wait a minute and try again."
        return f"The data source answered with an error (HTTP {e.code}). Try again shortly."
    if isinstance(e, urllib.error.URLError):
        return "Could not reach the data source. Check your internet connection."
    if isinstance(e, TimeoutError):
        return "The request timed out. Try again."
    if "Authentication" in name or "401" in str(e):
        return ("TypeSafe rejected the API key (401). Stop the app with Ctrl+C, run ./start.sh again and "
                "paste the full key, with no spaces. Keys start with apikey_.")
    if "RateLimit" in name:
        return "TypeSafe is rate limiting requests. Wait a moment and try again."
    if isinstance(e, ValueError):
        return str(e)
    return f"{name}: {e}"
