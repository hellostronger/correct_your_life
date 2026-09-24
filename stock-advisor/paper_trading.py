"""模拟交易（Paper Trading）核心：LLM 决策 → 收盘价成交 → 5 交易日结算 → 反思沉淀。

设计移植自 TradingAgents（TauricResearch，Apache-2.0）三样核心：
1. 结构化 trader 决策：分析师报告 + 交易员严格 JSON（action/confidence/止损/理由）
2. 反思循环：pending → 到期自动平仓 → Reflector 生成 2-4 句经验 → get_past_context
   反注入下一轮决策（point-in-time 纪律）
3. 成败按 alpha（相对基准超额）判定而非裸涨跌——牛市里闭眼买也算赢的假经验被剔除

免费数据源（零成本，平替 yfinance——A股被墙）：
- OHLCV/收盘价：东财 push2his 日K（f51-f57），兜底腾讯 web.ifzq fqkline
- 基准：sh→上证指数 / sz→深证成指 / hk→恒指，全走东财/腾讯日K

本模块不 import app（避免循环依赖）：依赖函数通过 deps dict 注入，
deps = {"get_conn", "em_kline_fn", "tx_symbol_fn", "quote_fn", "conf",
        "news_rows_fn", "events_fn", "notify_fn", "trading_days_fn"}
"""

import json
import re
import time
import traceback
from datetime import datetime, timedelta

import paper_memory

DEFAULTS = {
    "enabled": False,          # 总开关，false 时线程轮内直接跳过
    "initial_cash": 100000.0,  # 初始虚拟资金
    "holding_days": 5,         # 持有几个交易日后自动平仓结算
    "max_position_pct": 25,     # 单票市值上限（% of 总资产）
    "sleep_seconds": 3,         # 逐股决策间 sleep（防 LLM 网关限流）
    "n_same": 5, "n_cross": 3,  # 经验注入条数
    "keep_per_ticker": 30,      # 经验库每股保留条数
}

EM_FIELDS_OHLCV = "f51,f52,f53,f54,f55,f56,f57"   # 日期,开,收,高,低,量,额

ANALYST_PROMPT = """\
你是一支合并分析团队（技术面+消息面+基本面视角），为一支股票做简短投资分析。
用户会给你结构化上下文：实时行情、近60日K线摘要（涨跌幅/回撤/量能）、近期新闻
标题（已按利好/利空标记）、解禁/增发事件、大盘量能概况、历史决策复盘经验。

必须依次输出以下五节（缺一不可）：
①技术面：引用K线摘要的具体数字（如「现价距20日高点回撤8.2%」「近5日累计+3.1%」）
②消息面：引用新闻标题并区分利好/利空；无新闻就明说
③看多论点：3 条
④看空论点：3 条
⑤裁决：Buy / Overweight / Hold / Underweight / Sell 之一 + 一句话理由

规则：数字必须来自上下文，禁止编造；每条论点须指回具体证据。总长 300 字以内，中文。"""

TRADER_PROMPT = """\
你是交易员，把分析师报告转化为具体交易决策。你会得到：分析报告 + 账户现状
（现金、该股已有持仓与浮盈、单票仓位上限、总资产）。

只输出一个 JSON 代码块，不要其他文字：
```json
{"action":"buy|sell|hold","confidence":1-10,"target_value_pct":1-20,
 "stop_loss_pct":2-15,"reasoning":"2-4句操作理由"}
```
字段含义：
- action：buy=买入（用现金按建议仓位），sell=清仓该股已有持仓，hold=观望不动
- target_value_pct：本次买入动用资金占总资产百分比（1-20）
- stop_loss_pct：止损线距买入价的百分比（2-15）
- reasoning：必须引用报告和账户数据的具体数字说明为什么是这笔交易而不是相反

硬性纪律：现金不足时 action 必须是 hold；没有已有持仓时不能 sell；
无充分依据倾向 hold——模拟交易亏的是后续统计的胜率，乱动比不动差。"""

REFLECTOR_PROMPT = """\
你是交易复盘员。一笔模拟交易已到期结算，你会得到：当时的决策与理由、
分析师报告要点、入场价→结算价收益、同期基准收益、超额收益 alpha。

请写 2-4 句复盘经验，依次覆盖：
1. alpha 说明了什么（决策是否真的跑赢市场，还是只是随大盘涨跌）
2. 结果支持或削弱了当时论点的哪一部分（引用 reasoning 的具体内容）
3. 一条对下次同类分析的具体、可操作的教训

重要：持有窗口只有几个交易日，可能短于当时论点的兑现周期——若结果无法
评判，就直说「窗口太短无法判断」，不要硬编结论。只输出经验文本本身，
中文，不要客套话。"""


# ---------------- 免费数据封装 ----------------

