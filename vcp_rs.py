"""
vcp_rs.py — VCP (Volatility Contraction Pattern) + Relative Strength (RS) ratings.

Ported/adapted from SENTINEL PRO
(github.com/Ericproof/US-stock-rightsdie-scanner, MIT licensed). Two complementary
Minervini-style screens that fuse naturally with the Bollinger/TTM squeeze scanner:

1. vcp_analyze(df) -> dict
   Quantifies "price contraction" as a 105-point score:
     - Tightness (40pt): high-low ranges over 20/30/40/60d are shrinking
     - Volume   (30pt): recent volume dried up vs the 60d average
     - MA Align (30pt): price > MA50 > MA150 > MA200  (perfect order, rising)
     - Pivot    ( 5pt): price sits near its 20/50d pivot high
   Also returns a breakout readiness "status" (ACTION / WAIT / EXTENDED) measured
   against the 20-day pivot high (SENTINEL convention).

2. RSAnalyzer
     rs_raw_score(close)       -> float  weighted 12/6/3/1-month return
     assign_rs_percentiles(recs) -> mutates in place, adds 'rs_rating' (1..100,
                                   100 = strongest) cross-sectionally

Pure functions: pandas + numpy only. No network. Requires >=130 bars of OHLCV
(for the 60-day range window + MA200).
"""

from __future__ import annotations
import numpy as np
import pandas as pd

RS_INVALID = -999.0

# candidate thresholds (tunable)
MIN_VCP_SCORE = 60       # tight contraction + dry-up strength
MIN_VCP_MA = 20          # require trend alignment (price>MA50>MA150>MA200)
MIN_RS_RATING = 50       # VCP candidates must be at/above median RS


# ---------------------------------------------------------------------------
# Relative Strength
# ---------------------------------------------------------------------------
def rs_raw_score(close: pd.Series) -> float:
    """Weighted trailing return: 12m*0.4 + 6m*0.2 + 3m*0.2 + 1m*0.2.

    Gracefully falls back to the full available history when <252 bars exist.
    Returns RS_INVALID if fewer than 21 bars (cannot compute even 1-month)."""
    try:
        c = close
        if len(c) < 21:
            return RS_INVALID
        r12 = (c.iloc[-1] / c.iloc[-252] - 1) if len(c) >= 252 else (c.iloc[-1] / c.iloc[0] - 1)
        r6 = (c.iloc[-1] / c.iloc[-126] - 1) if len(c) >= 126 else (c.iloc[-1] / c.iloc[0] - 1)
        r3 = (c.iloc[-1] / c.iloc[-63] - 1) if len(c) >= 63 else (c.iloc[-1] / c.iloc[0] - 1)
        r1 = (c.iloc[-1] / c.iloc[-21] - 1) if len(c) >= 21 else (c.iloc[-1] / c.iloc[0] - 1)
        return (r12 * 0.4) + (r6 * 0.2) + (r3 * 0.2) + (r1 * 0.2)
    except Exception:
        return RS_INVALID


def assign_rs_percentiles(records: list[dict]) -> None:
    """Given records each carrying a numeric 'raw_rs', sort ascending and stamp
    'rs_rating' (1..100, 100 = strongest). Mutates in place. Records without a
    valid raw_rs are left untouched (no rs_rating key)."""
    valid = [r for r in records
             if r.get("raw_rs") is not None and r["raw_rs"] != RS_INVALID]
    if not valid:
        return
    valid.sort(key=lambda x: x["raw_rs"])
    total = len(valid)
    if total == 1:
        valid[0]["rs_rating"] = 50
        return
    for i, item in enumerate(valid):
        # IBD-style percentile: weakest=1, strongest=100, robust for any n
        item["rs_rating"] = int((i / (total - 1)) * 99) + 1


