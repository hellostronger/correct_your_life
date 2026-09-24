"""LLM 提款建议模块：把提款计划的达成数据喂给 Claude，生成个性化建议。

设计：
- 配置存 config.yaml 的 llm 段（api_key/base_url/model/enabled/auto_advice），
  网页「提款计划」页可改；api_key 留空时兜底读环境变量 ANTHROPIC_API_KEY。
- 建议文本存云库 sa_plan_advice（一计划多条历史），页面取最新一条。
- 调用走 anthropic 官方 SDK（1.x）。模型默认 claude-opus-5；按官方要求
  默认开启服务端 fallbacks（按拒答类别自动改路 Claude Opus 4.8），并在读取
  content 前检查 stop_reason == "refusal"。
- 本模块不 import app（避免循环依赖）：上下文由 app.py 组装好传进来。

零成本提醒：调用产生 API 费用；auto_advice 每日每计划至多一次，不会盘中反复触发。
"""

import os
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.yaml"

# 默认模型用 Claude Opus 5（Anthropic 最新主力，1M 上下文）。
DEFAULT_LLM_CONF = {
    "enabled": False,
    "api_key": "",
    "base_url": "",        # 留空 = Anthropic 官方端点；代理/网关在此填
    "model": "claude-opus-5",
    "auto_advice": False,  # 盘后里程碑推送后自动补一条 AI 建议（每日至多一次）
}

SYSTEM_PROMPT = """\
你是「提款计划」场景的个人持仓顾问，服务于一个本地股票管理系统。用户会给你一段
纯文本上下文：提款计划（目标金额/截止日/进度/系统达成路径与卖出凑钱建议）、
当前持仓明细（成本/现价/浮动盈亏/量能倍数）、自选观察池（未持仓的潜在买入候选，
含行情/市值/量能/行业板块）、板块轮动（评分靠前板块与持仓股板块归属）、
未来数日解禁/增发事件、近期相关新闻标题、大盘量能与指数概况。

请基于且仅基于这些数据，输出一份 Markdown 操作建议，结构固定为：

### 📌 现状判断
（提款达成难度与时间充裕度、组合整体健康度，一两段）

### 💰 卖出建议
（逐只：卖谁 → 为什么 → 建议数量与预计回笼资金 → 时点。优先兑现浮盈高且
量能萎缩/板块走弱的持仓；临近解禁（供给冲击）或增发新股上市（摊薄）的
持仓考虑提前处理；以系统 sell_plan 为基础调整时说明理由）

### 📥 买入建议
（从自选观察池出发：买谁 → 建议投入金额（须留足提款所需，不得为买入挪走
凑钱资金）→ 理由（板块轮动评分靠前、放量、消息面等）。没有合适机会就明确
写「暂无，建议持币」，不要硬凑）

### ⚠️ 风险点
（最多 5 条：集中度、事件冲击、行情依赖、流动性等）

### 🔁 备选路径
（目标不现实时：降目标/延日期/场外补本金的量化权衡）

要求：
- 数字必须来自上下文，禁止编造行情或预测具体点位；不确定就说不确定。
- 每条建议尽量指回上下文里的具体数据（如「浮盈 +12%」「板块评分第 3」「10-14 解禁」）。
- 中文、口语化但专业；总长 700 字以内。
- 结尾单独一行：> ⚠️ 以上由 AI 依据持仓数据生成，仅供参考，不构成投资建议。
"""


def load_llm_conf() -> dict:
    """读 config.yaml 的 llm 段，补齐默认值（每轮现读，改配置即生效）。"""
    conf = dict(DEFAULT_LLM_CONF)
    try:
        text = CONFIG_FILE.read_text(encoding="utf-8") if CONFIG_FILE.exists() else ""
        if text:
            import yaml
            data = yaml.safe_load(text) or {}
            section = data.get("llm") or {}
            conf.update({k: v for k, v in section.items() if v is not None})
    except Exception:
        pass
    if not conf["api_key"]:
        conf["api_key"] = os.environ.get("ANTHROPIC_API_KEY", "")
    return conf


def _render_llm_block(conf: dict) -> str:
    def q(v) -> str:
        s = str(v if v is not None else "")
        return "'" + s.replace("'", "''") + "'"   # YAML 单引号标量转义
    return "\n".join([
        "# LLM 提款建议（Anthropic Claude API；api_key 留空则读环境变量 ANTHROPIC_API_KEY）",
        "# enabled=允许网页手动生成；auto_advice=盘后里程碑提醒后自动补一条建议（每日每计划至多一次）",
        "llm:",
        f"  enabled: {str(bool(conf.get('enabled'))).lower()}",
        f"  api_key: {q(conf.get('api_key', ''))}",
        f"  base_url: {q(conf.get('base_url', ''))}",
        f"  model: {q(conf.get('model') or DEFAULT_LLM_CONF['model'])}",
        f"  auto_advice: {str(bool(conf.get('auto_advice'))).lower()}",
    ])


def save_llm_conf(conf: dict) -> None:
    """把 llm 段写回 config.yaml（仿 notify 段的整块替换，其余内容原样保留）。"""
    text = CONFIG_FILE.read_text(encoding="utf-8") if CONFIG_FILE.exists() else ""
    block = _render_llm_block(conf)
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.rstrip() == "llm:"), None)
    if start is not None:
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if lines[j] and not lines[j][0].isspace() and not lines[j].startswith("#"):
                end = j
                break
        while start > 0 and (lines[start - 1].startswith("#") or not lines[start - 1].strip()):
            start -= 1
        lines[start:end] = [""] + block.splitlines()
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(block.splitlines())
    CONFIG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def mask_key(key: str) -> dict:
    """给 GET 接口用：不回显完整 key，只报是否已配置 + 尾 4 位。"""
    return {"has_key": bool(key), "key_tail": key[-4:] if key else ""}


