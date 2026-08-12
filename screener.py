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
import time
import datetime as dt
import numpy as np
import pandas as pd

from squeeze_core import compute, squeeze_signal
from data_provider import get_histories
from universe import build_universe
from fundamentals import get_fundamentals
from multidim import multidim_scores

HERE = os.path.dirname(__file__)
RESULTS = os.path.join(HERE, "results")
os.makedirs(RESULTS, exist_ok=True)

# ---- tunables (mirror config.yaml) ----
MIN_PRICE = 5.0
MIN_AVG_VOLUME = 500_000      # shares/day, skip illiquid
MIN_DAYS = 120
TOP_N = 40
ENRICH_TOP_N = 40          # top candidates get company profile + multi-dim score
FUND_SLEEP = 0.15          # politeness delay between fundamentals fetches


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
                                   use_sp400=True, use_sp600=True,
                                   use_nasdaq100=False, use_russell2000=False)
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

    # enrich top candidates with company profile + multi-dimensional score
    for r in results[:ENRICH_TOP_N]:
        try:
            f = get_fundamentals(r["ticker"])
            r["fundamentals"] = f
            r["multidim"] = multidim_scores(f, r["signal"], r["score"])
        except Exception as e:
            print(f"  [enrich] {r['ticker']} failed: {e}")
            r["fundamentals"] = {}
            r["multidim"] = {"dims": {}, "composite": None,
                             "narrative": "", "intro": {}, "fund_missing": True}
        time.sleep(FUND_SLEEP)
    return results


def _fmt_beta(b):
    return "—" if b is None else f"{b:.1f}"


def _bar(label: str, val):
    if val is None:
        return (f'<div class="barrow"><span class="bl">{label}</span>'
                f'<span class="btxt miss">缺失</span></div>')
    color = "#7dd3fc" if val >= 75 else ("#fbbf24" if val >= 60 else "#f87171")
    return (f'<div class="barrow"><span class="bl">{label}</span>'
            f'<span class="btrack"><span class="bfill" style="width:{val:.0f}%;'
            f'background:{color}"></span></span>'
            f'<span class="bval">{val:.0f}</span></div>')


