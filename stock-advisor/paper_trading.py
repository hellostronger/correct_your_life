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
    "max_positions": 0,         # 同时持仓**只数**上限；0=不限。满了就不再开新仓（卖出不受影响）
    "latest_trade_time": "15:55",  # 轮次级别的最后允许成交时刻（**粗闸**）。
                                  # 权威闸门是每只票自己的交易时段（A股 15:00 收、
                                  # 港股 16:00 收），这里只防止整个轮次在傍晚白跑；
                                  # 取 15:55 是为了别把港股 15:05~16:00 的正常交易也挡掉。
                                  # 超过它一律不再成交，避免用收盘价成交
    "quote_max_age_min": 0,     # >0 时要求行情快照比现在新不超过这么多分钟（0=不额外限制）
    "sleep_seconds": 3,         # 逐股决策间 sleep（防 LLM 网关限流）
    "n_same": 5, "n_cross": 3,  # 经验注入条数
    "keep_per_ticker": 30,      # 经验库每股保留条数
    # —— 盘中每 30 分钟判断一轮（替代原来「盘后单点」）——
    "interval_minutes": 30,       # 盘中判断周期（分钟）
    "start_time": "09:35",        # 盘中窗口起（含）；09:30 开盘留 5 分钟等行情稳定
    "end_time": "14:40",          # 盘中窗口止（含）；留 20 分钟尾盘，之后不再开新仓
    "stop_loss_max_pct": 5,       # 止损宽度上限(%)：LLM 定的止损线不得比这更宽
                                 # 取 min(LLM值, 本值)：LLM 给 8% 而上限 5% → 实际用 5%。
                                 # 2026-09-29 修：原来写的是 max(LLM值, 本值)，而
                                 # max(8,5)=8，等于「取更宽的那个」，上限只在 LLM 给得
                                 # 更紧时把它**放宽**，与意图完全相反 —— 这个上限从上线
                                 # 起到发现为止一次都没生效过（溜溜梅 8% 止损即实证）。
    # —— 追高闸门（2026-09-29 因溜溜梅翻车加）——
    # 写在代码里而不是只写进经验库：NeurIPS《The Losing Winner》证明 LLM 会
    # reward-hack 代理目标 —— 把「不要追高」当提示词，LLM 完全可以自己论证
    # 「但前景光明」把它压过去。风控必须在 LLM 之外。
    # 实证：06658 溜溜梅 20 日涨 37.7%、距 20 日高点回撤 12.4% 时买入，
    # 隔夜即止损 8.98%。高位票赔率差：向上空间被压缩，向下有获利盘兑现。
    # 20% 而非 30%：实测 14 只票，20% 时拦下游族网络(+16% 紧贴高点)与溜溜梅
    # (+26%)，而茅台/五粮液/平安银行等全部放行。30% 时溜溜梅反而漏网。
    "max_chase_pct20": 20,        # 近 20 日涨幅超过这个值(%) → 禁止新开仓；0=关闭
    "max_high_prox_pct": 10,      # 涨幅为正且距20日高点回撤<此值 → 禁止；0=关闭
    # —— 波动率自适应止损（2026-09-29 加）——
    # 固定百分比止损对高波动票就是个随机数发生器：日均振幅 6% 的票，
    # 一天内触及 5% 止损的概率约 18%。止损宽度必须由波动率决定。
    # 实测 ATR(14)%：茅台 1.37 / 平安 1.73 / 比亚迪 1.99 / 国安 5.57 /
    # 游族 5.23 / 溜溜梅 12.36 —— 同一张 5% 单对它们含义完全不同。
    "atr_stop_k": 1.5,            # 止损 = k × ATR(14)；0=关闭（退回固定 %）
    "atr_stop_floor_pct": 5,      # ATR 止损的下限(%)，防止低波动期止损被压得过窄
                                  # （k×ATR 还要再与 stop_loss_max_pct 取小）
    # —— 交易规则（模拟成交也必须守，不然统计出来的胜率是假的）——
    "tplus0_extra": "",           # 逗号分隔的代码/关键词，强制当 T+0（覆盖自动判定）
    "tplus1_extra": "",           # 逗号分隔的代码/关键词，强制当 T+1
    "fees": {},                   # 交易费率覆盖（见 DEFAULT_FEES），完整键在 config.yaml
    "judge_holdings_only": False, # True=每轮只判已持仓（最省）；False=全自选股都判
}

EM_FIELDS_OHLCV = "f51,f52,f53,f54,f55,f56,f57"   # 日期,开,收,高,低,量,额


# ---------------- 交易费用（按真实市场规则扣） ----------------
# 为什么必须扣：不扣的话「总资产涨幅」会系统性高于真实可实现的收益，
# 而且因为费用是双边+最低 5 元，小额高频交易（现在每 30 分钟一轮，单笔金额
# 可能只有几千元）的偏差被放大很多 —— 5 元佣金对 2000 元的单子就是 0.25%，
# 相当于凭空多赚 0.25%。胜率统计会跟着失真。
#
# 各项依据（2026 现行）：
#   A 股股票  佣金 万2.5、最低 5 元（双向）；印花税 0.05% **仅卖出**
#              （2023-08-28 由 0.1% 减半）；过户费 0.001%（双向，沪深已统一）
#   场内基金  佣金同上；**免印花税**；过户费 0.001%
#   港股      佣金 万2.5、最低 5 元；印花税 0.1%（**双向都收**，与 A 股不同）
#              + 交易征费 0.0027% + 交易费 0.00565% + 结算费 0.002%（2~100 元）
DEFAULT_FEES = {
    "a_commission_rate": 0.00025, "a_commission_min": 5.0,
    "a_stamp_duty": 0.0005,        # 仅卖出
    "transfer_fee": 0.00001,       # 双向
    "hk_commission_rate": 0.0025, "hk_commission_min": 5.0,
    "hk_stamp_duty": 0.001,        # 双向
    "hk_levy": 0.000027, "hk_tx_fee": 0.0000565, "hk_ccass": 0.00002,
    "hk_settle_min": 2.0, "hk_settle_max": 100.0,
}


# ---------------- 交易时段（收市后不许成交） ----------------
# 为什么必须有：改成「盘中每 30 分钟判断」后，我发现兜底补跑那一支
# **没有上界** —— 只要过了 decide_time(15:35) 就触发，于是 17:24、17:48
# 还在成交（2026-09-28 实测各成交 2 笔）。更糟的是收市后行情接口照样返回
# 价格（返回的是当日收盘价），代码里没有时间戳校验，就用那个「收盘价」
# 成交了 —— 真实盘根本做不到这种事，胜率统计直接失真。
#
# 三层闸门，任何一层挡住就不成交：
#   1) 本函数：按 A股/港股各自的交易时段判定
#   2) quote 的 updated_at：快照时间必须是今天（顺带挡掉周末/节假日）
#   3) 收市后的兜底补跑：见 app._paper_loop 的 latest_trade_time

# (起, 止) 本地时间。格式 [(h, m, h, m), ...]
TRADING_SESSIONS = {
    "sh": ((9, 30, 11, 30), (13, 0, 15, 0)),
    "sz": ((9, 30, 11, 30), (13, 0, 15, 0)),
    "hk": ((9, 30, 12, 0), (13, 0, 16, 0)),
}


def market_session_state(code: str = "", now=None) -> dict:
    """该品种此刻能不能成交。返回 {open, market, reason, hhmm}。

    code 为空时按「任一市场开市」判定（用于轮次级别的门）。
    """
    now = now or datetime.now()
    mm = now.hour * 60 + now.minute
    mk = market_of(code) if code else ""
    markets = [mk] if mk else ["sh", "hk"]
    sess = {m: TRADING_SESSIONS.get(m) for m in markets}
    for m in markets:
        for (h1, m1, h2, m2) in (sess.get(m) or ()):
            if h1 * 60 + m1 <= mm <= h2 * 60 + m2:
                return {"open": True, "market": m,
                        "reason": f"{m} 交易时段内", "hhmm": now.strftime("%H:%M")}
    names = {"sh": "A股", "sz": "A股", "hk": "港股"}
    if not code:
        return {"open": False, "market": "",
                "reason": "A 股与港股均已收市", "hhmm": now.strftime("%H:%M")}
    return {"open": False, "market": mk,
            "reason": f"{names.get(mk, mk)}不在交易时段"
                      f"（{':'.join('%02d:%02d' % (h1, m1) + '-' + '%02d:%02d' % (h2, m2) for h1, m1, h2, m2 in (sess.get(mk) or ()))}）",
            "hhmm": now.strftime("%H:%M")}


def quote_is_fresh(quote: dict, now=None, max_age_minutes: int = 0) -> tuple[bool, str]:
    """行情快照是否还能用于成交。

    行情接口收市后仍会返回当日收盘价，且不带「已收市」标记；只有快照时间
    诚实。规则：
      - 没有 updated_at -> 放行（数据源没给时间，退回时段闸门判断）
      - updated_at 不是今天 -> 拒绝（隔夜数据，绝不能拿来成交）
      - 交易时段内且快照比现在还新 -> 拒绝（时钟/源异常）
      - max_age_minutes > 0 时，超过该年龄也拒绝（盘中卡住的死数据）
    """
    q = quote or {}
    raw = q.get("updated_at")
    if not raw:
        return True, "行情无时间戳（退回时段闸门判断）"
    now = now or datetime.now()
    if isinstance(raw, str):
        txt = raw.strip().replace("/", "-")
        t = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                t = datetime.strptime(txt, fmt)
                break
            except ValueError:
                continue
        if t is None:
            return True, f"行情时间戳无法解析（{raw}），退回时段闸门"
    elif isinstance(raw, datetime):
        t = raw.replace(tzinfo=None) if raw.tzinfo else raw
    else:
        return True, f"行情时间戳类型未知（{type(raw)}），退回时段闸门"
    if t.date() != now.date():
        return False, f"行情快照是 {t.date()} 的，非今日数据（拒绝用它成交）"
    age = (now - t).total_seconds() / 60.0
    if max_age_minutes and age > max_age_minutes:
        return False, f"行情快照已过期 {age:.0f} 分钟（上限 {max_age_minutes}）"
    return True, f"行情快照 {t.strftime('%H:%M:%S')}（{age:.0f} 分钟前）"