def benchmark_symbol(code: str) -> str:
    """TradingAgents benchmark_map 的本地版：A股按交易所取上证/深成指，港股取恒指。
    返回腾讯符号（供东财 _em_secid / 腾讯 fqkline 共用）。
    港股代码：5 位且首位为 0（00700/02513），或 4 位（0700）——与 A 股 6 位区分。"""
    if len(code) == 5 and code.startswith("0"):
        return "hkHSI"
    if code.startswith("hk"):
        return "hkHSI"
    if code.startswith(("0", "3")) and len(code) == 6:   # 深市 000/002/300
        return "sz399001"
    return "sh000001"


def fetch_kline_ohlcv(em_kline_fn, symbol: str, days: int = 60) -> list[dict]:
    """近 N 个交易日 OHLCV（东财 f51-f57，升序）。供决策上下文摘要。

    em_kline_fn = app._em_kline_fields(symbol, days, fields)。
    返回 [{date, open, close, high, low, vol, amount}]。
    """
    rows = em_kline_fn(symbol, days, EM_FIELDS_OHLCV)
    out = []
    for k in rows:
        p = k.split(",")
        if len(p) < 7:
            continue
        try:
            out.append({"date": p[0][:10], "open": float(p[1]), "close": float(p[2]),
                        "high": float(p[3]), "low": float(p[4]),
                        "vol": float(p[5]), "amount": float(p[6])})
        except ValueError:
            continue
    return out


