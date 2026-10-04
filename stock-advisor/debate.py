"""Bull/Bear/Risk-Critic 对抗模块：TradingAgents-astock 多角色对抗的本地移植版。

设计（对应 TradingAgents-astock 的 researchers/ + research_manager/ + risk_mgmt/）：
- Bull Researcher：看多分析师，用 A 股特色论据（政策顺风、北向资金、游资接力等）
- Bear Researcher：看空分析师，用 A 股特色风险（政策逆风、解禁、T+1 陷阱等）
- Risk Critic：风险辩手，**专盯结构性风险**（2026-10-04 加）
- Research Manager：裁判，综合辩论给出投资计划（Buy/Hold/Sell + 理由）

## 为什么加 Risk Critic（实测驱动的补丁）

2026-10-04 跑 15 只持仓时发现 `603718 *ST海利`：Bull/Bear 辩论后
**Judge 给了 Buy**。一个带 *ST（退市风险警示）的亏损股在辩论里被判 Buy ——
说明 Bull/Bear 的框架里**没有专门讲 A 股结构性风险**，辩手会把它当
普通亏损股讨论。

TradingAgents 原版把这块放在 `risk_mgmt/conservative_debator.py`
（专讲 T+1 锁定、涨跌停无法出场、ST/退市、质押爆仓），我最初移植时
只带了 researchers/ 里的 Bull/Bear，**漏了这一层**。Risk Critic 把它补回来，
且只做「找风险」这一件事，不参与多空方向判断。

**成本**：默认只在「名称含 ST/*ST/退市」等高风险票上自动插入，
普通票不额外花钱（`risk_critic_always: false`）。

本模块不 import app（避免循环依赖）：LLM 调用通过 llm_advisor.ask() 走统一出口。
配置项在 config.yaml 的 paper_debate 段：
  enabled: false          # 总开关
  max_rounds: 1           # 辩论轮次（1 = Bull→Bear→Manager，2 = 多一轮）
  trigger_codes: ""       # 逗号分隔的代码白名单；空 = 对所有票启用
  trigger_on_buy: true    # 是否对 buy 决策启用辩论
  trigger_on_sell: true   # 是否对 sell 决策启用辩论
  min_confidence: 5       # 交易员 confidence >= 此值时才触发辩论（0=不限）
  risk_critic: true       # 是否启用风险辩手（ST/退市/质押等结构性风险）
  risk_critic_always: false   # True=每只票都跑；False=仅 ST/*ST 等高风险票
  enforce_rating: false  # Judge 强信号评级是否约束最终动作（见 apply_rating_veto）
"""

import re
from typing import Callable

import llm_advisor

# 辩论轮次对应的 LLM 调用次数（Bull + Bear + Manager = 3 次/轮）
DEBATE_LLM_COST_PER_ROUND = 3
# 风险辩手额外 1 次调用（仅高风险票）
RISK_CRITIC_LLM_COST = 1

# Judge 评级里视为「强信号」的方向。用于 enforce_rating。
BEARISH_RATINGS = frozenset({"Sell", "Underweight"})
BULLISH_RATINGS = frozenset({"Buy", "Overweight"})


def load_debate_conf(conf: dict) -> dict:
    """从 paper_trading 的 conf 中读取辩论配置，补齐默认值。"""
    defaults = {
        "enabled": False,
        "max_rounds": 1,
        "trigger_codes": "",
        "trigger_on_buy": True,
        "trigger_on_sell": True,
        "min_confidence": 5,
        "risk_critic": True,
        "risk_critic_always": False,
        "enforce_rating": False,
    }
    section = conf.get("paper_debate") or {}
    defaults.update({k: v for k, v in section.items() if v is not None})
    return defaults