def is_fund(code: str, name: str = "") -> bool:
    """场内基金（ETF/LOF/REIT）—— 决定免不免印花税。

    代码前缀：51x/52x/56x/58x（沪）、15x/16x/18x/50x/20x（深）。
    注意别和股票撞车：688 开头是**科创板股票**（不是 58x 基金），
    600/000/002/003/300/301 也都不在列。
    """
    c, n = (code or "").strip(), (name or "").upper()
    if any(k in n for k in ("ETF", "LOF", "REIT", "QDII", "分级", "基金")):
        return True
    return len(c) == 6 and c.startswith(("51", "52", "56", "58", "15", "16",
                                         "18", "50", "20", "90"))


def trade_fees(code: str, name: str, side: str, value: float,
               fee_conf: dict | None = None) -> dict:
    """算一笔成交的费用。返回 {commission, stamp_duty, transfer, levy, total}。

    value = 成交金额（不含费）。total 是**全部**费用之和。
    买入时总付出 = value + total；卖出时净收入 = value - total。
    """
    f = {**DEFAULT_FEES, **(fee_conf or {})}
    value = max(0.0, float(value or 0))
    if value <= 0:
        return {"commission": 0.0, "stamp_duty": 0.0, "transfer": 0.0,
                "levy": 0.0, "total": 0.0}
    if market_of(code) == "hk":
        comm = max(value * float(f["hk_commission_rate"]), float(f["hk_commission_min"]))
        stamp = value * float(f["hk_stamp_duty"])          # 港股双向
        levy = (value * (float(f["hk_levy"]) + float(f["hk_tx_fee"]))
                + min(max(value * float(f["hk_ccass"]), float(f["hk_settle_min"])),
                      float(f["hk_settle_max"])))
        total = comm + stamp + levy
        return {"commission": round(comm, 4), "stamp_duty": round(stamp, 4),
                "transfer": 0.0, "levy": round(levy, 4), "total": round(total, 4)}
    # A 股：股票 vs 场内基金
    comm = max(value * float(f["a_commission_rate"]), float(f["a_commission_min"]))
    stamp = 0.0 if is_fund(code, name) else (
        value * float(f["a_stamp_duty"]) if side == "sell" else 0.0)
    transfer = value * float(f["transfer_fee"])
    total = comm + stamp + transfer
    return {"commission": round(comm, 4), "stamp_duty": round(stamp, 4),
            "transfer": round(transfer, 4), "levy": 0.0, "total": round(total, 4)}



# ---------------- 交易规则（T+0/T+1、最小买入单位） ----------------
# 为什么必须守：改成盘中每 30 分钟判断后，一天会成交很多次。而 A 股股票是
# T+1 —— 当天买的当天卖在真实市场根本做不到。之前「盘后只判一次」时这个问题
# 被掩盖了（一天最多一次决策，买和卖不会同时发生）；现在不守就等于让模拟盘
# 干出真实盘做不到的交易，胜率/收益率统计直接失真。
#
# 品种判定只能靠代码前缀 + 名称关键词（本地没有品种主数据）。所以做成
# 「自动判定 + 配置强制覆盖」，判错了能在 config.yaml 里一行改回来。

# 名称里出现这些词 → T+0（当日可卖）。境内股票型 ETF 不在此列。
_T0_NAME_HINTS = (
    "货币", "日利", "添益", "理财", "现金", "货币基金",
    "国债", "债", "信用债", "同业存单", "可转债", "短融", "地方债",
    "黄金", "白银", "商品", "原油", "期货", "豆粕", "有色",
    "纳指", "标普", "日经", "德国", "法国", "沙特", "巴西",
    "中概", "互联", "港股", "恒生", "香港", "海外", "全球", "国际",
    "美元", "亚太", "东南亚", "欧洲", "日本", "越南", "印度",
    "QDII", "LOF", "分级", "货币型", "债", "REIT",
)
# 名称里出现这些词 → 明确是境内股票型，强制 T+1（优先级高于上面的 T0 词表，
# 因为「港股科技ETF」里有"港股"但也可能实际跟踪港股——那个确实是 T0；
# 而「军工龙头ETF」这种境内股票型必须 T1）
_T1_NAME_HINTS = ("沪深300", "中证500", "中证1000", "上证50", "科创50",
                  "创业板", "军工", "白酒", "医药", "半导体", "芯片", "新能源")


def market_of(code: str) -> str:
    """港股 / 沪 / 深。与 app._tx_symbol 的判定保持一致。"""
    c = (code or "").strip()
    if len(c) == 5 and c.isdigit():
        return "hk"
    if c.startswith(("6", "9", "5")):
        return "sh"
    return "sz"


def is_star_market(code: str) -> bool:
    """科创板 688xxx / 689xxx（科创板 CDR）。"""
    return (code or "").startswith(("688", "689"))


def is_bse(code: str) -> bool:
    """北交所 8xxxxx / 4xxxxx（原新三板精选层）。"""
    return (code or "").startswith(("8", "4")) and len(code or "") == 6


def is_t_plus_0(code: str, name: str = "") -> bool:
    """该品种是否 T+0（当日买入当日可卖）。

    - 港股：全部 T+0
    - 货币/债券/黄金/商品/跨境(QDII) ETF 与 LOF：T+0
    - 境内股票型 ETF、A股股票、科创板、北交所：T+1
    """
    code, name = (code or "").strip(), (name or "").strip()
    override = _rule_overrides()
    for o in override["t0"]:
        if o and (o == code or o in name):
            return True
    for o in override["t1"]:
        if o and (o == code or o in name):
            return False
    if market_of(code) == "hk":
        return True
    upper = name.upper()
    for hint in _T1_NAME_HINTS:          # 境内股票型先判，避免被 "债/港股" 等词误伤
        if hint in name:
            return False
    for hint in _T0_NAME_HINTS:
        if hint in name or hint in upper:
            return True
    return False


def buy_shares_for(code: str, name: str, budget: float, price: float) -> int:
    """按该品种的申报规则，算出能用 budget 买多少股（0 = 买不起/不合规）。

    - 主板/创业板/ETF/LOF：100 股整数倍
    - 科创板 688/689：最少 200 股，超出部分 1 股递增（所以用整除而不是向下取整到 100）
    - 北交所：最少 100 股，1 股递增
    港股按 100 股的默认手数（本地没有每只港股的具体 board lot 数据）。
    """
    if price <= 0 or budget <= 0:
        return 0
    raw = int(budget / price)
    if is_star_market(code):
        return raw if raw >= 200 else 0            # 不足 200 股直接放弃，不下废单
    if is_bse(code):
        return raw if raw >= 100 else 0
    lots = raw // 100
    return lots * 100 if lots >= 1 else 0


_OVERRIDE_CACHE: dict = {}


def _rule_overrides() -> dict:
    """从 config 读 tplus0_extra / tplus1_extra，按内容缓存（内容变了自动重读）。"""
    try:
        import yaml
        from pathlib import Path
        p = Path(__file__).resolve().parent / "config.yaml"
        raw = p.read_text(encoding="utf-8")
        paper = (yaml.safe_load(raw) or {}).get("paper") or {}
        sig = (str(paper.get("tplus0_extra")), str(paper.get("tplus1_extra")))
    except Exception:
        sig = ("", "")
    if _OVERRIDE_CACHE.get("sig") != sig:
        def _split(v):
            return [x.strip() for x in str(v or "").replace("，", ",").split(",") if x.strip()]
        _OVERRIDE_CACHE["sig"] = sig
        _OVERRIDE_CACHE["t0"] = _split(sig[0])
        _OVERRIDE_CACHE["t1"] = _split(sig[1])
    return {"t0": _OVERRIDE_CACHE.get("t0", []), "t1": _OVERRIDE_CACHE.get("t1", [])}


