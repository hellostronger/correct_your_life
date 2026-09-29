# -*- coding: utf-8 -*-
"""模拟盘策略对照：绑定 / 影子卖出 / 对照。

为什么要有这个（2026-09-29）
--------------------------
模拟盘的一个重要用途是**探索策略**。但「接个止盈」解决不了探索：真卖一次只能跑一套
策略，跑完只知道「这套止盈赚了多少」，回答不了真正的问题 ——
移动止盈和固定止盈哪个好？策略卖 vs LLM 自己判卖哪个更准？

所以这里是**影子模式**：同一笔模拟买入并行挂多个策略，各自只记「影子卖出」，
**不动真仓、不改现金**，跑到最后同台比对。

三个容易踩的坑，这里怎么处理
----------------------------
1) 判定语义必须与真实持仓一致，否则对照没有意义。
   -> 直接复用 app._eval_strategy（与 check_strategies_once 同一套）。

2) LLM 把仓位平掉后策略就失去跟踪对象，对照最值钱的样本恰好是「LLM 卖早了」。
   -> 绑定行上冻结 base_cost/base_shares（frozen_at），策略继续跟踪这只虚拟持仓。
   有实盘持仓时每轮刷新成本，回补/加仓也自然并进来。

3) 每只票要能「死拿」作共同基准，否则 edge（策略比死拿好多少）没有参照。
   -> hold_pct = 现价 / 基准成本 - 1，对所有策略同一口径。
"""
import json
from datetime import datetime

from psycopg2.extras import execute_values

# ---------------- 预置策略 ----------------
# sa_strategies 原本是空的（系统建好了但一条策略都没定义过），预置 7 条。
#
# 关键不是「多」，是**覆盖决策维度**。原来只有 3 条，全是「锁利润」，结果：
#   - 一条砍亏损的都没有 —— 而「该多早砍掉亏损」恰恰是模拟盘最该回答的问题
#   - 一条不看价格的都没有 —— 时间止盈是唯一不依赖行情形态的退出理由
# 现在把 app._eval_strategy 支持的 6 种 kind 全部覆盖，再加一组移动止盈的
# 参数敏感性（5% / 8%）：只给一个 8% 没法回答「这个参数调 3 个点会怎样」。
#
# 命名统一「角色·参数」，角色说明它替谁做决策：
#   砍亏损 = stop_loss / 时间 = time_stop / 锁利润 = pct/trailing/drawdown/ladder
PRESET_STRATEGIES = [
    # ---- 锁利润：跟随回撤（参数敏感性对照：5% 紧、8% 松）----
    {"name": "锁利·移动止盈5%", "kind": "trailing", "target_pct": 5,
     "drawdown_pct": 5, "config": {},
     "note": "从最高点回撤5%就走。最紧的一条，回吐最少但最容易被洗出去。"
             "和「移动止盈8%」配对看，能看出这个参数每放宽3个点的代价。"},
    {"name": "锁利·移动止盈8%", "kind": "trailing", "target_pct": 8,
     "drawdown_pct": 8, "config": {},
     "note": "从最高点回撤8%就走。适合趋势票，中庸基准线。"},
    # ---- 锁利润：先赚够再启动回撤（与移动止盈的区别就在「先赚够」）----
    {"name": "锁利·回撤止盈(涨10%后回撤5%)", "kind": "drawdown", "target_pct": 10,
     "drawdown_pct": 5, "config": {},
     "note": "先涨过10%才开始跟踪峰值，之后回撤5%才卖。跟移动止盈的区别是"
             "「先有利润才启动」——不赚钱的票不会因为小震荡被洗出去。"},
    # ---- 锁利润：固定阈值 ----
    {"name": "锁利·固定止盈15%", "kind": "pct", "target_pct": 15,
     "drawdown_pct": None, "config": {},
     "note": "涨15%无条件走。简单可预期，适合震荡市；单边行情里会卖早。"},
    # ---- 锁利润：分批落袋 ----
    {"name": "锁利·分批+10%/+20%", "kind": "ladder", "target_pct": 20,
     "drawdown_pct": None,
     "config": {"steps": [{"pct": 10, "ratio": 0.5}, {"pct": 20, "ratio": 0.5}]},
     "note": "涨10%卖一半落袋，涨20%卖剩下。单边上涨行情里最能跑，"
             "代价是震荡市里利润被切碎。"},
    # ---- 砍亏损（唯一一条不看「赚了多少」的止盈类策略）----
    {"name": "砍亏损·止损8%", "kind": "stop_loss", "target_pct": 8,
     "drawdown_pct": None, "config": {},
     "note": "跌破成本8%就走。用来回答「LLM 的止损是不是砍太早/太晚」——"
             "注意模拟盘本身还有 ATR 自适应止损在跑，两条一起看能分清"
             "「固定比例止损」和「波动率止损」哪个更有效。"},
    # ---- 时间止盈（唯一一条不依赖价格的退出理由）----
    {"name": "时间·持有10个交易日", "kind": "time_stop", "target_pct": 10,
     "drawdown_pct": None, "config": {"hold_days": 10},
     "note": "拿满10个交易日无条件走。看的是「资金效率」而不是「价格」——"
             "回答「赚的那点波动，值不值得占着仓位」。"},
]


