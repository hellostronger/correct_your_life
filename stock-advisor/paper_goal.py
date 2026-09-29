"""模拟盘「目标模式」：设定收益率目标 + 期限，看虚拟盘跑得出来不。

回答的问题是「我盯的这套策略，在 N 天内能不能赚到 X%」——把它变成一个
**可验收的目标**而不是事后才知道的盈亏数字。

## 两个独立的进度，不能混

    已变现进度   目标金额 vs 实际提款        （提款计划，见 app.py 提款模块）
    本模块       目标收益率 vs 虚拟盘总资产

本模块只管收益率这一个口径，分母是**建目标那一刻的总资产**（不是 initial_cash），
这样中途设目标也成立，而且不受 `reset_account` 清表影响。

## 模式：observe / constrain

    observe    只度量与提醒，不碰 LLM 决策链（默认）
    constrain  目标进度进决策上下文；**临期未达标时收紧**单票上限降风险

为什么 constrain 只在临期收紧、而不在落后时加码：
为了达标而放大仓位是反直觉的赌徒行为——落后时加仓会让失败概率进一步放大。
真正合理的是「快到期了还没赚到，别再把已有收益搭进去」，所以约束只做减法。
用户可以同时开多个 observe 目标对比，也可以只让一个目标带约束。

## 期限按自然日

    end_date = start_date + horizon_days - 1（今天算第 1 天）

交易日口径会与模拟盘的 cycle 时段（交易时段内每 30 分钟一轮）对不齐，
「还剩 5 个交易日」在跨长假时与体感差得远。自然日更好沟通。
"""

from datetime import date, datetime, timedelta
from pathlib import Path

# 目标收益率超过这个数就基本不可能达成，提示而不是静默接受
MAX_SANE_TARGET_PCT = 500.0
MAX_HORIZON_DAYS = 3650


# ---------------- 建表 ----------------

DDL = """
CREATE TABLE IF NOT EXISTS sa_paper_goals (
    id                BIGSERIAL PRIMARY KEY,
    target_return_pct NUMERIC(8,2)  NOT NULL CHECK (target_return_pct > 0),
    horizon_days      INTEGER       NOT NULL CHECK (horizon_days > 0),
    start_date        DATE          NOT NULL,
    end_date          DATE          NOT NULL,
    -- 建目标那一刻的实时总资产。**必须存快照**而不是回查 sa_paper_equity：
    # reset_account 会 DELETE 掉 equity 表，回查就找不到起点了
    base_value        NUMERIC(14,2) NOT NULL,
    mode              VARCHAR(10)   NOT NULL DEFAULT 'observe'
                      CHECK (mode IN ('observe', 'constrain')),
    status            VARCHAR(12)   NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active', 'reached', 'expired', 'cancelled')),
    reached_at        DATE,
    -- 终局收益率（达标/到期那一刻算的），留着是为了横向对比多个目标
    result_pct        NUMERIC(10,4),
    note              VARCHAR(255)  NOT NULL DEFAULT '',
    created_at        TIMESTAMPTZ   NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sa_paper_goals_status
    ON sa_paper_goals (status, start_date);
"""


def ensure_tables(deps) -> None:
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute(DDL)
        conn.commit()


# ---------------- 读账户当前总资产 ----------------

def current_total(deps) -> float:
    """虚拟盘当前总资产 = 现金 + 持仓按最新收盘估值。

    口径与 account_overview / _snapshot_equity 一致（用收盘价，不用实时价），
    否则目标进度会在盘中跳动、结算后又变一次。
    """
    from paper_trading import _account_row, _derive_paper_positions
    from paper_trading import fetch_close_series, _real_dict_cursor
    get_conn = deps["get_conn"]
    em_kline_fn, tx_symbol_fn = deps["em_kline_fn"], deps["tx_symbol_fn"]
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=_real_dict_cursor(deps))
        account = _account_row(cur)
        if account is None:
            return 0.0
        positions = _derive_paper_positions(cur)
    market_value = 0.0
    for code, p in positions.items():
        closes = fetch_close_series(em_kline_fn, tx_symbol_fn(code), days=10)
        if closes:
            market_value += p["shares"] * closes[max(closes)]
    return round(float(account["cash"]) + market_value, 2)


# ---------------- 进度计算 ----------------

