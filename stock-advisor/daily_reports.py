"""每日报告：盘前简报 / 盘后复盘（daily_reports.py）。

这两个报告原是 Claude Code 会话级 CronCreate 定时任务（会话关掉就没了、7 天过期），
2026-09-25 搬进 app.py 自己的守护线程（_daily_reports_loop），时间点由
config.yaml 的 schedule 段控制、网页「⚙️ 调度设置」可改。

与原 cron 的差异（有意为之）：
- 原版对每只自选股各发一次 WebSearch（35 只 = 35 次联网搜索 + 35 份中间文件），
  再由 Claude 逐份汇总。app 内没有 WebSearch 工具，改成把库里的新闻/行情/
  事件/日历/板块/币圈一次性喂给模型，每天正好 2 次 LLM 调用。成本与稳定性都更好，
  代价是没有「今天刚发生、库里还没有」的消息——prompt 里已要求模型只依据上下文，
  并在数据陈旧时明说。
- 输出从「每股一个文件 + 一个总结」合并为单个 Markdown（每只股票一个小节，
  保留原五段结构）。前端「分析报告」tab 按文件名展示，daily-summary.md 有专门
  标签「📋 每日总结」，文件名不可改。

本模块不 import app（避免循环依赖）：数据函数与 LLM 配置由 app.py 注入。
"""

import re
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
REPORTS_DIR = BASE_DIR / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

PREMARKET_FILE = "premarket-brief.md"
POSTMARKET_FILE = "daily-summary.md"

DISCLAIMER = "> ⚠️ 以上由 AI 依据本机行情/新闻数据生成，仅供学习参考，不构成投资建议。"

PREMARKET_PROMPT = """\
【输出纪律（最重要，先读这条）】只输出最终的 Markdown 正文，第一行就是正文的第一句话。
绝对不要输出思考过程、写作计划、草稿、英文推理、章节清单或"我们需要…"之类的自述。
想清楚了就直接写正文。

你是个人投资者的盘前简报助手，服务于一个本地股票管理系统。现在是 A 股开盘前，
你会拿到一段纯文本上下文：自选观察池（未持仓候选，含现价/涨跌幅/量能/行业）、
真实持仓明细（成本/现价/浮动盈亏）、未来数日解禁/增发事件、自选股近期新闻标题、
财经日历今明事件、大盘量能与指数概况、币圈 24 小时动向（趋势参考）、模拟账户概况。

请基于且仅基于这些数据，输出一份 Markdown 盘前简报，结构固定为：

### 🌅 隔夜与消息面
（分条：昨夜/今晨的重要消息，每条括注来源媒体；没有就明确说"库内无新增消息"，
不要用常识补写新闻）

### 📊 自选股一览
（表格：名称/代码/现价/涨跌幅/量能/行业；只列上下文里出现过的股票）

### 🎯 今日关注点
（3-5 条：可能影响今日开盘的事件、量能线索、板块方向）

### 🧭 持仓提示
（结合真实持仓：浮盈浮亏状态、今日该盯什么、触发了什么风险）

### 🪙 币圈 24h 参考
（一两句：币圈涨跌对隔夜风险偏好的暗示，注明是趋势参考、非 A 股标的本身）

### 📈 情绪评分
（-5 ~ +5 的整数，一句话理由）

要求：
- 数字必须来自上下文，禁止编造行情、消息或预测具体点位；不确定就说不确定。
- 中文，总长 1200 字以内；「自选股一览」表格最多列 20 只（优先列有涨跌幅的），
  列只留 名称/代码/现价/涨跌幅，其余股票在表下一行汇总一句带过。
- 必须写满上面全部 7 个章节，缺一节视为不合格。
- 结尾单独一行：%s
""" % DISCLAIMER