def sellable_shares(cur, code: str, name: str, today: str) -> tuple[int, int, str]:
    """今天真正能卖多少股。返回 (可卖, 持仓, 说明)。

    A 股 T+1：当天买入的部分当天不可卖，但**昨天及更早买的那部分可以卖**
    （所以不是「有买入就全部不可卖」，是按买入日期分层算）。
    港股/T+0 品种：全部可卖。
    """
    cur.execute("""SELECT trade_date, side, shares FROM sa_paper_trades
                   WHERE code = %s AND side IN ('buy','sell')
                   AND status <> 'skipped' ORDER BY id""", (code,))
    rows = cur.fetchall()
    total = 0
    today_buy = 0
    for r in rows:
        d = str(r[0])[:10] if not isinstance(r[0], dict) else str(r["trade_date"])[:10]
        side = r[1] if not isinstance(r[0], dict) else r["side"]
        sh = int(r[2] or 0) if not isinstance(r[0], dict) else int(r["shares"] or 0)
        if sh <= 0:
            continue
        if side == "buy":
            total += sh
            if d == today:
                today_buy += sh
        else:
            total = max(0, total - sh)          # 简化：卖出不区分日期
    if is_t_plus_0(code, name):
        return total, total, "T+0 当日可卖"
    locked = min(today_buy, total)
    free = total - locked
    if locked > 0:
        return free, total, f"T+1：今日买入的 {locked} 股当日不可卖"
    return free, total, "T+1：可卖昨日及更早持仓"


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
必须严格遵守随附的【交易规则约束】——T+1 品种当日买入的份额当日不可卖，
被锁定时请直接给 hold，不要规划卖不掉的单；
无充分依据倾向 hold——模拟交易亏的是后续统计的胜率，乱动比不动差。
全部用中文输出（含 reasoning 字段）。"""

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


def atr_pct(bars: list[dict], period: int = 14) -> float | None:
    """ATR(period) 占现价的百分比 —— 止损宽度的波动率依据。

    真波幅 TR = max(H-L, |H-前收|, |L-前收|)，ATR 取近 period 日均值，
    再除以现价得到百分比，便于跨价位比较。

    为什么必须有它：固定百分比止损对高波动个股就是随机数发生器。
    日均振幅 6% 的票，一天内触及 5% 止损的概率约 18%（障碍穿越一阶近似），
    即每 5~6 笔就有 1 笔被纯噪声扫掉。止损宽度要跟波动率挂钩才谈得上风控。

    数据不够（<period+1 根）返回 None，调用方退回固定百分比。
    """
    if not bars or len(bars) < min(period, 5) + 1:
        return None
    trs: list[float] = []
    prev_close = None
    for b in bars:
        try:
            h, l, c = float(b["high"]), float(b["low"]), float(b["close"])
        except (KeyError, TypeError, ValueError):
            return None
        if prev_close is None:
            tr = h - l
        else:
            tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)
        prev_close = c
    if len(trs) < min(period, 5) + 1:
        return None
    window = trs[-period:] if len(trs) >= period else trs
    last = float(bars[-1]["close"] or 0)
    if last <= 0:
        return None
    return sum(window) / len(window) / last * 100


def vol_adjusted_stop(bars: list[dict], conf: dict) -> tuple[float | None, str]:
    """按波动率给止损宽度，返回 (止损%, 说明)。未启用或数据不足返回 (None, "")。

    止损% = max(k × ATR%, floor)。floor 兜底是因为 ATR 有个危险特性：
    低波动期 ATR 会很小，于是止损被压得极窄，然后波动率一扩张就必然被打掉。
    「低波动常常是暴风雨前的平静」——所以要设下限。
    """
    k = float(conf.get("atr_stop_k") or 0)
    if k <= 0:
        return None, ""
    ap = atr_pct(bars)
    if not ap:
        return None, ""
    floor = float(conf.get("atr_stop_floor_pct") or 0)
    stop = max(k * ap, floor)
    return stop, (f"ATR14={ap:.2f}%×{k:g}={k * ap:.2f}%，"
                  f"下限 {floor:g}% → 止损 {stop:.2f}%")


def chase_check(bars: list[dict], conf: dict) -> str:
    """追高闸门：命中返回原因串（禁止新开仓），未命中返回 ""。

    两条独立的判据，命中任一即拦：
      - 近 20 日涨幅 > max_chase_pct20      —— 已经涨太多，向上空间被压缩
      - 距 20 日最高价回撤 < max_high_prox_pct —— 紧贴高位，获利盘随时兑现

    这两条用的都是 _kline_summary 已经在算的数字，信息包里本来就有 ——
    问题从来不是「没采到」，而是「采到了但没有硬约束去挡」。
    实证：06658 溜溜梅 20 日涨 37.7%、距高点回撤 12.4%，隔夜止损 8.98%。
    """
    if len(bars) < 10:
        return ""
    closes = [b["close"] for b in bars]
    last = closes[-1]
    lim20 = float(conf.get("max_chase_pct20") or 0)
    lim_prox = float(conf.get("max_high_prox_pct") or 0)
    if lim20 <= 0 and lim_prox <= 0:
        return ""
    hits = []
    # 判据一：近 20 日涨幅。这个是对的 —— 溜溜梅 +37.7% 会被拦，
    # 而茅台 -5.0%、宁德 -20.8% 不会。它衡量「已经涨了多少」。
    if lim20 > 0 and len(closes) > 20:
        rise = (last / closes[-21] - 1) * 100
        if rise > lim20:
            hits.append(f"近20日已涨 {rise:+.1f}%（>{lim20:g}%）")
    # 判据二：**涨幅大** AND 紧贴近期高点。两个条件必须同时成立。
    #
    # 2026-09-29 修正：第一版写成「距 20 日最高价回撤 < 15% 就拦」，实测把
    # 14 只票里的 10 只全拦了 —— 茅台、五粮液、平安银行无一幸免。原因是
    # 这个判据**根本区分不了「高位强势股」和「温和下跌股」**：任何票只要近
    # 20 天没创新高就必然满足（跌 5% 的茅台离前高只有 7.8% 回撤）。
    # 真正要拦的是「涨了很多**且**还在高位」，两个条件缺一不可。
    if lim_prox > 0 and len(closes) > 20:
        rise = (last / closes[-21] - 1) * 100
        high20 = max(b["high"] for b in bars[-20:])
        if high20 > 0 and rise > 0:
            dd = (last / high20 - 1) * 100
            if dd > -lim_prox and rise >= min(lim_prox, 10):
                hits.append(f"近20日涨 {rise:+.1f}% 且距20日高点仅回撤 "
                            f"{dd:.1f}%（<{lim_prox:g}%，高位）")
    return "；".join(hits)


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

def _llm_call(system_prompt: str, user_text: str, max_tokens: int = 1500,
              extra: str = "") -> str:
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
    messages = [{"role": "user", "content": user_text + (extra or "")}]
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

    成本的定义是**净投入**：买入时把该笔手续费也算进成本（cost = 成交额+费用），
    卖出时按净回款（成交额-费用）核减。这样 pnl_pct 反映的是「扣除全部摩擦后」
    的真实盈亏，而不是价差本身 —— 否则总资产和持仓收益永远对不上。
    """
    cur.execute(
          "SELECT code, name, side, shares, price, fee_total, trade_date "
          "FROM sa_paper_trades "
        "WHERE side IN ('buy','sell') AND status <> 'skipped' "
        "ORDER BY id")
    pos: dict[str, dict] = {}
    for row in cur.fetchall():
        if isinstance(row, dict):
            code, name, side = row["code"], row["name"], row["side"]
            shares, price, fee = row["shares"], row["price"], row["fee_total"]
            tdate = row["trade_date"]
        else:
            code, name, side, shares, price, fee = row[:6]
            tdate = row[6] if len(row) > 6 else None
        shares, price = int(shares or 0), float(price or 0)
        fee = float(fee or 0)
        if shares <= 0 or price <= 0:
            continue
        p = pos.setdefault(code, {"shares": 0, "cost": 0.0, "name": name,
                                  "entry_date": None})
        if side == "buy":
            net = price * shares + fee          # 净投入含买入费用
            # 持仓均价与「加权平均买入日」同步更新 —— alpha 的基准起点
            # 要用它。只记首次买入日不够：加仓后真实持有期已变长，用
            # 最早那天算会把基准区间拉长、系统性低估超额收益。
            if p["entry_date"] and tdate:
                p["entry_date"] = _weighted_date(p["entry_date"], p["shares"],
                                               tdate, shares)
            elif tdate:
                p["entry_date"] = tdate
            p["cost"] = (p["cost"] * p["shares"] + net) / (p["shares"] + shares)
            p["shares"] += shares
        else:
            sell = min(shares, p["shares"])
            p["shares"] -= sell
            if p["shares"] <= 0:
                p["shares"], p["cost"] = 0, 0.0
                p["entry_date"] = None
    return {c: p for c, p in pos.items() if p["shares"] > 0}


# ---------------- 决策流水线 ----------------

SOCIAL_CAVEAT = (
    "⚠️ 以下是**社媒内容**（公众号 / B站动态与评论 / 微博 / X），非权威信息："
    "可能失真、被操纵、断章取义，也可能是股吧闲聊。它的证据等级**低于公告、"
    "财报与持牌媒体新闻** —— 可作为「市场情绪与题材热度」的参考，不要当作事实依据，"
    "更不要因为某条社媒内容就改变对基本面的判断。若某条内容与新闻或公告矛盾，"
    "一律以新闻公告为准。"
)


def _social_block(social: dict) -> str:
    """把社媒三层信号渲染进决策上下文。

    分三层给 LLM，语义完全不同，不能混在一堆里：
      ① 直呼本股      —— 高可信，直接相关
      ② 命中配置的概念 —— 中可信，反映本股所属板块的动向
      ③ 全市场热度    —— **仅供参考**，由 LLM 自行判断「这个题材与本股有无关系」

    第③层是主力：社媒讲的是板块概念而非公司全名（实测按公司名匹配社媒内容
    命中率为 0.0%：「PCB龙头，直线封涨停」里没有任何一个自选股的公司名），
    让 LLM 做语义判断比任何静态映射表都准，而且天然没有假阳性。
    """
    lines = ["【社媒信号（公众号 / B站 / 微博 / X）】", SOCIAL_CAVEAT]
    d = social.get("direct") or []
    if d:
        lines.append("① 近期直接提到本公司的内容：")
        lines += ["- " + x for x in d]
    else:
        lines.append("① 近期没有直接提到本公司的社媒内容。")
    c = social.get("concept") or []
    if c:
        lines.append("② 命中本股所属概念/板块的内容：")
        lines += ["- " + x for x in c]
    else:
        lines.append("② 该股未配置概念词（social_keys），或近期无相关内容。"
                     "如需启用，请在自选股里给该股填概念词。")
    h = social.get("hot") or []
    if h:
        lines.append("③ 全市场社媒热度榜（**与本股未必相关**，请自行判断题材能否映射到"
                     "本股；只在你认为确有关联时才写进理由）：")
        lines += ["- " + x for x in h]
    else:
        lines.append("③ 近期无社媒热度内容。")
    n = social.get("note")
    if n:
        lines.append("（采集说明：%s）" % n)
    return "\n".join(lines)


def _build_context(stock: dict, quote: dict, bars: list[dict],
                   news: list[dict], events: list[dict],
                   market_note: str, account: dict, positions: dict,
                   past_context: str, conf: dict, social: dict | None = None) -> str:
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
    if conf.get("_goal_line"):
        # 目标进度（收益率目标）。observe 模式也给 LLM 看，只是不加硬约束——
        # 让它知道这轮是朝着一个具体收益目标去的；constrain 且临期时会带
        # 「禁止开新仓」的明确指令（系统侧也真的会拒单）
        ctx.append(conf["_goal_line"])
    if social:
        ctx.append(_social_block(social))
    ctx.append(f"【历史决策复盘经验】\n{past_context}")
    return "\n".join(ctx)


def position_limit_status(cur, conf: dict) -> dict:
    """当前持仓只数 vs 上限。0 / 未配 = 不限。

    「持仓」口径与 UI 一致：sa_paper_trades 里 buy/sell 流水推导出的
    shares>0 的标的（hold 行 shares=0 不算，skipped 不算）。
    """
    try:
        cap = int(conf.get("max_positions") or 0)
    except (TypeError, ValueError):
        cap = 0
    held = _derive_paper_positions(cur)
    n = len(held)
    # 目标临期收紧：禁止开新仓。算作 full（与持仓只数上限同一档），这样
    # _rule_brief 会把它写成硬约束、_execute_decision 也真的会拒单——
    # 只在 prompt 里叮嘱一句 LLM 未必听，拒单在代码里才靠得住
    frozen = bool(conf.get("_no_new_positions"))
    return {"held": n, "cap": cap, "codes": sorted(held),
            "unlimited": cap <= 0, "full": (cap > 0 and n >= cap) or frozen,
            "frozen": frozen,
            "room": (max(0, cap - n) if cap > 0 else None)}


