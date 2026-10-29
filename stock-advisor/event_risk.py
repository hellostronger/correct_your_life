# -*- coding: utf-8 -*-
"""重大事件（财报）的条件式风险预案：**披露前冻结预期，披露后核对实际**。

为什么单独一个模块
------------------
现有链路只到「财报排期 → 财经日历 → 今明提醒 / 日报一句话」，
没有「事前把预期钉死、事后拿实际值去对」的闭环，更没有「不达标怎么办」。
用户要的是 **if / else if / else 的动作预案**：

    天孚通信案例：市场预期 9 亿，实际没达标 → 开盘快跑
    摩尔线程案例：估值太高 → 反推「需要多少利润才撑得住」→ 披露前先离场

所以本模块**不碰交易**：不 import `paper_trading`，不下单、不改风控、不改持仓。
只产出建议文本 + 结构化 JSON，供日历页 / 日报 / 通知共用同一份措辞。

五类「预期」不能互相冒充（这是本模块最容易被做错的地方）
--------------------------------------------------------
| 类型 | 来源 | 能当什么用 |
|---|---|---|
| 市场一致预期（季度净利润） | **任何公开免费源都取不到** | 只能标成「不可得」 |
| 机构一致预期（**年度** EPS） | 101 `/financials/consensus` | 只能**推算**到季度，且必须标注推算方法 |
| 公司业绩预告区间 | 101 `/financials/preannounce` | 公司自己给的数，比推算强，但仍不是实际值 |
| 估值隐含利润要求 | 实时市值 ÷ 同行 PE 中位 | 「这个估值需要多少利润」，**不是共识** |
| 正式财报实际值 | 101 `/financials/report` | 唯一的事实 |

用户举的「天孚 9 亿」是**季度共识**，实测在 akshare 全家桶里取不到（`consensus`
只有年度 EPS）。本模块的处理是：把它当「无法核证的市场传闻」，绝不写死，
而是在 `gaps` 里明写「季度一致预期不可得，X 亿无法核证」，并用**两种独立推算**
并列展示 —— 两种推算分歧大时本身就是「不要赌」的证据。

估值反推的正确公式（错了会得出完全相反的结论）
----------------------------------------------
    所需利润 = 总市值 ÷ 同行 PE 中位数

不是「市值 ÷ 本股 PE」，也不是「一致预期 × PE」。算出来的是
「**维持当前估值需要多少利润**」，要和实际/预告放在一起看缺口。

单位口径（全程元，展示层才换亿）
--------------------------------
101 `/financials/*` 一律**元**、`%/元每股`；`/quote` 的 `market_cap` 是**亿元**。
两种单位混用会差 1e8 倍且不报错 —— 本模块在边界处显式换算，函数名带单位。

持久化
------
`data/event_plans/{code}_{period}.json`，原子写（`os.replace`）。
**不新增数据库表**（改 schema 影响面超出单次会话）；JSON 同时是可读审计材料，
能回答「当时到底冻结了哪些数」。
"""
from __future__ import annotations

import json
import math
import os
from datetime import date, datetime, timedelta, timezone
from statistics import median

import market_data

TZ = timezone(timedelta(hours=8))

DEFAULTS = {
    "beat_pct": 10.0,          # ≥ 预期 ×(1+10%) → 超预期
    "miss_pct": 10.0,          # <  预期 ×(1-10%) → 不及预期
    "deep_miss_pct": 30.0,     # <  预期 ×(1-30%) → 大幅不及（快跑）
    "expensive_ratio": 1.5,    # 本股 PE > 同行中位 ×1.5 → 估值偏贵
    "cheap_ratio": 0.8,        # 本股 PE < 同行中位 ×0.8 → 估值偏便宜
    "peer_cap": 60,            # 同行取样上限（/quote 批量，URL 长度与耗时都有限）
    "peer_min_count": 5,       # 同行有效样本 < 5 → 中位数不可信，判「计算失效」
    "spread_warn_pct": 30.0,   # 两种推算分歧超过此值 → 明写「预期本身不可靠」
    "beat_band": 0.10,         # 分支匹配时「符合预期」的容差带
    "valuation_severe_pct": 20.0,  # 利润 < 估值要求的 20% → 视为没有利润支撑
    "valuation_weak_pct": 50.0,    # 利润 < 估值要求的 50% → 减仓
}

_DIR = os.path.dirname(os.path.abspath(__file__))
PLAN_DIR = os.path.join(_DIR, "data", "event_plans")

_QMONTHS = {"03": 1, "06": 2, "09": 3, "12": 4}


# ---------------------------------------------------------------- 小工具

def _f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _yi(v):
    """元 → 亿元（展示用，保留 2 位以外不 round）。"""
    x = _f(v)
    return None if x is None else x / 1e8


def _q(iso):
    """'2026-09-30' → 3（第几季度末）。"""
    return _QMONTHS.get(str(iso or "")[5:7])


def _prev_q(iso):
    """上一个季末报告期（ISO）。"""
    d = date.fromisoformat(str(iso)[:10])
    m = d.month
    if m <= 3:
        return "%04d-12-31" % (d.year - 1)
    if m <= 6:
        return "%04d-03-31" % d.year
    if m <= 9:
        return "%04d-06-30" % d.year
    return "%04d-09-30" % d.year


def _same_q_last_year(iso):
    return "%04d%s" % (int(str(iso)[:4]) - 1, str(iso)[4:10])


def _annualize(cum, iso):
    """累计净利润 → 年化（季报累计 × 4/季度数）。同行与本股用同一口径。"""
    q = _q(iso)
    v = _f(cum)
    return None if (v is None or not q) else v * 4.0 / q


def _ttm(series: dict, latest_iso: str):
    """近四季度 —— 最新累计 + 上年年报 - 上年同期累计。"""
    cur = _f(series.get(latest_iso))
    fy = _f(series.get("%04d-12-31" % (int(latest_iso[:4]) - 1)))
    prev = _f(series.get(_same_q_last_year(latest_iso)))
    if cur is None or fy is None or prev is None:
        return None
    return cur + fy - prev