def ask_advice(context_text: str, conf: dict | None = None) -> str:
    """调 Claude 生成提款建议，返回 Markdown 文本。失败抛 RuntimeError。"""
    conf = conf or load_llm_conf()
    if not conf["api_key"]:
        raise RuntimeError("未配置 Anthropic API key（config.yaml llm.api_key "
                           "或环境变量 ANTHROPIC_API_KEY）")
    client_kwargs = {"api_key": conf["api_key"], "timeout": 120.0, "max_retries": 2}
    if conf.get("base_url"):
        client_kwargs["base_url"] = conf["base_url"]
    messages = [{"role": "user",
                 "content": f"以下是本次提款计划的完整上下文，请生成建议：\n\n{context_text}"}]
    try:
        from anthropic import Anthropic
    except ImportError as exc:
        raise RuntimeError("anthropic SDK 未安装：pip install 'anthropic>=1.5'") from exc
    client = Anthropic(**client_kwargs)
    msg = None
    errors = []
    # 逐级降级：带 thinking/betas/fallbacks 的完整特性仅 Anthropic 官方端点支持，
    # 代理网关（如 api.b.ai）会挂到超时——base_url 非空时直接走最简调用。
    attempts = []
    if not conf.get("base_url"):
        attempts.append(("full", dict(thinking={"type": "adaptive"},  # 权衡买卖顺序，开自适应思考
                                      betas=["server-side-fallback-2026-07-01"],  # 拒答自动改路 Opus 4.8
                                      fallbacks="default")))
    attempts.append(("basic", None))
    for label, kwargs in attempts:
        try:
            if label == "full":
                # betas/fallbacks 仅 beta.messages 命名空间支持（SDK 1.5 实测）
                msg = client.beta.messages.create(model=conf["model"], max_tokens=4096,
                                                  system=SYSTEM_PROMPT, messages=messages,
                                                  **kwargs)
            else:
                msg = client.messages.create(model=conf["model"], max_tokens=4096,
                                             system=SYSTEM_PROMPT, messages=messages)
            break
        except Exception as exc:
            errors.append(f"{label}: {type(exc).__name__}: {exc}")
    if msg is None:
        raise RuntimeError(f"Claude API 调用失败: {' | '.join(errors)}")
    if msg.stop_reason == "refusal":
        raise RuntimeError("请求被安全策略拒绝（stop_reason=refusal），请调整上下文后重试")
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    if not text:
        raise RuntimeError(f"Claude 返回空内容（stop_reason={msg.stop_reason}）")
    return text


def build_context_text(view: dict, held: list[dict], news_titles: list[str],
                       market_note: str, watch_lines: list[str] | None = None,
                       event_lines: list[str] | None = None,
                       sector_lines: list[str] | None = None) -> str:
    """把计划视图 + 持仓明细 + 新闻 + 市场概况拼成喂给模型的纯文本。

    可选增强段（app.py 组好格式化行传进来，None/空 = 省略该段）：
    watch_lines 自选观察池（买入候选）、event_lines 未来解禁/增发事件、
    sector_lines 板块轮动与个股板块归属。
    """
    w, d = view["withdrawn"], float(view["target_amount"])
    lines = [
        f"【计划】截止日 {view['target_date']}（剩 {view['trading_days_left']} 个交易日），"
        f"目标提取 {d:,.0f} 元；已提 {w:,.0f} 元（{w / d * 100:.0f}%），还需 {view['need_now']:,.0f} 元。",
        f"【系统判定】{view.get('difficulty') or '—'}；持仓总市值（折人民币）{view['holdings_mv']:,.0f} 元，"
        + (f"缺口 {view['gap']:,.0f} 元，需组合上涨 {view.get('required_total_pct'):g}%。"
           if view.get("gap", 0) > 0 and view.get("required_total_pct") is not None
           else "市值已覆盖目标，直接卖出凑钱即可。"),
        f"【市场概况】{market_note or '（暂无）'}",
        "【持仓明细】",
    ]
    for h in held:
        lines.append(
            f"- {h['name']}（{h['code']}）：{h['net_shares']} 股，成本 {h['avg_cost']:g}，"
            f"现价 {h.get('price') or '—'}，浮动 {h.get('pnl_pct') if h.get('pnl_pct') is not None else '—'}%，"
            f"市值 {h.get('market_value_cny', h.get('market_value', 0)):,.0f} 元"
            + (f"（量能为前 5 日均量的 {h['vol_ratio']} 倍）" if h.get("vol_ratio") else ""))
    if view.get("sell_plan"):
        lines.append("【系统卖出凑钱建议（按浮盈降序）】")
        for s in view["sell_plan"]:
            lines.append(f"- 卖 {s['name']}（{s['code']}）{s.get('sell_shares') or '?'} 股，"
                         f"回笼约 {s['sell_value']:,.0f} 元（当前浮动 {s['pnl_pct'] if s['pnl_pct'] is not None else '—'}%）")
    if watch_lines:
        lines.append("【自选观察池（未持仓，潜在买入候选）】")
        lines.extend(watch_lines)
    if sector_lines:
        lines.append("【板块轮动】")
        lines.extend(sector_lines)
    if event_lines:
        lines.append("【未来数日解禁/增发事件（抛压与摊薄）】")
        lines.extend(event_lines)
    if news_titles:
        lines.append("【近期新闻标题】")
        lines.extend(f"- {t}" for t in news_titles[:12])
    lines.append(f"（数据时点 {datetime.now().strftime('%Y-%m-%d %H:%M')}，价格为盘中/收盘快照）")
    return "\n".join(lines)