def preset_strategies(conn) -> int:
    """把预置策略写进 sa_strategies（同名已存在则跳过）。返回新增条数。"""
    n = 0
    with conn.cursor() as cur:
        for s in PRESET_STRATEGIES:
            cur.execute("SELECT id FROM sa_strategies WHERE name = %s", (s["name"],))
            if cur.fetchone():
                continue
            cur.execute(
                "INSERT INTO sa_strategies (name, kind, target_pct, drawdown_pct, config, note) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (s["name"], s["kind"], s["target_pct"], s["drawdown_pct"],
                 json.dumps(s["config"], ensure_ascii=False), s["note"]))
            n += 1
    return n


def ensure_preset() -> int:
    """运行期也能补预置（init_db 之后手动调用）。"""
    with _conn() as conn:
        n = preset_strategies(conn)
        conn.commit()
    return n


def _conn():
    """延迟取 app.get_conn —— 避免 import 环。"""
    from app import get_conn
    return get_conn()


def _today(now=None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d")


def _float(v):
    return float(v) if v is not None else None


def _cfg(v):
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip():
        try:
            return json.loads(v) or {}
        except Exception:
            return {}
    return {}


def _held_days(deps: dict, entry_date, now):
    """持有**交易日**数。必须与真实持仓同口径。

    app._eval_strategy 的 time_stop 判的是「已持有 N 个交易日」，真实持仓那边用
    _trading_days_between 算（跳周末）。这里原先用自然日，会让「持有10日」在第 7 个
    自然日就触发 —— 早 40%，而且和我自己在注释里写的「同一套语义」直接矛盾。

    entry_date 取自绑定行的 base_entry_date 而不是模拟持仓的 entry_date：
    仓位被 LLM 平掉后持仓就没了，entry_date 跟着消失，而影子仓还得继续跟踪，
    time_stop 恰恰是那种「持仓没了就没意义」的策略 —— 那样等于冻结后时间止盈全瞎。
    """
    if not entry_date:
        return None
    fn = deps.get("trading_days_fn")
    if fn is None:                      # 依赖没注入时的兜底，行为退化为 0 而不是崩溃
        return None
    try:
        return int(fn(str(entry_date)[:10], now))
    except Exception:
        return None


def _strategy_conf(deps: dict) -> dict:
    conf = deps.get("conf") or {}
    return conf if isinstance(conf, dict) else {}


def auto_bind(deps: dict, codes: list[str]) -> int:
    """给还没绑过策略的持仓自动挂上启用中的策略，返回新增绑定数。

    为什么必须有这个：探索策略靠的是**时间积累**的对照数据。如果要用户手动给
    15 只持仓 × 3 套策略点 45 次，那这些数据永远攒不起来，绑定一个月后
    base_cost 仍是空的，compare 全是 no_base —— 功能等于没开。

    auto_bind=false 可以关掉（用户想手动挑策略组合时用）。
    """
    conf = _strategy_conf(deps)
    if not conf.get("auto_bind", True):
        return 0
    codes = sorted({c for c in codes if c})
    if not codes:
        return 0
    get_conn = deps["get_conn"]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM sa_strategies ORDER BY id")
        sids = [r[0] for r in cur.fetchall()]
        if not sids:
            return 0
        cur.execute("SELECT strategy_id, code FROM sa_paper_strategy_bindings")
        have = {(r[0], r[1]) for r in cur.fetchall()}
        ins = [(sid, c, "auto") for sid in sids for c in codes if (sid, c) not in have]
        if not ins:
            return 0
        # execute_values 而非 executemany：后者每行一次网络往返，这台远程共享云库
        # 单次往返约 65ms（实测 executemany 写 1200 行花了 81.7 秒）。
        execute_values(cur,
                       "INSERT INTO sa_paper_strategy_bindings (strategy_id, code, note) "
                       "VALUES %s ON CONFLICT (strategy_id, code) DO NOTHING",
                       ins, page_size=500)
    return len(ins)


def stock_names(deps: dict, codes: list[str]) -> dict:
    """票名。_derive_paper_positions 只含未平仓持仓，已平仓/影子仓要从流水里回捞。"""
    codes = [c for c in codes if c]
    if not codes:
        return {}
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT ON (code) code, name FROM sa_paper_trades "
                    "WHERE code = ANY(%s) AND name IS NOT NULL AND name <> '' "
                    "ORDER BY code, id DESC", (codes,))
        return {r[0]: r[1] for r in cur.fetchall()}