def run_decisions(deps: dict, slot: str = "", only: set | None = None) -> dict:
    """为每只自选股生成买卖决策并按当前价成交（盘中每 30 分钟一轮）。

    slot：决策轮次标签（'HH:MM'）。同一 slot 内同股只决策一次（幂等），
    不同 slot 各自独立决策 —— 这是「每半小时判断一次」的落点。
    only：只判这些 code（None = 全部自选股）。盘中已持仓的票可只判持仓，
    省下 LLM 调用。
    逐股 try/except 隔离，一股失败不影响其余（仿新闻抓取循环）。
    返回 {date, slot, decided: [...]}。
    """
    get_conn = deps["get_conn"]
    conf = {**DEFAULTS, **(deps.get("conf") or {})}
    # 目标模式（paper_goal.py）：约束型目标在「临期未达标」时收紧单票上限并
    # 禁止开新仓。**只做减法**——落后时放大仓位是为了达标而加赌注，会把失败
    # 概率进一步推高；临期未达标真正该做的是别再把已有收益搭进去。
    # conf 上面刚 spread 成了本轮副本，改它不污染配置文件。
    try:
        import paper_goal
        _gnote = paper_goal.constrain_note(deps)
    except Exception as exc:
        print(f"[paper] 目标模式读取失败（忽略）: {exc}", flush=True)
        _gnote = None
    if _gnote:
        conf["_goal_line"] = paper_goal.context_line(deps)
        if _gnote.get("tighten"):
            _old = float(conf["max_position_pct"])
            conf["max_position_pct"] = min(_old, float(_gnote["position_cap_pct"]))
            conf["_no_new_positions"] = True
            print(f"[paper] 目标临期收紧：单票上限 {_old}% -> "
                  f"{conf['max_position_pct']}%，本轮禁止开新仓"
                  f"（目标 {_gnote['target_return_pct']:g}%，"
                  f"当前 {_gnote['return_pct']:+.2f}%，剩 {_gnote['days_left']} 天）",
                  flush=True)
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
        # 持仓只数上限：满了就把「还没持仓的票」从候选里剔掉。
        # 放这里而不是只在成交处拦，是为了**省 LLM 调用** —— 满仓时没必要
        # 再为新标的跑两次 LLM（每轮 35 只 × 2 次是主要成本）。
        # 已持仓的票仍然照判（要判断卖不卖），卖出不受上限影响。
        limit = position_limit_status(cur, conf)
        blocked_by_limit = set()
        if limit["full"]:
            blocked_by_limit = {s["code"] for s in stocks if s["code"] not in positions}
            if blocked_by_limit:
                stocks = [s for s in stocks if s["code"] not in blocked_by_limit]
                print(f"[paper] 持仓已满 {limit['held']}/{limit['cap']}，"
                      f"本轮跳过 {len(blocked_by_limit)} 只未持仓标的（省 LLM 调用）",
                      flush=True)
        # 幂等守卫：**本 slot** 已决策过的股票集合（任何 side 都算）
        cur.execute("SELECT DISTINCT code FROM sa_paper_trades "
                    "WHERE trade_date = %s AND slot = %s", (today, slot))
        done = {r["code"] for r in cur.fetchall()}
    if only is not None:
        stocks = [s for s in stocks if s["code"] in only]
    for stock in stocks:
        code = stock["code"]
        if code in done:
            continue
        try:
            _decide_one(deps, conf, stock, today, results, slot)
            done.add(code)   # 股间也不重入（并发手动触发/线程双写防护）
        except Exception as exc:
            # UniqueViolation = 本 slot 已有决策行（线程与手动触发并发），静默跳过
            if "sa_paper_trades_trade_date_slot_code_side_key" in str(exc):
                print(f"[paper] {code} 本轮已有决策，跳过", flush=True)
            else:
                traceback.print_exc()
                print(f"[paper] decide {code} failed: {exc}", flush=True)
        time.sleep(float(conf.get("sleep_seconds", 3)))
    return {"date": today, "slot": slot, "decided": results,
            "position_limit": limit, "skipped_by_limit": sorted(blocked_by_limit)}


def _real_dict_cursor(deps):
    """deps 注入 psycopg2.extras.RealDictCursor（模块不 import psycopg2 也可，直接 import）。"""
    import psycopg2.extras
    return psycopg2.extras.RealDictCursor


def _rule_brief(code: str, name: str, held: bool, free_shares: int = 0,
                limit: dict | None = None) -> str:
    """给 LLM 看的一句交易规则说明（避免它规划出市场做不到的操作）。"""
    t0 = is_t_plus_0(code, name)
    mk = market_of(code)
    if is_star_market(code):
        lot = "科创板：买入最少 200 股，超出部分可 1 股递增"
    elif is_bse(code):
        lot = "北交所：买入最少 100 股，可 1 股递增"
    elif mk == "hk":
        lot = "港股：按 100 股的默认手数计（本地无逐只 board lot 数据）"
    else:
        lot = "买入须为 100 股的整数倍"
    t1 = "T+0（当日买入当日可卖）" if t0 else "T+1（当日买入当日不可卖，最早次日卖出）"
    sell = ""
    if held and not t0:
        sell = (f"；该股今日可卖 {free_shares} 股"
                + ("（今日买入的部分被 T+1 锁定）" if free_shares < 1 else ""))
    elif held and t0:
        sell = "；该股 T+0，持仓可随时卖"
    extra = ""
    lim = limit or {}
    if not held and lim.get("frozen"):
        extra = ("\n- ⛔ 账户目标已临期未达标，进入**收紧期**：本轮**不允许开新仓**，"
                 "只允许对已有持仓减仓或持有落袋。你必须给 hold 或 sell，"
                 "不要给出任何 buy 方案（会被系统直接拒单）")
    elif not held and lim.get("full"):
        extra = (f"\n- 账户当前持仓 {lim['held']} 只已达上限 {lim['cap']} 只，"
                 f"本轮**不允许开新仓**：你必须给 hold，"
                 f"不要给出任何 buy 方案（会被系统直接拒单）")
    elif not held and lim.get("cap"):
        extra = (f"\n- 账户持仓上限 {lim['cap']} 只，当前 {lim['held']} 只，"
                 f"还剩 {lim['room']} 个名额")
    return (f"\n\n【交易规则约束（硬性，违反会被系统拒单）】\n"
            f"- 市场：{ {'hk': '港股', 'sh': '沪市', 'sz': '深市'}[mk] }；{t1}\n"
            f"- {lot}\n"
            f"- 卖出{ sell.lstrip('；') if sell else '无持仓'}{extra}")


def _decide_one(deps, conf, stock, today, results, slot: str = ""):
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
    # 社媒信号（公众号/B站/微博/X）。取数失败返回空 dict，_social_block 会渲染成
    # 「近期无内容」，不会因为某个采集模块挂掉而让整轮决策失败。
    social_fn = deps.get("social_fn")
    social: dict = {}
    if social_fn:
        try:
            social = social_fn(code, name) or {}
        except Exception as exc:
            print(f"[paper] {code} 社媒信号异常: {exc}", flush=True)
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
                         account, positions, past, conf, social)
    # 3. LLM 调用 1：分析师
    report = _llm_call(ANALYST_PROMPT, ctx + f"\n\n数据时点 {today}。请给出分析报告。")
    # 4. LLM 调用 2：交易员（结构化）
    # 把交易规则也告诉它，否则它会规划出真实市场做不到的事（比如让 A 股当天买当天卖）
    with get_conn() as conn:
        c2 = conn.cursor()
        _free, _held, _rule = sellable_shares(c2, code, name, today)
        _lim = position_limit_status(c2, conf)
    rules = _rule_brief(code, name, positions.get(code) is not None, _free, _lim)
    # 波动率信息并进交易员提示：让 LLM 自己也能按波动率定止损，而不是拍脑袋。
    # 代码层会用 min() 再夹一次，但先给它数字比事后纠正更省事。
    _ap = atr_pct(bars)
    _vol_hint = ""
    if _ap:
        _vol_hint = (f"\n【波动率】ATR(14)={_ap:.2f}%（占现价），"
                     f"按 1.5×ATR 建议止损宽度约 {_ap * 1.5:.1f}%。"
                     f"该股日均振幅接近 {_ap * 1.6:.1f}%，止损若明显窄于此，"
                     f"一天内被噪声打掉的概率约 {_ap * 1.6 * 0.45:.0f}%。")
    raw_trader = _llm_call(
        TRADER_PROMPT,
        f"【分析报告】\n{report}\n\n【账户现状】\n可用现金 {account['cash']:,.0f} 元，"
        f"总资产 {account['total_value']:,.0f} 元，"
        f"已持有该股 {positions[code]['shares']} 股（成本 {positions[code]['cost']:g}，"
        f"今日可卖 {_free} 股）"
        if code in positions else
        f"【分析报告】\n{report}\n\n【账户现状】\n可用现金 {account['cash']:,.0f} 元，"
        f"总资产 {account['total_value']:,.0f} 元，该股无持仓",
        extra=rules + _vol_hint)
    decision = _validate_decision(_extract_json(raw_trader) or {})
    if decision is None:
        decision = {"action": "hold", "confidence": 1, "target_value_pct": 0,
                    "stop_loss_pct": 8, "reasoning": "（交易员输出解析失败，保守观望）"}
        decision["_parse_failed"] = True
    decision["report"] = report
    decision["raw"] = raw_trader
    # 5. 成交执行（资金硬约束代码强制）
    executed, note = _execute_decision(deps, conf, stock, today, decision, quote,
                                       slot, bars=bars)
    results.append({"code": code, "name": name, "action": decision["action"],
                     "executed": executed, "reasoning": decision["reasoning"],
                     "note": note})