def goal_progress(goal: dict, total: float, today: date | None = None) -> dict:
    """一个目标当前的进度视图。纯函数，不碰库——好测。

    收益率   = (当前总资产 / base_value − 1) × 100
    时间进度 = 已过天数 / 总天数 × 100（自然日）
    应达收益率 = 目标 × 时间进度，用来一眼看出「超前还是落后」

    required_daily_pct 是**复合日收益率**：(1+目标)^(1/剩余天数) − 1。
    用它比「目标/剩余天数」准——收益率是连乘的，不是加的。
    """
    today = today or date.today()
    start = plan_date(goal["start_date"])
    end = plan_date(goal["end_date"])
    base = float(goal["base_value"]) or 0.0
    target = float(goal["target_return_pct"])
    span = (end - start).days + 1                  # 含头含尾的自然日数
    passed = min(span, max(0, (today - start).days + 1))
    days_left = max(0, (end - today).days + 1) if today <= end else 0
    time_pct = round(passed / span * 100, 1) if span else 0.0
    cur_pct = round((total / base - 1) * 100, 2) if base else 0.0
    on_track_pct = round(target * time_pct / 100, 2)
    out = {
        **goal,
        "target_return_pct": target,
        "base_value": base,
        "current_total": round(total, 2),
        "return_pct": cur_pct,
        "time_pct": time_pct,
        "on_track_pct": on_track_pct,               # 按时间进度此刻"应该"到的收益率
        "gap_pct": round(on_track_pct - cur_pct, 2),  # >0 = 落后
        "return_progress_pct": round(min(100.0, cur_pct / target * 100), 1) if target else 0.0,
        "days_total": span,
        "days_left": days_left,
        "days_passed": passed,
        "target_value": round(base * (1 + target / 100), 2),
    }
    if days_left > 0 and base > 0:
        out["required_daily_pct"] = round(((1 + target / 100) ** (1 / days_left) - 1) * 100, 4)
        # 还差多少（按当前总资产涨到目标值）
        out["required_total_pct"] = round((out["target_value"] / total - 1) * 100, 2) if total else None
    else:
        out["required_daily_pct"] = None
        out["required_total_pct"] = None
    return out


def plan_date(v) -> date:
    """date / ISO 字符串 / datetime 都能吃。"""
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def evaluate(progress: dict, today: date | None = None) -> str | None:
    """判定状态迁移。返回**新状态**（None = 没变化）。

    判定顺序：达标 > 到期。达标优先于到期，因为「最后一天达标」既算 reached
    也算 expired，说哪个都对；先判达标对用户更有利（这轮赢了）。
    """
    today = today or date.today()
    if progress["status"] != "active":
        return None
    if progress["return_pct"] >= progress["target_return_pct"]:
        return "reached"
    if today > plan_date(progress["end_date"]):
        return "expired"
    return None


# ---------------- 约束模式：临期收紧 ----------------

# 剩余天数 ≤ 此值且尚未达标 -> 收紧仓位
TIGHTEN_DAYS_LEFT = 5
# 收紧后的单票上限（% of 总资产），乘数形式便于按落后程度分级
TIGHTEN_FACTOR = 0.6


def constrain_note(deps, today: date | None = None) -> dict | None:
    """给决策链用的目标状态；observe 目标或无 active 目标时返回 None。

    返回里带 tighten（bool）与 position_cap_pct（收紧后的单票上限）。
    收紧**只做减法**：临期未达标时降风险，而不是为了达标放大仓位。
    """
    g = active_goal(deps)
    if not g:
        return None
    p = goal_progress(g, current_total(deps), today)
    tighten = (g.get("mode") == "constrain"
               and p["days_left"] <= TIGHTEN_DAYS_LEFT
               and p["return_pct"] < p["target_return_pct"])
    cap = deps["conf"].get("max_position_pct", 25)
    note = {
        "goal_id": g["id"],
        "mode": g["mode"],
        "target_return_pct": float(g["target_return_pct"]),
        "return_pct": p["return_pct"],
        "days_left": p["days_left"],
        "required_daily_pct": p["required_daily_pct"],
        "gap_pct": p["gap_pct"],
        "tighten": tighten,
        "position_cap_pct": round(float(cap) * TIGHTEN_FACTOR, 1) if tighten else None,
    }
    return note


def context_line(deps) -> str:
    """塞进 LLM 决策上下文的一行（无目标时返回空串）。"""
    g = active_goal(deps)
    if not g:
        return ""
    p = goal_progress(g, current_total(deps))
    lead = "落后" if p["gap_pct"] > 0 else "领先"
    s = (f"【账户目标】{p['target_return_pct']:g}% / {p['days_total']} 天"
         f"（到期 {p['end_date']}）· 当前 {p['return_pct']:+.2f}%"
         f"（时间进度 {p['time_pct']:g}%，应达 {p['on_track_pct']:+.2f}%，{lead}"
         f" {abs(p['gap_pct']):.2f} 个百分点）")
    if p["days_left"] and p["required_daily_pct"] is not None:
        s += f"· 还剩 {p['days_left']} 天，需日均 {p['required_daily_pct']:+.3f}%"
    if g["mode"] == "constrain" and p["days_left"] <= TIGHTEN_DAYS_LEFT \
            and p["return_pct"] < p["target_return_pct"]:
        s += "\n  ⚠️ 已进入到期前收紧期：本轮**禁止开新仓**，只允许减仓/持有落袋"
    return s


# ---------------- 读写 ----------------

def active_goal(deps) -> dict | None:
    """当前生效的约束目标：优先 mode='constrain' 的 active，其次最新的 active。

    多个目标并存是为了**对比**（observe），但约束只能有一个——两套互相冲突的
    仓位规则没法同时成立。
    """
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM sa_paper_goals WHERE status = 'active' "
                    "ORDER BY (mode = 'constrain') DESC, start_date DESC, id DESC LIMIT 1")
        row = cur.fetchone()
    return dict(row) if row else None


