"""
multidim.py — multi-dimensional score + rule-based narrative for the squeeze report.

Turns the raw fundamentals dict (from fundamentals.py) and the technical signal
into five 0-100 sub-scores, a weighted composite, a Chinese 基本面观点 (fundamental
view) paragraph, and a company-intro block.

Design intent: the squeeze scan answers "which names are coiling". This layer
answers "of those coils, which are also fundamentally decent and pointing the
right way" — so the user can triage the morning list fast. All rules are
deterministic (offline, no LLM) and explicitly conservative: missing data is
treated as neutral (50) rather than penalized.
"""

from __future__ import annotations


def _clamp(x, lo=0, hi=100):
    try:
        return max(lo, min(hi, x))
    except TypeError:
        return lo


def _quality_score(roe, profit_margin, debt_to_equity):
    parts = []
    if roe is not None:
        if roe <= 0: parts.append(12)
        elif roe < 0.08: parts.append(40)
        elif roe < 0.12: parts.append(52)
        elif roe < 0.18: parts.append(66)
        elif roe < 0.25: parts.append(80)
        elif roe < 0.35: parts.append(90)
        else: parts.append(95)
    if profit_margin is not None:
        if profit_margin <= 0: parts.append(12)
        elif profit_margin < 0.05: parts.append(42)
        elif profit_margin < 0.12: parts.append(58)
        elif profit_margin < 0.20: parts.append(74)
        elif profit_margin < 0.30: parts.append(86)
        else: parts.append(94)
    if debt_to_equity is not None:
        d = debt_to_equity
        if d <= 0.3: parts.append(92)
        elif d <= 0.6: parts.append(84)
        elif d <= 1.0: parts.append(72)
        elif d <= 1.5: parts.append(56)
        elif d <= 2.5: parts.append(40)
        else: parts.append(26)
    return sum(parts) / len(parts) if parts else None


def _value_score(forward_pe, pe, dividend_yield):
    pev = forward_pe if forward_pe is not None else pe
    if pev is None:
        return None
    if pev <= 0:
        return 22  # negative earnings -> structurally weak
    if pev <= 12: s = 90
    elif pev <= 15: s = 80
    elif pev <= 20: s = 70
    elif pev <= 25: s = 60
    elif pev <= 30: s = 50
    elif pev <= 40: s = 38
    elif pev <= 60: s = 28
    else: s = 20
    if dividend_yield:
        s = min(100, s + min(dividend_yield * 100 * 4, 10))
    return _clamp(s)


def _growth_score(rev_g, earn_g):
    vals = [v for v in (rev_g, earn_g) if v is not None]
    if not vals:
        return None
    g = sum(vals) / len(vals)
    if g <= -0.10: return 18
    if g < 0: return 38
    if g < 0.05: return 52
    if g < 0.10: return 62
    if g < 0.20: return 74
    if g < 0.30: return 84
    return 92


def _momentum_score(mom, above_ma20):
    import math
    base = 50 if above_ma20 else 32
    m = mom or 0.0
    return _clamp(base + 45 * math.tanh(m / 0.25))


def _squeeze_score(raw_score):
    return _clamp(raw_score)


# ---- formatting helpers ----
def _pct(x, dec=0):
    if x is None:
        return "—"
    return f"{x * 100:.{dec}f}%"


def _mcap_str(mc):
    if mc is None:
        return "—"
    if mc >= 1e12:
        return f"${mc / 1e12:.2f}T"
    if mc >= 1e9:
        return f"${mc / 1e9:.1f}B"
    if mc >= 1e6:
        return f"${mc / 1e6:.0f}M"
    return f"${mc:,.0f}"


def _intro_block(fund: dict) -> dict:
    summary = fund.get("summary") or ""
    if summary:
        summary = summary[:300] + ("…" if len(summary) > 300 else "")
    return {
        "name": fund.get("name") or "",
        "sector": fund.get("sector") or "—",
        "industry": fund.get("industry") or "—",
        "market_cap": _mcap_str(fund.get("marketCap")),
        "beta": fund.get("beta"),
        "summary": summary,
    }


