#!/usr/bin/env python3
"""Peter research — 今日推荐模块 (today_picks).

Reads results/squeeze_latest.json (produced by screener.py) and emits a curated
"今日推荐" watchlist so the daily output is no longer a long undifferentiated
list:

  • 今日推荐买入  — VCP status==ACTION (or squeeze fired up) with rs_rating>=70
                     and score>=80, ranked by score. Each row shows 公司/行业,
                     当前价, 买点(pivot), 止损, 目标, 盈亏比(R:R 框架 3:1), 评分,
                     RS, 以及"为什么买"(中文 narrative + VCP 信号)。
  • 持仓/观察池核对 — cross-checks watchlist.txt against today's results.
  • 回避/弱势      — lowest-momentum names (mom<0 / below MA20) as a caution list.

Outputs: results/today_picks.html  +  results/today_picks.md

NOTE on 盈亏比: entry = VCP pivot (close adjusted by dist_to_pivot_pct).
stop/target are FRAMEWORK-derived: risk = clip(1.5×ATR%, 4%, 8%); target =
entry + 3×risk  =>  R:R ≈ 3:1 for every name. This is a risk-frame, not a
measured-move target. Real stops should use the base low / structure.
"""
import json
import html
from pathlib import Path

HERE = Path(__file__).resolve().parent
RES = HERE / "results"
SRC = RES / "squeeze_latest.json"
OUT_HTML = RES / "today_picks.html"
OUT_MD = RES / "today_picks.md"
WL = HERE / "watchlist.txt"

BUY_N = 15
SELL_N = 8
RS_FLOOR = 70
SCORE_FLOOR = 80
RR_FRAMEWORK = 3.0


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load():
    return json.loads(SRC.read_text(encoding="utf-8"))


def pivot_of(r):
    sig = r.get("signal", {}) or {}
    close = fnum(sig.get("close"))
    vcp = r.get("vcp", {}) or {}
    dp = fnum(vcp.get("dist_to_pivot_pct"))
    if close and dp is not None:
        return round(close / (1.0 + dp / 100.0), 2)
    return close


def risk_pct(r):
    vcp = r.get("vcp", {}) or {}
    sig = r.get("signal", {}) or {}
    close = fnum(sig.get("close")) or 1.0
    atr = fnum(vcp.get("atr"))
    atr_pct = (atr / close * 100.0) if atr else 0.0
    if atr_pct <= 0:
        atr_pct = 3.0
    rp = atr_pct * 1.5
    return max(0.04, min(0.08, rp))


def build_pick(r):
    sig = r.get("signal", {}) or {}
    close = fnum(sig.get("close"))
    entry = pivot_of(r)
    rp = risk_pct(r)
    stop = round(entry * (1.0 - rp), 2)
    target = round(entry * (1.0 + RR_FRAMEWORK * rp), 2)
    f = r.get("fundamentals", {}) or {}
    vcp = r.get("vcp", {}) or {}
    narr = (r.get("multidim", {}) or {}).get("narrative", "") or ""
    why = (narr[:170] + ("…" if len(narr) > 170 else ""))
    return {
        "ticker": r["ticker"],
        "name": f.get("name") or r["ticker"],
        "sector": f.get("sector") or r.get("sector") or "",
        "industry": f.get("industry") or "",
        "close": close, "entry": entry, "stop": stop, "target": target,
        "rr": RR_FRAMEWORK, "risk_pct": rp,
        "score": r.get("score"), "rs": r.get("rs_rating"),
        "vcp_score": vcp.get("score"),
        "vcp_signals": vcp.get("signals", []) or [],
        "why": why,
    }


def esc(x):
    return html.escape(str(x)) if x is not None else "—"


def load_watchlist():
    if not WL.exists():
        return []
    out = []
    for line in WL.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line.upper())
    return out


