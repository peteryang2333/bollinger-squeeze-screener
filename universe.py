"""
universe.py — build the list of tickers to scan.

Default live universe (reliable, scraped from Wikipedia):
- watchlist.txt : your hand-picked names (one per line). Highest priority.
- S&P 500       : large caps
- S&P 400       : mid caps      -> 500 + 400 = "S&P 900"
- S&P 600       : small caps    -> 900 + 600 = "S&P 1500" (our default pool)

Nasdaq-100 and Russell-2000 loaders are kept but DISABLED by default: their
free Wikipedia/stockanalysis sources are currently unreliable (Wikipedia
restructured those pages; stockanalysis returns 404). Add a curated list or a
paid index-constituents feed if you want them back. The S&P 1500 pool already
covers large + mid + small caps and comfortably exceeds the "S&P 900" minimum.
"""

from __future__ import annotations
import os
import json
import time
import urllib.request
import ssl
import pandas as pd
from io import StringIO

HERE = os.path.dirname(__file__)
WATCHLIST = os.path.join(HERE, "watchlist.txt")
CACHE = os.path.join(HERE, "data", "universe_cache.json")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
CACHE_MAX_AGE_DAYS = 7


def load_watchlist() -> list[str]:
    if not os.path.exists(WATCHLIST):
        return []
    with open(WATCHLIST) as f:
        return [l.strip().upper() for l in f if l.strip() and not l.startswith("#")]


def _scrape_table(url: str, symbol_cols: list[str]) -> list[str]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    html = urllib.request.urlopen(req, timeout=15, context=CTX).read().decode()
    tables = pd.read_html(StringIO(html))
    for t in tables:
        for col in symbol_cols:
            if col in t.columns:
                return t[col].astype(str).str.replace(".", "-", regex=False).tolist()
    return []


def load_sp500() -> list[str]:
    try:
        return _scrape_table("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                             ["Symbol", "Ticker symbol"])
    except Exception as e:
        print(f"  [universe] S&P500 scrape failed: {e}")
        return []


def load_sp400() -> list[str]:
    try:
        return _scrape_table("https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
                             ["Symbol", "Ticker symbol"])
    except Exception as e:
        print(f"  [universe] S&P400 scrape failed: {e}")
        return []


def load_sp600() -> list[str]:
    try:
        return _scrape_table("https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
                             ["Symbol", "Ticker symbol"])
    except Exception as e:
        print(f"  [universe] S&P600 scrape failed: {e}")
        return []


def load_nasdaq100() -> list[str]:
    try:
        return _scrape_table("https://en.wikipedia.org/wiki/Nasdaq-100", ["Ticker", "Ticker Symbol"])
    except Exception as e:
        print(f"  [universe] Nasdaq100 scrape failed: {e}")
        return []


def load_russell2000() -> list[str]:
    try:
        return _scrape_table("https://en.wikipedia.org/wiki/List_of_Russell_2000_companies",
                             ["Symbol", "Ticker symbol"])
    except Exception as e:
        print(f"  [universe] Russell2000 scrape failed: {e}")
        return []


def _load_cache() -> dict:
    if os.path.exists(CACHE):
        try:
            with open(CACHE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_cache(data: dict):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "w") as f:
        json.dump(data, f)


def _get(name: str, loader) -> list[str]:
    """Load an index list: live scrape -> recent cache -> empty."""
    fresh = loader()
    cache = _load_cache()
    if fresh:
        cache[name] = {"ts": time.time(), "syms": fresh}
        _save_cache(cache)
        return fresh
    # scrape failed: use cache if fresh enough
    entry = cache.get(name)
    if entry and (time.time() - entry["ts"]) < CACHE_MAX_AGE_DAYS * 86400:
        print(f"  [universe] using cached {name} ({len(entry['syms'])} syms)")
        return entry["syms"]
    print(f"  [universe] {name} unavailable and no fresh cache -> skipped")
    return []


def build_universe(use_watchlist: bool = True, use_sp500: bool = True,
                   use_sp400: bool = True, use_sp600: bool = True,
                   use_nasdaq100: bool = False, use_russell2000: bool = False,
                   extra: list[str] = None) -> list[str]:
    out: list[str] = []
    if use_watchlist:
        out += load_watchlist()
    if use_sp500:
        out += _get("sp500", load_sp500)
    if use_sp400:
        out += _get("sp400", load_sp400)
    if use_sp600:
        out += _get("sp600", load_sp600)
    if use_nasdaq100:
        out += _get("nasdaq100", load_nasdaq100)
    if use_russell2000:
        out += _get("russell2000", load_russell2000)
    if extra:
        out += extra
    # de-dup, keep order
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


if __name__ == "__main__":
    u = build_universe()
    print(f"universe size: {len(u)}")
    print("watchlist sample:", load_watchlist()[:20])