def _tencent_closes(symbol: str, days: int, quote_fn) -> dict:
    """兜底源：腾讯日K收盘价 {date: close}。

    主源 proxy.finance.qq.com（2026-09-24 实测可用）；web.ifzq.gtimg.cn 被
    WAF 拦（501）作为第二备。东财 push2his 偶发断连，所以这里兜底链要可靠。
    """
    for host in ("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get",
                 "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"):
        try:
            import requests
            resp = requests.get(host, params={"param": f"{symbol},day,,,{days},qfq"},
                                timeout=15,
                                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            resp.raise_for_status()
            bars = ((resp.json().get("data") or {}).get(symbol) or {})
            bars = bars.get("qfqday") or bars.get("day") or []
            out = {}
            for b in bars:
                if len(b) > 2:
                    try:
                        out[str(b[0])[:10]] = float(b[2])
                    except (ValueError, TypeError):
                        pass
            if out:
                return out
        except Exception:
            continue
    return {}


def fetch_close_series(em_kline_fn, symbol: str, days: int = 30) -> dict:
    """近 N 个交易日收盘价序列 {date: close}。

    双源兜底：东财 push2his（偶发断连）→ 腾讯 proxy.finance.qq.com。
    都失败返回 {}（调用方留 pending 重试）。
    """
    rows = fetch_kline_ohlcv(em_kline_fn, symbol, days)
    if rows:
        return {r["date"]: r["close"] for r in rows}
    return _tencent_closes(symbol, days, None)


def fetch_ohlcv(em_kline_fn, symbol: str, days: int = 60) -> list[dict]:
    """近 N 个交易日完整 OHLCV。东财挂时兜底腾讯 fqkline（同样有 OHLCV）。

    腾讯 bar 格式：[date, open, close, high, low, volume, ...]（注意 close 在第 3 位）。
    """
    rows = fetch_kline_ohlcv(em_kline_fn, symbol, days)
    if rows:
        return rows
    try:
        import requests
        resp = requests.get(
            "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get",
            params={"param": f"{symbol},day,,,{days},qfq"}, timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        resp.raise_for_status()
        bars = ((resp.json().get("data") or {}).get(symbol) or {})
        bars = bars.get("qfqday") or bars.get("day") or []
        out = []
        for b in bars:
            if len(b) >= 6:
                try:
                    out.append({"date": str(b[0])[:10], "open": float(b[1]),
                                "close": float(b[2]), "high": float(b[3]),
                                "low": float(b[4]), "vol": float(b[5]), "amount": 0.0})
                except (ValueError, TypeError):
                    continue
        return out
    except Exception:
        return []


def _kline_summary(bars: list[dict]) -> str:
    """近60日K线摘要文本：区间涨跌幅、20日高点回撤、量能倾向。"""
    if len(bars) < 10:
        return "（K线数据不足）"
    closes = [b["close"] for b in bars]
    last = closes[-1]
    def pct(n):
        return f"{(last / closes[-n - 1] - 1) * 100:+.1f}%" if len(closes) > n else "—"
    high20 = max(b["high"] for b in bars[-20:])
    dd = (last / high20 - 1) * 100
    vols = [b["vol"] for b in bars if b["vol"]]
    vol_note = "—"
    if len(vols) >= 6 and vols[-1]:
        avg5 = sum(vols[-6:-1]) / 5
        vol_note = ("放量" if vols[-1] > avg5 * 1.3
                    else "缩量" if vols[-1] < avg5 * 0.7 else "平量")
        vol_note += f"（今日量为5日均量的 {vols[-1] / avg5:.1f} 倍）"
    lo60 = min(b["low"] for b in bars)
    return (f"最新收盘 {last:g}；近5日 {pct(5)}、近10日 {pct(10)}、近20日 {pct(20)}；"
            f"距近20日最高 {high20:g} 回撤 {dd:.1f}%；近60日最低 {lo60:g}；量能：{vol_note}")


# ---------------- LLM 调用（照抄 llm_advisor 两段式） ----------------

def _llm_call(system_prompt: str, user_text: str, max_tokens: int = 1500) -> str:
    """两段式调用：base_url 非空（代理网关）直接 basic；官方端点先试 full。
    与 llm_advisor.ask_advice 同款降级与 refusal 检查。失败抛 RuntimeError。"""
    import llm_advisor
    conf = llm_advisor.load_llm_conf()
    if not conf["api_key"]:
        raise RuntimeError("未配置 LLM api_key（config.yaml llm 段或环境变量）")
    from anthropic import Anthropic
    kwargs = {"api_key": conf["api_key"], "timeout": 120.0, "max_retries": 2}
    if conf.get("base_url"):
        kwargs["base_url"] = conf["base_url"]
    client = Anthropic(**kwargs)
    messages = [{"role": "user", "content": user_text}]
    msg, errors = None, []
    attempts = []
    if not conf.get("base_url"):
        attempts.append(("full", dict(thinking={"type": "adaptive"},
                                       betas=["server-side-fallback-2026-07-01"])))
    attempts.append(("basic", None))
    for label, extra in attempts:
        try:
            if label == "full":
                msg = client.beta.messages.create(model=conf["model"], max_tokens=max_tokens,
                                                   system=system_prompt, messages=messages,
                                                   **extra)
            else:
                msg = client.messages.create(model=conf["model"], max_tokens=max_tokens,
                                              system=system_prompt, messages=messages)
            break
        except Exception as exc:
            errors.append(f"{label}: {type(exc).__name__}: {exc}")
    if msg is None:
        raise RuntimeError(f"LLM 调用失败: {' | '.join(errors)}")
    if msg.stop_reason == "refusal":
        raise RuntimeError("请求被安全策略拒绝（stop_reason=refusal）")
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    if not text:
        raise RuntimeError(f"LLM 返回空内容（stop_reason={msg.stop_reason}）")
    return text


def _extract_json(text: str) -> dict | None:
    """三级容错解析交易员 JSON：剥 ```json 围栏 → 首尾大括号截取 → 放弃。"""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if not m:
        s, e = text.find("{"), text.rfind("}")
        if s >= 0 and e > s:
            m = type("M", (), {"group": staticmethod(lambda g, _s=s, _e=e: text[_s:_e + 1])})()
        else:
            return None
    try:
        d = json.loads(m.group(1))
        return d if isinstance(d, dict) else None
    except json.JSONDecodeError:
        try:  # 单引号修复
            fixed = m.group(1).replace("'", '"')
            d = json.loads(fixed)
            return d if isinstance(d, dict) else None
        except json.JSONDecodeError:
            return None


def _clip(v, lo, hi, default):
    try:
        return max(lo, min(float(v), hi))
    except (TypeError, ValueError):
        return default


def _validate_decision(d: dict) -> dict | None:
    """交易员 JSON 二次校验：enum/数值范围 clip，不合格返回 None（降级 hold）。"""
    action = str(d.get("action", "")).lower().strip()
    if action not in ("buy", "sell", "hold"):
        return None
    return {
        "action": action,
        "confidence": int(_clip(d.get("confidence"), 1, 10, 5)),
        "target_value_pct": _clip(d.get("target_value_pct"), 1, 20, 5),
        "stop_loss_pct": _clip(d.get("stop_loss_pct"), 2, 15, 8),
        "reasoning": str(d.get("reasoning") or "")[:2000] or "（未给出理由）",
    }


# ---------------- 账户与持仓（流水推导，不建持仓表） ----------------

def _account_row(cur) -> dict | None:
    cur.execute("SELECT initial_cash, cash, total_value FROM sa_paper_account WHERE id = 1")
    r = cur.fetchone()
    if not r:
        return None
    if isinstance(r, dict):    # RealDictCursor
        vals = (r["initial_cash"], r["cash"], r["total_value"])
    else:
        vals = tuple(r)
    return {"initial_cash": float(vals[0]), "cash": float(vals[1]),
            "total_value": float(vals[2]) if vals[2] is not None else float(vals[0])}


def _derive_paper_positions(cur) -> dict[str, dict]:
    """从 sa_paper_trades buy/sell 流水推导持仓 {code: {shares, cost, name}}。

    与真实持仓 sa_trades→_derive_holdings 同思路：buy 累加加权成本，
    sell 按移动平均成本核减。auto_close 的卖出行同样参与推导。
    """
    cur.execute(
        "SELECT code, name, side, shares, price FROM sa_paper_trades "
        "WHERE side IN ('buy','sell') AND status <> 'skipped' "
        "ORDER BY id")
    pos: dict[str, dict] = {}
    for row in cur.fetchall():
        if isinstance(row, dict):
            code, name, side = row["code"], row["name"], row["side"]
            shares, price = row["shares"], row["price"]
        else:
            code, name, side, shares, price = row
        shares, price = int(shares or 0), float(price or 0)
        if shares <= 0 or price <= 0:
            continue
        p = pos.setdefault(code, {"shares": 0, "cost": 0.0, "name": name})
        if side == "buy":
            p["cost"] = (p["cost"] * p["shares"] + price * shares) / (p["shares"] + shares)
            p["shares"] += shares
        else:
            sell = min(shares, p["shares"])
            p["shares"] -= sell
            if p["shares"] <= 0:
                p["shares"], p["cost"] = 0, 0.0
    return {c: p for c, p in pos.items() if p["shares"] > 0}


# ---------------- 决策流水线 ----------------

def _build_context(stock: dict, quote: dict, bars: list[dict],
                   news: list[dict], events: list[dict],
                   market_note: str, account: dict, positions: dict,
                   past_context: str, conf: dict) -> str:
    ctx = [
        f"【股票】{stock['name']}（{stock['code']}）",
        f"【实时行情】现价 {quote.get('price') or '—'}，"
        f"今日 {(quote.get('change_pct') if quote.get('change_pct') is not None else '—')}%，"
        f"总市值 {quote.get('market_cap') or '—'}亿",
        f"【近60日K线摘要】{_kline_summary(bars)}",
    ]
    if market_note:
        ctx.append(f"【大盘量能概况】{market_note}")
    if news:
        ctx.append("【近7日新闻（已标记利好/利空）】")
        for n in news[:10]:
            tag = {"pos": "利好", "neg": "利空"}.get(n.get("sentiment") or "", "中性")
            ctx.append(f"- [{tag}] {n['title']}（{n.get('media') or n.get('source') or '媒体'}）")
    else:
        ctx.append("【近7日新闻】无相关新闻")
    if events:
        ctx.append("【未来14日解禁/增发事件】")
        for e in events:
            ctx.append(f"- {e}")
    me = positions.get(stock["code"])
    ctx.append(f"【模拟账户现状】可用现金 {account['cash']:,.0f} 元，总资产 "
                f"{account['total_value']:,.0f} 元；"
                + (f"已持有该股 {me['shares']} 股，成本 {me['cost']:g}"
                   if me else "该股无持仓")
                + f"；单票仓位上限 {conf['max_position_pct']}%")
    ctx.append(f"【历史决策复盘经验】\n{past_context}")
    return "\n".join(ctx)


def run_decisions(deps: dict) -> dict:
    """盘后为每只自选股生成买卖决策并按当日收盘价成交。

    幂等：同日同股已有决策（trade_date+code+side 唯一）则跳过该股。
    逐股 try/except 隔离，一股失败不影响其余（仿新闻抓取循环）。
    返回 {date, decided: [{code, name, action, executed, reasoning}...]}。
    """
    get_conn = deps["get_conn"]
    conf = {**DEFAULTS, **(deps.get("conf") or {})}
    today = datetime.now().strftime("%Y-%m-%d")
    results = []
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=_real_dict_cursor(deps))
        cur.execute("SELECT code, name FROM sa_watchlist ORDER BY code")
        stocks = [dict(r) for r in cur.fetchall()]
        account = _account_row(cur)
        if account is None:  # 首次运行：初始化账户
            cur.execute(
                "INSERT INTO sa_paper_account (id, initial_cash, cash, total_value) "
                "VALUES (1, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (conf["initial_cash"],) * 3)
            conn.commit()
            account = {"initial_cash": conf["initial_cash"],
                       "cash": conf["initial_cash"], "total_value": conf["initial_cash"]}
        positions = _derive_paper_positions(cur)
        # 幂等守卫：今日已决策过的股票集合（任何 side 都算）
        cur.execute("SELECT DISTINCT code FROM sa_paper_trades WHERE trade_date = %s",
                    (today,))
        done = {r["code"] for r in cur.fetchall()}
    for stock in stocks:
        code = stock["code"]
        if code in done:
            continue
        try:
            _decide_one(deps, conf, stock, today, results)
            done.add(code)   # 股间也不重入（并发手动触发/线程双写防护）
        except Exception as exc:
            # UniqueViolation = 已有同日决策行（线程与手动触发并发），静默跳过
            if "sa_paper_trades_trade_date_code_side_key" in str(exc):
                print(f"[paper] {code} 已有今日决策，跳过", flush=True)
            else:
                traceback.print_exc()
                print(f"[paper] decide {code} failed: {exc}", flush=True)
        time.sleep(float(conf.get("sleep_seconds", 3)))
    return {"date": today, "decided": results}


def _real_dict_cursor(deps):
    """deps 注入 psycopg2.extras.RealDictCursor（模块不 import psycopg2 也可，直接 import）。"""
    import psycopg2.extras
    return psycopg2.extras.RealDictCursor


def _decide_one(deps, conf, stock, today, results):
    get_conn, em_kline_fn = deps["get_conn"], deps["em_kline_fn"]
    quote_fn, tx_symbol_fn = deps["quote_fn"], deps["tx_symbol_fn"]
    news_fn, events_fn = deps.get("news_fn"), deps.get("events_fn")
    market_fn = deps.get("market_fn")
    code, name = stock["code"], stock["name"]
    symbol = tx_symbol_fn(code)
    # 1. 免费数据：行情 + K线 + 新闻 + 事件
    quotes = quote_fn([code])
    quote = quotes.get(code) or {}
    if not isinstance(quote.get("price"), (int, float)) or quote["price"] <= 0:
        print(f"[paper] {code} 行情不可得，跳过今日决策", flush=True)
        return
    bars = fetch_ohlcv(em_kline_fn, symbol, 60)
    if len(bars) < 10:
        print(f"[paper] {code} K线不足，跳过今日决策", flush=True)
        return
    news = news_fn(code, limit=10) if news_fn else []
    events = events_fn(code, days=14) if events_fn else []
    market_note = ""
    if market_fn:
        mv = market_fn()
        market_note = (f"大盘均量比 {mv.get('overall_ratio')}，{mv.get('overall_label')}"
                       if mv.get("overall_ratio") is not None else "")
    # 2. 上下文组装（经验 point-in-time 注入）
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=_real_dict_cursor(deps))
        account = _account_row(cur) or {"cash": 0, "total_value": 0, "initial_cash": 0}
        positions = _derive_paper_positions(cur)
        past = paper_memory.get_past_context(
            cur, code, n_same=int(conf.get("n_same", 5)),
            n_cross=int(conf.get("n_cross", 3)), as_of=today)
    ctx = _build_context(stock, quote, bars, news, events, market_note,
                         account, positions, past, conf)
    # 3. LLM 调用 1：分析师
    report = _llm_call(ANALYST_PROMPT, ctx + f"\n\n数据时点 {today}。请给出分析报告。")
    # 4. LLM 调用 2：交易员（结构化）
    raw_trader = _llm_call(
        TRADER_PROMPT,
        f"【分析报告】\n{report}\n\n【账户现状】\n可用现金 {account['cash']:,.0f} 元，"
        f"总资产 {account['total_value']:,.0f} 元，"
        f"已持有该股 {positions[code]['shares']} 股（成本 {positions[code]['cost']:g}）"
        if code in positions else
        f"【分析报告】\n{report}\n\n【账户现状】\n可用现金 {account['cash']:,.0f} 元，"
        f"总资产 {account['total_value']:,.0f} 元，该股无持仓")
    decision = _validate_decision(_extract_json(raw_trader) or {})
    if decision is None:
        decision = {"action": "hold", "confidence": 1, "target_value_pct": 0,
                    "stop_loss_pct": 8, "reasoning": "（交易员输出解析失败，保守观望）"}
        decision["_parse_failed"] = True
    decision["report"] = report
    decision["raw"] = raw_trader
    # 5. 成交执行（资金硬约束代码强制）
    executed, note = _execute_decision(deps, conf, stock, today, decision, quote)
    results.append({"code": code, "name": name, "action": decision["action"],
                     "executed": executed, "reasoning": decision["reasoning"],
                     "note": note})


def _execute_decision(deps, conf, stock, today, decision, quote) -> tuple[bool, str]:
    """按当日收盘价成交。返回 (是否成交, 备注)。资金约束全部代码强制。"""
    get_conn = deps["get_conn"]
    code = stock["code"]
    price = float(quote["price"])
    action = decision["action"]
    with get_conn() as conn:
        cur = conn.cursor()
        account = _account_row(cur)
        positions = _derive_paper_positions(cur)
        me = positions.get(code)
        if action == "buy":
            budget = account["total_value"] * float(decision["target_value_pct"]) / 100
            budget = min(budget, account["cash"])
            max_pos_value = account["total_value"] * float(conf["max_position_pct"]) / 100
            if me:
                budget = min(budget, max(0, max_pos_value - me["shares"] * me["cost"]))
            shares = int(budget / price) // 100 * 100   # A股整手
            if shares < 100:
                cur.execute(
                    _insert_trade_sql(),
                    (code, stock["name"], today, "buy", 0, price, 0,
                     decision["confidence"], decision["stop_loss_pct"],
                     decision["reasoning"], decision.get("report", ""),
                     json.dumps({"decision": decision, "raw": decision.get("raw", "")},
                                ensure_ascii=False), "skipped"))
                conn.commit()
                return False, "现金或仓位上限不足，未成交（记为 skipped）"
            value = shares * price
            cur.execute(_insert_trade_sql(),
                        (code, stock["name"], today, "buy", shares, price, value,
                         decision["confidence"], decision["stop_loss_pct"],
                         decision["reasoning"], decision.get("report", ""),
                         json.dumps({"decision": decision}, ensure_ascii=False), "open"))
            cur.execute("UPDATE sa_paper_account SET cash = cash - %s, updated_at = now() "
                        "WHERE id = 1", (value,))
            conn.commit()
            return True, f"买入 {shares} 股 × {price:g}"
        if action == "sell":
            if not me:
                cur.execute(
                    _insert_trade_sql(),
                    (code, stock["name"], today, "sell", 0, price, 0,
                     decision["confidence"], None, decision["reasoning"],
                     decision.get("report", ""),
                     json.dumps({"decision": decision}, ensure_ascii=False), "skipped"))
                conn.commit()
                return False, "无持仓可卖（记为 skipped）"
            shares = me["shares"]
            value = shares * price
            cur.execute(_insert_trade_sql(),
                        (code, stock["name"], today, "sell", shares, price, value,
                         decision["confidence"], None, decision["reasoning"],
                         decision.get("report", ""),
                         json.dumps({"decision": decision}, ensure_ascii=False), "open"))
            cur.execute("UPDATE sa_paper_account SET cash = cash + %s, updated_at = now() "
                        "WHERE id = 1", (value,))
            conn.commit()
            return True, f"卖出 {shares} 股 × {price:g}"
        # hold：记录决策理由（可解析性），不占资金
        cur.execute(_insert_trade_sql(),
                    (code, stock["name"], today, "hold", 0, None, None,
                     decision["confidence"], None, decision["reasoning"],
                     decision.get("report", ""),
                     json.dumps({"decision": decision}, ensure_ascii=False), "open"))
        conn.commit()
        return False, "观望"


def _insert_trade_sql() -> str:
    return ("INSERT INTO sa_paper_trades "
            "(code, name, trade_date, side, shares, price, value, confidence, "
            " stop_loss_pct, reasoning, report, decision_raw, status) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)")


# ---------------- 结算与反思 ----------------

def settle_and_reflect(deps: dict) -> dict:
    """结算到期 open 交易（持有满 holding_days 个交易日自动平仓）+ LLM 复盘 +
    经验入库 + 当日资产快照。价格取不到的交易保持 open 下轮重试。"""
    get_conn, em_kline_fn = deps["get_conn"], deps["em_kline_fn"]
    tx_symbol_fn, quote_fn = deps["tx_symbol_fn"], deps["quote_fn"]
    trading_days_fn = deps.get("trading_days_fn")
    notify_fn = deps.get("notify_fn")
    conf = {**DEFAULTS, **(deps.get("conf") or {})}
    hold_days = int(conf["holding_days"])
    today = datetime.now().strftime("%Y-%m-%d")
    settled, pending = [], []
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=_real_dict_cursor(deps))
        cur.execute(
            "SELECT * FROM sa_paper_trades WHERE status = 'open' "
            "AND side IN ('buy','sell') AND trade_date < %s ORDER BY id", (today,))
        opens = [dict(r) for r in cur.fetchall()]
    for t in opens:
        try:
            code = t["code"]
            held = trading_days_fn(str(t["trade_date"])[:10], datetime.now()) \
                if trading_days_fn else 0
            if held < hold_days:
                continue
            symbol = tx_symbol_fn(code)
            closes = fetch_close_series(em_kline_fn, symbol, days=hold_days + 30)
            dates = sorted(closes)
            entry_dates = [d for d in dates if d >= str(t["trade_date"])[:10]]
            if len(entry_dates) < hold_days + 1:
                pending.append({"code": code, "reason": "收盘价序列不足，下轮重试"})
                continue
            settle_date = entry_dates[hold_days]
            settle_price = closes[settle_date]
            entry_price = float(t["price"])
            raw_ret = settle_price / entry_price - 1
            bench_sym = benchmark_symbol(code)
            bench_closes = fetch_close_series(em_kline_fn, bench_sym, days=hold_days + 30)
            b_dates = sorted(d for d in bench_closes
                             if str(t["trade_date"])[:10] <= d <= settle_date)
            # 买卖方向对齐：sell 的 alpha 相对「不卖继续持有」
            if t["side"] == "buy":
                bench_ret = (bench_closes[b_dates[-1]] / bench_closes[b_dates[0]] - 1) \
                    if len(b_dates) >= 2 else None
                alpha = raw_ret - bench_ret if bench_ret is not None else None
            else:
                bench_ret = None
                alpha = -raw_ret  # 卖出后下跌=卖对了
            _close_trade(deps, t, settle_date, settle_price, raw_ret, alpha,
                         bench_sym, hold_days)
            settled.append({"id": t["id"], "code": code, "side": t["side"],
                            "trade_date": str(t["trade_date"])[:10],
                            "entry_price": entry_price,
                            "reasoning": t.get("reasoning") or "",
                            "report": (t.get("report") or "")[:600],
                            "settle_date": settle_date,
                            "raw_return": raw_ret, "alpha": alpha})
        except Exception as exc:
            traceback.print_exc()
            print(f"[paper] settle {t.get('code')} failed: {exc}", flush=True)
            pending.append({"code": t.get("code"), "reason": f"结算异常: {exc}"})
    # 反思逐笔进行（LLM 串行，放循环外逐条调）
    for s in settled:
        try:
            _reflect_one(deps, conf, s)
        except Exception as exc:
            print(f"[paper] reflect {s['code']} failed: {exc}", flush=True)
    # 经验蒸馏（P7）+ 膨胀控制 + 资产快照
    with get_conn() as conn:
        cur = conn.cursor()
        try:
            paper_memory.maybe_distill(
                cur, _llm_call, today, every=int(conf.get("distill_every", 20)))
        except Exception as exc:
            print(f"[paper] distill failed: {exc}", flush=True)
        try:
            paper_memory.prune(cur, int(conf.get("keep_per_ticker", 30)))
        except Exception as exc:
            print(f"[paper] prune failed: {exc}", flush=True)
        conn.commit()
    snap = _snapshot_equity(deps)
    if notify_fn and settled:
        lines = "\n".join(
            f"{s['side']} {s['code']}：收益 {s['raw_return'] * 100:+.1f}%"
            + (f"，alpha {(s['alpha']) * 100:+.1f}%" if s.get("alpha") is not None else "")
            for s in settled)
        try:
            notify_fn("🧪 模拟交易结算", f"今日结算 {len(settled)} 笔：\n{lines}")
        except Exception:
            pass
    return {"date": today, "settled": settled, "pending": pending, "equity": snap}


