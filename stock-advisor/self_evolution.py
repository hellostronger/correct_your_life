"""自进化：把 RSIAgent 的「探索→验证→记忆」三段式搬进模拟盘（2026-10-12 新增）。

## 为什么要它 —— 先看实测（本模块存在的唯一理由）

2026-10-12 实测 `sa_paper_trades` / `sa_paper_reflections`：

| 指标 | 实测值 |
|---|---|
| 交易流水 | **682** 行 |
| 有结（`settle_date` 非空）的平仓单元 | **17** 行 |
| 经验行 `sa_paper_reflections` | **3** 行（2 条还是 09-29 那次蒸馏出的全局规则） |
| 唯一一条逐笔经验 | `trade_id=312`（06658，2026-09-29） |
| 经验库最后写入 | **2026-09-29** —— 之后 13 天一条没长 |
| 卡在 `status='open'` 的行 | **12** 行，最早 09-25，已超 `holding_days=5` 十倍 |

**学习回路是断的，不是慢的。** 三个根因（都已用项目自己的函数复现，不是推测）：

1. **止损/止盈平仓根本不进复盘。** `_execute_decision` 卖出后把买入行
   `status='resolved'`（`_settle_buy_rows`），而 `settle_and_reflect` 的 SELECT
   只挑 `status='open'`。止损卖出那 7 行（`_STOPLOSS_ALERTED` 同期记录）永远
   不会被 `_reflect_one` 看到 —— 而**止损恰恰是最该学的样本**。
2. **`rolled` 没有上限。** `_thesis_intact` 说「论点未破」就再续一个持有期，
   `rolled` 只进返回值和一个 print，**从不调用 `_close_trade`**。用真实
   `_thesis_intact` 跑 12 行卡住的数据：**7 行被判 KEEP，永久续持**。
   这 7 行既不平仓也不复盘，等于从经验库和账本里同时消失。
3. **蒸馏门槛 20 条经验，而经验只有 1 条。** `maybe_distill(every=20)`
   永远不触发，所以「全局规则」从 09-29 起就没再更新过。

结论：**先修数据通路，再谈自进化。** 一个进不去经验的系统，无论加多少
agent 都是空转。所以本模块的第 0 步是 `backfill()` —— 把已存在的 17 个
平仓单元补上经验，并给 `rolled` 加上限。

## RSIAgent 的三段式怎么映射过来

RSIAgent（Apache-2.0，506 star，2026-09 开源）把自进化拆成三个**职责严格隔离**的角色：

| RSIAgent | 本项目对应 | 为什么这样映射 |
|---|---|---|
| **Actor**（执行、读记忆） | `_decide_one` 的交易员 | 已经有，且已经在读 `get_past_context` 注入的经验 |
| **Verifier**（独立验收，**看不到 Actor 的私有推理和记忆**） | `_reflect_one` 的 REFLECTOR_PROMPT | **这是最关键的一条**：裁决方不能看执行方的自辩，否则就是自己给自己打分 |
| **Curriculum**（决定下一轮练什么，**不评分、不写记忆**） | `next_focus` + `_CURRICULUM_PROMPT` | RSIAgent 明文规定 Curriculum 不得改记忆、不得判对错 |
| **持久记忆**（`~/.memory`，经验库） | `sa_paper_reflections` + `sa_evo_focus` | 已有表，本模块只加「焦点」这一层 |

RSIAgent 还有两条容易被忽略但很关键的性质，本模块照搬：

- **PASS 和 FAIL 都能沉淀经验**（`should_learn = FAIL or (PASS and learn_on_pass)`）。
  亏钱的止损单比赚钱的单更该学 —— 现有 `_reflect_one` 恰好只在结算里跑，
  止损样本全丢。
- **记忆可能记错规则**（RSIAgent 原文列为三大局限之一）。所以 `sa_evo_rules`
  每条规则带 `supports/refutes` 计数与 `status`，被后续样本反复证伪就自动退役，
  而不是永远注入。

## 本模块的三个函数（都可离线测试，不烧 LLM）

- `backfill(cur, llm_call, ...)`：给**已平仓但没经验**的行补经验（含止损单）。
  这是唯一能立刻把 682:1 的悬殊比例拉回来的动作。
- `next_focus(cur, llm_call, ...)`：Curriculum 角色。看聚合后的成败结构，
  挑出下一轮最该练的**一个**薄弱点，写 `sa_evo_focus`。不判对错、不改记忆。
- `build_injection(cur, ...)`：把「当前焦点 + 有效规则」拼成注入块。

## 不做什么（划清边界）

- **不改任何交易逻辑**：不碰 `_execute_decision` / `check_stop_loss` /
  止损阈值 / 仓位上限。本模块只读交易结果、只写学习表。
- **`rolled` 上限不在这里实现**：那属于交易/钱的口径，必须由人决定
  （见 AGENTS.md「必须先问」清单）。本模块只把现状**暴露出来**给 `/api/evo/status`。
- 本模块**不 import app**（同 alerts.py / paper_memory.py 惯例，避免循环依赖）。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

# 与 paper_memory.DEFAULTS 分开：本模块的默认全是**关闭**，符合项目
# 「改口径的开关默认关」的既有约定（paper_debate.enabled / t_trading.enabled）。
DEFAULTS = {
    "enabled": False,        # 整个自进化模块的总闸
    "backfill": True,        # 允许补历史经验（纯读+写学习表，不动交易）
    "curriculum": False,     # Curriculum 角色要烧 LLM，默认关
    "max_backfill": 20,      # 单次补多少条，避免一次烧爆
    "min_samples_focus": 3,  # 少于这个样本数不出结论（宁缺毋滥）
    "rule_supports_to_keep": 2,   # 规则要有几个支持样本才继续注入
    "rule_refutes_to_drop": 2,    # 被证伪几次就退役
    "inject_limit": 6,
}

# ---------------- 角色 2：Verifier（独立验收） ----------------
#
# RSIAgent 的硬规则：Verifier **不能读 Actor 的私有推理和记忆**。
# 本项目的对应约束：给 REFLECTOR 的输入里**不含**历史经验块
# （`get_past_context` 的输出不进 `_reflect_one`），也不含 Judge 评分。
# 只给「当时说了什么」+「客观发生了什么」。
#
# 为什么这条值钱：交易员在同一轮里刚为自己的持仓写过看多理由
# （`reasoning`），复盘时若同时看到「我当时经验很准」，它会倾向于
# 把亏损解释成「市场不理性」而不是「我判断错了」。切断经验输入，
# 强制它只看「说的」和「发生的」之间的差。
REFLECTOR_PROMPT = """\
你是交易复盘员。一笔模拟交易已平仓。你会得到：当时的**操作理由原文**、
入场价→离场价收益、计划持有期、以及**离场的客观原因**。