def _execute_decision(deps, conf, stock, today, decision, quote,
                      slot: str = "", bars: list | None = None) -> tuple[bool, str]:
    """按当前价成交（盘中原价，不再是收盘价）。返回 (是否成交, 备注)。资金约束全部代码强制。

    bars：近 60 日 OHLCV，供追高闸门与 ATR 止损用（_decide_one 已有，不重复拉）。
    止损扫描路径调本函数时传 None —— 止损只减仓，不触发新开仓闸门。
    """
    get_conn = deps["get_conn"]
    code = stock["code"]
    price = float(quote["price"])
    action = decision["action"]
    with get_conn() as conn:
        cur = conn.cursor()
        # 幂等守卫下沉到数据层：**同一 slot 内**同股已有任何决策行（含反向）即拒绝。
        # 2026-09-24 实发问题：手动触发+线程并发下 LLM 两次跑出 buy 和 sell
        # 各插入一行（UNIQUE 只限 trade_date+code+side，方向不同不拦），
        # 同日买+卖双开导致持仓推导=0 但两笔都待结算、账目语义错乱。
        # 改为按 slot 判定：同一轮里仍只允许一个方向，但**不同轮次可以各自决策**，
        # 这正是「每 30 分钟判断一次」需要的语义。
        cur.execute("SELECT 1 FROM sa_paper_trades WHERE trade_date = %s AND slot = %s "
                    "AND code = %s AND status <> 'skipped' LIMIT 1", (today, slot, code))
        if cur.fetchone():
            return False, "该股本轮已有决策，跳过"
        account = _account_row(cur)
        positions = _derive_paper_positions(cur)
        me = positions.get(code)
        fees_c = conf.get("fees")
        # 收市闸门（权威）：真实市场收市后接不到单，模拟盘不能拿收盘价成交。
        # 行情接口收市后照样返回价格，所以必须自己判时段 + 校验快照时间。
        _st = market_session_state(code)
        if not _st["open"]:
            return False, f"{_st['market'] or '市场'}不在交易时段（{_st['reason']}），不成交"
        _fresh, _why = quote_is_fresh(
            quote, max_age_minutes=int(conf.get("quote_max_age_min") or 0))
        if not _fresh:
            return False, f"行情不可用于成交：{_why}"
        if action == "buy":
            # 持仓只数上限的**权威**校验点。run_decisions 里已经先剔过一轮候选
            # （为了省 LLM 调用），但手动触发 / 止损自动卖出后的加仓 / 未来新增
            # 的调用路径都只到这里，所以真正的闸门必须落在这里。
            # 加仓（me 已存在）不占新名额，不受限制。
            limit = position_limit_status(cur, conf)
            if not me and limit["full"]:
                cur.execute(
                    _insert_trade_sql(),
                    (code, stock["name"], today, slot, "buy", 0, price, 0,
                     decision["confidence"], decision["stop_loss_pct"],
                     decision["reasoning"], decision.get("report", ""),
                     json.dumps({"decision": decision}, ensure_ascii=False),
                     "skipped", 0, "{}"))
                conn.commit()
                return False, (f"持仓只数已达上限 {limit['held']}/{limit['cap']}，"
                               f"不再开新仓（记为 skipped；卖出不受此限）")
            budget = account["total_value"] * float(decision["target_value_pct"]) / 100
            budget = min(budget, account["cash"])
            # 追高闸门：**只拦新开仓**，加仓已持仓的票不受影响（不新增风险敞口）。
            # 放在这里而不是只写进提示词，是因为 LLM 会自己把「不要追高」论证掉
            # —— NeurIPS《The Losing Winner》实证 LLM 系统性 reward-hack 代理目标。
            if not me:
                _chase = chase_check(bars or [], conf)
                if _chase:
                    cur.execute(
                        _insert_trade_sql(),
                        (code, stock["name"], today, slot, "buy", 0, price, 0,
                         decision["confidence"], decision["stop_loss_pct"],
                         decision["reasoning"], decision.get("report", ""),
                         json.dumps({"decision": decision, "veto": "chase"},
                                    ensure_ascii=False), "skipped", 0, "{}"))
                    conn.commit()
                    return False, f"追高闸门：{_chase}，禁止新开仓（记为 skipped）"
            # 波动率自适应止损：ATR 止损与 stop_loss_max_pct 上限取**较小**的那个。
            # 两者都是「收紧」方向，所以 min；ATR 关掉时退回 LLM 给的值。
            _bars = bars or []
            _atr_sl, _atr_note = vol_adjusted_stop(_bars, conf)
            _sl = float(decision["stop_loss_pct"] or 0)
            if _atr_sl and _atr_sl > 0:
                decision["stop_loss_pct"] = min(_sl, _atr_sl) if _sl > 0 else _atr_sl
            if decision["stop_loss_pct"] is not None:
                decision["stop_loss_pct"] = min(
                    float(decision["stop_loss_pct"]),
                    float(conf.get("stop_loss_max_pct") or 100))
            max_pos_value = account["total_value"] * float(conf["max_position_pct"]) / 100
            if me:
                budget = min(budget, max(0, max_pos_value - me["shares"] * me["cost"]))
            # 申报单位按品种走：主板/创业板/ETF 100 股整数倍，科创板最少 200 股
            # 且超出后 1 股递增（原来一律 //100*100，科创板会算出 100 股的废单）
            shares = buy_shares_for(code, stock.get("name", ""), budget, price)
            if shares < 1:
                cur.execute(
                    _insert_trade_sql(),
                    (code, stock["name"], today, slot, "buy", 0, price, 0,
                     decision["confidence"], decision["stop_loss_pct"],
                     decision["reasoning"], decision.get("report", ""),
                     json.dumps({"decision": decision, "raw": decision.get("raw", "")},
                                ensure_ascii=False), "skipped", 0, "{}"))
                conn.commit()
                why = ("科创板最少 200 股，预算不足" if is_star_market(code)
                       else "现金或仓位上限不足，不足 1 手")
                return False, f"{why}，未成交（记为 skipped）"
            value = shares * price
            fee = trade_fees(code, stock.get("name", ""), "buy", value, fees_c)
            # 现金约束要把费用算进去：否则「买得起」但「付完钱就变负」
            while shares >= 1 and value + fee["total"] > account["cash"]:
                shares -= 100 if not is_star_market(code) else 1
                if shares < 1:
                    break
                value = shares * price
                fee = trade_fees(code, stock.get("name", ""), "buy", value, fees_c)
            if shares < 1:
                cur.execute(
                    _insert_trade_sql(),
                    (code, stock["name"], today, slot, "buy", 0, price, 0,
                     decision["confidence"], decision["stop_loss_pct"],
                     decision["reasoning"], decision.get("report", ""),
                     json.dumps({"decision": decision}, ensure_ascii=False),
                     "skipped", 0, "{}"))
                conn.commit()
                return False, "现金不足以支付成交额与手续费，未成交（记为 skipped）"
            cur.execute(_insert_trade_sql(),
                        (code, stock["name"], today, slot, "buy", shares, price, value,
                         decision["confidence"], decision["stop_loss_pct"],
                         decision["reasoning"], decision.get("report", ""),
                         json.dumps({"decision": decision}, ensure_ascii=False), "open",
                         fee["total"], json.dumps(fee, ensure_ascii=False)))
            cur.execute("UPDATE sa_paper_account SET cash = cash - %s, updated_at = now() "
                        "WHERE id = 1", (value + fee["total"],))
            conn.commit()
            return True, (f"买入 {shares} 股 × {price:g}，费用 {fee['total']:g}"
                          f"（佣金 {fee['commission']:g}"
                          + (f" + 过户费 {fee['transfer']:g}" if fee["transfer"] else "")
                          + "）")
        if action == "sell":
            if not me:
                cur.execute(
                    _insert_trade_sql(),
                    (code, stock["name"], today, slot, "sell", 0, price, 0,
                     decision["confidence"], None, decision["reasoning"],
                     decision.get("report", ""),
                     json.dumps({"decision": decision}, ensure_ascii=False),
                     "skipped", 0, "{}"))
                conn.commit()
                return False, "无持仓可卖（记为 skipped）"
            # T+1 闸门：A股当天买的当天不能卖。盘中多轮交易后这条必须有，
            # 否则模拟盘会干出真实盘做不到的事，胜率统计直接失真。
            free, held, rule_note = sellable_shares(cur, code, stock.get("name", ""), today)
            if free < held:
                if free < 1:
                    cur.execute(
                        _insert_trade_sql(),
                        (code, stock["name"], today, slot, "sell", 0, price, 0,
                         decision["confidence"], None, decision["reasoning"],
                         decision.get("report", ""),
                         json.dumps({"decision": decision, "rule": rule_note},
                                    ensure_ascii=False), "skipped", 0, "{}"))
                    conn.commit()
                    return False, f"{rule_note}，本次无法卖出（记为 skipped）"
            shares = min(free, held) if free < held else held
            if shares < 1:
                return False, f"{rule_note}，无可卖股"
            value = shares * price
            fee = trade_fees(code, stock.get("name", ""), "sell", value, fees_c)
            cur.execute(_insert_trade_sql(),
                        (code, stock["name"], today, slot, "sell", shares, price, value,
                         decision["confidence"], None, decision["reasoning"],
                         decision.get("report", ""),
                         json.dumps({"decision": decision}, ensure_ascii=False), "open",
                         fee["total"], json.dumps(fee, ensure_ascii=False)))
            # 卖出成交了，但它**平掉的那笔买入还没结**。这一行是 2026-09-29 补的，
            # 补的是止损/T+0 卖出后 buy 一直挂着 status='open' 造成的重复结算：
            #   - 结算队列（settle_and_reflect）挑的正是 status='open'，
            #     所以被卖掉的买入 5 个交易日后还会被当「未平仓」再平一次，
            #     _close_trade 里再 cash += value - fee，凭空多出一笔现金；
            #   - 持仓推导只认 shares 的买卖净额，不看 status，所以总资产是对的，
            #     错的只有结算与经验库。
            # 现在按成交价把被平掉的买入就地结算（raw_return 用真实卖价），
            # 并把这条卖出行标成 'closed'（不是持仓了，别再进结算队列）。
            #
            # alpha = 个股区间收益 - 基准指数区间收益。基准取该股所属市场的
            # 指数（上证/深成指/恒指），用**买入日到卖出日**的收盘价算。
            # 胜率统计按 alpha>0 判胜负，所以这里必须算 —— 原来不写 alpha
            # 导致 4 笔已结算全是 NULL，SUM(CASE WHEN alpha>0) 恒为 NULL，
            # 页面胜率恒显 0%（2026-09-29 实测）。
            _alpha, _bench = _alpha_for_round(cur, code, me, price, today, deps)
            _settle_buy_rows(cur, code, shares, price, today,
                             decision.get("report", ""), _alpha, _bench)
            cur.execute("UPDATE sa_paper_trades SET status = 'closed', settle_date = %s, "
                        "settle_price = %s WHERE id = "
                        "(SELECT id FROM sa_paper_trades WHERE code=%s AND trade_date=%s "
                        " AND slot=%s AND side='sell' AND status='open' ORDER BY id DESC LIMIT 1)",
                        (today, price, code, today, slot))
            # 净回款 = 成交额 - 费用
            cur.execute("UPDATE sa_paper_account SET cash = cash + %s, updated_at = now() "
                        "WHERE id = 1", (value - fee["total"],))
            conn.commit()
            extra = (f" + 印花税 {fee['stamp_duty']:g}" if fee["stamp_duty"] else "")
            return True, (f"卖出 {shares} 股 × {price:g}，费用 {fee['total']:g}"
                          f"（佣金 {fee['commission']:g}{extra}"
                          + (f" + 杂费 {fee['levy']:g}" if fee["levy"] else "") + "）")
        # hold：记录决策理由（可解析性），不占资金、**不产生持仓**。
        # status 用 'none'（无仓位）而不是 'open' —— 否则页面会把这行显示成
        # 「持有中」，和同一行 side 显示的「观望」自相矛盾（2026-09-28 实测
        # 335 行里 308 行是这种）。
        cur.execute(_insert_trade_sql(),
                    (code, stock["name"], today, slot, "hold", 0, None, None,
                     decision["confidence"], None, decision["reasoning"],
                     decision.get("report", ""),
                     json.dumps({"decision": decision}, ensure_ascii=False), "none",
                     0, "{}"))
        conn.commit()
        return False, "观望"