def _close_trade(deps, t, settle_date, settle_price, raw_ret, alpha, bench_sym, hold_days):
    """平仓落库：写反向成交行 + 回填结算字段 + 现金调整。"""
    get_conn = deps["get_conn"]
    code, side = t["code"], t["side"]
    shares = int(t["shares"] or 0)
    value = shares * settle_price if shares else 0
    with get_conn() as conn:
        cur = conn.cursor()
        # 反向成交行（auto_close 标记到期强平；status='resolved' 不再进入结算队列）
        cur.execute(
            "INSERT INTO sa_paper_trades "
            "(code, name, trade_date, side, shares, price, value, confidence, "
            " stop_loss_pct, reasoning, report, decision_raw, status, auto_closed) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,NULL,NULL,%s,'',%s,'resolved',TRUE)",
            (code, t["name"], settle_date, "sell" if side == "buy" else "buy",
             shares, settle_price, value, "到期自动平仓",
             json.dumps({"auto_close_of": t["id"]}, ensure_ascii=False)))
        cur.execute(
            "UPDATE sa_paper_trades SET status = 'resolved', settle_date = %s, "
            "settle_price = %s, raw_return = %s, alpha_return = %s, benchmark = %s "
            "WHERE id = %s",
            (settle_date, settle_price, raw_ret, alpha, bench_sym, t["id"]))
        if side == "buy":       # 买入到期平仓：现金回流
            cur.execute("UPDATE sa_paper_account SET cash = cash + %s WHERE id = 1",
                        (value,))
        else:                   # 卖出到期回补：现金扣回（恢复持仓成本）
            cur.execute("UPDATE sa_paper_account SET cash = cash - %s WHERE id = 1",
                        (value,))
        conn.commit()