def write_html_report(results: list[dict], asof: str) -> str:
    """Render a self-contained, readable HTML page (the "morning view")."""
    html_path = os.path.join(RESULTS, f"squeeze_{asof}.html")
    rows = []
    for i, r in enumerate(results, 1):
        s = r["signal"]
        md = r.get("multidim") or {}
        intro = md.get("intro") or {}
        dims = md.get("dims") or {}
        composite = md.get("composite")
        dirn = "▲ up" if (s["mom"] or 0) > 0 else ("▼ dn" if (s["mom"] or 0) < 0 else "—")
        cls = "up" if (s["mom"] or 0) > 0 else ("dn" if (s["mom"] or 0) < 0 else "flat")
        ttm = '<span class="badge">TTM</span>' if s["ttm_squeeze_on"] else ""
        fired = '<span class="badge fire">FIRED</span>' if (s.get("ttm_fired_up")) else ""
        comp = f"{composite:.0f}" if composite is not None else "—"

        rows.append(
            f"<tr class='main' onclick=\"tgl('d{i}')\">"
            f"<td>{i}</td><td class='tk'>{r['ticker']}</td>"
            f"<td>{r['score']}</td><td class='comp'>{comp}</td>"
            f"<td>{s['close']}</td><td>{s['bb_width_pct']}%</td>"
            f"<td>{s['bb_width_pctile']}</td><td>{ttm}{fired}</td>"
            f"<td>{s['mom']}</td><td>{'Y' if s['above_ma20'] else 'N'}</td>"
            f"<td class='{cls}'>{dirn}</td></tr>"
        )

        bars = "".join([
            _bar("挤压 Squeeze", dims.get("squeeze")),
            _bar("动量 Momentum", dims.get("momentum")),
            _bar("质量 Quality", dims.get("quality")),
            _bar("估值 Value", dims.get("value")),
            _bar("成长 Growth", dims.get("growth")),
        ])
        summary = intro.get("summary") or "（无业务概况）"
        rows.append(
            f"<tr class='detail' id='d{i}' style='display:table-row'><td colspan='11'>"
            f"<div class='card'>"
            f"<div class='col'><h3>公司基本介绍</h3>"
            f"<div class='kv'><span>名称</span><b>{intro.get('name') or r['ticker']}</b></div>"
            f"<div class='kv'><span>代码</span><b>{r['ticker']}</b></div>"
            f"<div class='kv'><span>板块</span><b>{intro.get('sector','—')} / {intro.get('industry','—')}</b></div>"
            f"<div class='kv'><span>市值</span><b>{intro.get('market_cap','—')}</b></div>"
            f"<div class='kv'><span>Beta</span><b>{_fmt_beta(intro.get('beta'))}</b></div>"
            f"<p class='summary'>{summary}</p></div>"
            f"<div class='col'><h3>基本面观点</h3>"
            f"<p class='view'>{md.get('narrative') or '（数据缺失）'}</p>"
            f"<h3>多维度打分（0–100，越高越好）</h3>{bars}"
            f"<div class='compbox'>综合分 <b>{comp}</b> / 100</div></div>"
            f"</div></td></tr>"
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
 tr.main{{cursor:pointer}} tr.main:hover{{background:#181b21}}
 .tk{{font-weight:700;color:#7dd3fc}} .comp{{font-weight:700;color:#fbbf24}}
 .up{{color:#f87171}} .dn{{color:#4ade80}} .flat{{color:#9aa0a6}}
 .badge{{display:inline-block;background:#1e3a5f;color:#7dd3fc;border-radius:4px;
   padding:1px 6px;font-size:11px;margin-right:4px}}
 .badge.fire{{background:#5b2330;color:#fca5a5}}
 .detail td{{background:#14171d;padding:0}}
 .card{{display:flex;gap:24px;padding:18px 14px;flex-wrap:wrap}}
 .col{{flex:1;min-width:300px}}
 .col h3{{font-size:13px;color:#9aa0a6;margin:0 0 8px;border-bottom:1px solid #23262d;padding-bottom:4px}}
 .kv{{display:flex;gap:10px;margin:3px 0;font-size:13px}}
 .kv span{{color:#8b8b8b;min-width:48px}} .kv b{{color:#e6e6e6}}
 .summary{{color:#b9bfc7;font-size:12px;line-height:1.6;margin-top:10px}}
 .view{{color:#e6e6e6;font-size:13px;line-height:1.7;margin:0 0 12px}}
 .barrow{{display:flex;align-items:center;gap:8px;margin:5px 0;font-size:12px}}
 .bl{{width:96px;color:#9aa0a6}}
 .btrack{{flex:1;background:#23262d;border-radius:4px;height:10px;overflow:hidden}}
 .bfill{{display:block;height:10px;border-radius:4px}}
 .bval{{width:28px;text-align:right;color:#e6e6e6}}
 .btxt.miss{{color:#8b8b8b}}
 .compbox{{margin-top:10px;color:#8b8b8b;font-size:13px}}
 .compbox b{{color:#fbbf24;font-size:16px}}
 .note{{margin-top:18px;color:#8b8b8b;font-size:12px;line-height:1.6}}
 .toolbar{{margin:0 0 14px;display:flex;gap:10px;align-items:center}}
 .toolbar button{{background:#1e2330;color:#cbd5e1;border:1px solid #2c3340;
   border-radius:6px;padding:6px 14px;font-size:13px;cursor:pointer}}
 .toolbar button:hover{{background:#262d3d}}
 .toolbar .hint{{color:#8b8b8b;font-size:12px}}
</style></head><body>
<h1>Bollinger / TTM Squeeze Scan</h1>
<div class="meta">as-of {asof} · {len(results)} candidates · generated {dt.datetime.now():%Y-%m-%d %H:%M}</div>
<div class="toolbar">
  <button onclick="collapseAll()">收起全部</button>
  <button onclick="expandAll()">展开全部</button>
  <span class="hint">默认已展开公司介绍 / 基本面观点 / 多维度打分；点任意行可单独收起/展开</span>
</div>
<table><thead><tr>
<th>#</th><th>Ticker</th><th>Score</th><th>综合分</th><th>Price</th><th>BB-W%</th>
<th>BB-Pctile</th><th>TTM</th><th>Mom%</th><th>&gt;MA20</th><th>Dir</th>
</tr></thead><tbody>{''.join(rows)}</tbody></table>
<div class="note">
 A squeeze only says <b>volatility will expand</b> — NOT the direction.<br>
 综合分 = 挤压 35% + 动量 22% + 质量 15% + 估值 13% + 成长 15%（基本面缺失项按中性 50 计）。<br>
 Confirm with momentum (Mom&gt;0 = upward bias) + volume + your MA/structure rules before entry.<br>
 Next step: open the top names on your chart, look for the squeeze <b>release</b>
 (BB re-emerging from Keltner) on volume, ideally with a pullback to MA20.<br>
 Research only — <b>not investment advice</b>. All signals are machine-generated.
</div>
<script>
function tgl(id){{var e=document.getElementById(id);
  e.style.display = (e.style.display==='table-row') ? 'none' : 'table-row';}}
function expandAll(){{document.querySelectorAll('tr.detail').forEach(function(e){{e.style.display='table-row';}});}}
function collapseAll(){{document.querySelectorAll('tr.detail').forEach(function(e){{e.style.display='none';}});}}
</script>
</body></html>"""
    with open(html_path, "w") as f:
        f.write(html)
    return html_path


def write_reports(results: list[dict], asof: str) -> tuple[str, str, str]:
    json_path = os.path.join(RESULTS, f"squeeze_{asof}.json")
    md_path = os.path.join(RESULTS, f"squeeze_{asof}.md")
    html_path = write_html_report(results, asof)
    payload = {"asof": asof, "count": len(results), "results": results}
    js = json.dumps(payload, indent=2)
    with open(json_path, "w") as f:
        f.write(js)
    # Stable "latest" alias so dashboards (e.g. Peter Research) can pull the
    # newest scan without guessing the date in the path.
    with open(os.path.join(RESULTS, "squeeze_latest.json"), "w") as f:
        f.write(js)

    lines = [f"# Bollinger / TTM Squeeze Scan — {asof}",
             "",
             f"*Candidates: {len(results)} · generated {dt.datetime.now():%Y-%m-%d %H:%M}*",
             "",
             "Ranked by squeeze tightness (lower BB-width percentile + TTM-on bonus).",
             "综合分 = 挤压 35% + 动量 22% + 质量 15% + 估值 13% + 成长 15%（基本面缺失项按中性 50 计）。",
             "**A squeeze only says volatility will expand — NOT the direction.**",
             "Confirm with momentum (mom>0 = upward bias) + volume + your MA/structure rules before entry.",
             "",
             "| # | Ticker | Score | 综合分 | Price | BB-W% | BB-Pctile | TTM | Mom | Above MA20 | Dir |",
             "|---|--------|-------|--------|-------|------|-----------|-----|-----|-----------|-----|"]
    for i, r in enumerate(results, 1):
        s = r["signal"]
        md = r.get("multidim") or {}
        comp = md.get("composite")
        comp = f"{comp:.0f}" if comp is not None else "—"
        dirn = "▲up" if (s["mom"] or 0) > 0 else ("▼dn" if (s["mom"] or 0) < 0 else "—")
        lines.append(
            f"| {i} | {r['ticker']} | {r['score']} | {comp} | {s['close']} | {s['bb_width_pct']} | "
            f"{s['bb_width_pctile']} | {'Y' if s['ttm_squeeze_on'] else '—'} | "
            f"{s['mom']} | {'Y' if s['above_ma20'] else 'N'} | {dirn} |"
        )
    lines += ["", "---", "## 候选详情（公司基本介绍 · 基本面观点 · 多维度打分）", ""]
    for i, r in enumerate(results, 1):
        s = r["signal"]
        md = r.get("multidim") or {}
        intro = md.get("intro") or {}
        dims = md.get("dims") or {}
        comp = md.get("composite")
        comp = f"{comp:.0f}" if comp is not None else "—"
        lines.append(f"### {i}. {r['ticker']} — {intro.get('name') or r['ticker']}")
        lines.append(f"- **板块**：{intro.get('sector','—')} / {intro.get('industry','—')}"
                     f"｜**市值**：{intro.get('market_cap','—')}｜**Beta**：{_fmt_beta(intro.get('beta'))}")
        lines.append(f"- **多维度打分**：综合分 **{comp}**"
                     f"（挤压 {dims.get('squeeze')}/ 动量 {dims.get('momentum')}"
                     f"/ 质量 {dims.get('quality')}/ 估值 {dims.get('value')}/ 成长 {dims.get('growth')}）")
        lines.append(f"- **基本面观点**：{md.get('narrative') or '（数据缺失）'}")
        summ = intro.get("summary")
        if summ:
            lines.append(f"- **业务概况**：{summ}")
        lines.append("")
    lines += ["---", "Sourced from public market data. Research only — not investment advice.",
              "Next step: open the top names on your chart, look for the squeeze *release*",
              "(BB re-emerging from Keltner) on volume, ideally with a pullback to MA20."]
    md_text = "\n".join(lines)
    with open(md_path, "w") as f:
        f.write(md_text)
    with open(os.path.join(RESULTS, "squeeze_latest.md"), "w") as f:
        f.write(md_text)
    return json_path, md_path, html_path


if __name__ == "__main__":
    asof = dt.date.today().isoformat()
    res = run()
    jp, mp, hp = write_reports(res, asof)
    print(f"[screener] wrote {jp}\n           {mp}\n           {hp}\n[top 10]")
    for r in res[:10]:
        print(f"  {r['ticker']:6} score={r['score']:5}  price={r['signal']['close']:>9}  "
              f"bbw%={r['signal']['bb_width_pct']:>5}  ttm={r['signal']['ttm_squeeze_on']}")