POSTMARKET_PROMPT = """\
【输出纪律（最重要，先读这条）】只输出最终的 Markdown 正文，第一行就是正文的第一句话。
绝对不要输出思考过程、写作计划、草稿、英文推理、章节清单或"我们需要…"之类的自述。
想清楚了就直接写正文。

你是个人投资者的盘后复盘助手，服务于一个本地股票管理系统。现在是 A 股收盘后，
你会拿到一段纯文本上下文：各股当日行情（收盘价/涨跌幅/量能）、当日及近期新闻标题、
真实持仓明细（成本/现价/浮动盈亏）、未来数日解禁/增发事件、大盘收盘概况与量能、
板块轮动评分、当日模拟交易决策、币圈 24 小时动向（趋势参考）。

请基于且仅基于这些数据，输出一份 Markdown 盘后复盘，结构固定为：

### 📈 大盘收盘
（指数涨跌、量能变化、涨跌家数，一两段）

### 🔄 板块轮动
（评分靠前板块及理由，2-4 条）

### 📋 个股复盘
（每只自选股一个小节，节内四行固定：
① 新闻要点（带来源媒体，无则写"当日库内无新增消息"）
② 情绪评分 -5~+5 及理由
③ 操作建议（买入/加仓/持有/减仓/观望 + 置信度百分比；结合真实持仓说明）
④ 关键风险（无则写"无"））

### 💼 持仓与模拟账户
（真实持仓浮盈浮亏提示 + 当日模拟交易决策结果）

### 🪙 币圈 24h 参考
（一两句隔夜情绪线索，注明仅趋势参考）

### 🔭 明日关注
（3-5 条）

要求：
- 数字必须来自上下文，禁止编造行情、消息或预测具体点位；不确定就说不确定。
- 每条结论尽量指回上下文里的具体数据。
- 中文，总长 2000 字以内。个股多时「📋 个股复盘」每只压缩到 1-2 行
  （新闻要点可合并为一行、情绪评分与操作建议合并为一行），保证后面三节写得完。
- 必须写满上面全部 6 个章节，缺一节视为不合格。
- 结尾单独一行：%s
""" % DISCLAIMER


def _llm_call(system_prompt: str, user_text: str, max_tokens: int = 8000) -> tuple[str, str]:
    """两段式调用（抄 paper_trading._llm_call）：base_url 非空（代理网关）直接 basic；
    官方端点先试 full（adaptive thinking + server-side fallback）。失败抛 RuntimeError。

    返回 (正文, stop_reason)。stop_reason 必须带回来：走网关的推理模型把推理
    token 也算进 max_tokens，正文写到一半 max_tokens 耗尽 → stop_reason="max_tokens"
    且正文戛然而止（2026-09-25 实测 4000 预算只出了 2313 字，缺两节）。

    timeout 给到 300s：上万 max_tokens 经代理网关常超过 180s。
    """
    import llm_advisor
    conf = llm_advisor.load_llm_conf()
    if not conf.get("enabled"):
        raise RuntimeError("LLM 未启用（网页「🏧 提款计划」页的 AI 配置里打开开关并填 API key）")
    if not conf["api_key"]:
        raise RuntimeError("未配置 LLM api_key（config.yaml llm 段或环境变量 ANTHROPIC_API_KEY）")
    from anthropic import Anthropic
    kwargs = {"api_key": conf["api_key"], "timeout": 300.0, "max_retries": 2}
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
    return text, (msg.stop_reason or "")


# ---------------- 上下文组装 ----------------

def _watchlist_rows(deps) -> list[dict]:
    """自选池全部（含已持仓）：代码/名称/现价/涨跌幅/量能/行业/新闻条数。"""
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("SELECT code, name FROM sa_watchlist ORDER BY added_at")
        rows = cur.fetchall()
    if not rows:
        return []
    codes = [r[0] for r in rows]
    names = {r[0]: r[1] for r in rows}
    quotes = deps["quote_fn"](codes)
    try:
        vols = deps["volume_fn"](codes)
    except Exception:
        vols = {}
    try:
        boards = deps["board_fn"](codes)
    except Exception:
        boards = {}
    out = []
    for code in codes:
        q = quotes.get(code) or {}
        vol = (vols.get(code) or {}).get("label") or ""
        ind = (boards.get(code) or {}).get("industry") or ""
        out.append({
            "code": code,
            "name": names.get(code) or q.get("name") or code,
            "price": q.get("price"),
            "change_pct": q.get("change_pct"),
            "vol": vol,
            "industry": ind,
            "error": q.get("error"),
        })
    return out


