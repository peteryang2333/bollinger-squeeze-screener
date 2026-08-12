"""
squeeze_core.py — Bollinger Band squeeze indicators.

Two complementary definitions of "squeeze" are implemented:

1. BB-WIDTH CONTRACTION (what most people mean by "布林带挤压到极致")
   Band width = (upper - lower) / middle, where upper/lower are the
   20-day Bollinger Bands (SMA20 +/- k*std20).
   A squeeze is flagged when current BB-width is in the lowest percentile
   of its own trailing distribution (default: <= 20th percentile of 100 days).
   This detects *volatility contraction* regardless of direction.

2. TTM SQUEEZE (John Carter) — stricter, directional-aware
   Squeeze is "on" when the Bollinger Bands are completely INSIDE the
   Keltner Channels (EMA20 +/- m*ATR). When BB re-emerges outside KC,
   the squeeze "fires" and the momentum histogram tells you the likely
   breakout direction.

Reference implementation (canonical): https://github.com/vegatek/ttm-squeeze
This module generalizes it and adds a percentile-based BB-width rank so we
can screen hundreds of names and rank them by "how squeezed".

Pure functions: pandas + numpy only. No network.
"""

from __future__ import annotations
import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

# ----- default parameters (overridable via config.yaml) -----
BB_PERIOD = 20
BB_STD = 2.0
KC_PERIOD = 20
KC_ATR_PERIOD = 20
KC_MULT = 1.5
MOM_PERIOD = 20          # momentum histogram lookback
BW_LOOKBACK = 100        # trailing window to rank BB-width percentile
BW_SQUEEZE_PCTILE = 20   # BB-width <= this percentile => squeezed


def bollinger_bands(close: pd.Series, period: int = BB_PERIOD, std: float = BB_STD):
    mid = close.rolling(period).mean()
    sd = close.rolling(period).std(ddof=0)
    upper = mid + std * sd
    lower = mid - std * sd
    width = (upper - lower) / mid          # normalized band width
    return mid, upper, lower, width


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)


def momentum_hist(close: pd.Series, period: int = MOM_PERIOD) -> pd.Series:
    """Linear-regression slope momentum, normalized to % of price.
    Positive => upward drift over the window (breakout bias up),
    negative => downward drift. Normalized so values are comparable
    across stocks of very different price levels.

    Fully vectorized (sliding-window weighted regression) so it scales
    to thousands of tickers without pandas rolling-apply overhead."""
    y = np.asarray(close, dtype=float)
    n = period
    x = np.arange(n, dtype=float)
    xbar = x.mean()
    denom = ((x - xbar) ** 2).sum()          # constant for fixed n
    sw = sliding_window_view(y, n)           # (L-n+1, n): window[i] = y[i:i+n]
    sum_y = sw.sum(axis=1)
    sum_xy = (sw * x).sum(axis=1)            # newest bar gets weight n-1
    slope = (sum_xy - xbar * sum_y) / denom  # OLS slope = cov(x,y)/var(x)
    last = sw[:, -1]
    mom = slope / last * 100.0
    out = np.full(len(y), np.nan)
    out[n - 1:] = mom
    return pd.Series(out, index=close.index)


def compute(df: pd.DataFrame,
            bb_period: int = BB_PERIOD, bb_std: float = BB_STD,
            kc_period: int = KC_PERIOD, kc_atr: int = KC_ATR_PERIOD, kc_mult: float = KC_MULT,
            mom_period: int = MOM_PERIOD,
            bw_lookback: int = BW_LOOKBACK, bw_pctile: int = BW_SQUEEZE_PCTILE) -> pd.DataFrame:
    """
    df must contain columns: Open, High, Low, Close (and a DatetimeIndex).
    Returns df with all indicator columns appended.
    """
    close = df["Close"]
    d = pd.DataFrame(index=df.index)

    mid, upper, lower, width = bollinger_bands(close, bb_period, bb_std)
    d["bb_mid"] = mid
    d["bb_upper"] = upper
    d["bb_lower"] = lower
    d["bb_width"] = width

    # Keltner (needs High/Low for ATR)
    kc_mid = close.ewm(span=kc_period, adjust=False).mean()
    atr = true_range(df["High"], df["Low"], close).rolling(kc_atr).mean()
    d["kc_mid"] = kc_mid
    d["kc_upper"] = kc_mid + kc_mult * atr
    d["kc_lower"] = kc_mid - kc_mult * atr

    # TTM squeeze: BB fully inside KC
    d["ttm_squeeze_on"] = (d["bb_lower"] > d["kc_lower"]) & (d["bb_upper"] < d["kc_upper"])

    # momentum histogram
    d["mom"] = momentum_hist(close, mom_period)

    # BB-width percentile rank over trailing window (continuous 0-100).
    # 100 = tightest in window (== rolling min of width), 0 = widest.
    # Computed by range-normalizing against the trailing min/max so it is
    # fully vectorized and fast for large universes.
    rw_max = width.rolling(bw_lookback).max()
    rw_min = width.rolling(bw_lookback).min()
    span = rw_max - rw_min
    pctile = np.where(span > 0, (rw_max - width) / span * 100.0, 100.0)
    d["bb_width_pctile"] = pctile
    d["bb_squeeze"] = width.rolling(bw_lookback).min().eq(width) | (d["bb_width_pctile"] <= bw_pctile)

    # "fire" events (squeeze just released) — useful for the daily report
    d["ttm_fired"] = d["ttm_squeeze_on"].shift(1) & (~d["ttm_squeeze_on"])
    d["ttm_fired_up"] = d["ttm_fired"] & (d["mom"] > 0)
    d["ttm_fired_dn"] = d["ttm_fired"] & (d["mom"] < 0)

    return pd.concat([df, d], axis=1)


def squeeze_signal(row: pd.Series) -> dict:
    """Summarize the latest bar into a compact signal dict."""
    return {
        "close": round(float(row["Close"]), 2),
        "bb_width_pct": round(float(row["bb_width"]) * 100, 2),
        "bb_width_pctile": (None if pd.isna(row["bb_width_pctile"])
                            else int(row["bb_width_pctile"])),
        "bb_squeeze": bool(row["bb_squeeze"]),
        "ttm_squeeze_on": bool(row["ttm_squeeze_on"]),
        "ttm_fired_up": bool(row.get("ttm_fired_up", False)),
        "ttm_fired_dn": bool(row.get("ttm_fired_dn", False)),
        "mom": (None if pd.isna(row["mom"]) else round(float(row["mom"]), 3)),
        "dist_to_bb_upper_pct": round(float((row["bb_upper"] - row["Close"]) / row["Close"] * 100), 2),
        "above_ma20": bool(row["Close"] > row["bb_mid"]),
    }


if __name__ == "__main__":
    # quick self-test on synthetic data: a flat consolidation then a pop
    import numpy as np
    n = 130
    rng = np.random.default_rng(0)
    price = np.concatenate([
        np.ones(60) * 100 + rng.normal(0, 0.5, 60),   # tight coil
        np.linspace(100, 130, 70),                     # expansion
    ])
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    df = pd.DataFrame({"Open": price, "High": price + 1, "Low": price - 1, "Close": price}, index=idx)
    out = compute(df)
    print("last bb_width_pct:", round(out['bb_width'].iloc[-1]*100, 2))
    print("ttm_squeeze_on at coil (row 55):", bool(out['ttm_squeeze_on'].iloc[55]))
    print("ttm_fired_up near breakout:", bool(out['ttm_fired_up'].iloc[62]))