def _today():
    return datetime.now(TZ)


def _now_iso():
    return _today().isoformat(timespec="seconds")


# ---------------------------------------------------------------- 101 客户端

def _svc(path: str, params: dict, timeout: int = 120) -> dict:
    """所有财务/行情都经 101（用户硬性要求：本机不直连任何数据源）。"""
    return market_data.service_get(path, params, timeout=timeout)


#: 业绩报表整表在 101 容器里**冷启动实测 229 秒**（`stock_yjbb_em` 4254 行），
#: 热缓存 2 秒、TTL 15 分钟。默认 120 秒会在冷启动时直接 ReadTimeout，
#: 表现成「预案加载失败」而不是「稍等」，所以财务端点单独放宽。
SLOW_TIMEOUT = 600


def fetch_bundle(code: str, period: str) -> dict:
    """101 `/financials/bundle`：report / preannounce / express / consensus / abstract。"""
    return _svc("/financials/bundle",
                {"code": code, "period": period.replace("-", "")},
                timeout=SLOW_TIMEOUT)


def fetch_report(period: str, codes=None) -> dict:
    p = str(period).replace("-", "")
    params = {"period": p}
    if codes:
        params["codes"] = ",".join(codes) if isinstance(codes, (list, tuple)) else str(codes)
    return _svc("/financials/report", params, timeout=SLOW_TIMEOUT)


def fetch_quotes(codes: list[str]) -> dict:
    """批量实时行情（含 market_cap，单位亿元）。"""
    syms = [market_data.tx_symbol(c) for c in codes]
    out = _svc("/quote", {"symbols": ",".join(syms)}, timeout=40)
    return (out or {}).get("quotes") or {}


def _deps(deps=None) -> dict:
    d = {
        "bundle": fetch_bundle,
        "report": fetch_report,
        "quote": fetch_quotes,
        "now": _now_iso,
    }
    if deps:
        d.update(deps)
    return d


# ---------------------------------------------------------------- 预期推算

def _shares_est(series: dict) -> tuple[float | None, str]:
    """用「同期累计净利润 ÷ 同期累计 EPS」近似总股本（加权股本，通常很接近）。"""
    np_s = series.get("net_profit") or {}
    eps_s = series.get("eps") or {}
    for d in sorted(eps_s, reverse=True):
        npv, eps = _f(np_s.get(d)), _f(eps_s.get(d))
        if npv and eps and eps > 0 and npv > 0:
            return npv / eps, d
    return None, ""


def _season_frac(series: dict, month: str) -> tuple[float | None, list]:
    """历史「本季累计 / 当年全年」的中位数 —— 把年度共识摊到季度上。"""
    np_s = series.get("net_profit") or {}
    fr, used = [], []
    for d, v in np_s.items():
        if str(d)[5:7] != month:
            continue
        ann = _f(np_s.get("%s-12-31" % str(d)[:4]))
        cum = _f(v)
        if ann is None or ann <= 0 or cum is None:
            continue
        r = cum / ann
        if -3 < r < 3:                       # 亏损年/异常年不进中位数
            fr.append(r)
            used.append(str(d)[:4])
    if not fr:
        return None, []
    return median(fr), sorted(used)


def _consensus_eps(cons: dict, year: int):
    eps = ((cons or {}).get("eps_by_year") or {})
    v = _f(eps.get(str(year)))
    return v, len(eps)


def estimates(code: str, period: str, parts: dict, conf: dict) -> dict:
    """把可得数据摊成「两条独立推算 + 分歧度」。

    返回 {items: [...], ref: value|None, spread_pct, notes: [...]}
    每条 item 都带 method / value / detail / confidence，**绝不冒充共识**。
    """
    cons = parts.get("consensus") or {}
    abs_ = parts.get("abstract") or {}
    series = abs_.get("series") or {}
    iso = str(period)[:10]
    q = _q(iso)
    notes, items = [], []

    if q is None:
        notes.append("报告期不是季末，无法按季度摊分")
        return {"items": [], "ref": None, "spread_pct": None, "notes": notes}

    shares, share_from = _shares_est(series)
    eps, eps_years = _consensus_eps(cons, int(iso[:4]))
    frac, frac_years = _season_frac(series, iso[5:7])

    if eps is not None and shares:
        ann_cons = eps * shares
        items.append({
            "method": "年度一致预期 × 历史季节性",
            "value": ann_cons * frac if frac is not None else None,
            "detail": ("一致预期 %s 年 EPS %.4f × 近似股本 %.2f 亿（按 %s 的净利润/EPS 反推）"
                       "= 年度约 %.2f 亿；再乘历史同季累计占比 %s"
                       % (iso[:4], eps, shares / 1e8, share_from,
                          ann_cons / 1e8,
                          ("%.1f%%（%d 年样本 %s）"
                           % (frac * 100, len(frac_years), "/".join(frac_years)))
                          if frac is not None and iso[5:7] != "12" else "1（年报）")),
            "confidence": "low" if eps_years <= 1 else "medium",
        })
        if frac is None:
            notes.append("历史季节性算不出（缺年报或当年亏损），只能给年度共识本身")
    else:
        if eps is None:
            notes.append("无机构一致预期（或该年度无覆盖）")
        if not shares:
            notes.append("无法反推股本（缺同期 EPS/净利润），年度共识无法折算成金额")

    lc = _f(series.get("net_profit", {}).get(_same_q_last_year(iso)))
    last_ann = _f(series.get("net_profit", {}).get("%04d-12-31" % (int(iso[:4]) - 1)))
    eps_ly, _ = _consensus_eps(cons, int(iso[:4]) - 1)
    if lc is not None and eps is not None and eps_ly and eps_ly > 0:
        g = eps / eps_ly - 1
        items.append({
            "method": "去年同期 × 一致增速",
            "value": lc * (1 + g),
            "detail": ("去年同期累计 %.2f 亿 × (1 + 一致预期增速 %+.1f%%"
                       " = %s 年 EPS %.4f / %s 年 %.4f - 1)"
                       % (lc / 1e8, g * 100, iso[:4], eps, int(iso[:4]) - 1, eps_ly)),
            "confidence": "low",
        })
    elif lc is None:
        notes.append("缺去年同期累计值，无法用「去年×增速」推算")
    elif last_ann is None:
        notes.append("缺上年年报，无法用「去年×增速」推算")

    vals = [it["value"] for it in items if it["value"] is not None]
    ref = spread = None
    if vals:
        ref = median(vals)
        if len(vals) >= 2 and ref:
            spread = (max(vals) - min(vals)) / abs(ref) * 100
            if spread >= conf["spread_warn_pct"]:
                notes.append("两种推算分歧 %.1f%%（>%.0f%%）：**预期本身不可靠**，"
                             "不应据此单边下注" % (spread, conf["spread_warn_pct"]))
    return {"items": items, "ref": ref, "spread_pct": spread, "notes": notes,
            "shares_est": shares, "season_frac": frac}


