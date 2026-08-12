"""
screener.py — orchestrates the daily squeeze scan and writes reports.

Outputs (into results/):
- squeeze_<YYYY-MM-DD>.json   machine-readable, full signal per ticker
- squeeze_<YYYY-MM-DD>.md     human-readable ranked watchlist for "next day"

Ranking logic:
A name is a *candidate* if EITHER:
  (a) bb_squeeze is True  (BB-width in lowest percentile of its own history), OR
  (b) ttm_squeeze_on is True (Bollinger fully inside Keltner).
We then rank by a composite "squeeze score":
  - tighter BB-width => higher
  - TTM on => bonus
  - low recent price volatility => bonus
  - adequate liquidity (volume / price > threshold) => required, else dropped
Direction hint comes from momentum histogram (mom>0 => upward bias).
"""

from __future__ import annotations
import os
import json
import datetime as dt
import numpy as np
import pandas as pd

from squeeze_core import compute, squeeze_signal
from data_provider import get_histories
from universe import build_universe

HERE = os.path.dirname(__file__)
RESULTS = os.path.join(HERE, "results")
os.makedirs(RESULTS, exist_ok=True)

# ---- tunables (mirror config.yaml) ----
MIN_PRICE = 5.0
MIN_AVG_VOLUME = 500_000      # shares/day, skip illiquid
MIN_DAYS = 120
TOP_N = 40


def _score(row: pd.Series) -> float:
    s = 0.0
    # BB-width percentile: lower => more squeezed (pctile 100 == lowest bucket)
    pctile = row["bb_width_pctile"]
    if pd.notna(pctile):
        s += pctile / 100.0 * 70.0          # up to 70 pts (continuous rank)
    if bool(row["ttm_squeeze_on"]):
        s += 25.0                           # TTM stricter bonus
    # narrower absolute width helps break ties
    s += max(0.0, (0.10 - row["bb_width"]) / 0.10) * 15.0
    return round(s, 2)


def run(universe: list[str] | None = None, top_n: int = TOP_N) -> list[dict]:
    if universe is None:
        # mirrors config.yaml — broad market pool per user decision
        universe = build_universe(use_watchlist=True, use_sp500=True,
                                   use_nasdaq100=True, use_russell2000=True)
    print(f"[screener] universe={len(universe)}")

    histories = get_histories(universe)
    print(f"[screener] fetched={len(histories)}")

    results: list[dict] = []
    for tk, df in histories.items():
        if len(df) < MIN_DAYS:
            continue
        if df["Close"].iloc[-1] < MIN_PRICE:
            continue
        avg_vol = df["Volume"].tail(50).mean()
        if avg_vol < MIN_AVG_VOLUME:
            continue
        try:
            out = compute(df)
        except Exception as e:
            print(f"  [compute] {tk} error: {e}")
            continue
        row = out.iloc[-1]
        sig = squeeze_signal(row)
        if not (sig["bb_squeeze"] or sig["ttm_squeeze_on"]):
            continue
        rec = {
            "ticker": tk,
            "score": _score(row),
            "sector": "",  # filled by caller if available
            "signal": sig,
            "avg_volume_50d": int(avg_vol),
        }
        results.append(rec)

    results.sort(key=lambda r: r["score"], reverse=True)
    results = results[:top_n]
    return results