def main():
    d = load()
    res = d.get("results", [])
    asof = d.get("asof", "?")

    buys = []
    for r in res:
        vcp = r.get("vcp", {}) or {}
        sig = r.get("signal", {}) or {}
        is_action = vcp.get("status") == "ACTION" or sig.get("ttm_fired_up")
        if not is_action:
            continue
        if (r.get("rs_rating") or 0) < RS_FLOOR:
            continue
        if (r.get("score") or 0) < SCORE_FLOOR:
            continue
        buys.append(build_pick(r))
    buys.sort(key=lambda b: -(b["score"] or 0))
    top_buys = buys[:BUY_N]

    # caution list: weakest momentum
    weak = [r for r in res if (r.get("signal", {}).get("mom") or 0) < 0
            or not r.get("signal", {}).get("above_ma20")]
    weak.sort(key=lambda r: (r.get("signal", {}).get("mom") or 0))
    top_weak = weak[:SELL_N]

    # watchlist cross-check
    wl = load_watchlist()
    wl_status = []
    by_tk = {r["ticker"]: r for r in res}
    for tk in wl:
        r = by_tk.get(tk)
        if not r:
            wl_status.append({"ticker": tk, "state": "未出现在今日扫描", "note": ""})
            continue
        vcp = r.get("vcp", {}) or {}
        sig = r.get("signal", {}) or {}
        st = []
        if vcp.get("status") == "ACTION":
            st.append("VCP近端")
        if sig.get("ttm_fired_up"):
            st.append("突破 fired")
        if sig.get("ttm_fired_dn"):
            st.append("下破")
        if (r.get("rs_rating") or 0) >= RS_FLOOR:
            st.append(f"RS{r.get('rs_rating')}")
        f = r.get("fundamentals", {}) or {}
        wl_status.append({
            "ticker": tk,
            "state": " / ".join(st) if st else "观察中",
            "note": f"{f.get('sector','')}/{f.get('industry','')} 评分{round(r.get('score') or 0,1)}",
        })

    # ---------------- HTML ----------------
    def ind_line(p):
        sec = p.get("sector") or ""
        ind = p.get("industry") or ""
        if sec or ind:
            return f'<div style="font-size:11px;color:#58a6ff">{esc(sec)}{(" › " + esc(ind)) if ind and ind != sec else ""}</div>'
        return ""

    def vcp_tags(p):
        if not p.get("vcp_signals"):
            return ""
        return " ".join(f'<span style="background:#1f6feb22;color:#58a6ff;padding:1px 5px;border-radius:5px;font-size:10px;margin-right:3px">{esc(t)}</span>' for t in p["vcp_signals"])

    buy_rows = ""
    for i, b in enumerate(top_buys, 1):
        sc = f"{b['score']:.0f}"
        buy_rows += f"""<tr>
<td>{i}</td>
<td><b>{esc(b['ticker'])}</b><div style="font-size:11px;color:#c9d1d9;margin-top:2px">{esc(b['name'])}</div>{ind_line(b)}</td>
<td>${b['close']:.2f}</td><td>${b['entry']:.2f}</td><td>${b['stop']:.2f}</td><td>${b['target']:.2f}</td>
<td><span style="color:#3fb950;font-weight:700">{b['rr']:.1f}:1</span></td>
<td>{esc(b['rs'])}</td><td>{esc(sc)}</td>
<td style="text-align:left;font-size:12px">{esc(b['why'])}<div style="margin-top:3px">{vcp_tags(b)}</div></td></tr>"""

    weak_rows = ""
    for i, r in enumerate(top_weak, 1):
        f = r.get("fundamentals", {}) or {}
        sig = r.get("signal", {}) or {}
        mom_s = f"{sig.get('mom'):.3f}"
        rs_s = f"{r.get('rs_rating')}"
        sc_s = f"{r.get('score'):.0f}"
        weak_rows += f"""<tr>
<td>{i}</td>
<td><b>{esc(r['ticker'])}</b><div style="font-size:11px;color:#c9d1d9;margin-top:2px">{esc(f.get('name') or r['ticker'])}</div>{ind_line({'sector':f.get('sector'),'industry':f.get('industry')})}</td>
<td>${fnum(sig.get('close')):.2f}</td>
<td>{esc(mom_s)}</td>
<td>{esc(rs_s)}</td>
<td>{esc(sc_s)}</td>
<td style="text-align:left;font-size:12px;color:#8b949e">{esc(('未站上MA20' if not sig.get('above_ma20') else '动量为负'))}</td></tr>"""

    wl_rows = ""
    for w in wl_status:
        wl_rows += f"""<tr><td><b>{esc(w['ticker'])}</b></td><td style="text-align:left;color:#58a6ff;font-size:12px">{esc(w['state'])}</td><td style="text-align:left;color:#8b949e">{esc(w['note'])}</td></tr>"""

    html_doc = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Peter research · 今日推荐 {esc(asof)}</title>
