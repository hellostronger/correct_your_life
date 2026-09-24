"""模拟交易经验库：TradingAgents TradingMemoryLog 的 DB 移植版。

设计（对应 TradingAgents decision_log.py / graph/reflection.py）：
- 每笔 resolved 交易产生一条经验（2-4 句话，LLM Reflector 生成）
- get_past_context：同股取最近 n_same 条全文 + 跨股取最近 n_cross 条仅经验句；
  as_of 做 point-in-time 过滤（只用「结论当时已知」的经验，防未来函数）
- distill_rules：积累足够经验后蒸馏全局规则（code='' 行），所有决策注入
- prune：每股票只留最近 keep_per_ticker 条，防库无限膨胀

本模块不 import app（避免循环依赖，同 alerts.py 惯例）：连接/游标由调用方传入。
"""

from datetime import date

DEFAULTS = {"n_same": 5, "n_cross": 3, "keep_per_ticker": 30, "distill_every": 20}


def store_lesson(cur, *, trade_id: int, code: str, action: str, decision_digest: str,
                 raw_return: float, alpha_return: float, holding_days: int,
                 benchmark: str, lesson_text: str, resolved_at, global_rule: bool = False) -> bool:
    """结算后写一条经验。trade_id 唯一约束 → 重跑幂等，返回是否新写入。

    global_rule=True 用于 distill_rules 产出的全局规则行（trade_id=0, code=''）。
    """
    cur.execute(
        "INSERT INTO sa_paper_reflections "
        "(trade_id, code, action, decision_digest, raw_return, alpha_return, "
        " holding_days, benchmark, lesson, resolved_at, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()) "
        "ON CONFLICT (trade_id) DO NOTHING",
        (trade_id if not global_rule else 0, code, action, decision_digest,
         raw_return, alpha_return, holding_days, benchmark, lesson_text, resolved_at))
    return cur.rowcount > 0


def get_past_context(cur, code: str, n_same: int = 5, n_cross: int = 3,
                     as_of=None) -> str:
    """组装注入 LLM 决策上下文的历史经验文本（新→旧）。

    as_of（date/str）做 point-in-time 过滤：只注入 resolved_at <= as_of 的经验
    ——结算发生在决策 N 个交易日后，未结算的交易其结论「当时不可知」。
    全局蒸馏规则（code=''）作为第三段注入（至多 5 条）。
    """
    try:
        if as_of is None:
            as_of = date.today()
        as_of = str(as_of)[:10]
    except Exception:
        as_of = str(date.today())
    cur.execute(
        "SELECT code, action, decision_digest, raw_return, alpha_return, benchmark, "
        "lesson, resolved_at FROM sa_paper_reflections "
        "WHERE resolved_at <= %s ORDER BY created_at DESC LIMIT 200",
        (as_of,))
    rows = [dict(r) for r in cur.fetchall()] if hasattr(cur, "fetchall") else []
    # RealDictCursor 时上游已转 dict；普通 cursor 不该传进来，防御一下
    if rows and not isinstance(rows[0], dict):
        rows = []
    same = [r for r in rows if r["code"] == code][:n_same]
    cross = [r for r in rows if r["code"] not in (code, "")][:n_cross]
    rules = [r for r in rows if r["code"] == ""][:5]
    if not (same or cross or rules):
        return "（暂无历史经验——这是该系统最早的决策之一）"
    lines: list[str] = []
    if same:
        lines.append("【本股历史决策复盘（新→旧，成败按相对基准的超额收益 alpha 判定）】")
        for r in same:
            alpha = float(r["alpha_return"]) * 100
            lines.append(
                f"- [{r['resolved_at']} | {r['action']} | 5日alpha {alpha:+.1f}%] "
                f"{r['decision_digest']} → 经验：{r['lesson']}")
    if cross:
        lines.append("【跨股票经验（最近）】")
        for r in cross:
            lines.append(f"- [{r['resolved_at']} | {r['code']}] 经验：{r['lesson']}")
    if rules:
        lines.append("【全局决策规则（从历史经验蒸馏）】")
        for r in rules:
            lines.append(f"- {r['lesson']}")
    return "\n".join(lines)


def prune(cur, keep_per_ticker: int = 30) -> int:
    """经验库膨胀控制：每股（含全局规则行）只保留最近 keep_per_ticker 条。

    全局规则行（code=''）单独保留最近 5 条，不被 prune 掉。
    """
    cur.execute(
        "DELETE FROM sa_paper_reflections WHERE id IN ("
        "  SELECT id FROM ("
        "    SELECT id, ROW_NUMBER() OVER (PARTITION BY code "
        "      ORDER BY created_at DESC) AS rn "
        "    FROM sa_paper_reflections WHERE code <> '') t WHERE t.rn > %s)",
        (keep_per_ticker,))
    n = cur.rowcount
    cur.execute(
        "DELETE FROM sa_paper_reflections WHERE code = '' AND id NOT IN ("
        "  SELECT id FROM sa_paper_reflections WHERE code = '' "
        "  ORDER BY created_at DESC LIMIT 5)")
    return n + cur.rowcount


def distill_rules(cur, llm_call, lessons_text: str, resolved_at) -> list[str]:
    """把近 N 条经验蒸馏成全局决策规则（P7 可选）。

    返回规则文本列表并写为 code='' 的行（trade_id=0 会被同批多行冲突——
    全局规则行用随机负 trade_id 避开唯一约束）。要求「只提炼有 ≥3 个样本
    支持的规则」，防过度拟合。
    """
    rules_raw = llm_call(
        "你是量化复盘教练。用户给你一批已结算模拟交易的经验记录（含每笔的 "
        "5日 alpha）。请从中蒸馏出 3-5 条「高胜率决策规则」：只提炼有明显 "
        "统计迹象（至少 3 笔同类样本支持）的规则，每条一句话、可操作、带 "
        "适用条件；样本不足宁缺毋滥。只输出规则列表（每行一条，- 开头），"
        "中文，不要编号不要客套。",
        f"以下是已结算交易的经验记录：\n\n{lessons_text}")
    rules = [l.strip().lstrip("-•").strip() for l in rules_raw.splitlines()
             if l.strip() and len(l.strip()) > 4][:5]
    if not rules:
        return []
    cur.execute(
        "SELECT COALESCE(MIN(trade_id), 0) - 1 FROM sa_paper_reflections")
    base_id = cur.fetchone()[0]
    base_id = min(base_id, -1)  # 全局规则行用负 trade_id，避开真实交易
    for i, rule in enumerate(rules):
        cur.execute(
            "INSERT INTO sa_paper_reflections "
            "(trade_id, code, action, decision_digest, raw_return, alpha_return, "
            " holding_days, benchmark, lesson, resolved_at, created_at) "
            "VALUES (%s, '', 'rule', '', 0, 0, 0, '', %s, %s, now())",
            (base_id - i, rule, resolved_at))
    return rules


def maybe_distill(cur, llm_call, resolved_at, every: int = 20) -> list[str]:
    """每积累 every 条新经验触发一次蒸馏（结算流程尾部调用）。"""
    cur.execute(
        "SELECT lesson FROM sa_paper_reflections WHERE code <> '' "
        "ORDER BY created_at DESC LIMIT %s", (every,))
    rows = cur.fetchall()
    if len(rows) < every:
        return []
    lessons_text = "\n".join(f"- {r[0]}" for r in rows)
    return distill_rules(cur, llm_call, lessons_text, resolved_at)