# ---------------------------------------------------------------- 估值反推

def _peers(code: str, industry: str, base_period: str, deps: dict,
           conf: dict) -> dict:
    """同行（东财行业口径）+ 年化 PE 中位数。全部经 101，无本机直连。"""
    out = {"industry": industry, "count": 0, "median_pe": None,
           "sample": [], "notes": [], "basis": "最新季报累计年化"}
    if not industry:
        out["notes"].append("基准报告期无行业字段，同行不可比")
        return out
    try:
        rows = (deps["report"](base_period) or {}).get("items") or []
    except Exception as exc:                    # noqa: BLE001
        out["notes"].append("同行财务取数失败：%s: %s"
                            % (type(exc).__name__, str(exc)[:120]))
        return out
    peers = [r for r in rows
             if r.get("industry") == industry and r.get("code") != code
             and _f(r.get("net_profit")) and _f(r.get("net_profit")) > 0]
    # 盈利为正且越大越稳；同行业可能上百家，按利润规模取前 N
    peers.sort(key=lambda r: -_f(r.get("net_profit")))
    peers = peers[:conf["peer_cap"]]
    if not peers:
        out["notes"].append("同行中没有「已披露且盈利为正」的公司，PE 不可比")
        return out
    try:
        caps = deps["quote"]([p["code"] for p in peers]) or {}
    except Exception as exc:                    # noqa: BLE001
        out["notes"].append("同行市值取数失败：%s: %s"
                            % (type(exc).__name__, str(exc)[:120]))
        return out
    pes = []
    for p in peers:
        sym = market_data.tx_symbol(p["code"])
        cap = _f((caps.get(sym) or {}).get("market_cap"))     # 亿元
        ann = _annualize(p.get("net_profit"), str(p.get("period") or base_period))
        if not cap or cap <= 0 or not ann or ann <= 0:
            continue
        pe = cap / (ann / 1e8)
        if not (0 < pe < 500):
            continue
        pes.append(pe)
        out["sample"].append({"code": p["code"], "name": p.get("name"),
                              "pe": round(pe, 2),
                              "market_cap_yi": round(cap, 2)})
    out["count"] = len(pes)
    if pes:
        out["median_pe"] = median(pes)
        out["sample"].sort(key=lambda x: -x["market_cap_yi"])
        out["sample"] = out["sample"][:12]
    if len(pes) < conf["peer_min_count"]:
        out["notes"].append("同行有效样本只有 %d 家（<%d），中位数不可信"
                            % (len(pes), conf["peer_min_count"]))
    return out


def valuation(code: str, parts: dict, base_period: str, deps: dict,
              conf: dict) -> dict:
    """市值 / 本股 PE / 同行 PE / **所需利润**，以及估值状态。"""
    out = {"market_cap_yi": None, "pe_annualized": None, "pe_ttm": None,
           "profit_ttm_yi": None, "peer": {}, "required": {},
           "state": "invalid", "reason": "", "notes": []}

    try:
        q = (deps["quote"]([code]) or {}).get(market_data.tx_symbol(code)) or {}
    except Exception as exc:                    # noqa: BLE001
        q = {}
        out["notes"].append("市值取数失败：%s: %s"
                            % (type(exc).__name__, str(exc)[:120]))
    cap = _f(q.get("market_cap"))
    out["market_cap_yi"] = cap

    rep = parts.get("report") or {}
    abs_ = parts.get("abstract") or {}
    series = abs_.get("series") or {}
    np_s = series.get("net_profit") or {}

    # 本股 PE：优先 TTM（更准），没有就退最新累计年化
    latest = None
    for d in sorted(np_s, reverse=True):
        if _f(np_s.get(d)) is not None:
            latest = d
            break
    ttm = _ttm(np_s, latest) if latest else None
    if ttm:
        out["profit_ttm_yi"] = round(ttm / 1e8, 4)
    ann = _annualize(_f(rep.get("net_profit")), str(rep.get("period") or base_period))

    if cap and ttm and ttm > 0:
        out["pe_ttm"] = cap / (ttm / 1e8)
    if cap and ann and ann > 0:
        out["pe_annualized"] = cap / (ann / 1e8)

    peer = _peers(code, rep.get("industry") or "", base_period, deps, conf)
    out["peer"] = peer
    med = peer.get("median_pe")
    self_pe = out["pe_ttm"] or out["pe_annualized"]

    req = {"annual_profit_yi": None, "period_profit_yi": None,
           "status": "invalid", "reason": ""}
    if not cap:
        req["reason"] = "缺实时总市值，无法反推所需利润"
    elif not med or peer.get("count", 0) < conf["peer_min_count"]:
        req["reason"] = "同行 PE 中位数不可得/样本不足，估值隐含利润无法计算"
    elif not self_pe or self_pe <= 0:
        req["reason"] = ("本股 PE 为负或缺失（亏损/无利润），"
                         "「所需利润」与「实际利润」无法同口径比较 —— 计算失效")
    else:
        req["annual_profit_yi"] = cap / med
        frac, used = _season_frac(series, str(base_period)[5:7])
        req["period_profit_yi"] = (cap / med) * frac if frac else None
        req["status"] = "ok"
        req["detail"] = ("总市值 %.2f 亿 ÷ 同行 PE 中位 %.1f = 维持该估值需年利润约 %.2f 亿"
                         % (cap, med, cap / med))
        if req["period_profit_yi"] is not None:
            req["detail"] += ("；按历史同季占比 %.1f%%（%s）折算本季累计需约 %.2f 亿"
                              % (frac * 100, "/".join(used),
                                 req["period_profit_yi"]))
    out["required"] = req

    if req["status"] != "ok" or not self_pe:
        out["state"] = "invalid"
        out["reason"] = req["reason"]
    elif self_pe > med * conf["expensive_ratio"]:
        out["state"] = "expensive"
        out["reason"] = ("本股 PE %.1f 高于同行中位 %.1f 的 %.0f%%"
                         % (self_pe, med, conf["expensive_ratio"] * 100))
    elif self_pe < med * conf["cheap_ratio"]:
        out["state"] = "cheap"
        out["reason"] = "本股 PE %.1f 低于同行中位 %.1f 的 %.0f%%" % (
            self_pe, med, conf["cheap_ratio"] * 100)
    else:
        out["state"] = "fair"
        out["reason"] = "本股 PE %.1f 与同行中位 %.1f 相仿" % (self_pe, med)
    return out