def _reflect_one(deps, conf, s):
    """单笔复盘：Reflector 生成 2-4 句经验并入库。

    s 直接携带原始决策行（id/trade_date/entry_price/reasoning/report），不再回查——
    同股多笔并发结算时按 id 回查会拿错行。
    """
    get_conn = deps["get_conn"]
    trade_id = s["id"]
    digest = (f"{s['side']} {s['code']} @ {s['entry_price']:g}："
              f"{(s['reasoning'] or '')[:120]}")
    bench_ret = (s["raw_return"] - (s["alpha"] or 0)) if s.get("alpha") is not None else None
    user = (
        f"【当时决策】{digest}\n"
        f"【分析报告要点】{(s.get('report') or '')[:600]}\n"
        f"【结果】{s['trade_date']} 入场 → {s['settle_date']} 结算，区间收益 "
        f"{s['raw_return'] * 100:+.1f}%"
        + (f"，同期基准收益 {bench_ret * 100:+.1f}%，超额 alpha {(s['alpha']) * 100:+.1f}%"
           if bench_ret is not None else "（基准数据缺失，alpha 无法计算）")
        + f"\n【持有期】{conf['holding_days']} 个交易日\n请写复盘经验。")
    lesson = _llm_call(REFLECTOR_PROMPT, user, max_tokens=600)
    with get_conn() as conn:
        cur = conn.cursor()
        paper_memory.store_lesson(
            cur, trade_id=trade_id, code=s["code"], action=s["side"],
            decision_digest=digest, raw_return=s["raw_return"],
            alpha_return=s["alpha"] if s.get("alpha") is not None else 0.0,
            holding_days=int(conf["holding_days"]), benchmark="",
            lesson_text=lesson[:2000], resolved_at=s["settle_date"])
        conn.commit()