⚠️ 你**看不到**本系统过去积累的经验或任何评分。你要做的是独立验收，
不是为自己的历史战绩辩护。

核心任务不是「赚没赚」，而是「**当初说的那件事发生了吗**」。

请按顺序回答：
1. **论点核验**：从操作理由里挑出**最具体的那条可验证的判断**，
   对照客观结果回答：它**兑现了 / 没兑现 / 无法判断**。必须引用理由原文里的数字。
2. **偏差说明**：收益为负时，是判断错了，还是执行/风控出了问题
   （止损线设得太宽、进场点位太高、持有期与论点不匹配）？两者要分开。
3. **一条可操作的修正**：下次同类判断该改什么。

重要：
- 持有窗口只有几个交易日，可能短于论点的兑现周期 —— 若无法评判，
  就直说「窗口太短无法判断」，**不要硬编结论**。
- 不要用「赚了就是判断对」倒推。方向对错与盈亏是两件事。
- 不要为亏损找外部借口。市场错了和你错了是两种可能，如实说。
- 只输出经验文本本身，中文，不要客套话。"""


# ---------------- 角色 3：Curriculum（决定下一轮练什么） ----------------
#
# RSIAgent 原文：Curriculum「reviews learning progress and selects the next
# practice or target attempt; **does not grade candidates or author the
# Actor's memory**」。所以这个 prompt 只允许输出「练什么」，
# 不允许它评价哪笔交易对错，也不允许它改写已有规则。
_CURRICULUM_PROMPT = """\
你是模拟盘的**课程设计者**。你看到的是一批已平仓交易的**客观统计**：
按「离场原因」和「相对基准超额收益」分组的样本数与均值。