# ---------------- 绑定 ----------------

def bind(deps: dict, code: str, strategy_id: int, note: str = "") -> dict:
    """给某只票挂上策略。同一 (strategy, code) 只挂一次。

    允许对**已平仓**的票绑定（回看对照），此时 base_cost 需由 compare 侧兜底，
    或用户手动补 —— 见 base_cost 参数。
    """
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("SELECT name, kind FROM sa_strategies WHERE id=%s", (strategy_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"策略 {strategy_id} 不存在")
        cur.execute(
            "INSERT INTO sa_paper_strategy_bindings (strategy_id, code, note) "
            "VALUES (%s,%s,%s) ON CONFLICT (strategy_id, code) DO NOTHING RETURNING id",
            (strategy_id, code, note))
        got = cur.fetchone()
        if got:
            return {"id": got[0], "strategy_id": strategy_id, "code": code,
                    "created": True, "enabled": True, "triggered": False,
                    "strategy_name": row[0], "kind": row[1]}
        cur.execute(
            "SELECT id, enabled, triggered_at FROM sa_paper_strategy_bindings "
            "WHERE strategy_id=%s AND code=%s", (strategy_id, code))
        ex = cur.fetchone()
        if not ex:
            raise ValueError("绑定失败")
        return {"id": ex[0], "strategy_id": strategy_id, "code": code, "created": False,
                "enabled": ex[1], "triggered": bool(ex[2]),
                "strategy_name": row[0], "kind": row[1]}


def unbind(deps: dict, binding_id: int) -> bool:
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM sa_paper_strategy_bindings WHERE id=%s", (binding_id,))
        return bool(cur.rowcount)


def set_enabled(deps: dict, binding_id: int, enabled: bool) -> bool:
    """临时停用/恢复某条绑定（不改历史影子卖出）。"""
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("UPDATE sa_paper_strategy_bindings SET enabled=%s WHERE id=%s",
                    (enabled, binding_id))
        return bool(cur.rowcount)


def list_bindings(deps: dict) -> dict:
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, name, kind, target_pct, drawdown_pct, config, note "
                    "FROM sa_strategies ORDER BY id")
        strategies = [{"id": r[0], "name": r[1], "kind": r[2], "target_pct": _float(r[3]),
                       "drawdown_pct": _float(r[4]), "config": _cfg(r[5]),
                       "note": r[6] or ""} for r in cur.fetchall()]
        cur.execute(
            "SELECT b.id, b.strategy_id, b.code, b.enabled, b.peak_price, b.ladder_step, "
            "b.triggered_at, b.base_cost, b.base_shares, b.base_entry_date, b.frozen_at, "
            "b.note, s.name, s.kind FROM sa_paper_strategy_bindings b "
            "JOIN sa_strategies s ON s.id=b.strategy_id ORDER BY b.code, s.id")
        binds = [{
            "id": r[0], "strategy_id": r[1], "code": r[2], "enabled": r[3],
            "peak_price": _float(r[4]), "ladder_step": r[5] or 0,
            "triggered_at": r[6].isoformat() if r[6] else None,
            "base_cost": _float(r[7]), "base_shares": r[8],
            "base_entry_date": str(r[9])[:10] if r[9] else None,
            "frozen": bool(r[10]),
            "note": r[11] or "", "strategy_name": r[12], "kind": r[13]}
            for r in cur.fetchall()]
        cur.execute("SELECT binding_id, count(*) FROM sa_paper_strategy_exits "
                    "GROUP BY binding_id")
        hits = {r[0]: r[1] for r in cur.fetchall()}
    now = datetime.now()
    for b in binds:
        b["exit_count"] = hits.get(b["id"], 0)
        # 交易日口径，与 scan_once / 真实持仓的 time_stop 一致
        b["held_days"] = _held_days(deps, b["base_entry_date"], now)
    return {"strategies": strategies, "bindings": binds}


