#!/usr/bin/env python3
"""Peter research — 今日推荐模块 (today_picks)  v2 (精细化版).

Reads results/squeeze_latest.json (produced by screener.py) and emits a curated
"今日推荐" so the daily output is no longer a long undifferentiated list:

  • 今日推荐买入 — VCP status==ACTION (or TTM squeeze fired up) with rs_rating>=70
                    and score>=80, ranked by a 共振 composite (not just score), then score.
                    每行含 公司/行业/简介, 当前价, 买点(pivot), 止损, 目标,
                    盈亏比, 评分, RS, 共振标签, 以及"为什么买"(中文 narrative + VCP 信号).

  • 回避/弱势      — 动量负或未站上 MA20 的 caution list。

  • 持仓/观察池核对 — cross-checks watchlist.txt against today's results.

盈亏比（重要，诚实标注）:
  entry = VCP 枢轴 (close / (1 + dist_to_pivot_pct/100))
  risk% = clip(1.5 × ATR%, 3%, 8%)        # 结构化止损：枢轴下方
  stop  = entry × (1 − risk%)
  target= entry × (1 + 2.5 × risk%)       # 2.5R 风险模型目标
  R:R   = 2.5 : 1  (每只股票的 stop/target 是独立的真实价位，由各自 ATR 决定)

说明：screener.py 当前未记录 base_low（基底低点），因此无法做"1:1 实测量度目标"。
若日后在 screener.py 加入 base_low，可把 target 改为 pivot + (pivot − base_low)，
得到真正的 measured-move 盈亏比。本模块为纯展示层，不修改扫描逻辑。
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
RISK_MULT = 2.5          # target = entry + 2.5 × risk
ATR_MULT = 1.5           # risk% = 1.5 × ATR%
RISK_MIN = 0.03
RISK_MAX = 0.08


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
    rp = atr_pct * ATR_MULT
    return max(RISK_MIN, min(RISK_MAX, rp))


def confluence(r):
    """Count aligned bullish signals 0..5 for a 共振 badge."""
    vcp = r.get("vcp", {}) or {}
    sig = r.get("signal", {}) or {}
    c = 0
    if vcp.get("status") == "ACTION":
        c += 1
    if sig.get("ttm_squeeze_on") or sig.get("ttm_fired_up"):
        c += 1
    if (r.get("rs_rating") or 0) >= 80:
        c += 1
    if sig.get("above_ma20") and (sig.get("mom") or 0) > 0:
        c += 1
    if vcp.get("is_dryup"):
        c += 1
    return c


def build_pick(r):
    sig = r.get("signal", {}) or {}
    close = fnum(sig.get("close"))
    entry = pivot_of(r)
    rp = risk_pct(r)
    risk_d = round(entry * rp, 2)
    stop = round(entry - risk_d, 2)
    target = round(entry + risk_d * RISK_MULT, 2)
    f = r.get("fundamentals", {}) or {}
    vcp = r.get("vcp", {}) or {}
    narr = (r.get("multidim", {}) or {}).get("narrative", "") or ""
    why = (narr[:160] + ("…" if len(narr) > 160 else ""))
    sigs = vcp.get("signals", []) or []
    extra = "；".join(sigs[:4])
    sub = (f.get("summary") or "")[:90]
    return {
        "ticker": r["ticker"],
        "name": f.get("name") or r["ticker"],
        "sector": f.get("sector") or r.get("sector") or "",
        "industry": f.get("industry") or "",
        "sub": sub,
        "close": close, "entry": entry, "stop": stop, "target": target,
        "rr": RISK_MULT, "risk_pct": rp,
        "score": r.get("score"), "rs": r.get("rs_rating"),
        "vcp_score": vcp.get("score"),
        "vcp_signals": sigs,
        "conf": confluence(r),
        "why": why, "extra": extra,
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
    total = d.get("count", len(res))

    buys = []
    seen = set()
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
        tk = r["ticker"]
        if tk in seen:
            continue
        seen.add(tk)
        buys.append(build_pick(r))
    # rank by 共振 first, then score
    buys.sort(key=lambda b: (-b["conf"], -(b["score"] or 0)))
    top_buys = buys[:BUY_N]

    # caution list: weakest momentum (exclude those already in buys)
    weak = [r for r in res
            if (r.get("signal", {}).get("mom") or 0) < 0
            or not r.get("signal", {}).get("above_ma20")]
    weak = [r for r in weak if r["ticker"] not in seen]
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
            st.append("RS" + str(r.get("rs_rating")))
        f = r.get("fundamentals", {}) or {}
        wl_status.append({
            "ticker": tk,
            "state": " / ".join(st) if st else "观察中",
            "note": f.get("sector", "") + "/" + f.get("industry", "") + " 评分" + str(round(r.get("score") or 0, 1)),
        })

    # ---------------- HTML ----------------
    def ind_line(p):
        sec = p.get("sector") or ""
        ind = p.get("industry") or ""
        if sec or ind:
            return '<div style="font-size:11px;color:#58a6ff">' + esc(sec) + ((" › " + esc(ind)) if ind and ind != sec else "") + "</div>"
        return ""

    def vcp_tags(p):
        if not p.get("vcp_signals"):
            return ""
        return " ".join('<span style="background:#1f6feb22;color:#58a6ff;padding:1px 5px;border-radius:5px;font-size:10px;margin-right:3px">' + esc(t) + "</span>" for t in p["vcp_signals"])

    def conf_badge(c):
        color = "#3fb950" if c >= 4 else ("#d29922" if c >= 3 else "#8b949e")
        return '<span style="background:' + color + '22;color:' + color + ';padding:1px 6px;border-radius:6px;font-size:10px;font-weight:700">共振 ' + str(c) + '/5</span>'

    buy_rows = ""
    for i, b in enumerate(top_buys, 1):
        sc = str(round(b["score"], 0)) if b["score"] is not None else "—"
        name_cell = ('<b>' + esc(b["ticker"]) + "</b><div style=\"font-size:11px;color:#c9d1d9;margin-top:2px\">" + esc(b["name"]) + "</div>"
                     + ind_line(b)
                     + ('<div style="font-size:10px;color:#8b949e;margin-top:2px">' + esc(b["sub"]) + "</div>" if b["sub"] else ""))
        why_cell = (esc(b["why"])
                    + ('<div style="margin-top:3px;color:#58a6ff;font-size:11px">' + esc(b["extra"]) + "</div>" if b["extra"] else "")
                    + ('<div style="margin-top:2px">' + conf_badge(b["conf"]) + "</div>"))
        buy_rows += ("<tr><td>" + str(i) + "</td><td style=\"text-align:left\">" + name_cell + "</td>"
                     + "<td>$" + format(b["close"], ".2f") + "</td><td>$" + format(b["entry"], ".2f") + "</td><td>$" + format(b["stop"], ".2f") + "</td><td>$" + format(b["target"], ".2f") + "</td>"
                     + '<td><span style="color:#3fb950;font-weight:700">' + format(b["rr"], ".1f") + ":1</span></td>"
                     + "<td>" + esc(b["rs"]) + "</td><td>" + esc(sc) + "</td>"
                     + '<td style="text-align:left;font-size:12px">' + why_cell + "</td></tr>")

    weak_rows = ""
    for i, r in enumerate(top_weak, 1):
        f = r.get("fundamentals", {}) or {}
        sig = r.get("signal", {}) or {}
        mom_s = format(sig.get("mom"), ".3f") if isinstance(sig.get("mom"), (int, float)) else "—"
        rs_s = str(r.get("rs_rating")) if r.get("rs_rating") is not None else "—"
        sc_s = format(r.get("score"), ".0f") if r.get("score") is not None else "—"
        note = "未站上MA20" if not sig.get("above_ma20") else "动量为负"
        weak_rows += ("<tr><td>" + str(i) + "</td><td style=\"text-align:left\"><b>" + esc(r["ticker"]) + "</b><div style=\"font-size:11px;color:#c9d1d9;margin-top:2px\">" + esc(f.get("name") or r["ticker"]) + "</div>" + ind_line({"sector": f.get("sector"), "industry": f.get("industry")}) + "</td>"
                      + "<td>$" + format(fnum(sig.get("close")), ".2f") + "</td><td>" + esc(mom_s) + "</td><td>" + esc(rs_s) + "</td><td>" + esc(sc_s) + "</td>"
                      + '<td style="text-align:left;font-size:12px;color:#8b949e">' + esc(note) + "</td></tr>")

    wl_rows = ""
    for w in wl_status:
        wl_rows += ("<tr><td><b>" + esc(w["ticker"]) + "</b></td><td style=\"text-align:left;color:#58a6ff;font-size:12px\">" + esc(w["state"]) + "</td><td style=\"text-align:left;color:#8b949e\">" + esc(w["note"]) + "</td></tr>")

    regime = "扫描池 " + str(total) + " 只；通过精选(ACTION+RS≥" + str(RS_FLOOR) + "+评分≥" + str(SCORE_FLOOR) + ") " + str(len(buys)) + " 只，展示 Top " + str(len(top_buys)) + "。"

    html_doc = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Peter research · 今日推荐 """ + esc(asof) + """</title>
<style>
body{background:#0d1117;color:#e6edf3;font-family:-apple-system,'Segoe UI',Roboto,'PingFang SC',sans-serif;margin:0;padding:24px;line-height:1.5}
h1{font-size:22px;margin:0 0 4px} h2{font-size:17px;margin:28px 0 10px;border-left:4px solid #1f6feb;padding-left:10px}
.sub{color:#8b949e;font-size:13px;margin-bottom:6px}
.card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:14px 16px;margin-bottom:14px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{background:#21262d;color:#c9d1d9;text-align:left;padding:8px 10px}
td{padding:8px 10px;border-top:1px solid #21262d;vertical-align:top}
tr:hover td{background:#1c2230}
.badge{display:inline-block;background:#3fb95022;color:#3fb950;padding:2px 8px;border-radius:6px;font-size:12px;font-weight:700}
.foot{color:#8b949e;font-size:12px;margin-top:30px;border-top:1px solid #30363d;padding-top:12px}
</style></head><body>
<h1>Peter research · 今日推荐 <span class="badge">TOP """ + str(len(top_buys)) + """ 买 / """ + str(len(top_weak)) + """ 回避</span></h1>
<div class="sub">数据源：bollinger-squeeze-screener 收盘扫描（asof """ + esc(asof) + """，宇宙 """ + str(d.get("rs_universe", "?")) + """ 只）。VCP status==ACTION 且 RS≥""" + str(RS_FLOOR) + """ 且 评分≥""" + str(SCORE_FLOOR) + """ 精选，按 共振 优先、评分次之 排序。</div>

<div class="card">
<b>盈亏比说明（结构化风险模型）：</b>买点 = VCP 枢轴（按 dist_to_pivot_pct 由收盘价回推）。止损 = 枢轴下方 <b>max(1.5×ATR, 3%)</b>，目标 = 枢轴 + <b>2.5×风险</b> → 每只 R:R = 2.5:1，但<b>止损/目标价位是按各自 ATR 算出的真实价位</b>，并非一律 3% 假设。实盘止损应依托基底低点/结构。<br>
<b>共振标签</b>：VCP ACTION + TTM挤压/TTM突破 + RS≥80 + 站上MA20且动量正 + 量能萎缩(VCP紧) 5 项对齐计数，越高越优。
</div>

<h2>① 今日推荐买入（精选 Top """ + str(len(top_buys)) + """，按 共振→评分 排序）</h2>
<table><thead><tr><th>#</th><th>代码 / 公司</th><th>当前价</th><th>买点</th><th>止损</th><th>目标</th><th>盈亏比</th><th>RS</th><th>评分</th><th>为什么买 / 共振</th></tr></thead>
<tbody>""" + buy_rows + """</tbody></table>

<h2>② 回避 / 弱势（动量负或未站上 MA20，Top """ + str(len(top_weak)) + """）</h2>
<table><thead><tr><th>#</th><th>代码 / 公司</th><th>当前价</th><th>动量</th><th>RS</th><th>评分</th><th>备注</th></tr></thead>
<tbody>""" + weak_rows + """</tbody></table>

<h2>③ 持仓 / 观察池核对（watchlist.txt）</h2>
<table><thead><tr><th>代码</th><th>今日状态</th><th>板块 / 评分</th></tr></thead>
<tbody>""" + wl_rows + """</tbody></table>

<div class="foot">
模块：today_picks.py（v2 精细化），读取 screener.py 产出的 squeeze_latest.json 自动生成。Peter research 每日扫描由 run_scan.sh 调度并推送至 GitHub。<br>
⚠️ 仅供参考，是否操作由你本人决策。AI 仅做数据整理与推送，不替代你下单。
</div>
</body></html>"""
    OUT_HTML.write_text(html_doc, encoding="utf-8")

    # ---------------- Markdown ----------------
    md = ["# Peter research · 今日推荐 (" + asof + ")\n",
          "数据源：bollinger-squeeze-screener 收盘扫描（宇宙 " + str(d.get("rs_universe", "?")) + " 只）。VCP ACTION 且 RS≥" + str(RS_FLOOR) + " 且 评分≥" + str(SCORE_FLOOR) + "。\n",
          "## ① 今日推荐买入 Top " + str(len(top_buys)) + "\n"]
    for i, b in enumerate(top_buys, 1):
        md.append(str(i) + ". **" + b["ticker"] + "** " + b["name"] + " — " + b["sector"] + "/" + b["industry"] + "\n"
                  + "   当前 $" + format(b["close"], ".2f") + " | 买点 $" + format(b["entry"], ".2f") + " | 止损 $" + format(b["stop"], ".2f") + " | 目标 $" + format(b["target"], ".2f") + " | R:R " + format(b["rr"], ".1f") + ":1 | RS " + str(b["rs"]) + " | 评分 " + format(b["score"], ".0f") + " | 共振 " + str(b["conf"]) + "/5\n"
                  + "   为什么：" + b["why"] + ((" [" + b["extra"] + "]") if b["extra"] else "") + "\n")
    md.append("\n## ② 回避 / 弱势 Top %d\n" % len(top_weak))
    for i, r in enumerate(top_weak, 1):
        f = r.get("fundamentals", {}) or {}
        sig = r.get("signal", {}) or {}
        md.append(str(i) + ". **" + r["ticker"] + "** " + str(f.get("name", "")) + " — 当前 $" + format(fnum(sig.get("close")), ".2f") + ", 动量 " + format(sig.get("mom"), ".3f") + ", RS " + str(r.get("rs_rating")) + ", 评分 " + format(r.get("score"), ".0f") + "\n")
    md.append("\n## ③ 持仓/观察池核对\n")
    for w in wl_status:
        md.append("- **" + w["ticker"] + "**: " + w["state"] + " — " + w["note"] + "\n")
    OUT_MD.write_text("\n".join(md), encoding="utf-8")

    print("BUY actionable (RS>=%d, score>=%d): %d | shown: %d" % (RS_FLOOR, SCORE_FLOOR, len(buys), len(top_buys)))
    print("WEAK: %d | shown: %d" % (len(weak), len(top_weak)))
    print("WATCHLIST cross-checked: %d" % len(wl_status))
    print("WROTE %s | %s" % (OUT_HTML, OUT_MD))


if __name__ == "__main__":
    main()