# ---------------------------------------------------------------- 预告三态

def guidance_state(pre_now, pre_last_year, disclose_date, conf: dict) -> dict:
    """「无预告」到底属于哪一种 —— 三种含义完全不同，不能混成一句「没发预告」。

    present       有预告（可直接用区间中值）
    missing       去年同期发了、今年没发 → **强风险信号**（尤其临近披露日）
    overdue       已过预约披露日仍无预告也无正式报告 → 异常
    not_due       尚未到披露期 → 正常

    无法判定的第二态（「不满足强制预告条件」）在公开数据里根本没有依据，
    只能写进 gaps 说明，**不许编一个结论**。
    """
    if pre_now:
        return {"state": "present", "reason": "已发布业绩预告"}
    if pre_last_year:
        return {"state": "missing",
                "reason": "去年同期有业绩预告、本期没有 —— 公司通常发预告，"
                          "本期缺失是风险信号"}
    today = date.fromisoformat(conf["today"])
    if disclose_date:
        try:
            dd = date.fromisoformat(str(disclose_date)[:10])
        except ValueError:
            dd = None
        if dd and today > dd:
            return {"state": "overdue",
                    "reason": "已过预约披露日 %s 仍无预告/正式报告" % dd}
    return {"state": "not_due", "reason": "尚未到披露期，未发预告属正常"}


# ---------------------------------------------------------------- 分支预案

def _pct(v):
    return "%.1f%%" % (v * 100) if v is not None else "—"


def plan_valuation_branches(val, conf) -> dict:
    """**没有市场预期时**改用「估值隐含利润」做基准备案。

    为什么必须有这条：高估值又没有一致预期（亏损股 / 次新股 / 卖方覆盖太少）
    恰恰是最该躲的一类 —— 此时若只说「分支不可用」，用户拿到的是一句正确的废话。
    而没有预期不等于没有基准：「维持现在的市值需要多少利润」= 总市值 ÷ 同行 PE
    中位，是能算出来的；实际利润对它的覆盖率就能判分支。

    口径说清楚：这条线衡量的是「当前估值要多少利润才撑得住」，
    **不是市场共识**，也不考虑明后年的增长故事（那是预期，不是事实）。
    """
    req = (val or {}).get("required") or {}
    if req.get("status") != "ok":
        return {"available": False, "branches": [], "thresholds": {},
                "reason": req.get("reason") or "估值隐含利润不可得"}
    annual = _f(req.get("annual_profit_yi"))
    period = _f(req.get("period_profit_yi"))
    if period is not None and period > 0:
        base, scope = period, "本季累计"
    else:
        base, scope = annual, "年化"
    if not base or base <= 0:
        return {"available": False, "branches": [], "thresholds": {},
                "reason": "估值隐含利润不可得（总市值或同行 PE 缺失）"}
    severe = base * conf["valuation_severe_pct"] / 100.0
    weak = base * conf["valuation_weak_pct"] / 100.0
    b = [
        {"when": "%s ≤ 0（由盈转亏）" % scope,
         "branch": "valuation_loss", "level": "critical",
         "then": "**离场**：利润为负或归零，当前估值没有任何利润支撑"},
        {"when": "%s < %.2f 亿（不到估值所需的 %.0f%%）"
                 % (scope, severe, conf["valuation_severe_pct"]),
         "branch": "valuation_severe", "level": "critical",
         "then": "**离场**：利润只覆盖维持该估值所需的一小部分，不等反弹"
                 "（若市场买的是明后年增长，本口径会低估其合理性 —— 但那是预期不是事实）"},
        {"when": "%.2f 亿 ≤ %s < %.2f 亿（估值所需的 %.0f%%~%.0f%%）"
                 % (severe, scope, weak, conf["valuation_severe_pct"],
                    conf["valuation_weak_pct"]),
         "branch": "valuation_weak", "level": "high",
         "then": "减仓（先减一半以上），不补仓"},
        {"when": "%.2f 亿 ≤ %s < %.2f 亿（估值所需的 %.0f%%~100%%）"
                 % (weak, scope, base, conf["valuation_weak_pct"]),
         "branch": "valuation_fair", "level": "info",
         "then": "不加仓，持有观察 —— 利润尚未覆盖维持当前估值所需"},
        {"when": "%s ≥ %.2f 亿（利润已覆盖维持该估值所需）" % (scope, base),
         "branch": "valuation_ok", "level": "info",
         "then": "持有观察；仍要看增速能否延续，别把一次性利润当常态"},
    ]
    return {"available": True, "branches": b,
            "thresholds": {"annual": annual, "period": period, "base": base,
                           "severe": severe, "weak": weak, "scope": scope}}