def _fmt_pct(v) -> str:
    return f"{v:+.2f}%" if isinstance(v, (int, float)) else "—"


def _news_block(deps, stocks: list[dict], per_stock: int = 6, total: int = 60) -> list[str]:
    """每股近 7 日新闻（情绪标注），每股≤per_stock 条、全量≤total 条控制 token。"""
    lines = []
    for s in stocks:
        rows = deps["news_fn"](s["code"], limit=per_stock)
        if not rows:
            continue
        head = f"- {s['name']}（{s['code']}）："
        items = []
        for r in rows:
            mark = {"pos": "🟢", "neg": "🔴"}.get(r.get("sentiment"), "")
            title = (r.get("title") or "").strip()
            if title:
                items.append(f"{mark}{title}（{r.get('media') or r.get('source') or '新闻'}）")
        if items:
            lines.append(head + "；".join(items))
        if len(lines) >= total:
            break
    return lines


def _holdings_block(deps) -> list[str]:
    """真实持仓：成本/现价/浮盈（失败降级为空段）。"""
    try:
        held = deps["holdings_fn"]()
    except Exception:
        return []
    lines = []
    for h in held:
        lines.append(f"- {h['name']}（{h['code']}）：{h['net_shares']} 股，成本 {h['avg_cost']:g}，"
                     f"现价 {h.get('price') or '—'}，浮盈 "
                     f"{h['pnl_pct'] if h.get('pnl_pct') is not None else '—'}%，"
                     f"市值 {h.get('market_value_cny', 0):,.0f} 元")
    return lines


def _build_context(deps, kind: str) -> str:
    """盘前/盘后共用一套上下文组装；kind 只影响标题与时间点措辞。"""
    stocks = _watchlist_rows(deps)
    if not stocks:
        raise RuntimeError("自选股为空：请先在「自选行情」页添加股票")
    lines = [f"【数据时点】{datetime.now().strftime('%Y-%m-%d %H:%M')}"
             f"（{'盘前，开盘前快照' if kind == 'premarket' else '盘后，收盘快照'}）"]
    try:
        lines.append(f"【市场概况】{deps['market_note_fn']() or '（暂无）'}")
    except Exception:
        lines.append("【市场概况】（暂无）")
    lines.append("【自选股行情】")
    for s in stocks:
        bits = [f"- {s['name']}（{s['code']}）：现价 {s['price']:g} "
                f"{_fmt_pct(s['change_pct'])}"] if s.get("price") else \
               [f"- {s['name']}（{s['code']}）：行情未取得（{s.get('error') or '未知原因'}）"]
        if s.get("vol"):
            bits.append(f"量能{s['vol']}")
        if s.get("industry"):
            bits.append(f"行业:{s['industry']}")
        lines.append("，".join(bits))
    held = _holdings_block(deps)
    if held:
        lines.append("【真实持仓】")
        lines.extend(held)
    codes = [s["code"] for s in stocks]
    try:
        event_lines = deps["events_fn"](codes)
    except Exception:
        event_lines = []
    if event_lines:
        lines.append("【未来 21 天解禁/增发事件】")
        lines.extend(event_lines)
    try:
        sector_lines = deps["sector_fn"](codes)
    except Exception:
        sector_lines = []
    if sector_lines:
        lines.append("【板块轮动】")
        lines.extend(sector_lines)
    try:
        cal_lines = deps["calendar_fn"]()
    except Exception:
        cal_lines = []
    if cal_lines:
        lines.append("【财经日历（今明）】")
        lines.extend(cal_lines)
    news = _news_block(deps, stocks)
    if news:
        lines.append("【自选股近 7 日新闻】")
        lines.extend(news)
    try:
        crypto = deps["crypto_fn"]()
    except Exception:
        crypto = []
    if crypto:
        lines.append("【币圈 24h 动向（趋势参考，非 A 股标的本身）】")
        lines.extend(crypto)
    try:
        overview = deps["paper_fn"]()
    except Exception:
        overview = None
    if overview and overview.get("account"):
        acc = overview["account"]
        lines.append(f"【模拟账户】总资产 {float(acc.get('total_value') or 0):,.0f} 元，"
                     f"现金 {float(acc.get('cash') or 0):,.0f} 元，"
                     f"持仓 {len(overview.get('positions') or [])} 只")
    if kind == "postmarket":
        try:
            decisions = deps["paper_decisions_fn"]()
        except Exception:
            decisions = []
        if decisions:
            lines.append("【当日模拟交易决策】")
            lines.extend(decisions)
    return "\n".join(lines)