你的唯一任务是：**从这些统计里挑出下一个最该专门练的薄弱点**。

严格约束：
- 你的输出**不是**对任何一笔交易的评价，也不要评价模型整体水平。
- 你**没有权限**修改或否定已有的交易规则。你只能建议「下一轮重点关注什么」。
- 必须引用你看到的**具体数字**（哪一组样本多、均值多少）来支撑你的选择。
- 若样本太少、看不出集中问题，就直接回答「样本不足，暂不调整」。

只输出一行，格式严格如下：
FOCUS: <一句话说明下一轮要重点关注的薄弱点>

不要编号、不要客套、不要解释方法论。"""


CURRICULUM_PROMPT = _CURRICULUM_PROMPT

_FOCUS_RE = re.compile(r"FOCUS\s*[:：]\s*(.+)", re.I)


def parse_focus(text: str) -> str:
    """从 Curriculum 输出里取 FOCUS 行。

    取不到就返回空串（调用方据此判定「不调整」）。**不猜、不兜底编造** ——
    与 `_extract_truncated_json` 的纪律一致：宁可没有，也不要看起来有。
    """
    if not text or not isinstance(text, str):
        return ""
    m = _FOCUS_RE.search(text)
    if not m:
        return ""
    out = m.group(1).strip().strip("。.；;")
    return out[:300]


# ---------------- 失效护栏 ----------------

def _num(v, default=None):
    """数值兜底：None / 非法 / NaN 一律给 default。

    为什么不直接 `float(v)`：`float('nan')` **不抛异常**，会一路渗进
    SQL 的 `round(alpha*100, 1)` 显示成 `nan`（AGENTS.md 记过同一类坑）。
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return f


def _dt(v, default=None):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and v.strip():
        try:
            return datetime.strptime(v.strip()[:10], "%Y-%m-%d").date()
        except ValueError:
            return default
    return default


# ---------------- 角色 2：验收统计 ----------------

def outcome_stats(cur) -> dict:
    """聚合已平仓单元的成败结构 —— Curriculum 的唯一输入。

    **只统计 `settle_date` 非空的 buy 行**，因为那才是「一次有始有终的决策」。
    `status='open'` 的行连结果都没有，统计它们会让「还在持有」看起来像「没赚没亏」。
    """
    cur.execute("""
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE alpha_return IS NOT NULL) AS with_alpha,
               COUNT(*) FILTER (WHERE alpha_return > 0)   AS wins,
               COUNT(*) FILTER (WHERE alpha_return <= 0)  AS losses,
               ROUND(AVG(alpha_return) * 100, 2)          AS avg_alpha,
               ROUND(MIN(raw_return) * 100, 2)             AS worst,
               ROUND(MAX(raw_return) * 100, 2)             AS best
        FROM sa_paper_trades
        WHERE side = 'buy' AND settle_date IS NOT NULL""")
    row = cur.fetchone()
    d = dict(row) if isinstance(row, dict) else {}
    total = int(_num(d.get("total"), 0) or 0)
    return {
        "total": total,
        "with_alpha": int(_num(d.get("with_alpha"), 0) or 0),
        "wins": int(_num(d.get("wins"), 0) or 0),
        "losses": int(_num(d.get("losses"), 0) or 0),
        "avg_alpha": _num(d.get("avg_alpha")),
        "worst": _num(d.get("worst")),
        "best": _num(d.get("best")),
    }