def _snapshot_equity(deps) -> dict:
    """写当日资产快照（现金 + 持仓按最新收盘价估值），返回快照 dict。"""
    get_conn, em_kline_fn = deps["get_conn"], deps["em_kline_fn"]
    tx_symbol_fn = deps["tx_symbol_fn"]
    today = datetime.now().strftime("%Y-%m-%d")
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=_real_dict_cursor(deps))
        account = _account_row(cur)
        if account is None:
            return {}
        positions = _derive_paper_positions(cur)
    market_value = 0.0
    for code, p in positions.items():
        closes = fetch_close_series(em_kline_fn, tx_symbol_fn(code), days=10)
        if closes:
            market_value += p["shares"] * closes[max(closes)]
    total = account["cash"] + market_value
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO sa_paper_equity (snap_date, cash, market_value, total, daily_return) "
            "SELECT %s, %s, %s, %s, "
            "(%s - COALESCE((SELECT total FROM sa_paper_equity WHERE snap_date < %s "
            " ORDER BY snap_date DESC LIMIT 1), %s)) / "
            "NULLIF(COALESCE((SELECT total FROM sa_paper_equity WHERE snap_date < %s "
            " ORDER BY snap_date DESC LIMIT 1), %s), 0) "
            "ON CONFLICT (snap_date) DO UPDATE SET "
            "cash = EXCLUDED.cash, market_value = EXCLUDED.market_value, "
            "total = EXCLUDED.total, daily_return = EXCLUDED.daily_return",
            (today, account["cash"], market_value, total,
             total, today, account["initial_cash"], today, account["initial_cash"]))
        cur.execute("UPDATE sa_paper_account SET total_value = %s, updated_at = now() "
                    "WHERE id = 1", (total,))
        conn.commit()
    return {"date": today, "cash": account["cash"], "market_value": market_value,
            "total": total}


