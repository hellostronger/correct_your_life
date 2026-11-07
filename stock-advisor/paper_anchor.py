# -*- coding: utf-8 -*-
"""个股锚定模式（2026-11-07 用户要求）。

用户场景：实盘半仓持有智谱（02513），要求系统**锚定这一只票**持续运作 ——
唯一目标是「在这只票上赚钱」：可以做T降成本、必要时空仓躲暴跌，
但空仓只是避险手段，**择机买回是必须完成的动作**。

现有引擎按全自选池（43 只）决策，没有「单票锚定」概念；本模块提供编排层：

- `seed_mirror()`   一次性镜像建仓：按实盘仓位占比 × 模拟盘总资产建仓，
                    成本价用实盘 avg_cost（`_derive_holdings()` 从 sa_trades
                    流水推导）。幂等（state.seed_done）。实盘无该票记录时
                    **降级 fresh 并如实说明，绝不编造成本**。
- `build_injection()` 决策注入块：使命声明 + 进度（初始成本→当前成本、
                    已实现盈亏、做T次数、空仓天数）+ 回补预案。
                    **只对锚定票输出**，其他票返回 ""（上下文逐字节不变）。
- `refill_watch()`  回补监控：空仓起始日落库、按交易日计超期、
                    超过 `max_refill_wait_days` 推送告警（去重）。
- `status()`        锚定绩效聚合（GET /api/paper/anchor 用）：成本线、
                    累计盈亏、做T/躲跌事件，全部从 sa_paper_trades 聚合，
                    **不新增交易表**。

边界（与 t_trading 同款纪律）：
- **不 import app** —— 数据函数靠 deps 注入（`holdings_fn`）。
- **回补不由代码硬执行**：满足条件仍由 LLM 结合注入的预案决策，
  本模块只做「监控 + 告警 + 注入」，绝不直接下单。
- 风控不撤：`check_stop_loss` 对全部持仓照常生效（在 app 侧保证）。
- 默认关（`paper.anchor.enabled=false`）：改的是决策口径，由用户在页面开启。
"""
import json
from datetime import datetime

import paper_trading as PT

ANCHOR_DEFAULTS = {
    "enabled": False,          # 总开关。默认关 —— 改的是决策口径（哪些票进 LLM）。
    "code": "",                # 锚定票代码（如 02513 / 600519）；空 = 未锚定。
    # 初始建仓方式：
    #   mirror = 镜像实盘（按实盘仓位占比建仓，成本价用实盘 avg_cost）
    #   keep   = 沿用模拟盘现有持仓，不额外建仓
    #   fresh  = 空仓起步，由锚定策略自己决定建仓时机
    "seed_mode": "mirror",
    "max_refill_wait_days": 5,  # 空仓后超过 N 个交易日未回补 → 推送告警
    "inject": {"enabled": True, "max_chars": 1600},
}


def load_conf(raw: dict | None) -> dict:
    """合并锚定配置。

    ⚠️ 嵌套 dict（inject）必须**深合并**，浅 update 会把 config.yaml 里
    只写了 `inject.enabled: false` 一个键的段落顶回默认值
    （AGENTS.md 记过 `load_mp_conf` 同类事故）。
    """
    conf = {**ANCHOR_DEFAULTS}
    for k, v in (raw or {}).items():
        if k == "inject" and isinstance(v, dict):
            conf[k] = {**conf[k], **v}
        else:
            conf[k] = v
    return conf