def _insert_trade_sql() -> str:
    return ("INSERT INTO sa_paper_trades "
            "(code, name, trade_date, slot, side, shares, price, value, confidence, "
            " stop_loss_pct, reasoning, report, decision_raw, status, fee_total, fee_detail) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)")


def _weighted_date(d1, shares1, d2, shares2):
    """按股数加权平均两个交易日，返回 date（或无法计算时返回较晚的那个）。

    加仓后持仓的「真实持有期起点」应该往后移：用最早的买入日算 alpha 会把
    基准区间拉长、��统性低估超额收益。取加权平均而不是简单取晚值，是为了让
    起点随仓位结构连续变化，避免加仓一次就跳变。
    纯函数，不碰数据库；入参可以是 date 或 'YYYY-MM-DD' 字符串。
    """
    from datetime import date as _date
    def _to_ord(d):
        if d is None:
            return None
        if isinstance(d, _date):
            return d.toordinal()
        s = str(d)[:10]
        try:
            return _date.fromisoformat(s).toordinal()
        except ValueError:
            return None
    o1, o2 = _to_ord(d1), _to_ord(d2)
    if o1 is None:
        return d2
    if o2 is None:
        return d1
    s1, s2 = float(shares1 or 0), float(shares2 or 0)
    tot = s1 + s2
    if tot <= 0:
        return _date.fromordinal(max(o1, o2))
    w = (o1 * s1 + o2 * s2) / tot
    return _date.fromordinal(int(round(w)))


def _alpha_for_round(cur, code: str, me: dict | None, sell_price: float,
                     today: str, deps: dict) -> tuple[float | None, str]:
    """算这一回合的超额收益 alpha = 个股区间收益 - 基准指数区间收益。

    返回 (alpha, benchmark_symbol)。拿不到基准（K线不足/接口失败）时返回
    (None, "")，调用方把 alpha 留空 —— 统计会自动降级成按 raw_return 判胜负，
    并在返回值里用 win_basis 标明口径，绝不会静默当成 alpha。

    基准口径与 settle_and_reflect 一致：按该股所属市场取指数
    （A 股沪市=上证、深市=深成指、港股=恒指），用买入日到卖出日的收盘价。
    """
    if not me or not me.get("cost"):
        return None, ""
    try:
        em_kline_fn = deps.get("em_kline_fn")
        tx_symbol_fn = deps.get("tx_symbol_fn")
        if not em_kline_fn or not tx_symbol_fn:
            return None, ""
        bench_sym = benchmark_symbol(code)
        closes = fetch_close_series(em_kline_fn, bench_sym, days=30)
        if len(closes) < 2:
            return None, ""
        entry_date = str(me.get("entry_date") or "")[:10]
        if not entry_date:
            return None, ""
        # 买入日之后的基准收盘序列（买入日当天的收盘作为起点）
        later = sorted(d for d in closes if d >= entry_date)
        if len(later) < 2:
            return None, ""
        bench_ret = closes[later[-1]] / closes[later[0]] - 1
        raw_ret = sell_price / float(me["cost"]) - 1
        return raw_ret - bench_ret, bench_sym
    except Exception as exc:
        print(f"[paper] alpha 计算失败 {code}: {exc}", flush=True)
        return None, ""


def _settle_buy_rows(cur, code: str, sell_shares: int, sell_price: float,
                     today: str, report: str = "", alpha: float | None = None,
                     benchmark: str = "") -> int:
    """卖出成交后，把被平掉的那部分买入就地结算（先进先出）。

    为什么必须有这一步：结算队列 settle_and_reflect 挑的是 status='open'。
    卖出只插 sell 行、buy 仍挂 'open' 的话，被止损卖掉的买入会在 5 个交易日后
    被当成「还持有」再结一次 —— _close_trade 会再插一笔反向的「到期平仓」行，
    并执行 cash += value - fee，而那笔现金卖出当天就已经回过账，等于凭空多钱。
    2026-09-29 实测：库里当时有 20 笔这样的行等着被重复结算。

    - 先先进出逐笔核销（buy 的 id 升序），与 _derive_paper_positions 的口径一致
    - 部分平仓时把买入按比例拆成「已平仓的那份」+「仍持有的那份」，不改动原行的
      shares，避免动到还没卖出的仓位
    - raw_return 用**真实卖价**算，经验库和收益率统计才是对的
    - alpha/basename 传入时一并写入。2026-09-29 起必须传：胜率统计按
      alpha_return>0 判胜负（相对基准才是「胜率」该有的含义），不传 alpha
      会让统计降级成按 raw_return 判并在返回值里标明口径已降级。
    """
    left = int(sell_shares or 0)
    if left < 1 or sell_price <= 0:
        return 0
    cur.execute("""SELECT id, trade_date, slot, shares, price, confidence,
                          stop_loss_pct, reasoning, report, decision_raw, fee_total
                   FROM sa_paper_trades
                   WHERE code=%s AND side='buy' AND status='open'
                   ORDER BY id""", (code,))
    rows = cur.fetchall()
    closed = 0
    for r in rows:
        if left <= 0:
            break
        bought = int(r["shares"] or 0)
        if bought < 1:
            continue
        entry = float(r["price"] or 0)
        if not entry:
            left -= bought
            continue
        take = min(bought, left)
        raw_ret = (sell_price / entry - 1)
        if take >= bought:
            # 整笔核销：买入行直接进已结算
            cur.execute("""UPDATE sa_paper_trades
                           SET status='resolved', settle_date=%s, settle_price=%s,
                               raw_return=%s, alpha_return=%s, benchmark=%s,
                               auto_closed=COALESCE(auto_closed, FALSE)
                           WHERE id=%s""",
                        (today, sell_price, raw_ret, alpha, benchmark, r["id"]))
            closed += 1
        else:
            # 部分核销：把没卖出的那部分拆成新行留 'open'，原行变成已平仓的那份
            keep = bought - take
            keep_price = entry
            keep_value = keep * keep_price
            keep_fee = float(r["fee_total"] or 0) * keep / bought
            cur.execute("""INSERT INTO sa_paper_trades
                           (code, name, trade_date, slot, side, shares, price, value,
                            confidence, stop_loss_pct, reasoning, report, decision_raw,
                            status, fee_total, fee_detail)
                           SELECT code, name, trade_date, slot, 'buy', %s, price, %s,
                                  confidence, stop_loss_pct, reasoning, report,
                                  decision_raw, 'open', %s, fee_detail
                           FROM sa_paper_trades WHERE id=%s""",
                        (keep, keep_value, round(keep_fee, 4), r["id"]))
            cur.execute("""UPDATE sa_paper_trades
                           SET status='resolved', settle_date=%s, settle_price=%s,
                               raw_return=%s, alpha_return=%s, benchmark=%s,
                               shares=%s, value=%s, fee_total=%s
                           WHERE id=%s""",
                        (today, sell_price, raw_ret, alpha, benchmark, take,
                         take * entry, round(float(r["fee_total"] or 0) * take / bought, 4),
                         r["id"]))
            closed += 1
        left -= take
    return closed


# ---------------- 盘中轮次：免 LLM 止损 + 周期台账 ----------------

def check_stop_loss(deps: dict, slot: str = "", max_pct: float | None = None) -> dict:
    """盘中免 LLM 的止损扫描：持仓现价跌破各自 stop_loss_pct 立即市价卖出。

    这是「每 N 分钟判断一次」最值钱的一半 —— 止损是最该发生在盘中的动作，
    却完全不需要 LLM（不烧钱、不等 3~10 秒、不会因解析失败而漏掉）。
    止损线取该股最近一笔**未平仓**买入时记录的值（当时 LLM 定的）；
    max_pct 是止损宽度的**上限**：实际止损取 min(LLM给的值, 上限)，把 LLM 定得
    过宽的止损收紧 —— 防止它给个 -15% 形同虚设的止损。上限只收紧、不会把过窄的
    止损放宽（min 而非 max，见下面 sl = min(sl, cap) 那行的说明）。
    逐股 try/except 隔离。
    """
    get_conn, quote_fn = deps["get_conn"], deps["quote_fn"]
    conf = {**DEFAULTS, **(deps.get("conf") or {})}
    today = datetime.now().strftime("%Y-%m-%d")
    cap = float(max_pct if max_pct is not None
                else conf.get("stop_loss_max_pct", 5))
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=_real_dict_cursor(deps))
        # 每只票取「最近一笔 open 买入」上记录的止损线
        cur.execute("""
            SELECT DISTINCT ON (code) code, name, stop_loss_pct
            FROM sa_paper_trades
            WHERE side='buy' AND status='open'
            ORDER BY code, id DESC""")
        stops = {r["code"]: r for r in cur.fetchall()}
        positions = _derive_paper_positions(cur)
    targets = [c for c in positions if c in stops]
    if not targets:
        return {"date": today, "slot": slot, "checked": 0, "sold": []}
    # 收市后**不成交**，但仍然要报：否则「止损触发了却没走」会静默。
    # 停牌/隔夜的止损要等下一个交易时段的第一轮处理。
    off_session = [c for c in targets if not market_session_state(c)["open"]]
    tradable = [c for c in targets if c not in off_session]
    # T+1 预筛：A 股当天买入的部分卖不掉。止损不能对锁定的份额生效，
    # 但要记下来「触发了却卖不掉」，否则会误以为风控在正常工作。
    sellable = {}
    with get_conn() as conn:
        c2 = conn.cursor()
        for code in targets:
            free, held, note = sellable_shares(
                c2, code, stops[code].get("name") or positions[code].get("name") or "",
                today)
            sellable[code] = (free, held, note)
    quotes = quote_fn(tradable) if tradable else {}
    sold, skipped = [], list(off_session)
    for code in tradable:
        try:
            q = quotes.get(code) or {}
            price = q.get("price")
            if not isinstance(price, (int, float)) or price <= 0:
                skipped.append({"code": code, "reason": "行情不可得"})
                continue
            row = stops[code]
            sl_raw = row.get("stop_loss_pct")
            sl = float(sl_raw) if sl_raw not in (None, "") else float(conf.get("stop_loss_pct", 8))
            # 宽度上限：把 LLM 定得过宽的止损收紧。**必须是 min 而不是 max**——
            # 原来写的是 `max(sl, cap)`，而 max(8, 5)=8，等于「取更宽的那个」，
            # 上限从来只会在 LLM 给得更紧时把它**放宽**，恰好与意图相反
            # （DEFAULTS 与 config_schema 的注释都写的是「取小值」）。
            # 2026-09-29 实锤：溜溜梅 LLM 要 8% 止损、配置上限 5%，8% 照走不误，
            # 隔夜跳空后实际亏 8.98%。min 只会收紧、不会放宽，符合「上限」语义。
            sl = min(sl, cap)
            cost = positions[code]["cost"]
            pnl_pct = (float(price) / cost - 1) * 100 if cost else 0.0
            if pnl_pct > -sl:
                continue                  # 还没到止损线，继续拿着
            free, held, rule_note = sellable.get(code, (0, 0, ""))
            if free < 1:
                # 触发了但一股都卖不掉（T+1 锁定）—— 必须记下来，否则会误判风控有效
                skipped.append({"code": code, "name": positions[code].get("name", ""),
                                "pnl_pct": round(pnl_pct, 2), "stop_loss_pct": sl,
                                "reason": f"止损已触发但{rule_note}，本轮无法卖出"})
                print(f"[paper] 止损触发但卖不掉 {code} {positions[code].get('name','')} "
                      f"({pnl_pct:.2f}% <= -{sl:g}%)：{rule_note}", flush=True)
                continue
            decision = {
                "action": "sell", "confidence": 100,
                "target_value_pct": 0, "stop_loss_pct": sl,
                "reasoning": (f"盘中止损：现价 {price:g} 较成本 {cost:.4f} 跌 {pnl_pct:.2f}%，"
                              f"触发止损线 -{sl:g}%（免 LLM 自动执行）"),
            }
            stock = {"code": code, "name": row.get("name") or positions[code].get("name") or ""}
            executed, note = _execute_decision(deps, conf, stock, today, decision,
                                               {"price": float(price)}, slot)
            (sold if executed else skipped).append(
                {"code": code, "name": stock["name"], "pnl_pct": round(pnl_pct, 2),
                 "stop_loss_pct": sl, "note": note})
        except Exception as exc:
            traceback.print_exc()
            skipped.append({"code": code, "reason": f"异常: {exc}"})
    if sold:
        print(f"[paper] 盘中止损 {len(sold)} 只: "
              f"{[s['code'] for s in sold]}", flush=True)
    if off_session:
        print(f"[paper] {len(off_session)} 只持仓已收市，止损待下一交易时段处理: "
              f"{off_session}", flush=True)
    return {"date": today, "slot": slot, "checked": len(tradable), "sold": sold,
            "skipped": skipped, "off_session": off_session}