def needs_risk_critic(debate_conf: dict, code: str, name: str) -> bool:
    """这只票是否需要跑风险辩手。

    默认只对**高风险票**跑（省 LLM 调用）：名称含 ST / *ST / 退市 / 暂停上市，
    或代码是 4/8 开头（北交所老代码段，流动性与信息质量都弱）。
    `risk_critic_always: true` 时对所有票跑。
    """
    if not debate_conf.get("risk_critic"):
        return False
    if debate_conf.get("risk_critic_always"):
        return True
    nm = str(name or "").upper().replace(" ", "")
    if "ST" in nm or "退市" in nm or "暂停" in nm:
        return True
    c = str(code or "")
    return bool(c and c[0] in "48")


def should_debate(debate_conf: dict, code: str, action: str, confidence: int) -> bool:
    """判断是否应该对本次决策启用辩论。"""
    if not debate_conf.get("enabled"):
        return False
    # 白名单过滤
    codes = (debate_conf.get("trigger_codes") or "").strip()
    if codes:
        allowed = {c.strip() for c in codes.split(",") if c.strip()}
        if code not in allowed:
            return False
    # 按 action 过滤
    if action == "buy" and not debate_conf.get("trigger_on_buy"):
        return False
    if action == "sell" and not debate_conf.get("trigger_on_sell"):
        return False
    # 按 confidence 过滤
    min_conf = int(debate_conf.get("min_confidence") or 0)
    if min_conf > 0 and confidence < min_conf:
        return False
    return True


def _compact_history(history: str, max_chars: int = 4000) -> str:
    """截断过长的辩论历史，防止 prompt 爆炸。"""
    if len(history) <= max_chars:
        return history
    return "...[前略]..." + history[-max_chars:]


# Risk Critic 的提示词：对应 TradingAgents risk_mgmt/conservative_debator.py，
# 但**只输出风险清单**，不给方向判断 —— 方向由 Judge 综合后给。
RISK_CRITIC_PROMPT = """\
你是 A 股风险审查员，职责**只有一个**：找出这只股票的结构性风险。
你不做多空方向判断，不给投资建议，只列风险点。

Stock: {name}（{code}）

【必须逐条检查的 A 股结构性风险，每条给出「是否命中」+ 一句依据】
1. **ST / *ST / 退市风险**：是否带 ST 或 *ST 标记？连续亏损？
   若是，说明：① 退市路径是什么（财务类/交易类/规范类）；
   ② 风险警示板权限限制（普通投资者可能无法买入）；
   ③ 是否被剔出两融标的（流动性枯竭）；
   ④ 注意 ST/*ST 的日涨跌幅**不更窄**（主板 ST 自 2026-07-06 起也是 ±10%，
   科创/创业板 ST 一直是 ±20%）—— 危险不在涨跌幅，在退市路径和买方萎缩。
2. **T+1 锁定**：当日买入不可当日卖出。若次日凌晨有利空/跳空，损失锁定，
   **无法当日止损**，这是 A 股最重要的结构性风险。
3. **跌停无法出场**：主板 ±10%、科创/创业板 ±20%、北交所 ±30%。
   跌停时买盘通常为空，卖单排队难成交。注意 2026-07-06 起收盘后
   15:05-15:30 有按收盘价的盘后固定价格交易覆盖所有 A 股 ——
   所以不是「绝对卖不掉」，而是「仍需找到对手盘，而跌停日正是没有对手盘的时候」。
4. **控股股东股权质押**：质押比例过高（>30%）时，股价下跌会触发平仓，
   形成「下跌 → 平仓 → 更多下跌」的正反馈。
5. **基本面恶化**：是否已亏损（PE 为负）？营收/扣非净利是否连续下滑？
   应收账款周转天数是否恶化？经营性现金流是否为负？
6. **流动性风险**：日均成交额是否过小（游资才能推动 = 机构无法退出）？
7. **估值陷阱**：周期股在盈利峰值给低 PE（"便宜"可能是顶部信号）。

【本票上下文】
{ctx}

【已知的事实】
- 最新价与成本、持仓股数见上下文
- 若名称含 ST 或 *ST，请在第 1 条明确写出所命中的具体风险

只输出一个 Markdown 表格，列为：风险项 | 是否命中 | 依据（引用上下文具体数字）。
命中写「命中」，不命中写「不适用」。表格后用 3-5 句总结**最致命的那一条**
（若全部不适用，写「未发现结构性风险」）。全部用中文，不要客套。"""