def write_html_report(results: list[dict], asof: str) -> str:
    """Render a self-contained, readable HTML page (the "morning view")."""
    html_path = os.path.join(RESULTS, f"squeeze_{asof}.html")
    rows = []
    for i, r in enumerate(results, 1):
        s = r["signal"]
        dirn = "▲ up" if (s["mom"] or 0) > 0 else ("▼ dn" if (s["mom"] or 0) < 0 else "—")
        cls = "up" if (s["mom"] or 0) > 0 else ("dn" if (s["mom"] or 0) < 0 else "flat")
        ttm = '<span class="badge">TTM</span>' if s["ttm_squeeze_on"] else ""
        fired = '<span class="badge fire">FIRED</span>' if (s.get("ttm_fired_up")) else ""
        rows.append(
            f"<tr><td>{i}</td><td class='tk'>{r['ticker']}</td><td>{r['score']}</td>"
            f"<td>{s['close']}</td><td>{s['bb_width_pct']}%</td>"
            f"<td>{s['bb_width_pctile']}</td><td>{ttm}{fired}</td>"
            f"<td>{s['mom']}</td><td>{'Y' if s['above_ma20'] else 'N'}</td>"
            f"<td class='{cls}'>{dirn}</td></tr>"
        )
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bollinger/TTM Squeeze — {asof}</title>
<style>
 body{{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
   background:#0f1115;color:#e6e6e6;margin:0;padding:24px;}}
 h1{{font-size:20px;margin:0 0 4px}} .meta{{color:#8b8b8b;font-size:13px;margin-bottom:16px}}
 table{{border-collapse:collapse;width:100%;font-size:13px}}
 th,td{{padding:8px 10px;text-align:left;border-bottom:1px solid #23262d}}
 th{{color:#9aa0a6;font-weight:600;position:sticky;top:0;background:#0f1115}}
 tr:hover{{background:#181b21}} .tk{{font-weight:700;color:#7dd3fc}}
 .up{{color:#f87171}} .dn{{color:#4ade80}} .flat{{color:#9aa0a6}}
 .badge{{display:inline-block;background:#1e3a5f;color:#7dd3fc;border-radius:4px;
   padding:1px 6px;font-size:11px;margin-right:4px}}
 .badge.fire{{background:#5b2330;color:#fca5a5}}
 .note{{margin-top:18px;color:#8b8b8b;font-size:12px;line-height:1.6}}
</style></head><body>
<h1>Bollinger / TTM Squeeze Scan</h1>
<div class="meta">as-of {asof} · {len(results)} candidates · generated {dt.datetime.now():%Y-%m-%d %H:%M}</div>
<table><thead><tr>
<th>#</th><th>Ticker</th><th>Score</th><th>Price</th><th>BB-W%</th>
<th>BB-Pctile</th><th>TTM</th><th>Mom%</th><th>&gt;MA20</th><th>Dir</th>
</tr></thead><tbody>{''.join(rows)}</tbody></table>
<div class="note">
 A squeeze only says <b>volatility will expand</b> — NOT the direction.<br>
 Confirm with momentum (Mom&gt;0 = upward bias) + volume + your MA/structure rules before entry.<br>
 Next step: open the top names on your chart, look for the squeeze <b>release</b>
 (BB re-emerging from Keltner) on volume, ideally with a pullback to MA20.<br>
 Research only — <b>not investment advice</b>. All signals are machine-generated.
</div></body></html>"""
    with open(html_path, "w") as f:
        f.write(html)
    return html_path


def write_reports(results: list[dict], asof: str) -> tuple[str, str, str]:
    json_path = os.path.join(RESULTS, f"squeeze_{asof}.json")
    md_path = os.path.join(RESULTS, f"squeeze_{asof}.md")
    html_path = write_html_report(results, asof)
    with open(json_path, "w") as f:
        json.dump({"asof": asof, "count": len(results), "results": results}, f, indent=2)

    lines = [f"# Bollinger / TTM Squeeze Scan — {asof}",
             "",
             f"*Candidates: {len(results)} · generated {dt.datetime.now():%Y-%m-%d %H:%M}*",
             "",
             "Ranked by squeeze tightness (lower BB-width percentile + TTM-on bonus).",
             "**A squeeze only says volatility will expand — NOT the direction.**",
             "Confirm with momentum (mom>0 = upward bias) + volume + your MA/structure rules before entry.",
             "",
             "| # | Ticker | Score | Price | BB-W% | BB-Pctile | TTM | Mom | Above MA20 | Dir |",
             "|---|--------|-------|-------|------|-----------|-----|-----|-----------|-----|"]
    for i, r in enumerate(results, 1):
        s = r["signal"]
        dirn = "▲up" if (s["mom"] or 0) > 0 else ("▼dn" if (s["mom"] or 0) < 0 else "—")
        lines.append(
            f"| {i} | {r['ticker']} | {r['score']} | {s['close']} | {s['bb_width_pct']} | "
            f"{s['bb_width_pctile']} | {'Y' if s['ttm_squeeze_on'] else '—'} | "
            f"{s['mom']} | {'Y' if s['above_ma20'] else 'N'} | {dirn} |"
        )
    lines += ["", "---", "Sourced from public market data. Research only — not investment advice.",
              "Next step: open the top names on your chart, look for the squeeze *release*",
              "(BB re-emerging from Keltner) on volume, ideally with a pullback to MA20."]
    with open(md_path, "w") as f:
        f.write("\n".join(lines))
    return json_path, md_path, html_path


if __name__ == "__main__":
    asof = dt.date.today().isoformat()
    res = run()
    jp, mp, hp = write_reports(res, asof)
    print(f"[screener] wrote {jp}\n           {mp}\n           {hp}\n[top 10]")
    for r in res[:10]:
        print(f"  {r['ticker']:6} score={r['score']:5}  price={r['signal']['close']:>9}  "
              f"bbw%={r['signal']['bb_width_pct']:>5}  ttm={r['signal']['ttm_squeeze_on']}")