def plan_branches(est_ref, val, conf) -> dict:
    """if / else if / else。基准优先用市场预期；**没有预期时退到估值隐含利润**。

    两套基准不能混着比（年度共识 vs 单季累计），所以 `basis` 字段必须跟着分支
    一起返回，`build_plan` 用它决定披露后走哪个匹配函数。
    """
    state = (val or {}).get("state")
    expensive = state == "expensive"
    invalid = state == "invalid"

    extra = []
    if expensive:
        extra.append({
            "when": "披露前（无论预期如何）",
            "branch": "pre_event_de-risk", "level": "high",
            "then": "**不在披露前博弈**：估值高于同行中位，"
                    "「不达标」的下行远大于「超预期」的上行，先减仓再等结果",
        })
    if invalid:
        extra.append({
            "when": "估值口径失效（PE≤0 / 同行不可比）",
            "branch": "valuation_invalid", "level": "high",
            "then": "**无法判断贵不贵 → 按最坏情况处理**：亏损/负 PE 的公司一旦"
                    "业绩不及预期没有估值底，披露前不加仓、披露后不抢反弹",
        })

    # 负基准不能做百分比分支：est_ref=-10 亿、实际 -20 亿时
    # dev = -20/-10-1 = +100% → 会被判成「超预期」，亏得越多越像利好。
    ref_n = _f(est_ref)
    if ref_n is None or ref_n <= 0:
        vb = plan_valuation_branches(val, conf)
        if vb["available"]:
            return {"available": True, "basis": "valuation",
                    "branches": extra + vb["branches"],
                    "thresholds": vb["thresholds"],
                    "reason": ("本期一致预期/预告为负（%.2f 亿元，公司处于亏损），"
                               "百分比偏离在亏损口径下会反向，改用估值隐含利润做基准"
                               % _yi(ref_n)) if ref_n is not None and ref_n < 0 else ""}
        if ref_n is not None and ref_n < 0:
            reason = ("本期一致预期/预告为负（%.2f 亿元，公司处于亏损），"
                      "「高于/低于预期百分之多少」在亏损口径下会反向（亏得更多反而"
                      "算出正增幅），所以不按增幅做 if/else —— 请按亏损是收窄还是扩大、"
                      "绝对额与现金流自行判断" % _yi(ref_n))
        else:
            reason = ("无法推算本期预期（缺一致预期/历史季报），分支阈值没有基准，"
                      "不做 if/else —— 请按公告原文与现金流自行判断")
        _rq = (val or {}).get("required") or {}
        if _rq.get("reason"):
            reason += "（估值隐含利润也不可得：%s）" % _rq["reason"]
        return {"available": False, "basis": "", "reason": reason,
                "branches": extra}

    beat = ref_n * (1 + conf["beat_pct"] / 100.0)
    miss = ref_n * (1 - conf["miss_pct"] / 100.0)
    deep = ref_n * (1 - conf["deep_miss_pct"] / 100.0)

    b = []
    b.append({
        "when": "实际（或预告中值）≥ %.2f 亿（预期 %.2f 亿的 %+.0f%% 以上）"
                % (_yi(beat), _yi(ref_n), conf["beat_pct"]),
        "branch": "beat", "level": "info",
        "then": "持有观察；估值不贵可分批加仓" if not expensive
                else "持有但不加仓 —— 估值已高于同行中位（%s），超预期更可能是"
                     "「兑现」而非「重估」" % (val.get("reason") or ""),
    })
    b.append({
        "when": "%.2f 亿 ~ %.2f 亿（预期 ±%.0f%% 内）"
                % (_yi(miss), _yi(beat), conf["miss_pct"]),
        "branch": "inline", "level": "info",
        "then": "持有观察，不动仓位；重点看下一季指引与毛利率是否走坏"
                if not expensive else
                "不加仓；估值已偏高，仅靠「符合预期」撑不住，跌破关键均线先减机动仓",
    })
    b.append({
        "when": "%.2f 亿 ~ %.2f 亿（低于预期 %.0f%%~%.0f%%）"
                % (_yi(deep), _yi(miss), conf["miss_pct"], conf["deep_miss_pct"]),
        "branch": "miss", "level": "high",
        "then": ("开盘减仓（先减一半以上），不补仓、不摊薄"
                 + ("；估值又高于同行中位，减到只留底仓" if expensive else "")),
    })
    b.append({
        "when": "< %.2f 亿（低于预期 %.0f%% 以上）或由盈转亏"
                % (_yi(deep), conf["deep_miss_pct"]),
        "branch": "deep_miss", "level": "critical",
        "then": "**开盘先离场（清仓），不在下跌里找理由**"
                + ("；当前估值已无利润支撑（%s），等财报证实再谈" % (val.get("reason") or "")
                   if expensive else ""),
    })

    return {"available": True, "basis": "estimate", "branches": extra + b,
            "thresholds": {"beat": beat, "inline_low": miss, "deep_miss": deep},
            "reason": ""}


def match_branch(value, est_ref, conf) -> dict:
    """实际值落在哪个分支（披露后用）。"""
    ref_n = _f(est_ref)
    if value is None or ref_n is None or ref_n == 0:
        return {"branch": "unknown", "dev_pct": None,
                "reason": "缺实际值或预期基准，无法匹配分支"}
    if ref_n < 0:
        # 亏损口径下 value/ref-1 的符号会反：亏损从中值 -10 亿扩大到 -20 亿
        # 算出 +100%（像超预期），收窄到 -5 亿算出 -50%（像暴跌）。
        return {"branch": "unknown", "dev_pct": None,
                "reason": ("预期基准为负（%.2f 亿元，公司处于亏损），"
                           "「实际/预期-1」在亏损口径下符号会反向"
                           "（亏得更多反而算出正增幅），不据此判定分支 —— "
                           "请按亏损是收窄还是扩大、绝对额判断"
                           % _yi(ref_n))}
    dev = value / ref_n - 1
    if dev >= conf["beat_pct"] / 100.0:
        br = "beat"
    elif dev > -conf["miss_pct"] / 100.0:
        br = "inline"
    elif dev > -conf["deep_miss_pct"] / 100.0:
        br = "miss"
    else:
        br = "deep_miss"
    return {"branch": br, "dev_pct": dev, "reason": ""}