def run_risk_critic(
    ctx: str,
    code: str,
    name: str,
    llm_call: Callable[[str, str, int], str] | None = None,
) -> str:
    """跑一次风险审查。返回风险清单文本（Markdown）。

    与 Bull/Bear 不同，这个角色**不给方向**——它只负责把 A 股结构性风险
    摆到台面上，让 Judge 在综合时看到。2026-10-04 的动机：*ST 海利在
    只有 Bull/Bear 的情况下被 Judge 判了 Buy。
    """
    if llm_call is None:
        llm_call = llm_advisor.ask
    prompt = RISK_CRITIC_PROMPT.format(name=name, code=code, ctx=_compact_history(ctx))
    return llm_call(prompt, "", max_tokens=900)


def run_debate(
    report: str,
    ctx: str,
    code: str,
    name: str,
    max_rounds: int = 1,
    llm_call: Callable[[str, str, int], str] | None = None,
    risk_critic_check: bool = False,
    risk_critic_text: str = "",
) -> dict:
    """运行 Bull/Bear 辩论，返回辩论结果。

    Args:
        report: 分析师报告（来自 paper_trading 的 ANALYST_PROMPT 输出）
        ctx: 完整决策上下文（含行情、新闻、经验等）
        code: 股票代码
        name: 股票名称
        max_rounds: 辩论轮次
        llm_call: LLM 调用函数，默认用 llm_advisor.ask()
        risk_critic_check: True 时在本函数内跑一次 Risk Critic（+1 次调用）
        risk_critic_text: 已有的风险清单文本（跑过了就别再跑一次）

    Returns:
        {
            "bull_history": str,      # 看多论点全文
            "bear_history": str,      # 看空论点全文
            "risk_report": str,       # 风险清单（Risk Critic 输出，可能为空）
            "history": str,           # 完整辩论记录
            "judge_decision": str,    # Research Manager 的投资计划
            "rounds": int,            # 实际轮次
            "llm_calls": int,         # LLM 调用次数
        }
    """
    if llm_call is None:
        llm_call = llm_advisor.ask

    bull_history = ""
    bear_history = ""
    history = ""
    risk_report = ""
    llm_calls = 0

    # --- Risk Critic（可选，只做风险清单，不参与多空方向）---
    # 放在最前面：先摆风险，Bull/Bear 辩手看到风险清单后，
    # 就不太容易把 ST/退市风险票当成普通亏损股来乐观辩论。
    if risk_critic_text or risk_critic_check:
        try:
            risk_report = run_risk_critic(ctx, code, name, llm_call=llm_call)
            llm_calls += RISK_CRITIC_LLM_COST
        except Exception:
            # 风险审查失败**不阻断**辩论（它只是补充材料）
            risk_report = ""

    # Risk Critic 的清单要在 Bull/Bear/Judge 三处都给出。
    # 给 Bull 时措辞中立（事实清单），但**明确要求它正面回应命中的风险** ——
    # 2026-10-04 的 *ST 海利被 Judge 判 Buy，就是因为辩手没意识到这是 ST 股。
    risk_block = ""
    if risk_report:
        risk_block = (
            "【风险审查员已标注的结构性风险（必须正面回应，不得回避）】\n"
            + _compact_history(risk_report, 2000)
            + "\n若你认为某条风险不成立或已被price in，必须**明确说明理由**；"
              "不要无视它直接论证看多。\n")

    for round_idx in range(max_rounds):
        # --- Bull Researcher ---
        bull_prompt = f"""You are a Bull Analyst advocating for investing in this A-share (China mainland) stock. Your task is to build a strong, evidence-based case emphasizing growth potential, competitive advantages, and positive market indicators.

A-Share Bull Framework — prioritize these China-specific bullish catalysts:
- Policy Tailwinds: Government subsidies, industry support policies, favorable regulatory signals
- Northbound Capital (北向资金): Sustained net inflow from Hong Kong Stock Connect
- Hot Money Momentum (游资接力): Consecutive limit-ups with volume confirmation, strong theme attribution
- Valuation Growth Story: Use forward PE, PEG to argue the current premium is justified
- Lockup Expiry Cleared: If major lockup periods have passed, this removes a key overhang

General bull points:
- Growth Potential: Market opportunities, revenue projections, and scalability
- Competitive Advantages: Unique products, dominant market positioning
- Positive Indicators: Financial health, industry trends, and recent positive news
- Bear Counterpoints: Critically analyze the bear argument with specific data

Stock: {name}（{code}）

Analyst Report:
{report}

Context (market data, news, events):
{_compact_history(ctx)}

{risk_block}
{_compact_history(f"Previous bull arguments: {bull_history}") if bull_history else ""}
{_compact_history(f"Previous bear arguments to counter: {bear_history}") if bear_history else ""}

Deliver a compelling bull argument that integrates A-share market dynamics. Refute the bear's concerns. Output in Chinese, conversational, no special formatting."""

        bull_resp = llm_call(bull_prompt, "", max_tokens=1500)
        llm_calls += 1
        bull_argument = f"Bull Analyst: {bull_resp}"
        bull_history += "\n" + bull_argument
        history += "\n" + bull_argument

        # --- Bear Researcher ---
        bear_prompt = f"""You are a Bear Analyst making the case against investing in this A-share (China mainland) stock. Your goal is to present a well-reasoned argument emphasizing risks, challenges, and negative indicators.

A-Share Bear Framework — prioritize these China-specific risk factors:
- Policy Headwinds: Sudden regulatory crackdowns, CSRC window guidance, sector-wide trading restrictions
- Lockup & Insider Selling: Upcoming lockup expiry dates, equity pledge liquidation risk
- Hot Money Withdrawal (游资撤退): Volume divergence after limit-ups, declining limit-up board count
- Valuation Bubble: PE far above 30x A-stock growth anchor, PEG > 2
- T+1 Trap: After a sharp rally, buyers today cannot exit until tomorrow
- Northbound Retreat: Net outflow from Stock Connect

General bear points:
- Risks and Challenges: Market saturation, financial instability, macroeconomic threats
- Competitive Weaknesses: Weaker market positioning, declining innovation
- Negative Indicators: Evidence from financial data, market trends, adverse news
- Bull Counterpoints: Expose over-optimistic assumptions with specific data

Stock: {name}（{code}）

Analyst Report:
{report}

Context (market data, news, events):
{_compact_history(ctx)}

{risk_block}
{_compact_history(f"Previous bear arguments: {bear_history}") if bear_history else ""}
{_compact_history(f"Previous bull arguments to counter: {bull_history}") if bull_history else ""}

Deliver a compelling bear argument grounded in A-share market realities. Refute the bull's claims. Output in Chinese, conversational, no special formatting."""

        bear_resp = llm_call(bear_prompt, "", max_tokens=1500)
        llm_calls += 1
        bear_argument = f"Bear Analyst: {bear_resp}"
        bear_history += "\n" + bear_argument
        history += "\n" + bear_argument

    # --- Research Manager (裁判) ---
    # 注意这里的 rating 纪律措辞：**Hold 不是「不确定时的逃逸口**。
    # 2026-10-04 的 *ST 海利被判 Buy 就是这么来的：辩手把 ST 股当普通亏损股
    # 讨论，Judge 又被「commit to a clear stance」推着远离 Hold。
    # 结构性风险（ST/退市/质押爆仓/跌停无法出场）是**否决项**，不是扣分项。
    veto_rule = (
        "【结构性风险 = 否决项，不是扣分项】\n"
        "若下面的风险审查清单里有任何一条命中「ST/*ST/退市路径」「控股股东质押过高」"
        "「跌停无法出场且已接近跌停」「持续亏损且无现金流支撑」，"
        "则**不得**给出 Buy 或 Overweight —— 直接 Underweight 或 Sell，"
        "并在 Reasoning 里点名是哪一条否决的。\n"
        "「跌停卖不掉」「T+1 无法当日止损」这类结构性缺陷不因股价便宜而被抵消。\n"
        if risk_report else "")
    manager_prompt = f"""As the Research Manager and debate facilitator, critically evaluate this round of debate and deliver a clear, actionable investment plan for the trader.

Stock: {name}（{code}）

Rating Scale (use exactly one):
- Buy: Strong conviction in the bull thesis; recommend taking or growing the position
- Overweight: Constructive view; recommend gradually increasing exposure
- Hold: Balanced view; recommend maintaining the current position
- Underweight: Cautious view; recommend trimming exposure
- Sell: Strong conviction in the bear thesis; recommend exiting or avoiding the position

Be decisive — but do not let decisiveness override the structural-risk veto below.
Reserve Hold ONLY when the evidence is genuinely balanced; a vague "maybe" is not a reason.

{veto_rule}{risk_block}
Debate History:
{_compact_history(history)}

Output format (strict):
你的**第一行**必须是 `Rating: X`（X 是上面五个之一），然后空一行，再写 Reasoning。
**不要**在 Rating 行之前输出任何内容 —— 不要写 "Let me analyze"、不要复述本 prompt、
不要以 "The user wants" 或 "As the Research Manager" 开头。
Reasoning: [2-4 sentences summarizing the strongest arguments from both sides and your conclusion]

Output in Chinese."""

    manager_resp = llm_call(manager_prompt, "", max_tokens=800)
    llm_calls += 1

    return {
        "bull_history": bull_history.strip(),
        "bear_history": bear_history.strip(),
        "risk_report": (risk_report or "").strip(),
        "history": history.strip(),
        "judge_decision": manager_resp.strip(),
        "rounds": max_rounds,
        "llm_calls": llm_calls,
    }