def record_cycle(deps: dict, cycle_date: str, slot: str, kind: str = "intraday",
                 trigger: str = "", planned: int = 0, acted: int = 0,
                 holds: int = 0, skipped: int = 0, elapsed_ms: int = 0,
                 error: str = "") -> None:
    """写一条周期台账（同一 date+slot 覆盖写，重试不会堆重复行）。"""
    try:
        with deps["get_conn"]() as conn:
            conn.cursor().execute(
                "INSERT INTO sa_paper_cycles "
                "(trade_date, slot, kind, trigger, planned, acted, holds, skipped, "
                " elapsed_ms, error) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (trade_date, slot) DO UPDATE SET "
                " kind=EXCLUDED.kind, trigger=EXCLUDED.trigger, planned=EXCLUDED.planned, "
                " acted=EXCLUDED.acted, holds=EXCLUDED.holds, skipped=EXCLUDED.skipped, "
                " elapsed_ms=EXCLUDED.elapsed_ms, error=EXCLUDED.error, created_at=now()",
                (cycle_date, slot, kind, trigger, planned, acted, holds, skipped,
                 elapsed_ms, error[:2000]))
            conn.commit()
    except Exception as exc:
        print(f"[paper] record_cycle 失败: {exc}", flush=True)


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
                         bench_sym, hold_days, conf.get("fees"))
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
    # 目标模式：结算完净值就该判定目标（达标/到期）。放在快照之后——进度要用
    # 当日收盘总资产，提前判会用到昨天的数。失败不阻塞结算主流程。
    try:
        import paper_goal
        closed = paper_goal.evaluate_active(deps, notify=True)
        if closed:
            snap = {**(snap or {}), "goals_closed": [
                {"id": c["id"], "status": c["status"], "return_pct": c["return_pct"]}
                for c in closed]}
    except Exception as exc:
        print(f"[paper] 目标判定失败（不影响结算）: {exc}", flush=True)
    if notify_fn and settled:
        lines = "\n".join(
            f"{s['side']} {s['code']}：收益 {s['raw_return'] * 100:+.1f}%"
            + (f"，alpha {(s['alpha']) * 100:+.1f}%" if s.get("alpha") is not None else "")
            for s in settled)
        try:
            notify_fn("🧪 模拟交易结算", f"今日结算 {len(settled)} 笔：\n{lines}",
                      event="paper_settle")
        except Exception:
            pass
    return {"date": today, "settled": settled, "pending": pending, "equity": snap}


def _close_trade(deps, t, settle_date, settle_price, raw_ret, alpha, bench_sym, hold_days,
                 fee_conf: dict | None = None):
    """平仓落库：写反向成交行 + 回填结算字段 + 现金调整（**净回款要扣手续费**）。"""
    get_conn = deps["get_conn"]
    code, side, name = t["code"], t["side"], t["name"]
    shares = int(t["shares"] or 0)
    value = shares * settle_price if shares else 0
    # 平仓也是一笔真实卖出，同样有佣金+印花税+过户费
    fee = trade_fees(code, name, "sell" if side == "buy" else "buy", value, fee_conf)
    with get_conn() as conn:
        cur = conn.cursor()
        # 反向成交行（auto_close 标记到期强平；status='resolved' 不再进入结算队列）
        cur.execute(
            "INSERT INTO sa_paper_trades "
            "(code, name, trade_date, slot, side, shares, price, value, confidence, "
            " stop_loss_pct, reasoning, report, decision_raw, status, auto_closed, "
            " fee_total, fee_detail) "
            "VALUES (%s,%s,%s,'',%s,%s,%s,%s,NULL,NULL,%s,'',%s,'resolved',TRUE,%s,%s)",
            (code, name, settle_date, "sell" if side == "buy" else "buy",
             shares, settle_price, value, "到期自动平仓",
             json.dumps({"auto_close_of": t["id"]}, ensure_ascii=False),
             fee["total"], json.dumps(fee, ensure_ascii=False)))
        cur.execute(
            "UPDATE sa_paper_trades SET status = 'resolved', settle_date = %s, "
            "settle_price = %s, raw_return = %s, alpha_return = %s, benchmark = %s "
            "WHERE id = %s",
            (settle_date, settle_price, raw_ret, alpha, bench_sym, t["id"]))
        if side == "buy":       # 买入到期平仓：现金回流（净回款 = 成交额 - 费用）
            cur.execute("UPDATE sa_paper_account SET cash = cash + %s WHERE id = 1",
                        (value - fee["total"],))
        else:                   # 卖出到期回补：现金扣回（含费用）
            cur.execute("UPDATE sa_paper_account SET cash = cash - %s WHERE id = 1",
                        (value + fee["total"],))
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