def match_valuation_branch(value, val, conf, period: str = "") -> dict:
    """实际利润落在「估值隐含利润」的哪个分支（披露后用）。`value` 是**元**。

    `annual_profit_yi` 是年度口径、`period_profit_yi` 是单季累计口径，所以实际值
    也要按同一口径折算：有单季要求就比单季，否则把累计利润年化（×4/季度数）。
    """
    req = (val or {}).get("required") or {}
    if req.get("status") != "ok":
        return {"branch": "unknown", "dev_pct": None, "basis": "valuation",
                "scope": "", "target_yi": None, "actual_yi": None,
                "reason": req.get("reason") or "估值隐含利润不可得"}
    annual = _f(req.get("annual_profit_yi"))
    period_req = _f(req.get("period_profit_yi"))
    if period_req is not None and period_req > 0:
        base, scope = period_req, "本季累计"
        got = None if value is None else _f(value) / 1e8
    else:
        base, scope = annual, "年化"
        ann = _annualize(value, period) if (value is not None and period) else None
        got = None if ann is None else ann / 1e8
    if not base or base <= 0:
        return {"branch": "unknown", "dev_pct": None, "basis": "valuation",
                "scope": scope, "target_yi": None, "actual_yi": got,
                "reason": "估值隐含利润不可得（总市值或同行 PE 缺失）"}
    if got is None:
        return {"branch": "unknown", "dev_pct": None, "basis": "valuation",
                "scope": scope, "target_yi": base, "actual_yi": None,
                "reason": "缺实际值或无法年化，无法匹配分支"}
    dev = got / base - 1
    if got <= 0:
        br = "valuation_loss"
    elif got < base * conf["valuation_severe_pct"] / 100.0:
        br = "valuation_severe"
    elif got < base * conf["valuation_weak_pct"] / 100.0:
        br = "valuation_weak"
    elif got < base:
        br = "valuation_fair"
    else:
        br = "valuation_ok"
    return {"branch": br, "dev_pct": dev, "basis": "valuation", "scope": scope,
            "target_yi": base, "actual_yi": got, "reason": ""}


_ACTION = {
    "beat": ("持有观察 / 可分批加仓", "info"),
    "inline": ("持有观察，不动仓位", "info"),
    "miss": ("减仓（先减一半以上），不补仓", "high"),
    "deep_miss": ("开盘先离场（清仓），不抢反弹", "critical"),
    "valuation_loss": ("**离场**：利润为负或归零，高估值没有任何利润支撑",
                       "critical"),
    "valuation_severe": ("**离场**：利润只覆盖估值要求的一小部分，不抢反弹",
                         "critical"),
    "valuation_weak": ("减仓（先减一半以上），不补仓", "high"),
    "valuation_fair": ("不加仓，持有观察 —— 利润尚未覆盖维持当前估值所需", "info"),
    "valuation_ok": ("利润已覆盖维持当前估值所需，持有观察", "info"),
    "unknown": ("数据不足以判断，按公告原文与现金流自行判断", "info"),
}


# ---------------------------------------------------------------- 组装