def _strip_prompt_leakage(text: str) -> str:
    """剥离 Judge 输出开头的 prompt 泄漏。

    nemotron 模型有时把 prompt 指令当输出开头（2026-10-04 实测：
    600406/603718 的 judge_decision 以 "The user wants me to act as a
    Research Manager..." 开头，3447 字符里没有 `Rating:` 行）。
    泄漏会污染兜底匹配（前 400 字符里全是 prompt 指令），必须先剥掉。
    """
    # 泄漏的常见开头（prompt 的前几句被复述）
    leak_markers = (
        "the user wants me to",
        "as the research manager",
        "let me analyze",
        "i need to provide",
        "i'll evaluate",
        "i will evaluate",
    )
    low = text.lower()
    for marker in leak_markers:
        if low.startswith(marker):
            # 找到泄漏结束的位置：第一个换行后的正文，或 Rating: 行
            # 策略：从 "Rating:" 行开始截取；没有则取第一个空行之后
            m = re.search(r"Rating\s*[:：]", text, re.I)
            if m:
                return text[m.start():]
            # 没有 Rating: 行 —— 取前 3 个换行之后的内容（跳过泄漏段）
            parts = text.split("\n\n", 3)
            if len(parts) >= 3:
                return parts[-1]
            return text
    return text