# ---------------- 总览 ----------------

def account_overview(deps: dict) -> dict:
    """前端总览：账户 + 持仓（按最新收盘估值）+ 近30日快照 + 胜率统计。"""
    get_conn, em_kline_fn = deps["get_conn"], deps["em_kline_fn"]
    tx_symbol_fn, quote_fn = deps["tx_symbol_fn"], deps["quote_fn"]
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=_real_dict_cursor(deps))
        account = _account_row(cur)
        positions = _derive_paper_positions(cur)
        cur.execute("SELECT snap_date, cash, market_value, total, daily_return "
                    "FROM sa_paper_equity ORDER BY snap_date DESC LIMIT 30")
        equity = [dict(r) for r in cur.fetchall()]
        # 统计：只算有 alpha 的 resolved 交易；hold 不计
        cur.execute(
            "SELECT side, COUNT(*) AS n, AVG(raw_return) AS avg_raw, "
            "AVG(alpha_return) AS avg_alpha, "
            "SUM(CASE WHEN alpha_return > 0 THEN 1 ELSE 0 END) AS wins "
            "FROM sa_paper_trades WHERE status = 'resolved' AND raw_return IS NOT NULL "
            "AND side IN ('buy','sell') GROUP BY side")
        stats = {"buy": {}, "sell": {}}
        for r in cur.fetchall():
            stats[r["side"]] = {k: (float(v) if v is not None else None)
                                for k, v in r.items() if k != "side"}
    quotes = quote_fn(list(positions.keys())) if positions else {}
    pos_out = []
    market_value = 0.0
    for code, p in positions.items():
        q = quotes.get(code) or {}
        price = q.get("price")
        if not isinstance(price, (int, float)) or price <= 0:
            closes = fetch_close_series(em_kline_fn, tx_symbol_fn(code), days=5)
            price = closes[max(closes)] if closes else None
        mv = p["shares"] * price if price else 0
        market_value += mv
        pos_out.append({"code": code, "name": p["name"], "shares": p["shares"],
                        "avg_cost": round(p["cost"], 4), "price": price,
                        "market_value": round(mv, 2),
                        "pnl_pct": round((price / p["cost"] - 1) * 100, 2)
                        if price and p["cost"] else None})
    total = (account["cash"] + market_value) if account else 0
    return {"account": account, "positions": pos_out,
            "market_value": round(market_value, 2), "total": round(total, 2),
            "equity": equity, "stats": stats,
            "total_pnl_pct": round((total / account["initial_cash"] - 1) * 100, 2)
            if account and account.get("initial_cash") else None}


def reset_account(deps: dict, initial_cash: float) -> dict:
    """清空三张业务表并重置账户（前端确认后调用）。"""
    get_conn = deps["get_conn"]
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM sa_paper_reflections")
        cur.execute("DELETE FROM sa_paper_equity")
        cur.execute("DELETE FROM sa_paper_trades")
        cur.execute("DELETE FROM sa_paper_account")
        cur.execute("INSERT INTO sa_paper_account (id, initial_cash, cash, total_value) "
                    "VALUES (1, %s, %s, %s)", (initial_cash,) * 3)
        conn.commit()
    return {"ok": True, "initial_cash": initial_cash}