<style>
body{{background:#0d1117;color:#e6edf3;font-family:-apple-system,'Segoe UI',Roboto,'PingFang SC',sans-serif;margin:0;padding:24px;line-height:1.5}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:17px;margin:28px 0 10px;border-left:4px solid #1f6feb;padding-left:10px}}
.sub{{color:#8b949e;font-size:13px;margin-bottom:6px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:14px 16px;margin-bottom:14px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{background:#21262d;color:#c9d1d9;text-align:left;padding:8px 10px}}
td{{padding:8px 10px;border-top:1px solid #21262d;vertical-align:top}}
tr:hover td{{background:#1c2230}}
.badge{{display:inline-block;background:#3fb95022;color:#3fb950;padding:2px 8px;border-radius:6px;font-size:12px;font-weight:700}}
.foot{{color:#8b949e;font-size:12px;margin-top:30px;border-top:1px solid #30363d;padding-top:12px}}
</style></head><body>
<h1>Peter research · 今日推荐 <span class="badge">TOP {BUY_N} 买 / {SELL_N} 回避</span></h1>
<div class="sub">数据源：bollinger-squeeze-screener 收盘扫描（asof {esc(asof)}，宇宙 {d.get('rs_universe','?')} 只）。VCP status==ACTION 且 RS≥{RS_FLOOR} 且 评分≥{SCORE_FLOOR} 精选。</div>

<div class="card">
<b>盈亏比说明：</b>买点 = VCP 枢轴（按 dist_to_pivot_pct 由收盘价回推）。止损/目标为<b>框架值</b>：风险 = clip(1.5×ATR%, 4%, 8%)，目标 = 买点 + 3×风险 → 每只 R:R≈3:1。这是风险框架，不是实测目标位；实盘止损应依托基底低点/结构。
</div>

<h2>① 今日推荐买入（精选 Top {BUY_N}，按评分排序）</h2>
<table><thead><tr><th>#</th><th>代码 / 公司</th><th>当前价</th><th>买点</th><th>止损</th><th>目标</th><th>盈亏比</th><th>RS</th><th>评分</th><th>为什么买</th></tr></thead>
<tbody>{buy_rows}</tbody></table>

<h2>② 回避 / 弱势（动量负或未站上 MA20，Top {SELL_N}）</h2>
<table><thead><tr><th>#</th><th>代码 / 公司</th><th>当前价</th><th>动量</th><th>RS</th><th>评分</th><th>备注</th></tr></thead>
<tbody>{weak_rows}</tbody></table>

<h2>③ 持仓 / 观察池核对（watchlist.txt）</h2>
<table><thead><tr><th>代码</th><th>今日状态</th><th>板块 / 评分</th></tr></thead>
<tbody>{wl_rows}</tbody></table>

<div class="foot">
模块：today_picks.py，读取 screener.py 产出的 squeeze_latest.json 自动生成。Peter research 每日扫描由 run_scan.sh 调度并推送至 GitHub。<br>
⚠️ 仅供参考，是否操作由你本人决策。AI 仅做数据整理与推送，不替代你下单。
</div>
</body></html>"""
    OUT_HTML.write_text(html_doc, encoding="utf-8")

    # ---------------- Markdown ----------------
    md = [f"# Peter research · 今日推荐 ({asof})\n",
          f"数据源：bollinger-squeeze-screener 收盘扫描（宇宙 {d.get('rs_universe','?')} 只）。VCP ACTION 且 RS≥{RS_FLOOR} 且 评分≥{SCORE_FLOOR}。\n",
          f"## ① 今日推荐买入 Top {BUY_N}\n"]
    for i, b in enumerate(top_buys, 1):
        md.append(f"{i}. **{b['ticker']}** {b['name']} — {b['sector']}/{b['industry']}\n"
                  f"   当前 ${b['close']:.2f} | 买点 ${b['entry']:.2f} | 止损 ${b['stop']:.2f} | 目标 ${b['target']:.2f} | R:R {b['rr']:.1f}:1 | RS {b['rs']} | 评分 {b['score']:.0f}\n"
                  f"   为什么：{b['why']}\n")
    md.append("\n## ② 回避 / 弱势 Top %d\n" % SELL_N)
    for i, r in enumerate(top_weak, 1):
        f = r.get("fundamentals", {}) or {}
        sig = r.get("signal", {}) or {}
        md.append(f"{i}. **{r['ticker']}** {f.get('name','')} — 当前 ${fnum(sig.get('close')):.2f}, 动量 {sig.get('mom'):.3f}, RS {r.get('rs_rating')}, 评分 {r.get('score'):.0f}\n")
    md.append("\n## ③ 持仓/观察池核对\n")
    for w in wl_status:
        md.append(f"- **{w['ticker']}**: {w['state']} — {w['note']}\n")
    OUT_MD.write_text("\n".join(md), encoding="utf-8")

    print(f"BUY actionable candidates (RS>= {RS_FLOOR}, score>= {SCORE_FLOOR}): {len(buys)} | shown: {len(top_buys)}")
    print(f"WEAK candidates: {len(weak)} | shown: {len(top_weak)}")
    print(f"WATCHLIST cross-checked: {len(wl_status)}")
    print(f"WROTE {OUT_HTML} | {OUT_MD}")


if __name__ == "__main__":
    main()