def build_plan(code: str, period: str, disclose_date: str = "",
               conf: dict | None = None, deps: dict | None = None,
               save: bool = True) -> dict:
    """冻结一只票在一个报告期的全部可得事实，产出 if/else 预案并落盘。

    `disclose_date` 是该期的预约披露日（来自 `earnings_sync` 事件），可为空。
    已披露（`report` 有值）时这份 plan 同时带 `advice` —— 所以披露前后**同一份
    文件**就能回答「当时怎么想的 / 现在该怎么做」。
    """
    conf = {**DEFAULTS, **(conf or {})}
    deps = _deps(deps)
    conf["today"] = str(conf.get("today") or _today().date())
    period = str(period)[:10]
    base_period = _prev_q(period)
    gaps: list[str] = []

    data = deps["bundle"](code, period) or {}
    parts = data.get("parts") or {}
    errs = data.get("errors") or {}
    for k, v in errs.items():
        gaps.append("「%s」取数失败：%s" % (k, v))

    report = parts.get("report") or {}
    pre = parts.get("preannounce") or {}
    flash = parts.get("express") or {}
    cons = parts.get("consensus") or {}
    abs_ = parts.get("abstract") or {}

    # 基准期（上一季末）的行业/利润 —— 预告期还没披露，同行必须用上一期
    try:
        base_rows = (deps["report"](base_period, [code]) or {}).get("items") or []
    except Exception as exc:                    # noqa: BLE001
        base_rows = []
        gaps.append("基准期 %s 业绩报表取数失败：%s" % (base_period, str(exc)[:120]))
    base = base_rows[0] if base_rows else {}

    # 行业缺失时用本股历史报告（abstract 不带行业），只能留空 → 同行不可比
    merged_parts = dict(parts)
    if not (parts.get("report") or {}).get("industry") and base.get("industry"):
        merged_parts["report"] = {**(parts.get("report") or {}),
                                  "industry": base.get("industry"),
                                  "period": (parts.get("report") or {}).get("period")
                                            or base.get("period")}

    est = estimates(code, period, parts, conf)
    val = valuation(code, merged_parts, base_period, deps, conf)

    # 「无预告」三态：去年同期有没有预告，要单独查去年同期的表
    pre_ly = {}
    try:
        ly = deps["bundle"](code, _same_q_last_year(period)) or {}
        pre_ly = (ly.get("parts") or {}).get("preannounce") or {}
    except Exception as exc:                    # noqa: BLE001
        pre_ly = {}
        gaps.append("去年同期预告取数失败（无法判定「去年有、今年没有」这一风险态）："
                    "%s" % str(exc)[:120])
    gstate = guidance_state(bool(pre), bool(pre_ly), disclose_date, conf)
    if gstate["state"] in ("missing", "overdue"):
        gaps.append("⚠ 业绩预告：%s" % gstate["reason"])

    gaps.append("季度一致预期不可得（公开免费源只有年度 EPS）——"
                "任何「本季预期 X 亿」的说法都无法核证，本页只给可复算的推算")
    gaps.append("「不满足强制预告条件」无法从公开数据判定：公司没发预告时，"
                "既可能是不需要发，也可能是要发却没发（后者用「去年同期有无预告」判）")
    for n in est.get("notes") or []:
        gaps.append(n)
    for n in val.get("notes") or []:
        gaps.append(n)
    for n in (val.get("peer") or {}).get("notes") or []:
        gaps.append(n)

    # 实际值：正式报表 > 快报 > 预告中值（后者标明不是实际值）
    actual = _f(report.get("net_profit"))
    actual_src = "report" if actual is not None else ""
    if actual is None:
        actual = _f(flash.get("net_profit"))
        actual_src = "express" if actual is not None else ""
    guide_mid = _f(pre.get("profit_mid"))

    plan = plan_branches(est["ref"], val, conf)
    basis = plan.get("basis") or ""

    advice = None
    if actual is not None:
        # 匹配函数必须跟着预案的基准走，否则「预案写的是估值五档、结论却按
        # 市场预期落地」—— 用户看到的两段互相矛盾。没有预期也没有估值基准时
        # 退回业绩预告中值（`guide`），再没有就只能 `unknown`。
        # 基准必须为正：负基准（亏损）走百分比会符号反转，有正的预告中值就优先
        # 用它；都不可用就交给 `match_branch` 返回 `unknown`，不硬判。
        if basis == "valuation":
            m = match_valuation_branch(actual, val, conf, period)
            action, level = _ACTION.get(m["branch"], _ACTION["unknown"])
        else:
            ref_for_match = est["ref"]
            if ref_for_match is None or ref_for_match <= 0:
                if guide_mid is not None and guide_mid > 0:
                    ref_for_match = guide_mid
                elif ref_for_match is None:
                    ref_for_match = guide_mid
            m = match_branch(actual, ref_for_match, conf)
            action, level = _ACTION.get(m["branch"], _ACTION["unknown"])
            if not basis and ref_for_match is not None:
                basis = "guide"
            if m["branch"] in ("miss", "deep_miss") \
                    and val.get("state") == "expensive":
                action += "；估值高于同行中位，跌幅可能被放大"
        advice = {"based_on": actual_src, "value": actual,
                  "value_yi": _yi(actual), "basis": basis, **m,
                  "action": action, "level": level}

    out = {
        "code": code,
        "name": report.get("name") or pre.get("name") or flash.get("name")
                or cons.get("name") or base.get("name") or "",
        "period": period,
        "base_period": base_period,
        "disclose_date": str(disclose_date or "")[:10],
        "generated_at": deps["now"](),
        "disclosed": actual is not None and actual_src == "report",
        "actual": ({"net_profit": actual, "net_profit_yi": _yi(actual),
                    "revenue": _f(report.get("revenue")),
                    "eps": _f(report.get("eps")),
                    "net_profit_yoy": _f(report.get("net_profit_yoy")),
                    "source": actual_src,
                    "announce_date": report.get("announce_date") or ""}
                   if actual is not None else None),
        "guidance": ({"profit_mid": guide_mid, "profit_mid_yi": _yi(guide_mid),
                      "kind": pre.get("kind"), "change_text": pre.get("change_text"),
                      "reason": pre.get("reason"),
                      "announce_date": pre.get("announce_date"),
                      "state": gstate["state"], "state_reason": gstate["reason"]}
                     if pre else
                     {"state": gstate["state"], "state_reason": gstate["reason"]}),
        "consensus": ({"eps_by_year": cons.get("eps_by_year") or {},
                       "reports": cons.get("reports"),
                       "ratings": cons.get("ratings") or {},
                       "note": "年度 EPS 一致预期，非季度共识"}
                      if cons else None),
        "baseline": ({"period": base.get("period"),
                      "net_profit_yi": _yi(base.get("net_profit")),
                      "net_profit_yoy": _f(base.get("net_profit_yoy")),
                      "industry": base.get("industry") or ""} if base else None),
        "estimates": est,
        "valuation": val,
        "gaps": gaps,
        "plan": plan,
        "advice": advice,
    }
    out["text"] = render(out)
    if save:
        save_plan(out)
    return out


# ---------------------------------------------------------------- 渲染

_LEVEL_TAG = {"critical": "【关键】", "high": "【高】"}


def _branch_line(b: dict) -> str:
    return "· %s%s → %s" % (_LEVEL_TAG.get(b.get("level"), ""),
                            b.get("when"), b.get("then"))


def _yi_txt(v, dash: str = "—") -> str:
    """元 → 「X.XX 亿」文本。None/NaN/Inf 一律破折号。

    不能直接 `"%.2f" % _yi(v)`：`"%.2f" % float("nan")` 不抛异常，
    会原样打印 `nan`（AGENTS.md 记过 `fnum` 拦不住 NaN 同一类坑）。
    """
    x = _yi(v)
    return dash if x is None else "%.2f 亿" % x


def _amt_txt(v, dash: str = "—") -> str:
    """**入参已经是亿元** → 「X.XX 亿」文本。None/NaN/Inf 一律破折号。

    ⚠ 与 `_yi_txt`（入参是**元**）严格区分：`valuation.required` 的
    `annual_profit_yi` / `period_profit_yi` 已经是亿元，混用会二次除以 1e8，
    静默打印出伪造的 `0.00 亿`。
    """
    x = _f(v)
    return dash if x is None else "%.2f 亿" % x