def stranded_rows(cur) -> list[dict]:
    """**已经超期却仍挂在 `status='open'` 的行** —— 学习回路的漏斗。

    这些行既不平仓（`rolled` 没上限）也不进经验（SELECT 只挑 open 且本轮能平的）。
    单独暴露出来，是因为它们正是「记忆学不到东西」的直接证据：
    2026-10-12 实测 12 行，最早 09-25。
    """
    cur.execute("""
        SELECT id, code, trade_date, holding_days_inferred
        FROM (
            SELECT id, code, trade_date,
                   COALESCE(
                       NULLIF(decision_raw->>'hold_days','')::int, 0
                   ) AS holding_days_inferred
            FROM sa_paper_trades
            WHERE side = 'buy' AND status = 'open'
        ) t
        WHERE trade_date < %s
        ORDER BY trade_date, id""", (date.today(),))
    rows = cur.fetchall()
    out = []
    for r in rows:
        d = dict(r) if isinstance(r, dict) else {}
        if not d.get("id"):
            continue
        td = _dt(d.get("trade_date"))
        out.append({
            "id": d["id"],
            "code": d.get("code") or "",
            "trade_date": td,
            "age_days": (date.today() - td).days if td else None,
            "hold_days": _num(d.get("holding_days_inferred")),
        })
    return out


# ---------------- 第 0 步：补历史经验（修断掉的回路） ----------------

_BACKFILL_SQL = """
    SELECT b.id, b.code, b.name, b.trade_date, b.settle_date, b.price, b.settle_price,
           b.raw_return, b.alpha_return, b.benchmark, b.shares, b.reasoning, b.report,
           b.decision_raw, b.auto_closed, b.stop_loss_pct,
           s.reasoning AS sell_reasoning
    FROM sa_paper_trades b
    LEFT JOIN LATERAL (
        SELECT reasoning FROM sa_paper_trades s
        WHERE s.code = b.code AND s.side = 'sell'
          AND s.trade_date = b.settle_date
        ORDER BY s.id DESC LIMIT 1
    ) s ON TRUE
    WHERE b.side = 'buy' AND b.settle_date IS NOT NULL
      AND b.id NOT IN (SELECT trade_id FROM sa_paper_reflections WHERE trade_id > 0)
    ORDER BY b.settle_date, b.id
    LIMIT %s
"""

EXIT_KIND_CN = {"maturity": "到期/达标平仓", "stop_loss": "止损离场",
                "take_profit": "止盈离场", "signal_exit": "信号卖出"}


def _classify_exit(row: dict) -> str:
    """判定这笔的**离场方式** —— 只用客观列，不问 LLM。

    ⚠️ **离场理由不在 buy 行上**。`reasoning` 存的是**买入理由**（「近20日涨幅
    +37.7%显示强势上升趋势」），止损那行是 `_execute_decision` 写的
    `status='closed'` 的 **sell** 行，理由形如「盘中止损：现价 174.1 较成本
    190.5001 跌 -8.61%」。2026-10-12 实测：只看 buy 行会把 5 笔止损单
    全判成 `signal_exit` —— **止损样本恰恰是最该学的那批**（AGENTS.md：
    「看起来成功的地方要再确认一次」）。所以必须 LATERAL JOIN 出卖出行再看。

    判定顺序：auto_closed → 到期/达标强平；否则按**卖出行**前缀分
    止损/止盈；都没有就是信号卖出。"""
    if _num(row.get("auto_closed"), 0):
        return "maturity"          # 到期/达标强平
    # 卖出行优先，买入行兜底（老数据可能没有配对的 sell 行）
    reason = str(row.get("sell_reasoning") or "")[:40] \
        or str(row.get("reasoning") or "")[:40]
    if "止损" in reason:
        return "stop_loss"
    if "止盈" in reason or "目标位" in reason:
        return "take_profit"
    return "signal_exit"