# ---------------- 扫描（影子卖出） ----------------

def _eval(kind, tp, dd, cfg, base, price, peak, gain_pct, held):
    """复用 app._eval_strategy —— 真实持仓与模拟盘必须同一套判定语义。"""
    from app import _eval_strategy
    return _eval_strategy(kind, tp, dd, cfg, base, price, peak, gain_pct, held)


def scan_once(deps: dict, now=None, slot: str = "") -> list[dict]:
    """扫一遍绑定逐个判定；触发的记一笔**影子卖出**（不动持仓/现金）。

    返回本轮触发明细。
    """
    import paper_trading as PT
    get_conn = deps["get_conn"]
    now = now or datetime.now()
    today = _today(now)

    BIND_SQL = (
        "SELECT b.id, b.strategy_id, b.code, b.peak_price, b.ladder_step, "
        "b.base_cost, b.base_shares, b.base_entry_date, b.frozen_at, s.name, s.kind, "
        "s.target_pct, s.drawdown_pct, s.config FROM sa_paper_strategy_bindings b "
        "JOIN sa_strategies s ON s.id=b.strategy_id "
        "WHERE b.enabled AND b.triggered_at IS NULL ORDER BY b.id")

    def _load():
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(BIND_SQL)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    with get_conn() as conn, conn.cursor() as cur:
        positions = PT._derive_paper_positions(cur)

    # 先自动绑定再取待判定的绑定 —— 顺序反了会死锁：新装时一条绑定都没有，
    # 提前 return 掉就永远等不到自动绑定，对照数据一辈子攒不出来。
    try:
        auto_bind(deps, list(positions.keys()))
    except Exception:
        # 自动绑定失败不该拖垮扫描（它是锦上添花，不是判定所需）
        pass

    rows = _load()
    if not rows:
        return []

    quotes = deps["quote_fn"](sorted({r["code"] for r in rows})) or {}
    out: list[dict] = []

    # 整轮复用**一个**写连接：库里每行绑定都可能要 UPDATE（刷新基准 / 抬峰值 /
    # 冻结 / 触发），逐行 get_conn() 在远程云库上每次约 200ms，45 行就是 9 秒。
    # 行情先取完再开写连接，避免拿着写连接等网络。
    with get_conn() as conn, conn.cursor() as cur:
        for r in rows:
            code = r["code"]
            price = _float((quotes.get(code) or {}).get("price"))
            if not price or price <= 0:
                continue

            # ---- 基准成本：有实盘模拟持仓就用它；没有则沿用冻结的影子仓 ----
            pos = positions.get(code) or {}
            live = int(pos.get("shares") or 0) > 0
            base = _float(pos.get("cost")) if live else _float(r["base_cost"])
            shares = int(pos.get("shares") or 0) if live else int(r["base_shares"] or 0)
            frozen = bool(r["frozen_at"])
            entry = (str(pos["entry_date"])[:10] if live and pos.get("entry_date")
                     else (str(r["base_entry_date"])[:10] if r.get("base_entry_date")
                           else None))

            if live:
                # 持仓变了（加仓/部分平仓）就刷新影子仓基准，并解冻
                if abs((_float(r["base_cost"]) or 0) - base) > 1e-6 or \
                        (r["base_shares"] or 0) != shares or frozen or \
                        str(r.get("base_entry_date") or "")[:10] != (entry or ""):
                    cur.execute(
                        "UPDATE sa_paper_strategy_bindings SET base_cost=%s, base_shares=%s, "
                        "base_entry_date=%s, frozen_at=NULL WHERE id=%s",
                        (base, shares, entry or None, r["id"]))
            elif not frozen and (_float(r["base_cost"]) or 0) > 0:
                # 实盘已平但从未冻结（老数据）→ 现在冻结，策略继续虚拟持有
                cur.execute("UPDATE sa_paper_strategy_bindings SET frozen_at=now() WHERE id=%s",
                            (r["id"],))

            if not base or base <= 0 or shares < 1:
                continue

            gain_pct = (price / base - 1) * 100
            held = _held_days(deps, entry, now)
            cfg = _cfg(r["config"])
            tp = _float(r["target_pct"])
            dd = _float(r["drawdown_pct"])
            kind = r["kind"]

            # ---- 分批止盈：到档不终结，逐档推进 ----
            if kind == "ladder":
                steps = cfg.get("steps") or []
                done = r["ladder_step"] or 0
                for i, st in enumerate(steps[done:], start=done):
                    if price < base * (1 + float(st["pct"]) / 100):
                        break
                    ratio = float(st.get("ratio") or 1)
                    reason = (f"到第 {i + 1}/{len(steps)} 档（涨幅 ≥{float(st['pct']):g}%），"
                              f"该卖 {ratio * 100:g}% 仓位")
                    cur.execute(
                        "UPDATE sa_paper_strategy_bindings SET ladder_step=%s, "
                        "triggered_at = CASE WHEN %s THEN now() ELSE triggered_at END "
                        "WHERE id=%s", (i + 1, i + 1 >= len(steps), r["id"]))
                    out.append(_record(cur, r["id"], r["strategy_id"], code, today, slot,
                                       price, base, shares, ratio, reason))
                    break
                continue

            # ---- 其余类型：复用真实持仓的判定 ----
            hit, note, new_peak = _eval(kind, tp, dd, cfg, base, price,
                                        _float(r["peak_price"]), gain_pct, held)
            peak_now = _float(r["peak_price"])
            if new_peak is not None and (peak_now is None or new_peak > peak_now):
                cur.execute("UPDATE sa_paper_strategy_bindings SET peak_price=%s WHERE id=%s",
                            (new_peak, r["id"]))
            if hit:
                cur.execute("UPDATE sa_paper_strategy_bindings SET triggered_at=now() "
                            "WHERE id=%s", (r["id"],))
                out.append(_record(cur, r["id"], r["strategy_id"], code, today, slot,
                                   price, base, shares, 1.0, note or "触发条件达成"))
    return out