def _narrative(fund: dict, composite: float, mom_dir: str) -> str:
    bits = []
    sector = fund.get("sector") or "—"
    industry = fund.get("industry") or "—"
    bits.append(f"所属板块：{sector} / {industry}。")

    fpe = fund.get("forwardPE") or fund.get("pe")
    if fpe is None:
        bits.append("估值数据缺失。")
    elif fpe <= 0:
        bits.append("当前亏损（负 PE），估值无参考意义。")
    else:
        label = "偏低" if fpe < 20 else ("合理" if fpe < 30 else "偏高")
        bits.append(f"远期 PE ≈ {fpe:.1f}（{label}）。")

    rg, eg = fund.get("revenueGrowth"), fund.get("earningsGrowth")
    if rg is not None or eg is not None:
        seg = []
        if rg is not None:
            seg.append(f"营收增速 {_pct(rg)}")
        if eg is not None:
            seg.append(f"盈利增速 {_pct(eg)}")
        bits.append("、".join(seg) + "。")
    else:
        bits.append("成长数据缺失。")

    pm, roe = fund.get("profitMargin"), fund.get("roe")
    if pm is not None or roe is not None:
        seg = []
        if pm is not None:
            seg.append(f"净利率 {_pct(pm)}")
        if roe is not None:
            seg.append(f"ROE {_pct(roe)}")
        bits.append("、".join(seg) + "。")

    de = fund.get("debtToEquity")
    if de is not None:
        lev = "低杠杆" if de <= 1.0 else ("中等杠杆" if de <= 2.0 else "高杠杆")
        bits.append(f"负债率(Debt/Eq) {de:.1f}，{lev}。")

    dy = fund.get("dividendYield")
    if dy:
        bits.append(f"股息率 {_pct(dy, 1)}。")

    # verdict line
    tag = "强" if composite >= 75 else ("中等" if composite >= 60 else "中性偏弱")
    if mom_dir == "up":
        bits.append(f"综合多维度评分 {composite:.0f}（{tag}），且动量向上，右侧突破概率较高。")
    elif mom_dir == "dn":
        bits.append(f"综合多维度评分 {composite:.0f}（{tag}），但动量向下，宜等企稳/放量向上再介入。")
    else:
        bits.append(f"综合多维度评分 {composite:.0f}（{tag}），方向待确认。")
    return "".join(bits)


def multidim_scores(fund: dict, signal: dict, squeeze_raw_score: float) -> dict:
    """Return the 5 sub-scores, composite, narrative, and intro block."""
    q = _quality_score(fund.get("roe"), fund.get("profitMargin"), fund.get("debtToEquity"))
    v = _value_score(fund.get("forwardPE"), fund.get("pe"), fund.get("dividendYield"))
    g = _growth_score(fund.get("revenueGrowth"), fund.get("earningsGrowth"))
    mom = _momentum_score(signal.get("mom"), signal.get("above_ma20"))
    sq = _squeeze_score(squeeze_raw_score)

    # missing fundamental dims default to neutral (50) so composite isn't skewed
    qc = q if q is not None else 50.0
    vc = v if v is not None else 50.0
    gc = g if g is not None else 50.0
    composite = 0.35 * sq + 0.22 * mom + 0.15 * qc + 0.13 * vc + 0.15 * gc

    mom_dir = "up" if (signal.get("mom") or 0) > 0 else ("dn" if (signal.get("mom") or 0) < 0 else "—")
    return {
        "dims": {
            "squeeze": round(sq, 1),
            "momentum": round(mom, 1),
            "quality": (round(q, 1) if q is not None else None),
            "value": (round(v, 1) if v is not None else None),
            "growth": (round(g, 1) if g is not None else None),
        },
        "composite": round(composite, 1),
        "narrative": _narrative(fund, composite, mom_dir),
        "intro": _intro_block(fund),
        "fund_missing": q is None and v is None and g is None,
    }


if __name__ == "__main__":
    import json
    import sys
    from fundamentals import get_fundamentals
    sig = {"mom": 0.05, "above_ma20": True}
    for t in (sys.argv[1:] or ["RAMP"]):
        f = get_fundamentals(t)
        md = multidim_scores(f, sig, 100.0)
        print(f"\n=== {t} ===")
        print("intro:", md["intro"])
        print("dims:", md["dims"])
        print("composite:", md["composite"])
        print("view:", md["narrative"])
