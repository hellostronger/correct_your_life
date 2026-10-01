"""打新额度提醒：按「20 日日均市值」的时滞决定何时通知。

为什么默认提前 20 天（2026-10-01 用户指出）
-------------------------------------------
额度规则是「T-2 日前 **20 个交易日日均市值** / 5000」。补仓立刻进窗口，
但**只占 1/20 的权重**，所以：

    新日均 = ((20 - n) × 旧日均 + n × 新仓位市值) / 20

要让日均从「不足」爬到「顶格」，需要的交易日数就是提前量。
用户等到 T-2 才动手，日均里 19 天都是低的，额度上不去 —— **通知晚了
等于没通知**。

所以提醒时间不是「T-2 前几天」这种固定值，而是**倒推出来的**：
    最早提醒日 = 申购日 - (爬满所需交易日数 + T-2 定格缓冲)

举例（真实数据）：
    当前沪市日均 12.7 万，长鑫顶格需 3349 万
    -> 爬满需 20 个交易日（刚好把窗口完全替换）
    -> 最早提醒 = 申购日 - 20 - 2 = 22 天前
    即申购前 22 天就该提醒，而不是前 1 天。

三档提醒（避免刷屏）：
    lead >= 22  early   提前量充足，现在补仓来得及
    3..21      normal  补仓有效但可能爬不满
    <= 2       urgent  本轮基本没戏，提示准备下一轮

「各种渠道的新闻」的接入点
------------------------
新股排期的数据源：akshare `stock_ipo_summary_cninfo`（巨潮资讯，官方披露）+
同花顺（A/H）。`sync()` 负责拉取，本模块只管「什么时候提醒、提醒什么」。

**关键：新股排期是会变的**（暂缓发行、修改申购日、撤销发行）。
所以：
  - 每天 sync 一次（app.py 的 _macro_loop 已做）
  - 同步后若某只票的申购日变了，要重置它的提醒状态（否则会漏提醒新日期）
  - 提醒带 `data_date`，让用户知道这是哪天的数据
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path

import ipo_calendar
import ipo_strategy
from ipo_quota import AVG_WINDOW, LOT_VALUE, avg_market_cap

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "data" / "ipo_quota_alert_state.json"

# T-2 定格缓冲：即使日均已达标，也要在申购日前 2 天提醒一次做确认
T2_BUFFER = 2


# ==========================================================================
# 提醒状态（避免同一天对同一只票重复推）
# ==========================================================================

def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(st: dict) -> None:
    try:
        STATE_DIR = STATE_FILE.parent
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except Exception as exc:                              # noqa: BLE001
        print(f"[ipo_quota_alert] 状态保存失败: {exc}", flush=True)


def _mark_sent(key: str, sub_date: str, band: str) -> None:
    st = _load_state()
    rec = st.get(key) or {}
    rec["sent_bands"] = sorted(set(rec.get("sent_bands") or []) | {band})
    rec["sub_date"] = sub_date
    rec["last_sent_at"] = date.today().isoformat()
    st[key] = rec
    _save_state(st)


def _already_sent(key: str, band: str) -> bool:
    return band in ((_load_state().get(key) or {}).get("sent_bands") or [])


def reset_if_sub_date_changed(items: list[dict]) -> list[str]:
    """排期改了（申购日变）就清掉提醒状态，否则新日期不会提醒。

    实测场景：新股暂缓/改期很常见，申购日一改，
    状态里记的还是旧日期的「已提醒」，新日期就被静默吞掉了。
    """
    st = _load_state()
    changed = []
    for it in items:
        code = str(it.get("code") or "").strip()
        sub = str(it.get("sub_date") or "")
        if not code or not sub:
            continue
        rec = st.get(code)
        if rec and rec.get("sub_date") and rec["sub_date"] != sub:
            st[code] = {"sub_date": sub,
                        "sent_bands": [], "reset_at": date.today().isoformat(),
                        "reset_reason": f"申购日 {rec['sub_date']} -> {sub}"}
            changed.append(code)
    if changed:
        _save_state(st)
    return changed


# ==========================================================================
# 提前量倒推
# ==========================================================================

def required_lead_days(need_cap: float, current_avg: float,
                       target: float | None = None,
                       desired_lots: int | None = None) -> dict:
    """补到 `target` 后，最少几个交易日的日均能让配号数达到 `desired_lots`。

    加 T-2 缓冲 = 需要提前几天通知。

    参数
    ----
    need_cap      顶格市值（新股要求的上限）
    current_avg   当前 20 日日均市值
    target        补仓后每天的市值（默认顶格）
    desired_lots  想达到的**配号数**（默认 = 顶格配号数）

    ⚠️ **我在这里错过两次**，值得记下来：

    第 1 次：拿「日均爬到 need_cap」当目标。结果无论补到 30 万还是 3000 万
            都是 20 天 —— 因为「日均达到顶格市值」必须等窗口完全替换，
            这跟补多少无关。语义完全错了。

    第 2 次：闭式解 `n = (need_cap - cur) * W / (target - cur)`。
            当 target 远小于 need_cap 时算出天文数字（补 20 万要爬到 1000 万
            -> 需 950 个交易日），而实际上补 20 万时窗口里 20 天后市值就是
            20 万，日均只会爬到 20 万，永远够不到 1000 万。

    正确语义：**配号数**才是用户能理解的量。「提前多久」= 补到 target 后，
    配号数涨到 desired_lots 需要几个交易日。所以直接用
    `projected_avg_at(n) / LOT_VALUE` 扫第一个达标点。
    """
    from ipo_quota import projected_avg_at
    target = float(target if target is not None else need_cap)
    lots_now = int(current_avg // LOT_VALUE)
    want_lots = int(desired_lots if desired_lots is not None
                    else need_cap // LOT_VALUE)

    if want_lots <= lots_now:
        return {"lead_trading_days": 0, "buffer": T2_BUFFER,
                "lead_calendar_days": T2_BUFFER + 1,
                "lots_now": lots_now, "desired_lots": want_lots,
                "note": f"当前已有 {lots_now} 个号，已达到目标 {want_lots} 个"}

    need_td = None
    for n in range(1, AVG_WINDOW + 1):
        proj = projected_avg_at(n, current_avg, target)
        if proj["lots"] >= want_lots:
            need_td = n
            break

    if need_td is None:
        # 补到 target 后，窗口完全替换也只有 target 的配号数，达不到目标
        best = int(target // LOT_VALUE)
        return {"lead_trading_days": AVG_WINDOW, "buffer": T2_BUFFER,
                "lead_calendar_days": int(AVG_WINDOW / 0.7) + T2_BUFFER,
                "unreachable": True,
                "lots_now": lots_now, "desired_lots": want_lots,
                "note": (f"补到 {target/10000:.0f} 万（{best} 个号）仍达不到 "
                         f"目标 {want_lots} 个号 —— 要么补到 "
                         f"{want_lots * LOT_VALUE / 10000:.0f} 万以上，"
                         f"要么接受 {best} 个号。提前 "
                         f"{AVG_WINDOW} 个交易日告知一次即可")}

    cal = int(need_td / 0.7) + T2_BUFFER + 1
    return {"lead_trading_days": need_td, "buffer": T2_BUFFER,
            "lead_calendar_days": cal, "target_cap": target,
            "lots_now": lots_now, "desired_lots": want_lots,
            "note": (f"补到每天 {target/10000:.0f} 万后，配号数涨到 "
                     f"{want_lots} 个需 {need_td} 个交易日"
                     f"（现在 {lots_now} 个），"
                     f"加 T-2 定格缓冲 → 申购前约 {cal} 天通知")}


def ipo_quota_days_needed(target_cap: float, current_avg: float,
                          target: float) -> int | None:
    """包一层，便于 mock 与避免循环 import。"""
    from ipo_quota import days_needed_to_reach
    r = days_needed_to_reach(target_cap, current_avg, target,
                             window=AVG_WINDOW)
    return r.get("days")


def band_for(lead_days: int) -> str:
    """三档：early / normal / urgent。阈值与 assess 的判定对齐。"""
    if lead_days >= 22:
        return "early"
    if lead_days >= 3:
        return "normal"
    return "urgent"


BAND_LABEL = {
    "early": "🟢 时间充裕",
    "normal": "🟡 抓紧",
    "urgent": "🔴 本轮来不及",
}


# ==========================================================================
# 生成提醒
# ==========================================================================

def build_alerts(*, holdings: list[dict], days: int = 45,
                 notify: bool = True) -> list[dict]:
    """为未来 days 天内待申购的新股生成额度提醒。

    `days` 默认给 45（自然日）而不是 30 —— 因为提前量本身可能就要 20+ 天，
    只看 30 天会漏掉那些「该现在通知、但申购日在 35 天后」的票。
    """
    subs = ipo_calendar.upcoming_full(days=days, market="A")
    if not subs:
        return []

    reset = reset_if_sub_date_changed(subs)
    if reset:
        print(f"[ipo_quota_alert] {len(reset)} 只票申购日变动，提醒状态已重置: "
              f"{', '.join(reset[:5])}", flush=True)

    # 日均市值算一次复用
    try:
        avg = avg_market_cap(holdings)
    except Exception as exc:                              # noqa: BLE001
        print(f"[ipo_quota_alert] 日均市值算不了，退回现价市值: {exc}",
              flush=True)
        return []

    out: list[dict] = []
    for it in subs:
        ns = ipo_strategy.eval_new_stock(it)
        need = ns.get("full_cap_need") or 0
        mkt = ns.get("market", "sh")
        if not need or mkt not in ("sh", "sz", "bj"):
            continue
        info = (avg.get("markets") or {}).get(mkt) or {}
        cur_avg = float(info.get("avg") or 0)
        lots_now = int(info.get("lots") or 0)

        lead = it.get("lead_days")
        if lead is None:
            continue
        lead = int(lead)
        band = band_for(lead)
        code = str(ns.get("code") or it.get("code") or "").strip()
        key = code or str(ns.get("name"))

        # 倒推「该提前几天通知」—— 用户指出的核心。
        # 目标是**配号数翻倍**（而不是市值），因为配号数才是用户能理解的量。
        # 长鑫那种顶格 3349 万的票，为多打几个号堆到顶格不现实。
        lots_full = int(need // LOT_VALUE)
        lots_want = min(lots_full, max(lots_now * 2, 2))
        # 要达到 lots_want 个号，日均需 lots_want * LOT_VALUE；
        # 若这超过顶格，就以顶格为上限（那就是 unreachable）
        target_useful = min(need, lots_want * LOT_VALUE)
        req = required_lead_days(need, cur_avg, target=target_useful,
                                 desired_lots=lots_want)
        # 另算「冲顶格要提前多久」—— 作为参考信息，不作为通知触发条件
        req_full = required_lead_days(need, cur_avg, target=need,
                                      desired_lots=lots_full)
        should_notify = lead <= req["lead_calendar_days"]

        # 已经在这个档位提醒过了就跳过（避免每天刷同一条）
        if notify and _already_sent(key, band) and should_notify:
            continue
        if not should_notify and not notify:
            # 手工调用时全给出来，方便看全貌
            pass

        verdict = ipo_strategy.assess(new_stock=ns, holdings=holdings,
                                      lead_days=lead, avg_cap=avg)

        alert = {
            "key": key,
            "code": code,
            "name": ns.get("name"),
            "market": mkt,
            "market_cn": verdict.get("market_cn"),
            "sub_date": it.get("sub_date"),
            "lead_days": lead,
            "band": band,
            "band_label": BAND_LABEL[band],
            # 额度现状
            "avg_market_cap_wan": round(cur_avg / 10000, 2),
            "lots_now": lots_now,
            "need_cap_wan": round(need / 10000, 2),
            "gap_wan": round(max(0.0, need - cur_avg) / 10000, 2),
            "lots_if_full": lots_full,
            # 够用的目标（不必堆到顶格）
            "target_useful": round(target_useful, 2),
            "target_useful_wan": round(target_useful / 10000, 2),
            "lots_if_useful": lots_want,
            # 该提前多久通知
            "required_lead": req,
            "required_lead_full": req_full,
            "should_notify": should_notify,
            # 数据可信度
            "cap_basis": verdict.get("cap_basis"),
            "avg_degraded": avg.get("degraded"),
            "avg_coverage_missing": (avg.get("coverage") or {}).get("missing"),
            "avg_notes": avg.get("notes"),
            "verdict": verdict.get("verdict"),
            "reason": verdict.get("reason"),
            "expected_profit": verdict.get("expected_profit"),
            "lot_rate": ns.get("lot_rate"),
            "quota_rule": verdict.get("quota_rule"),
            "data_date": avg.get("as_of"),
        }
        out.append(alert)

    # 只推「该通知」的，且按提前量升序（最紧急的排前面）
    to_send = [a for a in out if a["should_notify"]]
    to_send.sort(key=lambda a: a["lead_days"])
    if notify:
        for a in to_send:
            _mark_sent(a["key"], str(a["sub_date"]), a["band"])
    return to_send if notify else out


def format_wx(alerts: list[dict]) -> str:
    """微信通知正文。"""
    if not alerts:
        return ""
    lines = [
        f"🎫 打新额度提醒（{len(alerts)} 只）",
        "",
        "额度规则：T-2 日前 20 个交易日日均市值 ÷ 5000",
        "",
    ]
    for a in alerts:
        lines.append(f"{a['band_label']} {a['market_cn']}{a['name']}")
        lines.append(f"  申购日 {a['sub_date']}（还有 {a['lead_days']} 天）")
        lines.append(f"  日均市值 {a['avg_market_cap_wan']} 万 → "
                     f"{a['lots_now']} 个号")
        lines.append(f"  顶格需 {a['need_cap_wan']} 万（{a['lots_if_full']} 个号），"
                     f"缺口 {a['gap_wan']} 万")
        if a.get("gap_wan", 0) > 0 and a.get("target_useful_wan"):
            lines.append(f"  ├ 补到 {a['target_useful_wan']} 万即够用"
                         f"（{a['lots_if_useful']} 个号，翻倍）")
            lines.append(f"  └ 想顶格需 {a['need_cap_wan']} 万"
                         f"（{a['lots_if_full']} 个号）")
        if a.get("verdict") in ("shift", "partial"):
            lines.append(f"  建议：{a['reason']}")
        elif a.get("verdict") == "hold":
            lines.append("  ✅ 额度已够，无需动作")
        elif a.get("verdict") == "skip":
            lines.append(f"  ⚠️ {a['reason']}")
        elif a.get("verdict") == "too_late":
            lines.append("  ⏰ 本轮已来不及，准备下一轮")
        req = a.get("required_lead") or {}
        if req.get("note"):
            lines.append(f"  ℹ️ {req['note']}")
        reqf = a.get("required_lead_full") or {}
        if reqf.get("note") and reqf.get("lead_trading_days") != req.get("lead_trading_days"):
            lines.append(f"     冲顶格则需 {reqf.get('note')}")
        if a.get("avg_degraded"):
            lines.append(f"  ⚠️ 日均数据降级（{'; '.join((a.get('avg_notes') or [])[:1])}）"
                         f" 额度可能偏低")
        lines.append("")
    lines.append(f"数据日期 {alerts[0].get('data_date')}　"
                 f"每日 15:10 后按 20 日日均市值重算")
    return "\n".join(lines)