def _record(cur, bid, sid, code, today, slot, price, base, shares, ratio, reason) -> dict:
    """写一笔影子卖出。**不碰 sa_paper_trades、不动持仓、不改现金。**

    cur 是调用方持有的游标：整轮共用一个连接，别在循环里另开（远程库每次建连约 200ms）。
    """
    pnl_pct = (price / base - 1) * 100 if base else 0.0
    amount = shares * (price - base) * ratio
    cur.execute(
        "INSERT INTO sa_paper_strategy_exits (binding_id, strategy_id, code, exit_date, "
        "exit_slot, price, base_cost, shares, ratio, pnl_pct, pnl_amount, reason) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (bid, sid, code, today, slot or "", price, base, shares, ratio,
         pnl_pct, amount, reason))
    return {"binding_id": bid, "strategy_id": sid, "code": code, "date": today,
            "price": price, "base": base, "shares": shares, "ratio": ratio,
            "pnl_pct": round(pnl_pct, 2), "pnl_amount": round(amount, 2),
            "reason": reason}


def strategy_label(kind, tp, dd, cfg) -> str:
    """一行策略的人话描述（前端复用，与 app._strategy_config_display 同口径）。"""
    cfg = _cfg(cfg)
    if kind == "pct":
        return f"+{tp:g}% 固定止盈" if tp is not None else "固定止盈（未配）"
    if kind == "stop_loss":
        return f"-{tp:g}% 止损" if tp is not None else "止损（未配）"
    if kind == "drawdown":
        return (f"盈利>{tp:g}%后回撤{dd:g}%卖出" if tp is not None and dd is not None
                else f"回撤 {dd:g}%" if dd is not None else "回撤止盈（未配）")
    if kind == "trailing":
        return f"峰值回撤 {dd:g}% 卖出" if dd is not None else "移动止盈（未配）"
    if kind == "ladder":
        steps = cfg.get("steps") or []
        if not steps:
            return "分批止盈（未配档位）"
        return " ".join(f"涨{s['pct']:g}%卖{s.get('ratio', 1) * 100:g}%"
                        for s in steps)
    if kind == "time_stop":
        return f"持有 {cfg.get('hold_days', tp)} 天"
    return kind or "?"