def parse_judge_rating(judge_decision: str) -> str:
    """从 Research Manager 的输出中提取评级。

    顺序有讲究：**先匹配更长的词**（Underweight / Overweight），再匹配
    Buy / Sell / Hold。原来的写法是「先查 'buy' in text」，而
    `Underweight` 之外，英文正文里出现 "buy"（如 "buy the dip"）、
    "sell"（如 "sell-off"）都会误判 —— 这是**在自由文本里做子串匹配**，
    判据必须是 `Rating:` 那一行的精确值。

    2026-10-04 加：先剥离 prompt 泄漏（`_strip_prompt_leakage`），
    否则泄漏的 prompt 指令会污染前 400 字符的兜底匹配。
    """
    text = _strip_prompt_leakage(judge_decision)
    # 1) 优先取 `Rating: X` 这一行（prompt 明确要求输出这一行）
    m = re.search(r"Rating\s*[:：]\s*\**\s*"
                  r"(Overweight|Underweight|Buy|Sell|Hold)", text, re.I)
    if m:
        raw = m.group(1).lower()
        return {"overweight": "Overweight", "underweight": "Underweight",
                "buy": "Buy", "sell": "Sell", "hold": "Hold"}[raw]
    # 2) 退而求其次：独立成词的长词优先（用词边界，避免 sell-off 误命中）
    #    只在剥离泄漏后的正文里找，且要求出现在前 200 字符（正文开头）
    head = text[:200].lower()
    for word, rating in (("underweight", "Underweight"), ("overweight", "Overweight")):
        if re.search(rf"\b{word}\b", head):
            return rating
    if re.search(r"\bbuy\b", head):
        return "Buy"
    if re.search(r"\bsell\b", head):
        return "Sell"
    return "Hold"


