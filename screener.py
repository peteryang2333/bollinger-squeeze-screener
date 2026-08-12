"""
screener.py — orchestrates the daily squeeze + VCP scan and writes reports.

Outputs (into results/):
- squeeze_<YYYY-MM-DD>.json   machine-readable, full signal per ticker
- squeeze_<YYYY-MM-DD>.md     human-readable ranked watchlist for "next day"
- squeeze_latest.json / .md   stable aliases (dashboard pull without date-guess)

Two complementary right-side momentum screens are fused here:

1. BOLLINGER / TTM SQUEEZE (volatility coiling)
   A name is a *squeeze candidate* if EITHER:
     (a) bb_squeeze is True  (BB-width in lowest percentile of its own history), OR
     (b) ttm_squeeze_on is True (Bollinger fully inside Keltner).
   Ranked by a composite "squeeze score" (tighter BB-width + TTM bonus + width).

2. VCP (Volatility Contraction Pattern, Minervini) + RELATIVE STRENGTH
   Ported from SENTINEL PRO: a 105-pt contraction score (tightness + volume
   dry-up + MA alignment + pivot) plus a cross-sectional RS rating (1..100).
   A name is a *VCP candidate* if vcp score >= MIN_VCP_SCORE, trend-aligned
   (MA>=20) and RS >= MIN_RS_RATING.

Both lists share one enrichment pass (company profile + multi-dim score).
Direction hints come from the momentum histogram (mom>0 => upward bias).
"""

from __future__ import annotations
import os
import json
import time
import datetime as dt
import math
import numpy as np
import pandas as pd

from squeeze_core import compute, squeeze_signal
from data_provider import get_histories
from universe import build_universe
from fundamentals import get_fundamentals
from multidim import multidim_scores
from vcp_rs import rs_raw_score, assign_rs_percentiles, vcp_analyze, MIN_VCP_SCORE, MIN_VCP_MA, MIN_RS_RATING

HERE = os.path.dirname(__file__)
RESULTS = os.path.join(HERE, "results")
os.makedirs(RESULTS, exist_ok=True)


def _safe_int(x):
    """int() that survives NaN/inf/None (volume can be degenerate for some tickers)."""
    try:
        if x is None:
            return 0
        xf = float(x)
        if math.isnan(xf) or math.isinf(xf):
            return 0
        return int(xf)
    except Exception:
        return 0