def list_goals(deps, limit: int = 50) -> list[dict]:
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM sa_paper_goals ORDER BY created_at DESC, id DESC "
                    "LIMIT %s", (limit,))
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def create_goal(deps, target_return_pct: float, horizon_days: int,
                mode: str = "observe", note: str = "") -> dict:
    if target_return_pct <= 0:
        raise ValueError("目标收益率必须大于 0")
    if target_return_pct > MAX_SANE_TARGET_PCT:
        raise ValueError(f"目标收益率超过 {MAX_SANE_TARGET_PCT:g}% 就不现实了，"
                         f"请确认是不是填错了百分数（比如 20 而不是 2000）")
    if not 1 <= horizon_days <= MAX_HORIZON_DAYS:
        raise ValueError(f"期限需在 1 ~ {MAX_HORIZON_DAYS} 天之间")
    mode = (mode or "observe").strip()
    if mode not in ("observe", "constrain"):
        raise ValueError("mode 应为 observe（只度量）或 constrain（临期收紧）")
    total = current_total(deps)
    if total <= 0:
        raise ValueError("虚拟盘账户总资产为 0，无法设定目标")
    start = date.today()
    end = start + timedelta(days=int(horizon_days) - 1)
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        if mode == "constrain":
            # 同一时刻只允许一个约束目标，否则两套仓位规则互相打架
            cur.execute("SELECT id FROM sa_paper_goals "
                        "WHERE status = 'active' AND mode = 'constrain' LIMIT 1")
            if cur.fetchone():
                raise ValueError("已有一个进行中的约束目标（mode=constrain），"
                                 "先取消它或把新目标设为只度量")
        cur.execute(
            "INSERT INTO sa_paper_goals (target_return_pct, horizon_days, start_date, "
            "end_date, base_value, mode, note) VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "RETURNING id",
            (target_return_pct, int(horizon_days), start, end, total, mode, note.strip()))
        gid = cur.fetchone()[0]
        conn.commit()
    return goal_progress({"id": gid, "target_return_pct": float(target_return_pct),
                          "horizon_days": int(horizon_days), "start_date": start,
                          "end_date": end, "base_value": total, "mode": mode,
                          "status": "active", "note": note.strip()}, total)


def cancel_goal(deps, goal_id: int) -> dict:
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("UPDATE sa_paper_goals SET status = 'cancelled' "
                    "WHERE id = %s AND status = 'active' RETURNING id", (goal_id,))
        if cur.fetchone() is None:
            raise ValueError("目标不存在或已结束")
        conn.commit()
    return {"ok": True, "id": goal_id}


def close_goal(deps, goal_id: int, status: str, result_pct: float) -> dict:
    """达标/到期时写回终局。result_pct 存下来是为了让多个目标能横向对比。"""
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("UPDATE sa_paper_goals SET status = %s, reached_at = %s, "
                    "result_pct = %s WHERE id = %s AND status = 'active'",
                    (status, date.today(), result_pct, goal_id))
        conn.commit()
    return {"ok": True, "id": goal_id, "status": status, "result_pct": result_pct}


def evaluate_active(deps, notify: bool = True) -> list[dict]:
    """结算后调一次：把所有 active 目标过一遍，结掉该结的，返回需要提醒的。

    过期的目标**也要结**（status='expired'），否则它会一直挂在「进行中」里，
    用户的对比表永远收不到失败的样本——那对比就没意义了。
    """
    notify_fn = deps.get("notify_fn")
    total = current_total(deps)
    today = date.today()
    out = []
    for g in list_goals(deps, limit=50):
        if g["status"] != "active":
            continue
        p = goal_progress(g, total, today)
        new = evaluate(p, today)
        if not new:
            continue
        close_goal(deps, g["id"], new, p["return_pct"])
        p["status"] = new
        out.append(p)
        if notify and notify_fn:
            hit = new == "reached"
            if hit:
                verdict = "✅ 达成"
            else:
                short = round(p["target_return_pct"] - p["return_pct"], 2)
                verdict = f"⏰ 未达成，差 {short:g} 个百分点"
            note_line = f"\n• 备注：{p['note']}" if p.get("note") else ""
            end_line = (f"达标于 {p['end_date']}" if hit else f"到期 {p['end_date']}")
            notify_fn(
                "🎯 模拟盘目标" + ("达标" if hit else "到期"),
                f"目标 {p['target_return_pct']:g}% / {p['days_total']} 天"
                f"（{p['start_date']} ~ {p['end_date']}）\n\n"
                f"• {verdict}，最终收益率 {p['return_pct']:+.2f}%\n"
                f"• 起始净值 {p['base_value']:,.0f} → 期末 {p['current_total']:,.0f}\n"
                f"• {end_line}"
                f"\n• 时间进度曾走到 {p['time_pct']:g}%"
                f"（按进度应达 {p['on_track_pct']:+.2f}%）"
                + note_line)
    return out