def apply_rating_veto(debate_conf: dict, rating: str, action: str,
                     has_position: bool) -> tuple[str, str]:
    """`enforce_rating` 打开时，把强信号评级变成对最终动作的**硬约束**。

    返回 (最终动作, 说明)。`enforce_rating` 关闭时原样返回（纯观察）。

    ## 为什么需要它（实测）

    2026-10-04：600406 国电南瑞、000858 五粮液、002457 青龙管业
    三只的 Judge 评级分别是 Sell / Underweight / Sell，
    **但交易员复判后动作全是 hold** —— 辩论成了「参考意见」，
    没有任何约束力。加这个开关把评级变成闸门。

    ## 两条设计约束（都不是随意选的）

    1. **只拦「与评级方向相反」的动作，不代替交易员决策**。
       Judge 判 Sell 而交易员想 buy → 拦（降级为 hold）。
       Judge 判 Sell 而交易员想 hold → **不拦**：hold 已经是不加仓，
       代码层还有 `_execute_decision` 的资金/T+1/持仓数硬约束兜底。
    2. **只拦 buy/sell 这类「新增风险敞口」的动作**。
       已经持仓时，Judge 判 Buy 不该逼着加仓（要加仓交给资金上限管），
       所以 BULLISH 评级**不产生否决**，只有 BEARISH 评级否决加仓。

    这是「风控必须在 LLM 之外」的延续 —— 见 AGENTS.md 关于
    `stop_loss_max_pct` 那条：把约束写在提示词里 LLM 可以自己论证掉它。
    """
    if not debate_conf.get("enforce_rating"):
        return action, ""

    rating_n = str(rating or "").strip().capitalize()
    # 规整：Overweight/Underweight 的大小写
    rating_n = {"overweight": "Overweight", "underweight": "Underweight"}.get(
        rating_n.lower(), rating_n)

    if rating_n in BEARISH_RATINGS and action == "buy":
        return "hold", (f"辩论 Judge 评级 {rating_n}（强看空），否决加仓 → 降级 hold")
    if rating_n in BEARISH_RATINGS and action == "sell" and not has_position:
        return "hold", (f"辩论 Judge 评级 {rating_n}（强看空），但无持仓可卖 → hold")
    return action, ""


def format_debate_for_trader(debate_result: dict) -> str:
    """把辩论结果格式化成交易员 prompt 的附加段。"""
    rating = parse_judge_rating(debate_result["judge_decision"])
    risk = debate_result.get("risk_report") or ""
    parts = [
        f"\n\n【多空辩论结果（{debate_result['rounds']} 轮，评级 {rating}）】",
        debate_result["judge_decision"],
    ]
    if risk:
        parts.append("\n【风险审查员标注的结构性风险】\n" + risk[:1800])
    parts.append("\n【看多要点】\n" + debate_result["bull_history"][:1500])
    parts.append("\n【看空要点】\n" + debate_result["bear_history"][:1500])
    veto = debate_result.get("veto_note")
    if veto:
        parts.append(f"\n【系统硬约束】\n{veto}")
    return "\n".join(parts)