def _sanitize(o):
    """Recursively replace NaN/inf with None so the JSON stays browser-safe."""
    if isinstance(o, float):
        if math.isnan(o) or math.isinf(o):
            return None
        return o
    if isinstance(o, dict):
        return {k: _sanitize(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_sanitize(v) for v in o]
    return o


# ---- tunables (mirror config.yaml) ----
MIN_PRICE = 5.0
MIN_AVG_VOLUME = 500_000      # shares/day, skip illiquid
MIN_DAYS = 120                # minimum for squeeze math
MIN_DAYS_VCP = 130            # need MA200 + 4 range windows for VCP (superset of above)
TOP_N = 40
ENRICH_TOP_N = 40             # top candidates get company profile + multi-dim score
FUND_SLEEP = 0.15             # politeness delay between fundamentals fetches


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


def run(universe: list[str] | None = None, top_n: int = TOP_N):
    if universe is None:
        # mirrors config.yaml — broad market pool per user decision
        universe = build_universe(use_watchlist=True, use_sp500=True,
                                   use_sp400=True, use_sp600=True,
                                   use_nasdaq100=False, use_russell2000=False)
    print(f"[screener] universe={len(universe)}")

    histories = get_histories(universe)
    print(f"[screener] fetched={len(histories)}")

    # ---- scan every liquid, sufficiently-long ticker once ----
    scanned = []
    for tk, df in histories.items():
        if len(df) < MIN_DAYS_VCP:
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
        try:
            raw_rs = rs_raw_score(df["Close"])
        except Exception as e:
            print(f"  [rs] {tk} error: {e}")
            raw_rs = 0.0
        try:
            vcp = vcp_analyze(df)
        except Exception as e:
            print(f"  [vcp] {tk} error: {e}")
            vcp = {}
        scanned.append({
            "ticker": tk,
            "score": _score(row),
            "sector": "",
            "signal": sig,
            "avg_volume_50d": _safe_int(avg_vol),
            "raw_rs": raw_rs,
            "vcp": vcp,
        })

    # ---- cross-sectional RS rating (1..100, 100 = strongest) ----
    assign_rs_percentiles(scanned)
    rs_universe = sum(1 for s in scanned if "rs_rating" in s)
    print(f"[screener] RS universe={rs_universe}")

    # ---- squeeze candidates (unchanged primary screen) ----
    sq = [s for s in scanned if s["signal"]["bb_squeeze"] or s["signal"]["ttm_squeeze_on"]]
    sq.sort(key=lambda r: r["score"], reverse=True)
    squeeze_candidates = sq[:top_n]

    # ---- VCP breakout candidates (tight contraction + trend + RS) ----
    vc = [s for s in scanned
          if s["vcp"]["score"] >= MIN_VCP_SCORE
          and s["vcp"]["breakdown"]["ma"] >= MIN_VCP_MA
          and s.get("rs_rating", 0) >= MIN_RS_RATING]
    vc.sort(key=lambda r: r["vcp"]["score"], reverse=True)
    vcp_candidates = vc[:top_n]

    # ---- enrich the union of tickers (fundamentals + multi-dim score) ----
    enr = list(dict.fromkeys(
        [c["ticker"] for c in squeeze_candidates] +
        [c["ticker"] for c in vcp_candidates]))
    print(f"[screener] enriching {len(enr)} tickers "
          f"(squeeze={len(squeeze_candidates)} vcp={len(vcp_candidates)})")
    sq_set = {c["ticker"] for c in squeeze_candidates}
    for tk in enr:
        try:
            f = get_fundamentals(tk)
        except Exception as e:
            print(f"  [enrich] {tk} failed: {e}")
            f = {}
        for c in squeeze_candidates + vcp_candidates:
            if c["ticker"] != tk:
                continue
            c["fundamentals"] = f
            # feed the VCP score as the "squeeze raw" dim for VCP-only names
            md_score = c["vcp"]["score"] if tk not in sq_set else c["score"]
            c["multidim"] = multidim_scores(f, c["signal"], md_score)
        time.sleep(FUND_SLEEP)

    return squeeze_candidates, vcp_candidates, rs_universe


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------
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


def _vcp_block(vcp):
    if not vcp or vcp.get("score", 0) == 0:
        return ""
    bd = vcp.get("breakdown") or {}
    status = vcp.get("status", "—")
    scls = {"ACTION": "#4ade80", "WAIT": "#fbbf24", "EXTENDED": "#f87171"}.get(status, "#9aa0a6")
    bars = "".join([
        _bar("Tight", bd.get("tight")),
        _bar("Vol", bd.get("vol")),
        _bar("MA", bd.get("ma")),
        _bar("Pivot", bd.get("pivot")),
    ])
    sigs = ", ".join(vcp.get("signals") or []) or "—"
    return (f"<div class='col'><h3>VCP 波动收缩（Minervini）</h3>"
            f"<div class='kv'><span>VCP分</span><b>{vcp.get('score')}</b></div>"
            f"<div class='kv'><span>状态</span><b style='color:{scls}'>{status}</b></div>"
            f"<div class='kv'><span>ATR</span><b>{vcp.get('atr')}</b></div>"
            f"<div class='kv'><span>量比</span><b>{vcp.get('vol_ratio')}</b></div>"
            f"<div class='kv'><span>振幅%</span><b>{vcp.get('range_pct')}</b></div>"
            f"<div class='kv'><span>距枢轴%</span><b>{vcp.get('dist_to_pivot_pct')}</b></div>"
            f"<h3>VCP 分项（0–40）</h3>{bars}"
            f"<p class='view'>{sigs}</p></div>")


_HEADER = ("<th>#</th><th>Ticker</th><th>分数</th><th>综合分</th><th>RS</th>"
           "<th>VCP</th><th>Price</th><th>BB-W%</th><th>BB-Pctile</th><th>TTM</th>"
           "<th>Mom</th><th>&gt;MA20</th><th>方向</th>")


def _main_row(r, idx, score_disp):
    s = r["signal"]
    md = r.get("multidim") or {}
    composite = md.get("composite")
    comp = f"{composite:.0f}" if composite is not None else "—"
    rs = r.get("rs_rating")
    vcp = r.get("vcp") or {}
    dirn = "▲ up" if (s["mom"] or 0) > 0 else ("▼ dn" if (s["mom"] or 0) < 0 else "—")
    cls = "up" if (s["mom"] or 0) > 0 else ("dn" if (s["mom"] or 0) < 0 else "flat")
    ttm = '<span class="badge">TTM</span>' if s["ttm_squeeze_on"] else ""
    fired = '<span class="badge fire">FIRED</span>' if s.get("ttm_fired_up") else ""
    vstat = vcp.get("status", "—")
    vscore = vcp.get("score", 0)
    vcls = {"ACTION": "up", "WAIT": "flat", "EXTENDED": "dn"}.get(vstat, "flat")
    vcp_disp = f'{vscore} <span class="{vcls}">{vstat}</span>' if vscore > 0 else "—"
    rs_disp = rs if rs is not None else "—"
    return (f"<tr class='main' onclick=\"tgl('d{idx}')\">"
            f"<td>{idx}</td><td class='tk'>{r['ticker']}</td>"
            f"<td>{score_disp}</td><td class='comp'>{comp}</td>"
            f"<td class='rs'>{rs_disp}</td><td class='vcp'>{vcp_disp}</td>"
            f"<td>{s['close']}</td><td>{s['bb_width_pct']}%</td>"
            f"<td>{s['bb_width_pctile']}</td><td>{ttm}{fired}</td>"
            f"<td>{s['mom']}</td><td>{'Y' if s['above_ma20'] else 'N'}</td>"
            f"<td class='{cls}'>{dirn}</td></tr>")


def _detail_row(r, idx):
    s = r["signal"]
    md = r.get("multidim") or {}
    intro = md.get("intro") or {}
    dims = md.get("dims") or {}
    composite = md.get("composite")
    comp = f"{composite:.0f}" if composite is not None else "—"
    bars = "".join([
        _bar("挤压 Squeeze", dims.get("squeeze")),
        _bar("动量 Momentum", dims.get("momentum")),
        _bar("质量 Quality", dims.get("quality")),
        _bar("估值 Value", dims.get("value")),
        _bar("成长 Growth", dims.get("growth")),
    ])
    summary = intro.get("summary") or "（无业务概况）"
    vcp = r.get("vcp") or {}
    return (f"<tr class='detail' id='d{idx}' style='display:table-row'><td colspan='13'>"
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
            f"{_vcp_block(vcp)}"
            f"</div></td></tr>")


def _section(records, title, kind):
    if not records:
        return ""
    rows = []
    for i, r in enumerate(records, 1):
        score_disp = r["score"] if kind == "squeeze" else r["vcp"]["score"]
        rows.append(_main_row(r, i, score_disp))
        rows.append(_detail_row(r, i))
    return (f"<div class='sec'><h2>{title} · {len(records)} 只</h2>"
            f"<table><thead><tr>{_HEADER}</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>")


HTML_TMPL = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bollinger/TTM Squeeze + VCP — __META__</title>
<style>
 body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
   background:#0f1115;color:#e6e6e6;margin:0;padding:24px;}
 h1{font-size:20px;margin:0 0 4px} .meta{color:#8b8b8b;font-size:13px;margin-bottom:8px}
 .sec{margin-top:22px}
 .sec h2{font-size:15px;color:#9aa0a6;margin:0 0 10px;border-bottom:1px solid #23262d;padding-bottom:6px}
 table{border-collapse:collapse;width:100%;font-size:13px}
 th,td{padding:8px 10px;text-align:left;border-bottom:1px solid #23262d}
 th{color:#9aa0a6;font-weight:600;position:sticky;top:0;background:#0f1115}
 tr.main{cursor:pointer} tr.main:hover{background:#181b21}
 .tk{font-weight:700;color:#7dd3fc} .comp{font-weight:700;color:#fbbf24}
 .rs{font-weight:700;color:#c4b5fd} .vcp{font-size:12px}
 .up{color:#f87171} .dn{color:#4ade80} .flat{color:#9aa0a6}
 .badge{display:inline-block;background:#1e3a5f;color:#7dd3fc;border-radius:4px;
   padding:1px 6px;font-size:11px;margin-right:4px}
 .badge.fire{background:#5b2330;color:#fca5a5}
 .detail td{background:#14171d;padding:0}
 .card{display:flex;gap:24px;padding:18px 14px;flex-wrap:wrap}
 .col{flex:1;min-width:300px}
 .col h3{font-size:13px;color:#9aa0a6;margin:0 0 8px;border-bottom:1px solid #23262d;padding-bottom:4px}
 .kv{display:flex;gap:10px;margin:3px 0;font-size:13px}
 .kv span{color:#8b8b8b;min-width:48px} .kv b{color:#e6e6e6}
 .summary{color:#b9bfc7;font-size:12px;line-height:1.6;margin-top:10px}
 .view{color:#e6e6e6;font-size:13px;line-height:1.7;margin:0 0 12px}
 .barrow{display:flex;align-items:center;gap:8px;margin:5px 0;font-size:12px}
 .bl{width:96px;color:#9aa0a6}
 .btrack{flex:1;background:#23262d;border-radius:4px;height:10px;overflow:hidden}
 .bfill{display:block;height:10px;border-radius:4px}
 .bval{width:28px;text-align:right;color:#e6e6e6}
 .btxt.miss{color:#8b8b8b}
 .compbox{margin-top:10px;color:#8b8b8b;font-size:13px}
 .compbox b{color:#fbbf24;font-size:16px}
 .note{margin-top:18px;color:#8b8b8b;font-size:12px;line-height:1.6}
 .toolbar{margin:0 0 8px;display:flex;gap:10px;align-items:center}
 .toolbar button{background:#1e2330;color:#cbd5e1;border:1px solid #2c3340;
   border-radius:6px;padding:6px 14px;font-size:13px;cursor:pointer}
 .toolbar button:hover{background:#262d3d}
 .toolbar .hint{color:#8b8b8b;font-size:12px}
</style></head><body>
<h1>Bollinger / TTM 挤压 + VCP 突破扫描</h1>
<div class="meta">__META__</div>
<div class="toolbar">
  <button onclick="collapseAll()">收起全部</button>
  <button onclick="expandAll()">展开全部</button>
  <span class="hint">默认已展开公司介绍 / 基本面观点 / 多维度打分 / VCP；点任意行可单独收起/展开</span>
</div>
__BODY__
<div class="note">
 <b>挤压</b>只预示波动率将扩张——不判方向；右侧确认需 mom&gt;0 + 放量 + 回踩 MA20，止损挂 coil 低点。<br>
 <b>VCP</b>（Volatility Contraction Pattern）= 波动收缩 + 量缩 + 均线完美排列（Minervini），
 状态 ACTION=贴近枢轴可突破 / WAIT=仍在基部下方 / EXTENDED=已远离枢轴。<br>
 <b>RS</b>=相对强度评级（1–100，100 最强），跨全样本横截面排名。<br>
 综合分 = 挤压 35% + 动量 22% + 质量 15% + 估值 13% + 成长 15%（基本面缺失项按中性 50 计）。<br>
 研究仅用于参考，<b>非投资建议</b>，所有信号由机器生成。
</div>
<script>
function tgl(id){var e=document.getElementById(id);
  e.style.display = (e.style.display==='table-row') ? 'none' : 'table-row';}
function expandAll(){document.querySelectorAll('tr.detail').forEach(function(e){e.style.display='table-row';});}
function collapseAll(){document.querySelectorAll('tr.detail').forEach(function(e){e.style.display='none';});}
</script>
</body></html>"""


def write_html_report(squeeze_candidates, vcp_candidates, rs_universe, asof):
    html_path = os.path.join(RESULTS, f"squeeze_{asof}.html")
    body = _section(squeeze_candidates, "布林 / TTM 挤压候选", "squeeze")
    body += _section(vcp_candidates, "VCP 波动收缩突破候选（Minervini）", "vcp")
    meta = (f"as-of {asof} · 挤压 {len(squeeze_candidates)} 只 · "
            f"VCP {len(vcp_candidates)} 只 · RS 样本 {rs_universe} · "
            f"generated {dt.datetime.now():%Y-%m-%d %H:%M}")
    html = (HTML_TMPL.replace("__META__", meta)
            .replace("__BODY__", body))
    with open(html_path, "w") as f:
        f.write(html)
    return html_path


def write_reports(squeeze_candidates, vcp_candidates, rs_universe, asof):
    json_path = os.path.join(RESULTS, f"squeeze_{asof}.json")
    md_path = os.path.join(RESULTS, f"squeeze_{asof}.md")
    html_path = write_html_report(squeeze_candidates, vcp_candidates, rs_universe, asof)

    payload = {
        "asof": asof,
        "count": len(squeeze_candidates),
        "rs_universe": rs_universe,
        "results": squeeze_candidates,
        "vcp_candidates": vcp_candidates,
    }
    js = json.dumps(_sanitize(payload), indent=2, default=str)
    with open(json_path, "w") as f:
        f.write(js)
    # Stable "latest" alias so dashboards (e.g. Peter Research) can pull the
    # newest scan without guessing the date in the path.
    with open(os.path.join(RESULTS, "squeeze_latest.json"), "w") as f:
        f.write(js)

    # ---- markdown ----
    lines = [f"# Bollinger / TTM 挤压 + VCP 扫描 — {asof}",
             "",
             f"*挤压候选 {len(squeeze_candidates)} · VCP 候选 {len(vcp_candidates)} · "
             f"RS 样本 {rs_universe} · generated {dt.datetime.now():%Y-%m-%d %H:%M}*",
             "",
             "综合分 = 挤压 35% + 动量 22% + 质量 15% + 估值 13% + 成长 15%（基本面缺失项按中性 50 计）。",
             "**挤压只预示波动率将扩张——不判方向**；**VCP**=波动收缩+量缩+均线完美排列；**RS**=相对强度(1–100)。",
             "确认需 mom>0 + 放量 + 回踩 MA20；止损挂 coil 低点。",
             "",
             "## 布林 / TTM 挤压候选",
             "",
             "| # | Ticker | 分数 | 综合分 | RS | VCP分/状态 | Price | BB-W% | BB-Pctile | TTM | Mom | >MA20 | Dir |",
             "|---|--------|------|--------|----|-----------|-------|------|-----------|-----|-----|--------|-----|"]
    for i, r in enumerate(squeeze_candidates, 1):
        s = r["signal"]; md = r.get("multidim") or {}
        comp = md.get("composite"); comp = f"{comp:.0f}" if comp is not None else "—"
        rs = r.get("rs_rating"); rs = rs if rs is not None else "—"
        vcp = r.get("vcp") or {}; vd = f"{vcp.get('score',0)}/{vcp.get('status','—')}"
        dirn = "▲up" if (s["mom"] or 0) > 0 else ("▼dn" if (s["mom"] or 0) < 0 else "—")
        lines.append(
            f"| {i} | {r['ticker']} | {r['score']} | {comp} | {rs} | {vd} | "
            f"{s['close']} | {s['bb_width_pct']} | {s['bb_width_pctile']} | "
            f"{'Y' if s['ttm_squeeze_on'] else '—'} | {s['mom']} | "
            f"{'Y' if s['above_ma20'] else 'N'} | {dirn} |")
    lines += ["", "## VCP 波动收缩突破候选（Minervini）",
              "",
              "| # | Ticker | VCP分 | RS | 状态 | 综合分 | Price | 距枢轴% | 信号 |",
              "|---|--------|------|----|------|--------|-------|--------|------|"]
    for i, r in enumerate(vcp_candidates, 1):
        s = r["signal"]; md = r.get("multidim") or {}
        comp = md.get("composite"); comp = f"{comp:.0f}" if comp is not None else "—"
        rs = r.get("rs_rating"); rs = rs if rs is not None else "—"
        vcp = r.get("vcp") or {}
        sig = ", ".join(vcp.get("signals") or []) or "—"
        lines.append(
            f"| {i} | {r['ticker']} | {vcp.get('score',0)} | {rs} | {vcp.get('status','—')} | "
            f"{comp} | {s['close']} | {vcp.get('dist_to_pivot_pct')} | {sig} |")

    # details (dedup across both lists)
    seen = set()
    comb = []
    for r in squeeze_candidates + vcp_candidates:
        if r["ticker"] in seen:
            continue
        seen.add(r["ticker"]); comb.append(r)
    lines += ["", "---", "## 候选详情（公司基本介绍 · 基本面观点 · 多维度打分 · VCP）", ""]
    for i, r in enumerate(comb, 1):
        s = r["signal"]; md = r.get("multidim") or {}
        intro = md.get("intro") or {}; dims = md.get("dims") or {}
        comp = md.get("composite"); comp = f"{comp:.0f}" if comp is not None else "—"
        vcp = r.get("vcp") or {}
        lines.append(f"### {i}. {r['ticker']} — {intro.get('name') or r['ticker']}")
        lines.append(f"- **板块**：{intro.get('sector','—')} / {intro.get('industry','—')}"
                     f"｜**市值**：{intro.get('market_cap','—')}｜**Beta**：{_fmt_beta(intro.get('beta'))}"
                     f"｜**RS**：{r.get('rs_rating') if r.get('rs_rating') is not None else '—'}"
                     f"｜**VCP**：{vcp.get('score',0)} ({vcp.get('status','—')})")
        lines.append(f"- **多维度打分**：综合分 **{comp}**"
                     f"（挤压 {dims.get('squeeze')}/ 动量 {dims.get('momentum')}"
                     f"/ 质量 {dims.get('quality')}/ 估值 {dims.get('value')}/ 成长 {dims.get('growth')}）")
        if vcp.get("score", 0) > 0:
            bd = vcp.get("breakdown") or {}
            lines.append(f"- **VCP 分项**：Tight {bd.get('tight')}/ Vol {bd.get('vol')}"
                         f"/ MA {bd.get('ma')}/ Pivot {bd.get('pivot')}"
                         f"（量比 {vcp.get('vol_ratio')}，振幅% {vcp.get('range_pct')}，"
                         f"信号：{', '.join(vcp.get('signals') or []) or '—'}）")
        lines.append(f"- **基本面观点**：{md.get('narrative') or '（数据缺失）'}")
        summ = intro.get("summary")
        if summ:
            lines.append(f"- **业务概况**：{summ}")
        lines.append("")
    lines += ["---", "Sourced from public market data. Research only — not investment advice.",
              "Next step: open the top names on your chart, look for the squeeze *release*",
              "(BB re-emerging from Keltner) or the VCP *breakout* on volume."]
    md_text = "\n".join(lines)
    with open(md_path, "w") as f:
        f.write(md_text)
    with open(os.path.join(RESULTS, "squeeze_latest.md"), "w") as f:
        f.write(md_text)
    return json_path, md_path, html_path


if __name__ == "__main__":
    asof = dt.date.today().isoformat()
    sq, vc, rs_universe = run()
    jp, mp, hp = write_reports(sq, vc, rs_universe, asof)
    print(f"[screener] wrote {jp}\n           {mp}\n           {hp}\n[top 10 squeeze]")
    for r in sq[:10]:
        print(f"  {r['ticker']:6} score={r['score']:5}  rs={r.get('rs_rating')}  "
              f"price={r['signal']['close']:>9}  bbw%={r['signal']['bb_width_pct']:>5}  "
              f"ttm={r['signal']['ttm_squeeze_on']}")
    print(f"[top 10 VCP] (rs_universe={rs_universe})")
    for r in vc[:10]:
        vcp = r.get("vcp") or {}
        print(f"  {r['ticker']:6} vcp={vcp.get('score',0):3}  rs={r.get('rs_rating')}  "
              f"status={vcp.get('status','—')}")