# ---------------- 对照 ----------------

def compare(deps: dict, code: str = "") -> dict:
    """策略卖 vs 持有到今天 vs LLM 实际怎么卖。

    口径（避免误读）：
      - 基准 = 该票**未平仓模拟持仓**的加权成本；已平仓则用绑定的影子仓基准（frozen）
      - hold_pct  = 现价 / 基准 - 1，所有策略的共同对照（「死拿」）
      - strat_pct = 影子卖出的 ratio 加权收益；未触发时等于 hold_pct（策略在持仓中）
      - edge      = strat_pct - hold_pct，正数 = 这套策略比死拿更好
      - llm_pct   = 该票已实现平仓的平均收益（来自 rounds，FIFO 口径与页面一致）
    """
    import paper_trading as PT
    get_conn = deps["get_conn"]
    now_dt = datetime.now()          # compare 没有 now 参数（只读对照，不做判定）

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT b.id, b.code, b.strategy_id, b.enabled, b.peak_price, b.ladder_step, "
            "b.triggered_at, b.base_cost, b.base_shares, b.base_entry_date, b.frozen_at, "
            "s.name, s.kind, s.target_pct, s.drawdown_pct, s.config "
            "FROM sa_paper_strategy_bindings b JOIN sa_strategies s ON s.id=b.strategy_id "
            "ORDER BY b.code, s.id")
        cols = [d[0] for d in cur.description]
        binds = [dict(zip(cols, r)) for r in cur.fetchall()]
        cur.execute("SELECT binding_id, exit_date, exit_slot, price, base_cost, shares, "
                    "ratio, pnl_pct, pnl_amount, reason FROM sa_paper_strategy_exits "
                    "ORDER BY binding_id, exit_date, id")
        ex_cols = [d[0] for d in cur.description]
        exits: dict[int, list[dict]] = {}
        for r in cur.fetchall():
            exits.setdefault(r[0], []).append(dict(zip(ex_cols, r)))
        positions = PT._derive_paper_positions(cur)

    if not binds:
        return {"items": [], "summary": _summary([])}

    codes = sorted({b["code"] for b in binds})
    quotes = deps["quote_fn"](codes) or {}
    names = stock_names(deps, codes)

    # LLM 实际平仓：只查这几个 code，别全表 FIFO
    llm: dict[str, list[float]] = {}
    for c in codes:
        for r in PT.rounds(deps, code=c, only_closed=True):
            if r.get("pnl_pct") is not None:
                llm.setdefault(c, []).append(float(r["pnl_pct"]))

    groups: dict[str, dict] = {}
    for b in binds:
        c = b["code"]
        if c not in groups:
            groups[c] = {
                "code": c,
                "name": (positions.get(c) or {}).get("name") or names.get(c) or c,
                "live_shares": int((positions.get(c) or {}).get("shares") or 0),
                "price": _float((quotes.get(c) or {}).get("price")),
                "rows": []}

    # 现价 + 基准成本要在 price 拿到后算，所以分两轮填
    for b in binds:
        c = b["code"]
        g = groups[c]
        pos = positions.get(c) or {}
        live = int(pos.get("shares") or 0) > 0
        base = _float(pos.get("cost")) if live else _float(b["base_cost"])
        shares = int(pos.get("shares") or 0) if live else int(b["base_shares"] or 0)
        entry = (str(pos["entry_date"])[:10] if live and pos.get("entry_date")
                 else (str(b["base_entry_date"])[:10] if b.get("base_entry_date") else None))
        price = g["price"]
        hold_pct = round((price / base - 1) * 100, 2) \
            if (price and base and base > 0) else None

        ex = exits.get(b["id"]) or []
        ex_out = [{
            "date": str(e["exit_date"])[:10], "slot": e["exit_slot"] or "",
            "price": float(e["price"]), "base": float(e["base_cost"]),
            "shares": int(e["shares"] or 0), "ratio": float(e["ratio"]),
            "pnl_pct": round(float(e["pnl_pct"]), 2),
            "pnl_amount": round(float(e["pnl_amount"]), 2), "reason": e["reason"] or ""}
            for e in ex]
        if ex:
            w = sum(float(e["ratio"]) for e in ex_out) or 1.0
            strat_pct = sum(e["pnl_pct"] * e["ratio"] for e in ex_out) / w
            strat_amt = sum(e["pnl_amount"] for e in ex_out)
            status = "triggered"
        elif hold_pct is not None:
            # 未触发 = 策略认为还该拿着，收益就等于「死拿」
            strat_pct, strat_amt = hold_pct, None
            status = "frozen" if b["frozen_at"] else "holding"
        else:
            strat_pct, strat_amt, status = None, None, "no_base"

        g["rows"].append({
            "binding_id": b["id"], "strategy_id": b["strategy_id"],
            "name": b["name"], "kind": b["kind"],
            "label": strategy_label(b["kind"], _float(b["target_pct"]),
                                    _float(b["drawdown_pct"]), b["config"]),
            "enabled": bool(b["enabled"]), "triggered": bool(b["triggered_at"]),
            "target_pct": _float(b["target_pct"]),
            "drawdown_pct": _float(b["drawdown_pct"]),
            "peak": _float(b["peak_price"]), "ladder_step": b["ladder_step"] or 0,
            "base_cost": base, "base_shares": shares, "frozen": bool(b["frozen_at"]),
            "base_entry_date": entry,
            "held_days": _held_days(deps, entry, now_dt),
            "status": status, "exits": ex_out,
            "strat_pct": round(strat_pct, 2) if strat_pct is not None else None,
            "strat_amount": round(strat_amt, 2) if strat_amt is not None else None,
            "hold_pct": hold_pct,
            "edge": (round(strat_pct - hold_pct, 2)
                     if (strat_pct is not None and hold_pct is not None) else None),
        })

    for c, vals in llm.items():
        if c in groups:
            groups[c]["llm_pct"] = round(sum(vals) / len(vals), 2)
            groups[c]["llm_rounds"] = len(vals)
    for g in groups.values():
        g.setdefault("llm_pct", None)
        g.setdefault("llm_rounds", 0)
        g["rows"].sort(key=lambda r: (r["strat_pct"] is None, -(r["strat_pct"] or 0)))

    items = [groups[c] for c in sorted(groups)]
    if code:
        items = [g for g in items if g["code"] == code]
    # show_frozen 关掉时，把「LLM 已平仓、只剩影子仓」的票整组隐藏
    if not _strategy_conf(deps).get("show_frozen", True):
        items = [g for g in items if g["live_shares"] > 0]
        for g in items:
            g["rows"] = [r for r in g["rows"] if not r["frozen"]] or g["rows"]
    return {"items": items, "summary": _summary(items)}