def render(p: dict) -> str:
    """纯文本（日报/通知/页面共用同一份措辞）。"""
    L = []
    name = p.get("name") or p.get("code")
    L.append("【%s %s】%s 报告期" % (p.get("code"), name, p.get("period")))
    if p.get("disclose_date"):
        L.append("预约披露日：%s" % p["disclose_date"])

    a = p.get("actual")
    if a:
        L.append("实际（%s）：归母 %.2f 亿%s" % (
            "正式报表" if a["source"] == "report" else "快报",
            a["net_profit_yi"] or 0,
            "（同比 %+.1f%%）" % a["net_profit_yoy"]
            if a.get("net_profit_yoy") is not None else ""))
    g = p.get("guidance") or {}
    if g.get("profit_mid_yi") is not None:
        L.append("业绩预告中值：%.2f 亿（%s，%s）"
                 % (g["profit_mid_yi"], g.get("kind") or "—",
                    g.get("announce_date") or "日期未知"))
    elif g.get("state") and g["state"] != "present":
        L.append("业绩预告：%s" % g.get("state_reason"))

    est = p.get("estimates") or {}
    if est.get("ref") is not None:
        L.append("推算预期：%s（%s）"
                 % (_yi_txt(est["ref"]),
                    "两种推算分歧 %.0f%%" % est["spread_pct"]
                    if est.get("spread_pct") else "单一推算"))
        for it in est.get("items") or []:
            if it.get("value") is not None:
                L.append("  - %s：%s" % (it["method"], _yi_txt(it["value"])))
    else:
        L.append("推算预期：不可得（见下方缺口）")

    v = p.get("valuation") or {}
    if v.get("market_cap_yi"):
        L.append("估值：总市值 %.1f 亿；本股 PE %s；同行中位 PE %s（%d 家）→ %s"
                 % (v["market_cap_yi"],
                    "%.1f" % v["pe_ttm"] if v.get("pe_ttm") else "—",
                    "%.1f" % v["peer"]["median_pe"] if v["peer"].get("median_pe") else "—",
                    v["peer"].get("count") or 0,
                    {"expensive": "偏贵", "fair": "合理", "cheap": "偏便宜",
                     "invalid": "无法比较"}[v.get("state") or "invalid"]))
    req = v.get("required") or {}
    if req.get("status") == "ok":
        # ⚠ 这两个字段**已经是亿元**，必须走 _amt_txt；用 _yi_txt 会二次除以
        # 1e8，把「32.23 亿」印成「0.00 亿」（真出现过）。
        L.append("维持该估值需要：年利润 %s / 本季累计 %s"
                 % (_amt_txt(req.get("annual_profit_yi")),
                    _amt_txt(req.get("period_profit_yi"), "—（缺历史季节性）")))
    elif req.get("reason"):
        L.append("维持该估值需要：**计算失效** —— %s" % req["reason"])

    pl = p.get("plan") or {}
    L.append("")
    branches = pl.get("branches") or []
    if pl.get("available"):
        L.append("预案（if / else if / else）：")
        L.extend(_branch_line(b) for b in branches)
        if pl.get("basis") == "valuation":
            th = pl.get("thresholds") or {}
            L.append("  ↳ 基准是**估值隐含利润**（%s ≥ %s），不是市场一致预期："
                     "判的是「利润撑不撑得住当前估值」，不含明后年增长的假设"
                     % (th.get("scope") or "本季累计", _amt_txt(th.get("base"))))
        if pl.get("reason"):
            # 负基准（亏损）退到估值基准时，必须让用户看到「为什么换了基准」
            L.append("  ↳ %s" % pl["reason"])
    else:
        if branches:
            L.append("披露前动作（不依赖预期数值，先做）：")
            L.extend(_branch_line(b) for b in branches)
        L.append("预案：不可用 —— %s" % pl.get("reason"))

    adv = p.get("advice")
    if adv:
        L.append("")
        L.append("▶ 已出结果，落在「%s」分支（偏离 %s）：%s"
                 % (adv["branch"], _pct(adv.get("dev_pct")), adv["action"]))
        if adv.get("basis") == "valuation":
            L.append("  ↳ 该分支按估值隐含利润对比得出：%s 需 %s，实际 %s"
                     % (adv.get("scope") or "本季累计",
                        _amt_txt(adv.get("target_yi")),
                        _amt_txt(adv.get("actual_yi"))))
        elif adv.get("basis") == "guide":
            L.append("  ↳ 该分支按**业绩预告中值**对比得出（不是市场一致预期）")

    if p.get("gaps"):
        L.append("")
        L.append("数据缺口与口径（必须一起看）：")
        L.extend("  · %s" % x for x in p["gaps"])
    return "\n".join(L)


# ---------------------------------------------------------------- 落盘

def plan_path(code: str, period: str) -> str:
    return os.path.join(PLAN_DIR, "%s_%s.json" % (code, str(period)[:10]))


def save_plan(plan: dict) -> str:
    """原子写：先写 .tmp 再 os.replace —— 中途崩溃不会留下半个 JSON。"""
    os.makedirs(PLAN_DIR, exist_ok=True)
    path = plan_path(plan["code"], plan["period"])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)
    return path


def load_plan(code: str, period: str) -> dict | None:
    try:
        with open(plan_path(code, period), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def list_plans() -> list[dict]:
    out = []
    try:
        names = sorted(os.listdir(PLAN_DIR))
    except FileNotFoundError:
        return out
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(PLAN_DIR, n), encoding="utf-8") as f:
                d = json.load(f)
            out.append({"code": d.get("code"), "name": d.get("name"),
                        "period": d.get("period"),
                        "disclose_date": d.get("disclose_date"),
                        "generated_at": d.get("generated_at"),
                        "disclosed": d.get("disclosed"),
                        "branch": (d.get("advice") or {}).get("branch")})
        except (OSError, ValueError):
            continue
    out.sort(key=lambda x: (x.get("disclose_date") or "", x.get("code") or ""))
    return out