# ---------------------------------------------------------------------------
# VCP (Volatility Contraction Pattern)
# ---------------------------------------------------------------------------
def vcp_analyze(df: pd.DataFrame) -> dict:
    """Minervini VCP scoring (105-pt). Returns a dict; _empty() on <130 rows
    or any numeric failure so a bad ticker never breaks the scan."""
    try:
        if df is None or len(df) < 130:
            return _empty()
        close = df["Close"]
        high = df["High"]
        low = df["Low"]
        volume = df["Volume"]

        # ATR(14)
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ], axis=1).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-1])
        if pd.isna(atr) or atr <= 0:
            return _empty()

        # 1) Tightness (40pt) — high-low range over 20/30/40/60d shrinking
        periods = [20, 30, 40, 60]
        ranges = []
        for p in periods:
            h = float(high.iloc[-p:].max())
            l = float(low.iloc[-p:].min())
            ranges.append((h - l) / h)
        curr_range = ranges[0]
        avg_range = float(np.mean(ranges[:3]))           # 20/30/40 mean
        is_contracting = ranges[0] < ranges[1] < ranges[2]

        if avg_range < 0.10:
            tight_score = 40
        elif avg_range < 0.15:
            tight_score = 30
        elif avg_range < 0.20:
            tight_score = 20
        elif avg_range < 0.28:
            tight_score = 10
        else:
            tight_score = 0
        if is_contracting:
            tight_score += 5
        tight_score = min(40, tight_score)

        # 2) Volume (30pt) — recent volume dried up vs 60d average
        v20_avg = float(volume.iloc[-20:].mean())
        v60_avg = float(volume.iloc[-60:-40].mean())
        if pd.isna(v20_avg) or pd.isna(v60_avg):
            return _empty()
        v_ratio = v20_avg / v60_avg if v60_avg > 0 else 1.0
        if v_ratio < 0.45:
            vol_score = 30
        elif v_ratio < 0.60:
            vol_score = 25
        elif v_ratio < 0.75:
            vol_score = 15
        else:
            vol_score = 0
        is_dryup = v_ratio < 0.75

        # 3) MA Alignment (30pt) — price > MA50 > MA150 > MA200
        ma50 = float(close.rolling(50).mean().iloc[-1])
        ma150 = float(close.rolling(150).mean().iloc[-1])
        ma200 = float(close.rolling(200).mean().iloc[-1])
        price = float(close.iloc[-1])
        ma_score = 0
        if price > ma50:
            ma_score += 10
        if ma50 > ma150:
            ma_score += 10
        if ma150 > ma200:
            ma_score += 10

        # 4) Pivot Bonus (5pt) — price near 50d pivot high
        pivot50 = float(high.iloc[-50:].max())
        dist50 = (pivot50 - price) / pivot50
        pivot_bonus = 0
        if 0 <= dist50 <= 0.04:
            pivot_bonus = 5
        elif 0.04 < dist50 <= 0.08:
            pivot_bonus = 3

        score = int(min(105, tight_score + vol_score + ma_score + pivot_bonus))

        signals = []
        if tight_score >= 35:
            signals.append("Tight Base (VCP)")
        if is_contracting:
            signals.append("V-Contraction")
        if is_dryup:
            signals.append("Volume Dry-up")
        if ma_score >= 20:
            signals.append("Trend Alignment")
        if pivot_bonus > 0:
            signals.append("Near Pivot")

        # breakout-readiness status vs 20d pivot high (SENTINEL convention)
        pivot20 = float(high.iloc[-20:].max())
        dist20 = (price - pivot20) / pivot20
        if -0.05 <= dist20 <= 0.03:
            status = "ACTION"
        elif dist20 < -0.05:
            status = "WAIT"
        else:
            status = "EXTENDED"

        return {
            "score": score,
            "atr": round(atr, 2),
            "signals": signals,
            "is_dryup": is_dryup,
            "range_pct": round(float(curr_range), 4),
            "vol_ratio": round(float(v_ratio), 2),
            "dist_to_pivot_pct": round(float(dist20) * 100, 2),
            "status": status,
            "breakdown": {"tight": tight_score, "vol": vol_score,
                          "ma": ma_score, "pivot": pivot_bonus},
        }
    except Exception:
        return _empty()


def _empty() -> dict:
    return {
        "score": 0, "atr": 0.0, "signals": [], "is_dryup": False,
        "range_pct": 0.0, "vol_ratio": 1.0, "dist_to_pivot_pct": 0.0,
        "status": "—",
        "breakdown": {"tight": 0, "vol": 0, "ma": 0, "pivot": 0},
    }


if __name__ == "__main__":
    # quick self-test: a tightening base that contracts then sits near highs
    import pandas as pd
    n = 220
    rng = np.random.default_rng(1)
    base = 100 + np.linspace(0, 18, n)            # gentle uptrend
    # add shrinking noise: first half wide, second half tight
    noise = np.concatenate([rng.normal(0, 3.0, 110), rng.normal(0, 0.6, 110)])
    price = base + noise
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    df = pd.DataFrame({
        "Open": price, "High": price + np.abs(rng.normal(0, 0.8, n)),
        "Low": price - np.abs(rng.normal(0, 0.8, n)), "Close": price,
        "Volume": np.concatenate([np.full(110, 2_000_000.0),
                                  np.full(110, 600_000.0)]),
    }, index=idx)
    v = vcp_analyze(df)
    print("VCP score:", v["score"], "status:", v["status"], "breakdown:", v["breakdown"])
    rs = rs_raw_score(df["Close"])
    print("RS raw:", round(rs, 4))
