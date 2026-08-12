"""
fundamentals.py — fetch company profile + key fundamentals for report enrichment.

Primary source : yfinance  Ticker.info  (rich: sector, industry, summary,
                valuation, growth, profitability, leverage).
Fallback source: stockanalysis.com  /api/symbol/s/<ticker>/overview  (profile only).

Results are cached to a persistent dir (env SQUEEZE_CACHE, else ./data) keyed by
ticker so daily runs are cheap and a single source outage degrades gracefully
instead of failing the whole report. All fields are best-effort: missing data
comes back as None and downstream scoring treats it as neutral.
"""

from __future__ import annotations
import os
import json
import time
import ssl
import urllib.request

import pandas as pd

HERE = os.path.dirname(__file__)
CACHE_DIR = os.environ.get("SQUEEZE_CACHE", os.path.join(HERE, "data"))
CACHE_MAX_AGE = 24 * 3600  # refresh once per day
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

# numeric keys we care about (everything else is treated as text/optional)
NUM_KEYS = (
    "marketCap", "pe", "forwardPE", "pb", "ps", "dividendYield",
    "profitMargin", "revenueGrowth", "earningsGrowth", "roe",
    "debtToEquity", "beta",
)


def _cache_path(ticker: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"fund_{ticker.upper()}.json")


def _load_cache(ticker: str):
    p = _cache_path(ticker)
    if os.path.exists(p):
        try:
            if time.time() - os.path.getmtime(p) < CACHE_MAX_AGE:
                with open(p) as f:
                    return json.load(f)
        except Exception:
            pass
    return None


def _save_cache(ticker: str, data: dict) -> None:
    try:
        with open(_cache_path(ticker), "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def _finfo(ticker: str) -> dict:
    import yfinance as yf
    info = yf.Ticker(ticker).info
    # yfinance sometimes returns dividendYield already in percent units
    # (e.g. 3.24 meaning 3.24%) instead of a decimal (0.0324). Normalize to
    # decimal so downstream scoring/display are consistent.
    dy = info.get("dividendYield")
    if isinstance(dy, (int, float)) and dy > 1:
        dy = dy / 100.0
    return {
        "name": info.get("shortName") or info.get("longName"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "summary": info.get("longBusinessSummary"),
        "marketCap": info.get("marketCap"),
        "pe": info.get("trailingPE"),
        "forwardPE": info.get("forwardPE"),
        "pb": info.get("priceToBook"),
        "ps": info.get("priceToSalesTrailing12Months"),
        "dividendYield": dy,
        "profitMargin": info.get("profitMargins"),
        "revenueGrowth": info.get("revenueGrowth"),
        "earningsGrowth": info.get("earningsGrowth"),
        "roe": info.get("returnOnEquity"),
        "debtToEquity": info.get("debtToEquity"),
        "beta": info.get("beta"),
    }


def _stockanalysis_fallback(ticker: str) -> dict:
    try:
        url = f"https://stockanalysis.com/api/symbol/s/{ticker.lower()}/overview"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=15, context=CTX).read().decode()
        j = json.loads(raw)
        d = j.get("data", j) if isinstance(j, dict) else {}
        return {
            "name": d.get("name") or d.get("shortName"),
            "sector": d.get("sector"),
            "industry": d.get("industry"),
            "summary": d.get("description") or d.get("profile"),
        }
    except Exception as e:
        print(f"  [fund] {ticker} stockanalysis fallback failed: {e}")
        return {}


def get_fundamentals(ticker: str) -> dict:
    cached = _load_cache(ticker)
    if cached:
        return cached

    data: dict = {}
    try:
        data = _finfo(ticker)
        # yfinance sometimes returns an empty shell on a bad lookup
        if not (data.get("name") or data.get("sector") or data.get("marketCap")):
            data = {}
    except Exception as e:
        print(f"  [fund] {ticker} yfinance failed: {e}")
        data = {}

    # if profile missing, try the lighter fallback for at least name/sector/summary
    if not (data.get("name") or data.get("sector")):
        data.update({k: v for k, v in _stockanalysis_fallback(ticker).items() if v})

    # sanitize: a 0 PE/PB/PS means "no meaningful ratio" -> treat as missing
    for k in ("pe", "forwardPE", "pb", "ps"):
        v = data.get(k)
        if v is None or (isinstance(v, float) and (pd.isna(v) or v <= 0)):
            data[k] = None
    for k in NUM_KEYS:
        v = data.get(k)
        if isinstance(v, float) and pd.isna(v):
            data[k] = None

    _save_cache(ticker, data)
    return data


if __name__ == "__main__":
    import sys
    for t in (sys.argv[1:] or ["RAMP", "USB", "SBUX"]):
        print(t, get_fundamentals(t))