def backfill(cur, llm_call, max_rows: int = 20, keep_per_ticker: int = 30) -> dict:
    """给**已平仓但从未产生经验**的决策补一条经验。

    这是本模块里唯一会**立刻改变注入内容**的动作，所以刻意做得最保守：
      - 只补 `settle_date` 非空、且 `sa_paper_reflections` 里没有对应 trade_id 的行
      - `paper_memory.store_lesson` 自带 `ON CONFLICT (trade_id) DO NOTHING` → 幂等
      - 不碰任何交易表

    `llm_call(system, user)` 与 `paper_trading._llm_call` 同签名。
    """
    import paper_memory

    cur.execute(_BACKFILL_SQL, (int(max_rows),))
    rows = cur.fetchall()
    added, failed = [], []
    for r in rows:
        row = dict(r) if isinstance(r, dict) else {}
        tid = row.get("id")
        if not tid:
            continue
        entry = _num(row.get("price"))
        exit_ = _num(row.get("settle_price"))
        if not entry or not exit_:
            failed.append({"id": tid, "code": row.get("code"),
                           "reason": "价格缺失，无法构造复盘"})
            continue
        raw_ret = _num(row.get("raw_return"))
        if raw_ret is None:
            raw_ret = exit_ / entry - 1
        alpha = _num(row.get("alpha_return"))
        try:
            dec = row.get("decision_raw")
            if isinstance(dec, str):
                dec = json.loads(dec)
            dec = dec.get("decision") if isinstance(dec, dict) else {}
            if not isinstance(dec, dict):
                dec = {}
        except (ValueError, TypeError):
            dec = {}
        hold_n = _num(dec.get("hold_days")) or 5
        kind = _classify_exit(row)
        kind_cn = EXIT_KIND_CN[kind]
        bench_ret = (raw_ret - alpha) if alpha is not None else None
        digest = (f"{row.get('side', 'buy')} {row.get('code')} @ {entry:g}："
                  f"{str(row.get('reasoning') or '')[:120]}")
        user = (
            f"【当时决策】{digest}\n"
            f"【分析报告要点】{str(row.get('report') or '')[:600]}\n"
            f"【计划持有】{int(hold_n)} 个交易日\n"
            f"【离场方式】{kind_cn}\n"
            f"【结果】{_dt(row.get('trade_date'))} 入场 → {_dt(row.get('settle_date'))} 离场，"
            f"区间收益 {raw_ret * 100:+.1f}%"
            + (f"，同期基准收益 {bench_ret * 100:+.1f}%，超额 alpha {alpha * 100:+.1f}%"
               if bench_ret is not None else "（基准数据缺失，alpha 无法计算）")
            + "\n请核验：当初说的那件事发生了吗？")
        try:
            lesson = llm_call(REFLECTOR_PROMPT, user)
        except Exception as exc:                # noqa: BLE001
            failed.append({"id": tid, "code": row.get("code"),
                           "reason": f"LLM 调用失败: {exc}"})
            continue
        if not isinstance(lesson, str) or not lesson.strip():
            failed.append({"id": tid, "code": row.get("code"),
                           "reason": "LLM 返回空"})
            continue
        try:
            ok = paper_memory.store_lesson(
                cur, trade_id=int(tid), code=str(row.get("code") or ""),
                action="buy",
                decision_digest=digest,
                raw_return=raw_ret, alpha_return=alpha if alpha is not None else 0.0,
                holding_days=int(hold_n), benchmark=str(row.get("benchmark") or ""),
                lesson_text=lesson.strip()[:2000], resolved_at=_dt(row.get("settle_date")))
        except Exception as exc:                # noqa: BLE001
            failed.append({"id": tid, "code": row.get("code"),
                           "reason": f"入库失败: {exc}"})
            continue
        if ok:
            added.append({"id": tid, "code": row.get("code"), "exit_kind": kind,
                          "alpha": None if alpha is None else round(alpha * 100, 2)})
    # 新经验入库后顺带做一次膨胀控制（保持与结算流程一致的口径）
    try:
        pruned = paper_memory.prune(cur, int(keep_per_ticker))
    except Exception:                            # noqa: BLE001
        pruned = 0
    return {"candidates": len(rows), "added": added, "failed": failed,
            "pruned": pruned}