# ---------------- 输出 ----------------

# 正文合格性判据。两种实测踩过的坑（走代理网关的 nemotron-3-super）：
#   1) 草稿/推理泄漏——把整段英文写作计划当正文吐出来（报告变成一堆 "We need to…"）
#   2) 截断——推理 token 吃掉 max_tokens，正文写到某一节中途戛然而止（后面整节消失）
# 这里做机器判别，命中就用纠正提示 + 翻倍预算重跑一次；仍不合格则照落盘，
# 并在返回值里标 bad=True（前端/日志可见），不静默假装成功。

_DRAFT_MARKERS = ("We need to", "We must", "Let's ", "Now craft", "Make sure",
                  "Section ", "Let's draft", "We should", "We can ", "I need to")

# 每份报告必须出现的章节关键词（顺序无关；正文用 ### 标题）
REQUIRED_SECTIONS = {
    "盘前": ("隔夜", "自选股一览", "今日关注点", "持仓提示", "币圈", "情绪评分"),
    "盘后": ("大盘收盘", "板块轮动", "个股复盘", "持仓与模拟账户", "币圈", "明日关注"),
}

# 首选预算 / 纠正重跑时的预算。盘后逐股复盘天生更长，给得宽些。
BUDGET = {"盘前": (8000, 16000), "盘后": (16000, 28000)}


def _looks_like_draft(text: str) -> bool:
    head = text[:1500]
    if any(m in head for m in _DRAFT_MARKERS):
        return True
    letters = sum(c.isascii() and c.isalpha() for c in head)
    return letters > len(head) * 0.25      # 正文本该以中文为主


def _missing_sections(text: str, kind_label: str) -> list[str]:
    """缺哪些必备章节（截断与跑题都靠它兜住）。"""
    return [kw for kw in REQUIRED_SECTIONS.get(kind_label, ()) if kw not in text]


def _correction_hint(problems: list[str], kind_label: str) -> str:
    """按问题类型给纠正提示——重跑时针对性比笼统说「重新生成」有效得多。"""
    hints = []
    if "draft" in problems:
        hints.append("上次输出混入了思考过程/英文写作计划。只输出最终 Markdown 正文，"
                     "第一行直接是正文第一句，不要计划、清单、草稿或自述。")
    if "truncated" in problems:
        cap = 1200 if "盘前" in kind_label else 2000
        hints.append(f"上次输出因超出长度上限被截断。这次务必压缩：正文不超过 {cap} 字，"
                     "表格最多列 20 只股票、只留 名称/代码/现价/涨跌幅 四列，"
                     "持仓与币圈段各最多 5 条，写完即止。")
    if problems and "incomplete" in problems:
        hints.append("上次输出缺少这些章节，必须补齐且都要有实质内容："
                     + "、".join(p for p in problems if p.startswith("缺")))
    return "\n\n【纠正】" + " ".join(hints) if hints else ""