DDL = """
-- 锚定状态：每只锚定票一行。空仓起始日/天数/超期告警去重都在这里；
-- 锚定绩效本身从 sa_paper_trades 聚合（不新增交易表）。
CREATE TABLE IF NOT EXISTS sa_paper_anchor_state (
    code TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'anchored',   -- anchored=持仓中 / empty=空仓躲跌
    empty_since DATE,
    empty_days INTEGER NOT NULL DEFAULT 0,
    seed_done BOOLEAN NOT NULL DEFAULT FALSE,
    seed_price NUMERIC,
    seed_shares INTEGER,
    seed_note TEXT,
    last_alert_date DATE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def ensure_table(conn) -> None:
    """幂等建表（失败抛给调用方，由调用方决定要不要吞）。"""
    with conn.cursor() as c:
        c.execute(DDL)
    conn.commit()


def _row_dict(cur, row) -> dict:
    """dict 游标原样返回；tuple 游标按 description 拼成 dict
    （institution_signals 的 `_row()` 教训：生产用的正是 dict 游标）。"""
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    cols = [d[0] for d in cur.description or []]
    return dict(zip(cols, row))


def _state_row(cur, code: str) -> dict:
    """读状态行；不存在返回 {}。"""
    cur.execute("SELECT * FROM sa_paper_anchor_state WHERE code = %s", (code,))
    return _row_dict(cur, cur.fetchone())


def _upsert_state(cur, code: str, **fields) -> None:
    """真 upsert：行不存在就插入（ON CONFLICT DO UPDATE）。

    ⚠️ 曾经是纯 UPDATE —— seed_mirror 在 refill_watch 之前调用时行还不存在，
    UPDATE 静默零行，seed_done/seed_price 永远落不了库（2026-11-08 真库逮到）。
    """
    if not fields:
        return
    cols = ["code", *fields.keys()]
    vals = [code, *fields.values()]
    updates = ", ".join(f"{k} = EXCLUDED.{k}" for k in fields)
    cur.execute(
        f"INSERT INTO sa_paper_anchor_state ({', '.join(cols)}) "
        f"VALUES ({', '.join(['%s'] * len(cols))}) "
        f"ON CONFLICT (code) DO UPDATE SET {updates}, updated_at = now()", vals)


# ---------------- 镜像建仓 ----------------

def seed_mirror(deps: dict, conf: dict) -> dict:
    """按实盘仓位占比在模拟盘建锚定仓（一次性、幂等，绝不二次加仓）。

    实盘仓位占比 = 锚定票成本市值 / 实盘全部持仓成本市值合计
    （成本口径，不依赖实时行情 —— 镜像只需要「比例」而不是精确市值）。
    模拟盘建仓金额 = 该占比 × sa_paper_account.total_value，股数按申报单位
    取整（buy_shares_for），成本价 = 实盘 avg_cost，现金不足自动缩到手数≥1。

    返回 {ok, reason, ...}；**不抛异常**（种子失败绝不能拖住决策轮）。
    """
    code = str(conf.get("code") or "").strip()
    out: dict = {"ok": False, "code": code}
    if not code:
        out["reason"] = "未配置锚定代码"
        return out
    if conf.get("seed_mode") != "mirror":
        out["ok"] = True
        out["reason"] = f"seed_mode={conf.get('seed_mode')}，跳过镜像建仓"
        return out
    get_conn = deps.get("get_conn")
    holdings_fn = deps.get("holdings_fn")
    if get_conn is None or not callable(holdings_fn):
        out["reason"] = "deps 缺 get_conn / holdings_fn，无法镜像建仓"
        return out
    fee_c = (deps.get("conf") or {}).get("fees") or {}
    try:
        with get_conn() as conn:
            # sa_paper_trades / sa_paper_account 由 app.init_db 建，这里只建自己的
            ensure_table(conn)
            cur = conn.cursor()
            st = _state_row(cur, code)
            if st.get("seed_done"):
                out["ok"] = True
                out["reason"] = "已镜像建仓过（幂等跳过）"
                out["seed_done"] = True
                return out
            if PT._derive_paper_positions(cur).get(code):
                # 模拟盘已有该票持仓：沿用为锚定起点（等价 keep），不重复建仓
                _upsert_state(cur, code, seed_done=True,
                              seed_note="模拟盘已有持仓，沿用为锚定起点")
                conn.commit()
                out.update(ok=True, reason="模拟盘已有持仓，沿用（不重复建仓）")
                return out
            holdings = holdings_fn() or []
            me = next((h for h in holdings
                       if h.get("code") == code
                       and (h.get("net_shares") or 0) > 0), None)
            if me is None:
                # 降级 fresh：如实说明，绝不编造成本
                _upsert_state(cur, code,
                              seed_note="实盘无该票持仓记录，降级 fresh 模式")
                conn.commit()
                out.update(ok=True, fallback_fresh=True,
                           reason="实盘无该票持仓记录，降级 fresh（策略自建仓）")
                return out
            total_cost_val = sum((h.get("net_shares") or 0) * (h.get("avg_cost") or 0)
                                 for h in holdings)
            if total_cost_val <= 0 or not me.get("avg_cost"):
                out["reason"] = "实盘成本口径为 0，无法计算占比（不建仓）"
                return out
            ratio = (me["net_shares"] * me["avg_cost"]) / total_cost_val
            acc = PT._account_row(cur)
            if acc is None:
                out["reason"] = "模拟盘账户未初始化，本轮不建仓"
                return out
            budget = acc["total_value"] * ratio
            price = float(me["avg_cost"])
            name = me.get("name") or code
            shares = PT.buy_shares_for(code, name, budget, price)
            fee = PT.trade_fees(code, name, "buy", shares * price, fee_c) \
                if shares >= 1 else None
            while shares >= 1 and shares * price + fee["total"] > acc["cash"]:
                shares -= 100 if not PT.is_star_market(code) else 1
                if shares >= 1:
                    fee = PT.trade_fees(code, name, "buy", shares * price, fee_c)
            if shares < 1:
                out["reason"] = (f"模拟盘现金不足（需约 {budget:,.0f}，"
                                 f"可用 {acc['cash']:,.0f}），本轮不建仓")
                return out
            value = shares * price
            today = datetime.now().strftime("%Y-%m-%d")
            note = (f"[锚定镜像] 按实盘仓位占比 {ratio * 100:.1f}% 镜像建仓，"
                    f"成本价取实盘 avg_cost {price:g}；锚定模式种子单")
            cur.execute(
                PT._insert_trade_sql(),
                (code, name, today, "seed", "buy", shares, price, value,
                 5, None, note, "", json.dumps({"seed": True}),
                 "open", fee["total"], json.dumps(fee, ensure_ascii=False)))
            cur.execute("UPDATE sa_paper_account SET cash = cash - %s, "
                        "updated_at = now() WHERE id = 1",
                        (value + fee["total"],))
            _upsert_state(cur, code, seed_done=True, status="anchored",
                          seed_price=price, seed_shares=shares, seed_note=note)
            conn.commit()
            out.update(ok=True, shares=shares, price=price, value=value,
                       fee=fee["total"], ratio=ratio,
                       reason=f"镜像建仓 {shares} 股 × {price:g}"
                              f"（实盘占比 {ratio * 100:.1f}%）")
            return out
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"镜像建仓失败（不影响本轮）: {exc}"
        return out


# ---------------- 决策注入 ----------------

def _truncate(block: str, cap: int) -> str:
    """超长截断并留痕（与其他注入块同款：截断必须在，否则单块能顶掉整个
    上下文预算，且页面上看不出是截断还是模型就写了这么多）。"""
    if len(block) <= cap:
        return block
    return block[:cap] + f"\n[锚定块超长已截断，全文 {len(block)} 字符]"

def _perf(cur, code: str) -> dict:
    """锚定票的绩效聚合（全部来自 sa_paper_trades，只读）。"""
    out: dict = {"t_count": 0, "realized_pnl": None, "first_cost": None,
                 "settled_count": 0}
    # 做 T 次数：reasoning 带 [做T] 前缀的成交行（前缀定义在 t_trading.T_TAG，
    # 这里用字面量避免拉起 market_data 一串依赖；若改前缀两边要同步）。
    # ⚠️ 带 params 的 SQL 里字面量 % 必须写 %%（psycopg2 把 %' 当占位符解析，
    # 报 IndexError: tuple index out of range —— 2026-11-08 真库逮到）。
    cur.execute("SELECT COUNT(*) FROM sa_paper_trades "
                "WHERE code = %s AND reasoning LIKE '[做T]%%' "
                "AND status <> 'skipped'", (code,))
    row = cur.fetchone()
    out["t_count"] = int((row.get("count") if isinstance(row, dict)
                          else (row or (0,))[0]) or 0)
    # 已实现盈亏（口径：按已结算买入行估算 shares×(settle_price−price)−买入费）。
    # 卖出腿手续费没有逐笔对应关系，本口径如实标注，不冒充精确值。
    cur.execute(
        "SELECT COALESCE(SUM(shares * (settle_price - price) - fee_total), 0) "
        "AS pnl, COUNT(*) AS count, MIN(price) AS first_cost "
        "FROM sa_paper_trades WHERE code = %s AND side = 'buy' "
        "AND status = 'resolved' AND settle_price IS NOT NULL", (code,))
    row = _row_dict(cur, cur.fetchone())
    try:
        if row.get("pnl") is not None:
            out["realized_pnl"] = float(row["pnl"])
    except (TypeError, ValueError):
        pass
    out["settled_count"] = int(row.get("count") or 0)
    try:
        if row.get("first_cost") is not None:
            out["first_cost"] = float(row["first_cost"])
    except (TypeError, ValueError):
        pass
    return out


def build_injection(cur, code: str, conf: dict) -> str:
    """锚定注入块（复刻 institution_signals 的签名与调用约定）。

    只对锚定票输出；其他票 / 未启用 → 返回 ""（调用方不追加，
    上下文逐字节不变）。异常由调用方 try/except 吞（本函数不吞，
    以便调用方打印原因 —— 「静默降级比报错危险」）。
    """
    conf = load_conf(conf)
    acode = str(conf.get("code") or "").strip()
    ic = conf.get("inject") or {}
    if not conf.get("enabled") or not acode or not ic.get("enabled", True):
        return ""
    if str(code) != acode:
        return ""
    positions = PT._derive_paper_positions(cur)
    pos = positions.get(code)
    st = _state_row(cur, code)
    perf = _perf(cur, code)
    cap = max(400, int(ic.get("max_chars") or 1600))
    lines = ["【锚定模式（mission）：本票为唯一锚定标的】",
             "· 使命：所有决策只服务一个目标 —— 在本票上累计赚钱。可以做T降成本、"
             "可以空仓躲暴跌，但空仓只是避险手段，**择机买回是必须完成的动作**，"
             "不是可选的止盈离场。"]
    if pos:
        prog = f"· 进度：当前持仓 {pos['shares']:g} 股（成本 {pos['cost']:g}"
        if perf.get("first_cost"):
            prog += f"，初始成本 {perf['first_cost']:g}"
        if perf.get("realized_pnl") is not None:
            prog += f"，累计已实现盈亏 {perf['realized_pnl']:+,.0f} 元"
        prog += f"，做T {perf['t_count']} 次）。"
        lines.append(prog)
    else:
        empty_days = int(st.get("empty_days") or 0)
        wait = max(1, int(conf.get("max_refill_wait_days") or 5))
        left = max(0, wait - empty_days)
        txt = (f"· 当前**空仓**（第 {empty_days} 个交易日"
               + (f"，自 {st['empty_since']}" if st.get("empty_since") else "")
               + "）。回补预案：等待止跌企稳（如连续收盘站回 5 日均线，"
                 "或反弹收复近段跌幅的一半）后买回；可分批，不必一次满仓。")
        if left > 0:
            txt += f"距超期告警还剩 {left} 个交易日（上限 {wait}）。"
        else:
            txt += (f"⚠ 已超期 {empty_days - wait} 个交易日未回补（上限 {wait}），"
                    "除非趋势仍在恶化，否则应尽快执行回补。")
        lines.append(txt)
    if perf.get("settled_count"):
        lines.append(f"· 已结算样本 {perf['settled_count']} 笔；盈亏按买入行结算价"
                     "估算（卖出腿手续费未逐笔对应），只作趋势参考。")
    lines.append("· 纪律：以「本票累计盈亏」为准绳 —— 不要为了账面好看而拒绝回补，"
                 "也不要因为浮亏而拒绝止损。实盘侧同类操作仅为建议（模拟盘口径）。")
    block = "\n".join(lines)
    return _truncate(block, cap)


# ---------------- 回补监控 ----------------

def refill_watch(deps: dict, conf: dict) -> dict:
    """空仓监控：空仓起始日落库、按交易日计超期、超限推送（去重）。

    挂在 _paper_run_cycle 尾部（止损/决策/做T之后）。**只监控不下单**。
    返回 {ok, status, empty_days, alerted}；不抛异常。
    """
    conf = load_conf(conf)
    code = str(conf.get("code") or "").strip()
    out: dict = {"ok": False, "code": code}
    if not conf.get("enabled") or not code:
        out["reason"] = "锚定未启用"
        return out
    get_conn = deps.get("get_conn")
    if get_conn is None:
        out["reason"] = "deps 缺 get_conn"
        return out
    notify_fn = deps.get("notify_fn")
    trading_days_fn = deps.get("trading_days_fn")
    try:
        with get_conn() as conn:
            ensure_table(conn)
            cur = conn.cursor()
            st = _state_row(cur, code)
            positions = PT._derive_paper_positions(cur)
            held = bool(positions.get(code))
            now = datetime.now()
            today_str = now.strftime("%Y-%m-%d")
            if not st:
                cur.execute(
                    "INSERT INTO sa_paper_anchor_state (code, status, empty_since, "
                    "empty_days) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (code) DO NOTHING",
                    (code, "anchored" if held else "empty",
                     None if held else now.date(), 0))
                conn.commit()
                out.update(ok=True, status="anchored" if held else "empty",
                           empty_days=0)
                return out
            if held:
                if (st.get("status") or "anchored") == "empty":
                    _upsert_state(cur, code, status="anchored", empty_since=None,
                                  empty_days=0)
                    conn.commit()
                    out.update(ok=True, status="anchored", refilled=True,
                               reason="已回补，状态复位")
                else:
                    out.update(ok=True, status="anchored")
                return out
            # 空仓中：首见记起始日，之后按交易日累计
            empty_since = st.get("empty_since")
            if (st.get("status") or "") != "empty" or not empty_since:
                _upsert_state(cur, code, status="empty", empty_since=now.date(),
                              empty_days=0, last_alert_date=None)
                conn.commit()
                out.update(ok=True, status="empty", empty_days=0)
                return out
            days = 0
            if callable(trading_days_fn):
                try:
                    days = int(trading_days_fn(str(empty_since)[:10], now))
                except Exception:  # noqa: BLE001
                    days = 0
            wait = max(1, int(conf.get("max_refill_wait_days") or 5))
            alerted = False
            if days > wait and str(st.get("last_alert_date") or "")[:10] != today_str:
                # 当日去重：last_alert_date 是今天的不再重复推送
                alerted = True
                _upsert_state(cur, code, empty_days=days,
                              last_alert_date=now.date())
                if callable(notify_fn):
                    try:
                        notify_fn(
                            f"锚定回补超期：{code}",
                            f"锚定票 {code} 已空仓 {days} 个交易日"
                            f"（自 {empty_since}）未回补，超过上限 {wait} 天。"
                            "空仓只是避险手段，请评估止跌企稳信号后尽快回补"
                            "（锚定模式纪律：必须买回）。")
                    except Exception as exc:  # noqa: BLE001
                        out["notify_error"] = str(exc)[:120]
            elif days != int(st.get("empty_days") or 0) or alerted:
                _upsert_state(cur, code, empty_days=days)
            conn.commit()
            out.update(ok=True, status="empty", empty_days=days, alerted=alerted)
            return out
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"回补监控失败（不影响本轮）: {exc}"
        return out


# ---------------- 绩效聚合（GET /api/paper/anchor） ----------------

def status(deps: dict, conf: dict | None = None) -> dict:
    """锚定状态 + 绩效（纯只读）。conf 缺省时读 deps 里的 paper 配置。"""
    conf = load_conf(conf if conf is not None
                     else (deps or {}).get("conf") or {})
    code = str(conf.get("code") or "").strip()
    out: dict = {
        "enabled": bool(conf.get("enabled")),
        "code": code,
        "seed_mode": conf.get("seed_mode"),
        "max_refill_wait_days": conf.get("max_refill_wait_days"),
        "available": False,
    }
    if not code:
        out["reason"] = "未配置锚定代码"
        return out
    get_conn = (deps or {}).get("get_conn")
    if get_conn is None:
        out["reason"] = "deps 缺 get_conn"
        return out
    try:
        with get_conn() as conn:
            ensure_table(conn)
            cur = conn.cursor()
            pos = PT._derive_paper_positions(cur).get(code)
            st = _state_row(cur, code)
            perf = _perf(cur, code)
            cur.execute(
                "SELECT trade_date, slot, side, shares, price, value, status, "
                "reasoning, settle_date, settle_price, fee_total "
                "FROM sa_paper_trades WHERE code = %s "
                "ORDER BY id DESC LIMIT 50", (code,))
            rows = [_row_dict(cur, r) for r in cur.fetchall()]
            out.update(
                available=True,
                position=({"shares": pos["shares"], "cost": pos["cost"],
                           "name": pos.get("name")} if pos else None),
                state={k: (str(v) if isinstance(v, datetime) else v)
                       for k, v in st.items()} if st else {},
                perf=perf,
                recent=rows)
            return out
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"锚定状态读取失败: {exc}"
        return out