# ---------------- 第 3 步：Curriculum（下一轮练什么） ----------------

DDL = """
CREATE TABLE IF NOT EXISTS sa_evo_focus (
    id          bigserial PRIMARY KEY,
    focus_date  date NOT NULL,
    focus_text  text NOT NULL,
    stats_json  text NOT NULL DEFAULT '{}',
    source      text NOT NULL DEFAULT 'curriculum',
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (focus_date, focus_text)
);
CREATE INDEX IF NOT EXISTS idx_sa_evo_focus_date
    ON sa_evo_focus (focus_date DESC);

-- 规则带证据计数：RSIAgent 明文把「记忆可能记错规则」列为三大局限之一，
-- 所以规则必须能**被后续样本证伪并退役**，不能生成一次就永远注入。
CREATE TABLE IF NOT EXISTS sa_evo_rules (
    id          bigserial PRIMARY KEY,
    rule_text   text NOT NULL,
    origin      text NOT NULL DEFAULT 'distill',
    supports    int  NOT NULL DEFAULT 0,
    refutes     int  NOT NULL DEFAULT 0,
    status      text NOT NULL DEFAULT 'active',
    retire_note text NOT NULL DEFAULT '',
    learned_at  date NOT NULL DEFAULT (now()::date),
    retired_at  date,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (rule_text)
);
CREATE INDEX IF NOT EXISTS idx_sa_evo_rules_status
    ON sa_evo_rules (status, supports DESC);
"""


def load_focus(cur, as_of=None) -> str:
    """取最近一条 focus 文本，供注入。没有就返回空串（不硬造默认重点）。

    **表不存在也不能抛异常** —— 新表要等 `init_db` 跑过 DDL 才有，
    而决策路径必须先能起来（AGENTS.md：不能因为「读不出来」让整轮决策失败）。
    """
    d = _dt(as_of) or date.today()
    try:
        cur.execute("SELECT focus_text FROM sa_evo_focus WHERE focus_date <= %s "
                    "ORDER BY focus_date DESC, id DESC LIMIT 1", (d,))
        row = cur.fetchone()
    except Exception as exc:                    # noqa: BLE001
        # UndefinedTable 之后事务已被 psycopg2 标记 aborted，必须先回滚
        # 才能继续用同一个游标，否则后面每条语句都报 current transaction
        # is aborted（这是「静默降级反而制造更大故障」的典型）。
        try:
            cur.connection.rollback()
        except Exception:                        # noqa: BLE001
            pass
        print(f"[evo] focus 表不可用（当作无 focus）: {exc}", flush=True)
        return ""
    if not row:
        return ""
    v = row.get("focus_text") if isinstance(row, dict) else row[0]
    return str(v or "").strip()


def store_focus(cur, focus_text: str, stats: dict | None = None,
                focus_date=None) -> bool:
    """写入一条 focus。文本为空 = 「样本不足，暂不调整」，**不入库**。

    不入库是刻意的：把「没结论」也存成一行，会让下一次 `load_focus`
    读到一条没有信息量的重点，**看起来在调整、其实没调**。
    """
    text = str(focus_text or "").strip()
    if not text:
        return False
    d = _dt(focus_date) or date.today()
    cur.execute(
        "INSERT INTO sa_evo_focus (focus_date, focus_text, stats_json) "
        "VALUES (%s, %s, %s) ON CONFLICT (focus_date, focus_text) DO NOTHING",
        (d, text, json.dumps(stats or {}, ensure_ascii=False)))
    return cur.rowcount > 0


