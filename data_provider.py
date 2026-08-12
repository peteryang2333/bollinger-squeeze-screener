"""
data_provider.py — daily OHLCV fetch with local cache + provider fallback.

Design goals:
- Cheap to run daily: cache each ticker's full history in data/<TICKER>.csv and
  only refresh the last rows.
- Resilient: try yfinance (batched, fast) first; fall back to stockanalysis.com
  per-ticker API (the reliable endpoint we validated) if yfinance is blocked.
- No API keys required for the default path.

Usage:
    from data_provider import get_history
    df = get_history("GOOGL")   # DatetimeIndex, columns Open/High/Low/Close/Volume
"""

from __future__ import annotations
import os
import time
import json
import urllib.request
import ssl
import pandas as pd

CACHE_DIR = os.path.join(os.path.dirname(__file__), "data")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def _cache_path(ticker: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"{ticker.upper()}.csv")


def _save(df: pd.DataFrame, ticker: str):
    df.to_csv(_cache_path(ticker))


def _load_cache(ticker: str) -> pd.DataFrame | None:
    p = _cache_path(ticker)
    if os.path.exists(p):
        try:
            return pd.read_csv(p, index_col=0, parse_dates=True)
        except Exception:
            return None
    return None


# ---------- provider: stockanalysis.com (validated reliable fallback) ----------
def _fetch_stockanalysis(ticker: str, lookback_days: int = 400) -> pd.DataFrame | None:
    url = (f"https://stockanalysis.com/api/symbol/s/{ticker.lower()}"
           f"/history?range=2Y&period=Daily")
    try:
        req = urllib.request.Request(url, headers=UA)
        raw = json.loads(urllib.request.urlopen(req, timeout=20, context=CTX).read())
        rows = raw if isinstance(raw, list) else raw.get("data", [])
        if not rows:
            return None
        # API returns newest-first
        recs = []
        for r in rows:
            try:
                recs.append({
                    "Date": pd.to_datetime(r["t"]),
                    "Open": float(r["o"]), "High": float(r["h"]),
                    "Low": float(r["l"]), "Close": float(r["c"]),
                    "Volume": float(r.get("v", 0)),
                })
            except (KeyError, TypeError, ValueError):
                continue
        df = pd.DataFrame(recs).set_index("Date").sort_index()
        return df if len(df) > 30 else None
    except Exception as e:
        print(f"  [stockanalysis] {ticker} failed: {e}")
        return None


# ---------- provider: yfinance (correct mapping, works on CI/VM) ----------
_YF_DISABLED = False  # set True if yfinance rate-limits, so we stop retrying


def _fetch_yfinance_one(ticker: str) -> pd.DataFrame | None:
    global _YF_DISABLED
    if _YF_DISABLED:
        return None
    try:
        import yfinance as yf
        df = yf.Ticker(ticker).history(period="2y", interval="1d", auto_adjust=False)
        if df is None or df.empty:
            return None
        df = df.rename(columns=str.capitalize)
        return df[["Open", "High", "Low", "Close", "Volume"]]
    except Exception as e:
        if "RateLimit" in str(e) or "Too Many" in str(e):
            _YF_DISABLED = True
        print(f"  [yfinance] {ticker} failed: {e}")
        return None


def _fetch_yfinance_batch(tickers: list[str], chunk: int = 150) -> dict[str, pd.DataFrame]:
    """yfinance batched download, split into chunks to avoid oversized requests
    (a single 2,000+ ticker call tends to fail/timeout)."""
    global _YF_DISABLED
    out: dict[str, pd.DataFrame] = {}
    if _YF_DISABLED:
        return out
    try:
        import yfinance as yf
    except Exception as e:
        print(f"  [yfinance] not available: {e}")
        return out
    for i in range(0, len(tickers), chunk):
        grp = tickers[i:i + chunk]
        try:
            data = yf.download(grp, period="2y", interval="1d",
                               auto_adjust=False, progress=False, threads=False)
            if data is None or data.empty:
                continue
            if isinstance(data.columns, pd.MultiIndex):
                for t in grp:
                    try:
                        sub = data.xs(t, axis=1, level=1)
                        sub.columns = [c.capitalize() for c in sub.columns]
                        out[t.upper()] = sub[["Open", "High", "Low", "Close", "Volume"]]
                    except Exception:
                        continue
            else:
                out[grp[0].upper()] = data.rename(columns=str.capitalize)
        except Exception as e:
            if "RateLimit" in str(e) or "Too Many" in str(e):
                _YF_DISABLED = True
                print("  [yfinance] rate-limited — disabling for this run")
                break
            print(f"  [yfinance] chunk {i}-{i+len(grp)} failed: {e}")
    return out


def get_history(ticker: str, refresh: bool = True,
                max_age_hours: int = 20) -> pd.DataFrame | None:
    """
    Return a cached-or-fresh DataFrame of daily OHLCV.
    Source priority: yfinance (correct instrument mapping) -> stockanalysis (fallback).
    Refreshes from network only if cache is missing or older than max_age_hours.
    """
    cached = _load_cache(ticker)
    need_fetch = (cached is None)
    if cached is not None and refresh:
        age = (pd.Timestamp.now() - cached.index.max()).total_seconds() / 3600.0
        need_fetch = age > max_age_hours

    if need_fetch:
        df = _fetch_yfinance_one(ticker) or _fetch_stockanalysis(ticker)
        if df is not None and len(df) > 30:
            if cached is not None:
                df = pd.concat([cached, df]).drop_duplicates().sort_index()
            _save(df, ticker)
            return df
        return cached  # fall back to stale cache if both sources failed
    return cached


def get_histories(tickers: list[str], sleep_per: float = 0.2) -> dict[str, pd.DataFrame]:
    """Fetch many tickers: try a yfinance batch first, then fill any gaps
    directly with stockanalysis (no yfinance retry). Returns {TICKER: df}."""
    result: dict[str, pd.DataFrame] = {}
    batch = _fetch_yfinance_batch(tickers)
    for t in tickers:
        if t.upper() in batch:
            result[t.upper()] = batch[t.upper()]
    for t in tickers:
        if t.upper() in result:
            continue
        # gap-fill: use cached df if present, else stockanalysis (bypass yfinance
        # which already failed in the batch above)
        cached = _load_cache(t)
        df = cached if (cached is not None and len(cached) > 60) else _fetch_stockanalysis(t)
        if df is not None and len(df) > 60:
            _save(df, t)
            result[t.upper()] = df
        time.sleep(sleep_per)
    return result


if __name__ == "__main__":
    for t in ["GOOGL", "PANW", "APH", "CRCL"]:
        df = get_history(t)
        print(t, "rows:", (len(df) if df is not None else 0))