def _generate(system_prompt: str, context: str, kind_label: str) -> tuple[str, bool]:
    """调模型出正文；不合格（草稿/截断/缺节）则带纠正提示、翻倍预算重跑一次。
    返回 (正文, 是否仍不合格)。"""
    is_pre = "盘前" in kind_label
    user_text = (f"以下是{'盘前' if is_pre else '盘后'}上下文，"
                 f"请生成{'简报' if is_pre else '复盘'}：\n\n{context}")
    first, second = BUDGET.get(kind_label, (8000, 16000))
    body, stop = _llm_call(system_prompt, user_text, max_tokens=first)

    def problems(text: str, stop_reason: str) -> list[str]:
        out = []
        if _looks_like_draft(text):
            out.append("draft")
        if stop_reason == "max_tokens":
            out.append("truncated")
        missing = _missing_sections(text, kind_label)
        if missing:
            out.append("incomplete")
            out.extend(f"缺{s}" for s in missing)
        return out

    probs = problems(body, stop)
    if not probs:
        return body, False
    print(f"[reports] {kind_label} 输出不合格 {probs}，纠正重试一次", flush=True)
    body, stop = _llm_call(system_prompt, user_text + _correction_hint(probs, kind_label),
                           max_tokens=second)
    return body, bool(problems(body, stop))


def _strip_md_for_push(md: str, limit: int = 500) -> str:
    """微信不渲染 Markdown：去标记符、压空行、截断。"""
    text = re.sub(r"^#{1,6}\s*", "", md, flags=re.M)      # 标题井号
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)           # 粗体
    text = re.sub(r"[*`>]", "", text)                      # 强调/代码/引用
    text = re.sub(r"^\s*[-*]\s+", "- ", text, flags=re.M)  # 列表符号统一
    text = re.sub(r"\n{2,}", "\n", text)                   # 压空行
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text.strip()[:limit]


def _write_report(date_str: str, filename: str, title: str, body: str) -> str:
    day_dir = REPORTS_DIR / date_str
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / filename
    header = f"# {title}\n\n> 生成时间 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    path.write_text(f"{header}\n\n{body.rstrip()}\n", encoding="utf-8")
    return str(path)


def generate_premarket_brief(deps) -> dict:
    """生成盘前简报 → reports/<今日>/premarket-brief.md + 微信推送摘要。"""
    context = _build_context(deps, "premarket")
    body, bad = _generate(PREMARKET_PROMPT, context, "盘前")
    today = datetime.now().strftime("%Y-%m-%d")
    path = _write_report(today, PREMARKET_FILE, f"盘前简报 {today}", body)
    if bad:
        print("[reports] 盘前简报重跑后仍不合格（草稿/截断/缺节），已落盘请人工过目", flush=True)
    push_ok = False
    try:
        res = deps.get("notify_fn")(f"🌅 盘前简报 {today}", _strip_md_for_push(body))
        push_ok = bool((res or {}).get("sent"))
    except Exception as exc:
        print(f"[reports] 盘前推送失败: {exc}", flush=True)
    return {"ok": True, "kind": "premarket", "path": path, "pushed": push_ok,
            "bad": bad, "ts": datetime.now().isoformat(timespec="seconds"),
            "chars": len(body)}


def generate_postmarket_review(deps) -> dict:
    """生成盘后复盘 → reports/<今日>/daily-summary.md + 微信推送摘要。"""
    context = _build_context(deps, "postmarket")
    body, bad = _generate(POSTMARKET_PROMPT, context, "盘后")
    today = datetime.now().strftime("%Y-%m-%d")
    path = _write_report(today, POSTMARKET_FILE, f"盘后复盘 {today}", body)
    if bad:
        print("[reports] 盘后复盘重跑后仍不合格（草稿/截断/缺节），已落盘请人工过目", flush=True)
    push_ok = False
    try:
        res = deps.get("notify_fn")(f"📋 盘后复盘 {today}", _strip_md_for_push(body))
        push_ok = bool((res or {}).get("sent"))
    except Exception as exc:
        print(f"[reports] 盘后推送失败: {exc}", flush=True)
    return {"ok": True, "kind": "postmarket", "path": path, "pushed": push_ok,
            "bad": bad, "ts": datetime.now().isoformat(timespec="seconds"),
            "chars": len(body)}


GENERATORS = {"premarket": generate_premarket_brief, "postmarket": generate_postmarket_review}