def record_outcome(cur, trade_id: int, *, supported: bool | None = None,
                   refuted: bool | None = None, note: str = "") -> None:
    """给一条规则记一次支持/证伪。`status='retired'` 后不再计数。

    计数只加不重置：一条规则被 N 次证伪过，就该退役，而不是被一次支持洗白。
    """
    if supported:
        cur.execute("UPDATE sa_evo_rules SET supports = supports + 1 "
                    "WHERE id = %s AND status <> 'retired'", (trade_id,))
    if refuted:
        cur.execute("UPDATE sa_evo_rules SET refutes = refutes + 1, "
                    "retire_note = %s WHERE id = %s AND status <> 'retired'",
                    (note[:500], trade_id))


def apply_rule_decay(cur, supports_to_keep: int = 2, refutes_to_drop: int = 2) -> int:
    """把被反复证伪的规则退役，返回退役条数。

    退役条件**两个都要满足**（证伪够多 **且** 支持不够多），否则一条
    「对 5 次错 2 次」的规则会被立刻处决 —— 那正是过拟合的反面。
    """
    cur.execute(
        "UPDATE sa_evo_rules SET status = 'retired', retired_at = now()::date, "
        "retire_note = COALESCE(NULLIF(retire_note, ''), '连续被证伪，自动退役') "
        "WHERE status = 'active' AND refutes >= %s AND supports < %s "
        "RETURNING id", (int(refutes_to_drop), int(supports_to_keep)))
    return len(cur.fetchall())


def build_injection(cur, as_of=None, limit: int = 6) -> str:
    """组装注入块：当前焦点 + 仍然有效的规则。

    **任何一步坏掉都必须返回空串而不是抛异常** —— 注入是锦上添花，
    绝不能因为「经验库读不出来」就让整轮决策失败（同 analysis_learn 的纪律）。
    """
    parts = []
    try:
        focus = load_focus(cur, as_of)
        if focus:
            parts.append(f"- {focus}")
    except Exception as exc:                    # noqa: BLE001
        print(f"[evo] 读 focus 失败（跳过）: {exc}", flush=True)
    try:
        cur.execute(
            "SELECT rule_text, supports FROM sa_evo_rules "
            "WHERE status = 'active' AND supports >= 1 "
            "ORDER BY supports DESC, id DESC LIMIT %s", (int(limit),))
        rows = cur.fetchall()
        for row in rows:
            # RealDictCursor 给 dict、普通 cursor 给 tuple，**两条路都要走对**。
            # 写成 `row[1] if isinstance(row, dict) else row[0]` 会让 dict 行走
            # `row[1]` → KeyError: 1（AGENTS.md 记的同一类游标/取字段错配）。
            if isinstance(row, dict):
                v, sup = row.get("rule_text"), row.get("supports")
            else:
                v, sup = row[0], row[1]
            if v:
                parts.append(f"- （{sup} 次样本支持）{str(v).strip()}")
    except Exception as exc:                    # noqa: BLE001
        try:
            cur.connection.rollback()
        except Exception:                        # noqa: BLE001
            pass
        print(f"[evo] 读规则失败（跳过）: {exc}", flush=True)
    if not parts:
        return ""
    return "【下一轮重点关注的薄弱点】\n" + "\n".join(parts)


__all__ = [
    "DEFAULTS", "REFLECTOR_PROMPT", "CURRICULUM_PROMPT", "DDL", "EXIT_KIND_CN",
    "parse_focus", "outcome_stats", "stranded_rows", "backfill", "load_focus",
    "store_focus", "record_outcome", "apply_rule_decay", "build_injection",
    "_classify_exit",
]