def reconcile_account(deps, fix: bool = False) -> dict:
    """用成交流水重算现金，和账面 cash 对账。

    为什么需要：cash 是被增量 UPDATE 的（每次买减、每次卖加），不是从流水
    推导的。一旦某笔 UPDATE 因为并发/异常没落地，账面就会和流水永久对不上，
    而且没有任何地方会报警 —— 表现就是「总资产涨了但说不清是哪来的」
    （2026-09-28 实测差 5000，来自早期守护线程重复启动、两个循环并发下单的时期）。

    fix=False 只报告；fix=True 才把 cash 改写成流水推算值。
    """
    with deps["get_conn"]() as conn:
        cur = conn.cursor()
        cur.execute("SELECT initial_cash, cash FROM sa_paper_account WHERE id = 1")
        row = cur.fetchone()
        if not row:
            return {"ok": False, "error": "模拟账户不存在"}
        initial, cash_book = float(row[0]), float(row[1])
        cur.execute("""SELECT
              COALESCE(SUM(CASE WHEN side='buy'  THEN shares*price + fee_total END),0),
              COALESCE(SUM(CASE WHEN side='sell' THEN shares*price - fee_total END),0)
            FROM sa_paper_trades
            WHERE status <> 'skipped' AND side IN ('buy','sell')""")
        # 注意只能取一次：aggregate 查询只返回一行，第二次 fetchone() 是 None
        r = cur.fetchone()
        buy_sum, sell_sum = float(r[0]), float(r[1])
        cash_calc = round(initial - buy_sum + sell_sum, 2)
        drift = round(cash_book - cash_calc, 2)
        applied = False
        if fix and abs(drift) >= 0.01:
            cur.execute("UPDATE sa_paper_account SET cash = %s, updated_at = now() "
                        "WHERE id = 1", (cash_calc,))
            conn.commit()
            applied = True
    note = ("账面 cash 与成交流水一致 ✓" if abs(drift) < 0.01 else
            "账面比流水多 %.2f 元（早期并发下单时期的遗留，非当前代码路径）" % drift)
    return {"ok": True, "fixed": applied, "initial_cash": initial,
            "buy_total": round(buy_sum, 2), "sell_total": round(sell_sum, 2),
            "cash_book": round(cash_book, 2), "cash_calculated": cash_calc,
            "drift": drift, "note": note}


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
        # 统计：2026-09-29 重写。原来的口径有三个错，页面显示成「已结算 4 笔 /
        # 胜率 0%」：
        #   ① 按**行**统计，不按回合。一笔完整的「买入→卖出」会算成 2 行，
        #      而且卖出行本来就被排除在分母外，于是买入胜率和卖出胜率是
        #      两个互不相干的口径，页面却并排显示。
        #   ② wins 用 SUM(CASE WHEN alpha_return>0 ...)，而主动卖出路径
        #      （_settle_buy_rows）根本不写 alpha_return，全是 NULL。
        #      SQL 里 NULL>0 得 NULL，SUM 全 NULL 得 NULL → 前端 ||0 兜成 0
        #      → 胜率恒为 0%。这就是「胜率显示 0%」的直接原因。
        #   ③ 只取 status='resolved'，漏了 status='closed'（卖出平仓），
        #      于是已结算的笔数被系统性低估。
        # 现在：只认**已平仓的买入**（raw_return 非空，即有了结价），
        # 一行买入 = 一个回合，分母/分子都不再被 NULL 污染；
        # alpha 缺失时 wins 退回按 raw_return 判，并在返回值里标明口径。
        cur.execute("""
            SELECT COUNT(*)                                   AS n,
                   COUNT(*) FILTER (WHERE raw_return > 0)     AS wins_raw,
                   COUNT(*) FILTER (WHERE alpha_return > 0)   AS wins_alpha,
                   COUNT(*) FILTER (WHERE alpha_return IS NOT NULL) AS n_alpha,
                   AVG(raw_return)                            AS avg_raw,
                   AVG(alpha_return)                          AS avg_alpha
            FROM sa_paper_trades
            WHERE status IN ('resolved','closed') AND side = 'buy'
              AND raw_return IS NOT NULL""")
        r = cur.fetchone()
        n_all = int(r["n"] or 0)
        n_alpha = int(r["n_alpha"] or 0)
        # 有 alpha 就按 alpha 判（相对基准的胜负，这才是「胜率」该有的含义），
        # 全部缺失时退回 raw_return 判，并把 alpha_wins 置 0 提醒口径已降级。
        if n_alpha:
            wins = int(r["wins_alpha"] or 0)
            basis = "alpha"
        else:
            wins = int(r["wins_raw"] or 0)
            basis = "raw"
        avg_raw = float(r["avg_raw"]) if r["avg_raw"] is not None else None
        avg_alpha = float(r["avg_alpha"]) if r["avg_alpha"] is not None else None
        stats = {
            "n": n_all,
            "wins": wins,
            "avg_raw": avg_raw,
            "avg_alpha": avg_alpha,
            "n_alpha": n_alpha,
            # 前端据此显示「胜率(相对基准)」还是「胜率(绝对收益)」，
            # 避免把两种口径混为一谈。
            "win_basis": basis,
        }
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
    # account 里的 total_value 是数据库列，只在结算(_snapshot_equity)时刷新，
    # 所以盘中一直是陈的。页面拿它和 total / 持仓浮盈一起显示就会「对不上」
    # （2026-09-28 实发：卡片显示 100000、汇总显示 105244）。
    # 这里回填**实时**值，并把列里的旧值单独命名为 stored_total_value 备查。
    if account:
        account = {**account,
                   "stored_total_value": account.get("total_value"),
                   "total_value": round(total, 2)}
    unrealized = round(sum((p["market_value"] - p["shares"] * p["avg_cost"])
                           for p in pos_out if p.get("avg_cost")), 2)
    # 已实现 = 总盈亏 - 未实现。必须单独返回，否则页面只显示「持仓浮盈」时
    # 用户会发现它和「总资产涨幅」差一个数（差的就是平仓那部分）却无从解释。
    realized = round((total - account["initial_cash"]) - unrealized, 2) \
        if account and account.get("initial_cash") else 0.0
    try:
        # cap 来自 **config**，不是 sa_paper_account 那一行 —— 那一行只有
        # initial_cash/cash/total_value 三个字段，从里面取 max_positions 永远是 None
        cap = int((deps.get("conf") or {}).get("max_positions") or 0)
    except (TypeError, ValueError):
        cap = 0
    return {"account": account, "positions": pos_out,
            "market_value": round(market_value, 2), "total": round(total, 2),
            "equity": equity, "stats": stats,
            "positions_count": len(pos_out), "max_positions": cap,
            "positions_full": bool(cap > 0 and len(pos_out) >= cap),
            "positions_room": (max(0, cap - len(pos_out)) if cap > 0 else None),
            "position_pnl": unrealized, "realized_pnl": realized,
            "total_pnl": round(total - account["initial_cash"], 2)
            if account and account.get("initial_cash") else None,
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


# ---------------- 回合配对（页面「每笔交易」列表用） ----------------
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def rounds(deps: dict, code: str = "", only_closed: bool = False,
           only_open: bool = False) -> list[dict]:
    """配对出每笔模拟交易的完整过程。返回按最近活动倒序。

    only_closed / only_open 可分别只看已平仓 / 持有中。
    """
    get_conn = deps["get_conn"]
    sql = ("SELECT id, code, name, trade_date, slot, side, shares, price, value, "
           "fee_total, confidence, stop_loss_pct, reasoning, report, decision_raw, "
           "status, auto_closed, settle_date, settle_price, raw_return, "
           "alpha_return, benchmark "
           "FROM sa_paper_trades WHERE side IN ('buy','sell') "
           "AND status <> 'skipped'")
    params: list = []
    if code:
        sql += "AND code = %s "
        params.append(code)
    sql += "ORDER BY code, id"
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    # ---- 逐只股票独立先进先出核销 ----
    # 2026-09-29 修：第一版把**全表**按 id 顺序塞进同一个 FIFO 队列，结果
    # 512710 的卖出行 id=165 去核销了排在它前面的 000839 买入 id=10，
    # 于是国安股份@2.83 被算成「买入 512710@0.61 卖出」—— 显示成 +29571%。
    # 持仓是按 code 分别推导的（_derive_paper_positions），配对也必须按 code 分组，
    # 否则成交价对不上。ORDER BY code, id 让同code连续，再按 code 切分。
    closed: list[dict] = []
    by_code: dict[str, list[dict]] = {}
    for r in rows:
        by_code.setdefault(r["code"], []).append(r)

    for _code in sorted(by_code):
        closed.extend(_fifo_rounds(by_code[_code]))
    return _sort_rounds(closed, only_closed, only_open)


def _fifo_rounds(rows: list[dict]) -> list[dict]:
    """单只股票的先进先出配对（buy 先进队列，sell 从队首扣）。"""
    open_lots: list[dict] = []      # 未被卖掉的买入
    closed: list[dict] = []         # 已配对的片段
    for r in rows:
        shares = int(r["shares"] or 0)
        if shares <= 0:
            continue
        if r["side"] == "buy":
            open_lots.append(r)
            continue
        # sell：从最早的买入开始扣
        left = shares
        while left > 0 and open_lots:
            lot = open_lots[0]
            lot_sh = int(lot["shares"] or 0)
            take = min(lot_sh, left)
            if take < 1:
                open_lots.pop(0)
                continue
            entry_fee = float(lot["fee_total"] or 0)
            sell_fee = float(r["fee_total"] or 0)
            # 费用按股数分摊到本片段，净投入/净回款都算真实值
            e_in = entry_fee * take / lot_sh if lot_sh else 0.0
            s_out = sell_fee * take / shares if shares else 0.0
            net_in = take * float(lot["price"] or 0) + e_in
            net_out = take * float(r["price"] or 0) - s_out
            closed.append({
                "code": r["code"], "name": r["name"] or lot["name"],
                "entry": {
                    "id": lot["id"], "date": str(lot["trade_date"])[:10],
                    "slot": lot["slot"] or "", "price": float(lot["price"] or 0),
                    "shares": take, "fee": round(e_in, 2),
                    "net_in": round(net_in, 2),
                    "confidence": lot["confidence"],
                    "stop_loss_pct": (float(lot["stop_loss_pct"])
                                      if lot["stop_loss_pct"] is not None else None),
                    "reasoning": lot["reasoning"] or "",
                    "report": (lot["report"] or "")[:1200],
                },
                "exit": {
                    "id": r["id"], "date": str(r["trade_date"])[:10],
                    "slot": r["slot"] or "", "price": float(r["price"] or 0),
                    "shares": take, "fee": round(s_out, 2),
                    "net_out": round(net_out, 2),
                    "confidence": r["confidence"],
                    "reasoning": r["reasoning"] or "",
                },
                "pnl": round(net_out - net_in, 2),
                "pnl_pct": round((net_out / net_in - 1) * 100, 2) if net_in else None,
                "raw_return": (float(lot["raw_return"])
                               if lot["raw_return"] is not None else None),
                "alpha_return": (float(lot["alpha_return"])
                                 if lot["alpha_return"] is not None else None),
                "benchmark": lot["benchmark"] or "",
                "settled": bool(lot["settle_date"]),
                "auto_closed": bool(lot["auto_closed"] or r["auto_closed"]),
                "_last": str(r["trade_date"]),
            })
            left -= take
            if take >= lot_sh:
                open_lots.pop(0)
            else:
                lot["shares"] = lot_sh - take
                lot["fee_total"] = entry_fee - e_in
                break
    # ---- 剩余未卖出的买入 = 持有中的回合 ----
    for lot in open_lots:
        sh = int(lot["shares"] or 0)
        if sh <= 0:
            continue
        fee = float(lot["fee_total"] or 0)
        closed.append({
            "code": lot["code"], "name": lot["name"],
            "entry": {
                "id": lot["id"], "date": str(lot["trade_date"])[:10],
                "slot": lot["slot"] or "", "price": float(lot["price"] or 0),
                "shares": sh, "fee": round(fee, 2),
                "net_in": round(sh * float(lot["price"] or 0) + fee, 2),
                "confidence": lot["confidence"],
                "stop_loss_pct": (float(lot["stop_loss_pct"])
                                  if lot["stop_loss_pct"] is not None else None),
                "reasoning": lot["reasoning"] or "",
                "report": (lot["report"] or "")[:1200],
            },
            "exit": None,
            "pnl": None, "pnl_pct": None,
            "raw_return": (float(lot["raw_return"])
                           if lot["raw_return"] is not None else None),
            "alpha_return": (float(lot["alpha_return"])
                             if lot["alpha_return"] is not None else None),
            "benchmark": lot["benchmark"] or "",
            "settled": bool(lot["settle_date"]),
            "auto_closed": bool(lot["auto_closed"]),
            "holding_days": _tdays(str(lot["trade_date"])[:10], today()),
            "_last": str(lot["trade_date"]),
        })
    return closed


def _sort_rounds(closed: list[dict], only_closed: bool,
                 only_open: bool) -> list[dict]:
    """过滤 + 计算持有天数 + 按最近活动倒序。"""
    if only_closed:
        closed = [r for r in closed if r["exit"]]
    if only_open:
        closed = [r for r in closed if not r["exit"]]
    for r in closed:
        if r["exit"]:
            r["holding_days"] = _tdays(r["entry"]["date"], r["exit"]["date"])
        r.pop("_last", None)
    closed.sort(key=lambda r: (r["exit"]["date"] if r["exit"] else r["entry"]["date"]),
                reverse=True)
    return closed


def _tdays(d1: str, d2: str) -> int:
    """两个日期之间的自然日数（>=0）。解析失败返回 None。"""
    try:
        a = date.fromisoformat(str(d1)[:10])
        b = date.fromisoformat(str(d2)[:10])
        return max(0, (b - a).days)
    except (ValueError, TypeError):
        return None


def today() -> date:
    return datetime.now().date()
