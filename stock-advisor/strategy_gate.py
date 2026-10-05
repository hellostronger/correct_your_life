# -*- coding: utf-8 -*-
"""聚宽策略沙箱验证的**合格判据**、落库与汇报。

为什么单独一个模块：判断「跑出来的数能不能用」这件事，和跑沙箱是两件独立的
事。跑通了不等于可信 —— 实测 4 个候选里 4 个都 `ok=True`，但没有一个的收益
数字能直接拿去决策：

  · 万得微盘股复刻 ok，但沙箱只有日线，策略里 `run_daily('14:50')` 的日内择时
    完全没复现；ST 标记也拿不到（收益被系统性高估）。它自称年化很高。
  · 多因子LightGBM ok，但拼接源码里有一段裸的 `if dd >= 0.20:` 在模块层，
    `dd` 无定义；整份代码**没有 initialize**，聚宽策略不跑初始化就等于空跑。
  · 低位3连阳首板第4版 ok，但作者用 `...` 把核心选股逻辑藏了，作者自己还在
    注释里写明「before_open 没 return，get_buy 会 TypeError」。
  · 低位三连阳首板优化 ok，但抽出来的 14 行全是 `g.xxx = ...` 参数赋值 ——
    这是作者贴出来的**配置片段**，不是策略。

所以 `ok` 只说明「代码跑完了」，合格与否要另判。这里把判据写成代码而不是
留在脑子里，因为它要同时服务三个出口：落库的 portable_score、日报里的一行
提示、以及页面上的筛选。

判据分两层，不要混：
  · **硬否决**（blocker）：命中就不能用，收益数字一律不作数。
  · **扣分**（penalty）：不否决，但压低 portable_score，并在提示里点名。

阈值都放在 THRESHOLDS 里且写清出处，不是随手拍的。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 判据阈值
# ---------------------------------------------------------------------------
# 为什么是这些数：
#   * sharpe<0.5 —— A股主动策略长期跑不赢指数是常态，但年化 16% / 回撤 20%
#     对应的夏普只有 0.78，扣 1 分。低于 0.5 基本等于买了个 beta。
#   * max_drawdown>25% —— 超过 1/4 就要问一句这个策略是不是拿得住。
#   * excess<=0 —— 跑不赢它自己的基准（基准就是切片第一只票，见 app.sandbox_run
#     的 `bench = m["codes"][0]`），那就没有「策略」可言，只有「买对了这一只」。
#   * 交易数 <30 —— 样本不足以谈胜率。胜率是按卖出笔数算的（sandbox_runner
#     的 _trade_stats：买入没有盈亏），笔数少的时候胜率纯属噪声。
THRESHOLDS = {
    "min_sharpe": 0.5,
    "max_drawdown_pct": 25.0,
    "min_trades": 30,
    "min_trading_days": 60,
}

BLOCKER = "blocker"
PENALTY = "penalty"


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 判据
# ---------------------------------------------------------------------------

def judge(source: dict, run: dict) -> dict:
    """判一个策略跑完之后「能不能用」。

    source 侧来自 /api/sandbox/precheck 或 sa_strategy_source：
        {"title","syntax_state","syntax_ok","redacted","n_blocks","n_lines",
         "n_funcs","needs_all_market","universe_size","risks","looks_redacted"}
    run 侧来自沙箱：
        {"ok","error","metrics","trade_stats","warnings","n_rejected",
         "n_callback_errors","slice"}

    返回 {"verdict","flags":[{"level","code","msg"}],"score","metrics"}

    verdict 取值：
        "reject"  有硬否决 —— 结果不可信，不许进模拟盘
        "partial" 通过但有扣分项 —— 可以进模拟盘，提示里点名
        "pass"    通过

    刻意只有三档，没有「仅供参考」这种中间态：截面被截断、日内择时无法复现、
    源码是参数片段 —— 这几件事没有一件是「参考一下就行」的程度，它们的收益
    数字根本不是这个策略的收益。留一个中间档只会让人以为还能用。
    """
    flags: list[dict] = []

    def add(level, code, msg):
        flags.append({"level": level, "code": code, "msg": msg})

    # ---------------- 硬否决：源码本身就跑不出真实策略 ----------------
    if not source.get("syntax_ok"):
        add(BLOCKER, "syntax",
            "源码拼接后语法不正确（syntax_state=%s），沙箱跑的是残缺文件。"
            % (source.get("syntax_state") or "未知"))

    if source.get("redacted"):
        add(BLOCKER, "redacted",
            "作者用省略号把核心逻辑藏了（脱敏），跑出来的净值不代表真实策略。")

    # 没有 initialize 就没有策略。聚宽靠 initialize 注册回调，缺它等于空跑 ——
    # 而空跑**不会报错**，净值就是一条直线，极易被当成「稳健」误读。
    # 注意不能只看 n_funcs：辅助函数（cross_section_preprocess 之类）可以有
    # 十几个，但没有 initialize 就一个回调都注册不上。static_scan 已经给了
    # has_initialize，直接用它。
    if source.get("has_initialize") is False:
        nf = int(source.get("n_funcs") or 0)
        if nf == 0:
            add(BLOCKER, "no_initialize",
                "抽出来的源码里一个函数都没有：作者贴的是参数片段"
                "（如 g.xxx = ...），不是可运行策略。")
        else:
            add(BLOCKER, "no_initialize",
                "源码里有 %d 个函数但没有 initialize —— 聚宽靠 initialize "
                "注册回调，没有它就是空跑，净值是一条直线（且不报错）。"
                % nf)

    if not run.get("ok"):
        add(BLOCKER, "run_failed",
            "沙箱运行失败：%s" % ((run.get("error") or "")[:200] or "无错误信息"))

    # ok=True 但一条净值曲线都没产出：空跑。和缺 initialize 是同一类 ——
    # 代码没崩，所以不报错，但什么策略逻辑都没执行过。归硬否决而不是扣分：
    # 没有曲线就没有任何东西可评估，「不扣分放行」等于把空跑当成稳健。
    elif not (run.get("metrics") or {}).get("trading_days"):
        add(BLOCKER, "no_result",
            "沙箱跑完了但没有产出任何净值曲线（trading_days 为空）—— "
            "策略一次都没真正执行，等于空跑。")

    # ---------------- 硬否决：跑通了但数字不能代表策略 ----------------
    # 全市场策略被截断：截断发生在**策略自己的选股之前**，所以它的收益数字
    # 不是「样本小了点」，而是「换了个市场」。实测同一策略 400 只 +16.95%、
    # 800 只 +87.72%、1600 只 +128.57%。
    if source.get("needs_all_market") and source.get("universe_size"):
        add(BLOCKER, "universe_truncated",
            "需要全市场选股，但沙箱只给了 %d 只（容器内存上限）。截断发生在策略"
            "自己的选股之前，所以收益数字不代表真实表现 —— 实测同一策略 400 只"
            " +16.95%%、800 只 +87.72%%、1600 只 +128.57%%，截面一变结果就大变。"
            % int(source["universe_size"]))

    # 沙箱只有日线。任何按分钟择时的策略（run_daily(ctx, '14:50') 之类）
    # 在这里等于随机选时。
    for w in (run.get("warnings") or []):
        if "无法复现" in w or "日内" in w:
            add(BLOCKER, "intraday_unreproducible",
                "策略有日内择时，沙箱只有日线，该部分无法复现：%s" % w[:120])

    # ---------------- 扣分：能用，但要打折 ----------------
    met = run.get("metrics") or {}
    ts = run.get("trade_stats") or {}
    sharpe = _f(met.get("sharpe"))
    mdd = _f(met.get("max_drawdown"))
    ann = _f(met.get("annual_return"))
    # 样本量按**卖出**笔数算，不是成交笔数：胜率的分母是卖出（买入没有盈亏，
    # 见 sandbox_runner._trade_stats）。键名是 n_sell_trades —— 写成 n_sells
    # 会静默取不到、退回成交总数，于是 1778 笔（买入+卖出）被当成 1778 个样本，
    # 阈值 30 就永远不触发了。
    n_sell = ts.get("n_sell_trades")
    n_trades = int(n_sell if n_sell is not None else (ts.get("n_trades") or 0))

    if sharpe is not None and sharpe < THRESHOLDS["min_sharpe"]:
        add(PENALTY, "low_sharpe",
            "夏普 %.2f 偏低（门槛 %.1f），收益很可能只是拿了市场 beta。"
            % (sharpe, THRESHOLDS["min_sharpe"]))
    if mdd is not None and abs(mdd) * 100 > THRESHOLDS["max_drawdown_pct"]:
        add(PENALTY, "deep_drawdown",
            "最大回撤 %.1f%% 偏深（门槛 %.0f%%）。"
            % (abs(mdd) * 100, THRESHOLDS["max_drawdown_pct"]))
    if 0 < n_trades < THRESHOLDS["min_trades"]:
        add(PENALTY, "thin_sample",
            "只有 %d 笔卖出，样本不足以谈胜率（%.1f%%）。"
            % (n_trades, (_f(ts.get("win_rate")) or 0.0) * 100))
    if n_trades == 0 and met:
        add(PENALTY, "no_trades",
            "全程零卖出 —— 净值曲线是买入持有的直线，不是策略的结果。")

    days = int(met.get("trading_days") or 0)
    if days and days < THRESHOLDS["min_trading_days"]:
        add(PENALTY, "short_window",
            "只回测了 %d 个交易日，短于门槛 %d 天。"
            % (days, THRESHOLDS["min_trading_days"]))

    # 沙箱明确说了取不到的东西 —— 收益因此系统性偏高。
    for w in (run.get("warnings") or []):
        if "偏高" in w or "拿不到" in w:
            add(PENALTY, "data_gap",
                "沙箱数据有缺：%s" % w[:120])

    # 拒单多 = 流动性假设不成立。万得微盘股那轮 6185 次拒单 vs 1778 笔成交。
    rej = int(run.get("n_rejected") or 0)
    if rej > 0 and n_trades > 0 and rej > n_trades * 3:
        add(PENALTY, "many_rejects",
            "拒单 %d 次 vs 成交 %d 笔 —— 策略下单频率远超沙箱给的流动性，"
            "实盘会打滑，收益被高估。" % (rej, n_trades))

    # ---------------- 结论 + 打分 ----------------
    blockers = [f for f in flags if f["level"] == BLOCKER]
    penalties = [f for f in flags if f["level"] == PENALTY]

    if blockers:
        verdict = "reject"
    elif penalties:
        verdict = "partial"
    else:
        verdict = "pass"

    # portable_score 1~5（sa_strategy_def 的口径）。从 3 起步往下扣：
    # 有硬否决直接归 1（不可用），扣分项每条扣 1，下限 1。
    score = 3
    if blockers:
        score = 1
    else:
        score = max(1, 3 - len(penalties))

    return {
        "verdict": verdict,
        "score": score,
        "flags": flags,
        "blockers": [f["msg"] for f in blockers],
        "penalties": [f["msg"] for f in penalties],
        "metrics": met,
        "trade_stats": ts,
    }


# 能不能进模拟盘 / 日报。只 reject 挡掉。
def usable(j: dict) -> bool:
    return j.get("verdict") != "reject"


VERDICT_LABEL = {
    "reject": "不可用",
    "partial": "可用（有保留）",
    "pass": "可用",
}


def one_line(title: str, j: dict) -> str:
    """日报/汇报用的单行摘要。"""
    met = j.get("metrics") or {}
    ts = j.get("trade_stats") or {}
    lab = VERDICT_LABEL.get(j.get("verdict") or "", "?")
    bits = []

    def pct(key: str, as_magnitude: bool = False) -> str | None:
        """指标存的是**小数**（sandbox_runner 里 0.5166 就是 51.66%），要乘 100。
        as_magnitude 用于回撤：它存的是负数，而页面和日报都按幅度（正数）显示。
        取不到值返回 None —— 调用方负责跳过，不能拿 0 顶替（那是在编数字）。"""
        v = _f(met.get(key))
        if v is None:
            return None
        return "%.1f%%" % (abs(v) * 100 if as_magnitude else v * 100)

    for label, key, mag in (("总收益", "total_return", False),
                            ("年化", "annual_return", False),
                            ("回撤", "max_drawdown", True)):
        s = pct(key, mag)
        if s is not None:
            bits.append(label + " " + s)
    sh = _f(met.get("sharpe"))
    if sh is not None:
        bits.append("夏普 %.2f" % sh)
    if ts.get("n_sell_trades"):
        wr = _f(ts.get("win_rate")) or 0.0
        bits.append("%d 笔卖出/胜率 %.0f%%" % (int(ts["n_sell_trades"]), wr * 100))
    head = "%s —— %s｜可移植 %d/5" % ((title or "")[:40], lab, j.get("score") or 0)
    if bits:
        head += "｜" + "，".join(bits)
    probs = j.get("blockers") or j.get("penalties") or []
    if probs:
        head += "\n    ⚠ " + "\n    ⚠ ".join(probs)
    return head


# ---------------------------------------------------------------------------
# 从 sa_sandbox_run 的一行还原判定所需字段
# ---------------------------------------------------------------------------

# sa_sandbox_run 的 SELECT 列顺序。zip 出来的 dict 靠它对齐 —— 加列必须同步
# 改这里，否则判定会静默读错字段（比如把 created_at 当 error）。
_ROW_COLS = ("post_id", "title", "ok", "error", "total_return",
             "annual_return", "max_drawdown", "sharpe", "win_rate",
             "n_trades", "n_rejected", "warnings", "host", "created_at",
             "redacted", "syntax_state", "syntax_ok", "code")


def _result_json(result):
    """留档的 result 列可能是 str 也可能已经是 dict（psycopg2 的 jsonb
    适配器会先解一次，但走 _dict 路径时不会）。两种都要认。"""
    if isinstance(result, str):
        try:
            import json as _json
            return _json.loads(result) or {}
        except Exception:                             # noqa: BLE001
            return {}
    return result if isinstance(result, dict) else {}


def metrics_from_row(d: dict) -> dict:
    """还原 sandbox_runner 的 metrics 形状（**小数**，不是百分数）。

    两个坑：
    1. 量纲。落库时 total_return 等已乘过 100（app._pct_or_none），judge 假定
       的是小数。不除回去，「回撤 19.6」会被当成 1960%。
    2. 优先级。sa_sandbox_run 的**列**只在 save 成功时有值，而 result 里
       永远是完整的 —— 万得那轮 total_return 列是 NULL（那轮我 save=False
       之外的情况），只看列就会得出「空跑」的结论。所以先读列，缺了再从
       result 里挖。
    """
    met: dict = {}
    for k in ("total_return", "annual_return", "max_drawdown"):
        v = _f(d.get(k))
        if v is not None:
            met[k] = v / 100.0
    sh = _f(d.get("sharpe"))
    if sh is not None:
        met["sharpe"] = sh
    res = _result_json(d.get("result"))
    rmet = res.get("metrics") or {}
    if not met:
        # result 里也是小数（sandbox_runner 直接 return 的），不要再除。
        for k in ("total_return", "annual_return", "max_drawdown", "sharpe"):
            v = _f(rmet.get(k))
            if v is not None:
                met[k] = v
    if rmet.get("trading_days"):
        met["trading_days"] = rmet.get("trading_days")
    return met


def trade_stats_from_row(d: dict) -> dict:
    res = _result_json(d.get("result"))
    ts = res.get("trade_stats")
    if isinstance(ts, dict) and ts:
        return ts
    # result 里没有就退回列（列存的是百分数，win_rate 要还原成小数）。
    wr = _f(d.get("win_rate"))
    return {"n_trades": d.get("n_trades") or 0,
            "win_rate": (wr / 100.0) if wr is not None else None}


# ---------------------------------------------------------------------------
# 落库
# ---------------------------------------------------------------------------

def submit(get_conn, article_id: str, title: str, j: dict,
           run_id: int = None) -> int:
    """把判定结果写进 sa_strategy_def。已有行就更新，没有就建一行。

    为什么不只更新：jq_sandbox.save_run 只在**跑成功**时才登记（runnable='idea'），
    所以跑挂了的策略压根没有行 —— 而「为什么不能用它」恰恰是跑挂的那几个最需要
    留痕的。实测 4 个候选里 3 个跑挂，3 个都 submit 不到，结论无处可查。

    写什么：
      portable_score  1~5，judge() 算的
      enabled         只有 usable() 为真才开。enabled 是页面上的「已采纳」
      note            判定结论 + 所有 flag 的原因，换成人能读的一句话
      runnable        保持 'idea'：判定通过不代表它已经是本系统能执行的形态
                      （要 runnable='rules' 得先编译成 sa_strategies 的 6 种 kind）
    """
    reasons = j.get("blockers") or j.get("penalties") or []
    note = "%s｜可移植 %d/5%s" % (
        VERDICT_LABEL.get(j.get("verdict") or "", "?"),
        j.get("score") or 0,
        ("｜" + "；".join(reasons)) if reasons else "")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM sa_strategy_def WHERE article_id=%s "
                    "ORDER BY id LIMIT 1", (article_id,))
        row = cur.fetchone()
        if not row:
            cur.execute(
                """INSERT INTO sa_strategy_def
                   (name, article_id, runnable, code, params, universe,
                    portable_score, note, enabled)
                   VALUES (%s,%s,'idea','','{}'::jsonb,'[]'::jsonb,%s,%s,%s)
                   RETURNING id""",
                (title[:160], article_id, int(j.get("score") or 0),
                 note[:2000], usable(j)))
            return (cur.fetchone() or [0])[0]
        sid = row[0]
        cur.execute(
            """UPDATE sa_strategy_def
               SET portable_score=%s, enabled=%s, note=%s, updated_at=now()
               WHERE id=%s""",
            (int(j.get("score") or 0), usable(j), note[:2000], sid))
        return sid


# ---------------------------------------------------------------------------
# 汇报（日报回顾 / 页面）
# ---------------------------------------------------------------------------

def report_lines(get_conn, days: int = 14, limit: int = 8) -> list[str]:
    """最近验证过的聚宽策略，按判定结论给日报用的行。

    只报**跑过沙箱**的（sa_sandbox_run 有记录），不报光有源码的 —— 日报是给
    「今天该看什么」用的，堆一串没验证过的策略名没有信息量。

    读的是 sa_sandbox_run 而不是 sa_strategy_def：前者的 error/warnings/
    n_rejected 是判定依据（见 judge 的入参），后者只有我写回去的结论。
    """
    with get_conn() as conn, conn.cursor() as cur:
        # 前 18 列必须与 _ROW_COLS 同序；后两列（limits/result）不参与判定入参，
        # 但 metrics_from_row / trade_stats_from_row 要从 result 里挖
        # trading_days 和卖出笔数 —— 漏掉这两列的后果是「跑出了净值却被判成
        # 空跑」（万得那轮就是这样：save 没成功，指标全在 result 里）。
        cur.execute(
            """SELECT r.post_id, a.title, r.ok, r.error, r.total_return,
                      r.annual_return, r.max_drawdown, r.sharpe, r.win_rate,
                      r.n_trades, r.n_rejected, r.warnings, r.host,
                      r.created_at, s.redacted, s.syntax_state, s.syntax_ok,
                      s.code, r.limits, r.result
               FROM sa_sandbox_run r
               JOIN sa_strategy_article a ON a.post_id = r.post_id
               LEFT JOIN sa_strategy_source s ON s.post_id = r.post_id
               WHERE r.created_at > now() - make_interval(days => %s)
               ORDER BY r.post_id, r.created_at DESC""",
            (int(days),))
        rows = cur.fetchall()
    out = []
    seen = set()
    for r in rows:
        d = dict(zip(_ROW_COLS, r[:len(_ROW_COLS)]))
        d["limits"] = r[len(_ROW_COLS)]
        d["result"] = r[len(_ROW_COLS) + 1]
        pid = d["post_id"]
        if pid in seen:          # 同一策略跑多次只报最新那条（ORDER BY 已保证）
            continue
        seen.add(pid)
        if len(out) >= int(limit):
            break
        out.append(one_line(d["title"], judge_row(d)))
    return out


def judge_row(d: dict) -> dict:
    """一行 sa_sandbox_run + 源码特征 -> 判定结果。"""
    code = d.get("code") or ""
    scan = _static_scan(code)
    return judge(
        {"syntax_ok": d["syntax_ok"], "syntax_state": d["syntax_state"],
         "redacted": d["redacted"],
         "n_funcs": scan["n_funcs"], "has_initialize": scan["has_initialize"],
         "needs_all_market": "__ALL_MARKET__" in _referenced(code),
         "universe_size": (d.get("limits") or {}).get("universe")
         if isinstance(d.get("limits"), dict) else 0},
        {"ok": d["ok"],
         # error 列存的是「错误 + \n + traceback」。只取第一行 ——
         # 整段塞进日报会把几十行栈也带进去，那不是给人看的东西。
         "error": (d["error"] or "").split("\n")[0][:300],
         "metrics": metrics_from_row(d),
         "trade_stats": trade_stats_from_row(d),
         "warnings": d.get("warnings") or [],
         "n_rejected": d.get("n_rejected")})


def _static_scan(code: str) -> dict:
    try:
        import jq_sandbox as JS
        return JS.static_scan(code)
    except Exception:                                   # noqa: BLE001
        return {"n_funcs": 0, "has_initialize": False}


def _referenced(code: str) -> list:
    try:
        import jq_sandbox as JS
        return JS.referenced_codes(code)
    except Exception:                                   # noqa: BLE001
        return []