def _summary(items: list[dict]) -> dict:
    """全局口径：策略整体比死拿好多少 —— 探索策略最终要回答的那个问题。"""
    best_edges, spread = [], []
    by_kind: dict[str, list[float]] = {}
    for g in items:
        rows = [r for r in g["rows"] if r["edge"] is not None]
        if not rows:
            continue
        e = [r["edge"] for r in rows]
        best_edges.append(max(e))
        pcts = [r["strat_pct"] for r in rows if r["strat_pct"] is not None]
        if pcts:
            spread.append(max(pcts) - min(pcts))
        for r in rows:
            by_kind.setdefault(r["kind"], []).append(r["edge"])
    return {
        "codes": len(items),
        "bindings": sum(len(g["rows"]) for g in items),
        "triggered": sum(1 for g in items for r in g["rows"] if r["status"] == "triggered"),
        "frozen": sum(1 for g in items for r in g["rows"] if r["frozen"]),
        "kinds": sorted(by_kind),
        # 每种 kind 的平均超额：横向看「哪一类退出规则更有效」，
        # 比逐票看更有意义 —— 逐票的差异大多来自个股，不是策略。
        "edge_by_kind": {k: round(sum(v) / len(v), 2)
                         for k, v in sorted(by_kind.items())},
        "avg_best_edge": round(sum(best_edges) / len(best_edges), 2) if best_edges else None,
        "avg_spread": round(sum(spread) / len(spread), 2) if spread else None,
        "note": ("edge>0 = 该策略比死拿好。edge_by_kind 横向看哪一类退出规则更有效"
                 "（比逐票看可靠，逐票差异多半来自个股而非策略）。"
                 "avg_spread 越大说明几套策略分歧越大，越值得继续观察谁最终胜出。"),
    }
