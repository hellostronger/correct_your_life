"""C 阶段：微信当系统遥控器（wx_commands.py）。

## 定位

`wx_chat.reply_fn` 认出的指令在这里落地。B 阶段只回「还没接上」，现在接上。
**不碰 wx_inbound / notifier** —— 这是 B 先落地的回报：传输层与指令层完全解耦。

## 为什么要按副作用分级

指令分两类，混在一起会出事：

| 级别 | 指令 | 副作用 | 处理 |
|---|---|---|---|
| **只读** | 持仓 / 自选 / 状态 / 帮助 | 拉行情、读库 | 直接执行 |
| **有代价** | 复盘 / 简报 | **调 Claude（花钱）+ 写 reports 文件 + 推微信** | 必须先回确认 |

「复盘」看起来像只读查询，实际会触发一整轮 LLM 生成。用户随口在微信里
打两个字就烧掉一次调用、还往 reports 里落一个文件、当成"已推送"通知出去 ——
所以这类一律走二次确认。设计稿 A.2 里把这条标成待定，这里取**保守默认**：
宁可多问一句，不要静默花钱。

确认用一次性 token 而不是纯文本「确认」二字：`确认` 太容易手滑，
而 token 只能由上一条消息生成，不会被别的对话撞上。

## 格式

微信不渲染 Markdown，所以全部用「·」列表 + 短行，禁用表格和标题符号。
这是照抄官方 send.ts 那条「长回复在 7.3k 字符以上会被服务端丢弃」
（issue #284）之后定的策略：短比花哨重要。
"""

import re
import secrets
import threading
import time
from datetime import datetime

# 一次性确认 token。键是 token，值是 {user, cmd, 过期时间戳}
_confirms: dict[str, dict] = {}
_confirm_lock = threading.Lock()
CONFIRM_TTL_S = 300          # 5 分钟内有效，过期要重新触发

_state = {"last_cmd": None, "last_error": None, "runs": 0, "confirm_needed": 0,
          "confirm_done": 0}
_state_lock = threading.Lock()


def get_status() -> dict:
    with _state_lock:
        s = dict(_state)
    with _confirm_lock:
        s["pending_confirms"] = len(_confirms)
    return s


def _log_error(msg: str) -> None:
    with _state_lock:
        _state["last_error"] = msg
    print(f"[wx-cmd] {msg}", flush=True)


# ---------------- 格式化工具（微信友好） ----------------

def _pct(v) -> str:
    if v is None:
        return "—"
    return f"{v:+.2f}%"


def _money(v) -> str:
    try:
        return f"{float(v):,.0f}"
    except (TypeError, ValueError):
        return "—"


def fmt_holdings(deps) -> str:
    """持仓 + 浮动盈亏。数据源与「我的持仓」页完全同一套函数。"""
    app = deps["app"]
    try:
        pos = app._holdings_with_pnl(app._derive_holdings())
    except Exception as exc:
        _log_error(f"持仓查询失败: {exc}")
        return f"（查持仓出错：{exc}）"
    held = [p for p in pos if p.get("net_shares", 0) > 0]
    if not held:
        return "你现在没有持仓。要看自选股就说「自选」。"
    held.sort(key=lambda p: p.get("market_value_cny") or 0, reverse=True)
    total_mv = sum(p.get("market_value_cny") or 0 for p in held)
    total_pnl = sum(p.get("pnl_cny") or 0 for p in held)
    total_real = sum(p.get("realized_pnl_cny") or 0 for p in held)
    lines = [f"持仓 {len(held)} 只，市值合计 {_money(total_mv)} 元", ""]
    for p in held:
        lines.append(f"· {p.get('name') or '?'}（{p.get('code')}）"
                     f" {p.get('net_shares')}股  现价 {p.get('price') or '—'}"
                     f"  浮盈 {_pct(p.get('pnl_pct'))}"
                     f"（{_money(p.get('pnl_cny'))} 元）")
    lines.append("")
    lines.append(f"浮动合计 {_money(total_pnl)} 元"
                 + (f"，已实现 {_money(total_real)} 元" if total_real else ""))
    lines.append(f"（价格 {datetime.now().strftime('%H:%M')} 快照，港股已折人民币）")
    return "\n".join(lines)


def fmt_watchlist(deps, limit: int = 15) -> str:
    app = deps["app"]
    try:
        with app.get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT code, name, note FROM sa_watchlist ORDER BY added_at")
            rows = cur.fetchall()
        if not rows:
            return "自选股是空的。去「自选行情」页加。"
        codes = [r[0] for r in rows[:limit]]
        quotes = app.fetch_quotes(codes) if codes else {}
    except Exception as exc:
        _log_error(f"自选查询失败: {exc}")
        return f"（查自选出错：{exc}）"
    lines = [f"自选 {len(rows)} 只" + (f"（只列前 {limit} 只）" if len(rows) > limit else ""), ""]
    for code, name, note in rows[:limit]:
        q = quotes.get(code) or {}
        px = q.get("price")
        chg = q.get("change_pct")
        arrow = "—" if chg is None else (f"▲{chg:.2f}%" if chg > 0 else
                                         (f"▼{chg:.2f}%" if chg < 0 else "0.00%"))
        tail = f"  {note}" if note else ""
        lines.append(f"· {name}（{code}） {px or '—'}  {arrow}{tail}")
    return "\n".join(lines)


def _state_of(deps, name: str) -> dict:
    """安全取 app 的某个 *_state 全局：不存在 / 不是 dict 一律返回空 dict。

    刻意不写死字段清单 —— app.py 的守护线程 state 增删很频繁，写死就会出现
    「属性不存在」直接抛给用户（或者更糟：用 hasattr 兜底后永远显示"未跑过"，
    看起来正常其实在骗人）。取不到就明说取不到。
    """
    try:
        v = getattr(deps["app"], name, None)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _ts(iso) -> str:
    if not iso:
        return "未跑过"
    try:
        return str(iso)[5:16].replace("T", " ")
    except Exception:
        return str(iso)


def fmt_status(deps) -> str:
    """各后台任务最近一次运行时间 —— 用来回答"为什么今天没推我"。

    只列 app.py 里**确实存在**的 state，避免写出长期显示"未跑过"的假条目。
    """
    app = deps["app"]
    lines = []

    def add(label, iso, extra=""):
        lines.append(f"· {label}：{_ts(iso)}" + (f"，{extra}" if extra else ""))

    # 公众号（有专用的 status 函数）
    try:
        mp = app.wechat_mp.get_status(app._mp_deps())
        add("公众号拉取", mp.get("last_run"), f"未读 {mp.get('unread', 0)} 篇")
    except Exception as exc:
        lines.append(f"· 公众号：查不到状态（{exc}）")

    # 以下都是 app.py 里的 *_state 全局
    ps = _state_of(deps, "_paper_strategy_state")
    add("策略扫描", ps.get("last_run"), f"上次触发 {ps.get('last_count', 0)} 次"
        + (f"，出错：{ps['last_error']}" if ps.get("last_error") else ""))

    disc = _state_of(deps, "_discover_state")
    add("挖新股", disc.get("last_run"))

    news = _state_of(deps, "_news_state")
    add("新闻抓取", news.get("last_run"))

    rep = _state_of(deps, "_reports_state")
    add("盘前简报", rep.get("last_premarket"))
    add("盘后复盘", rep.get("last_postmarket"))

    # 微信入站
    try:
        ib = app.wx_inbound.get_status()
        add("微信入站", ib.get("last_poll"),
            "正常" if not ib.get("last_error") else f"出错：{ib['last_error']}")
        if ib.get("paused"):
            lines.append(f"· ⚠️ iLink token 过期，冷却中"
                         f"（剩 {round((ib.get('paused_left_s') or 0) / 60)} 分钟，"
                         f"需去「通知」页重新扫码）")
    except Exception as exc:
        lines.append(f"· 微信入站：查不到状态（{exc}）")

    return "后台任务最近一次运行\n" + "\n".join(lines)


HELP_TEXT = """可用指令（直接发这几个词就行）
· 持仓 —— 当前持仓、市值、浮动盈亏
· 自选 —— 自选股和今日涨跌
· 状态 —— 各后台任务最近一次跑的时间
· 复盘 / 简报 —— 生成 Claude 盘后复盘 / 盘前简报（要发一次确认，会花 API 费）

其他直接说人话就行，我会回答。
问持仓/行情时我会自己去查，不会凭空编数字。"""


# ---------------- 有代价的指令：确认令牌 ----------------

def _new_confirm(user_id: str, cmd: str) -> str:
    """发一个短且不可猜的一次性确认码。

    ⚠️ 别把命令名或时间戳编进 token：① 太长会超出确认消息正则的字符上限
    （旧实现生成 `postmarket71092` 共 14 字符，而 `_CONFIRM_RE` 只允许
    `\S{4,12}` —— 结果 bot 自己发出去的确认码自己认不出来，永远执行不了）；
    ② 时间戳可预测，别人猜出来就能替你触发报告。
    """
    token = secrets.token_hex(3)          # 6 位十六进制，约 16^6
    with _confirm_lock:
        _prune_confirms()
        _confirms[token] = {"user": user_id, "cmd": cmd, "exp": time.time() + CONFIRM_TTL_S}
    with _state_lock:
        _state["confirm_needed"] += 1
    return token


def _take_confirm(user_id: str, token: str):
    """校验并消费一次性令牌。错用户/错令牌/过期都返回 None。"""
    with _confirm_lock:
        _prune_confirms()
        rec = _confirms.get(token)
        if not rec or rec["user"] != user_id:
            return None
        del _confirms[token]              # 一次性：用过即废
        return rec["cmd"]


def _prune_confirms() -> None:
    now = time.time()
    for k in [k for k, v in _confirms.items() if v["exp"] < now]:
        del _confirms[k]


def _generate_report(deps, kind: str) -> None:
    """后台线程体：真的去生成。异常只记 state，不外抛（线程里抛了没人接）。"""
    app = deps["app"]
    label = "盘后复盘" if kind == "postmarket" else "盘前简报"
    try:
        app.daily_reports.GENERATORS[kind](app._report_deps())
        print(f"[wx-cmd] {label}生成完成", flush=True)
    except Exception as exc:
        _log_error(f"{label}生成失败: {type(exc).__name__}: {exc}")


def _run_report(deps, kind: str) -> str:
    """起后台线程生成报告，**立刻返回**。

    ⚠️ 绝不能同步跑。同步跑的后果不是"慢一点"，而是**入站长轮询线程被占住
    30~90 秒，期间 bot 完全收不到任何微信消息** —— 你以为它在生成报告，其实在
    聋着。app.py 的 `/api/reports/{kind}` 也是「查重入 → 起线程 → 立刻返回」，
    这里保持一致。

    报告本身会在生成完时自己推一条到微信（`daily_reports` 里的 notify_fn），
    所以用户不需要在这里等结果。
    """
    app = deps["app"]
    label = "盘后复盘" if kind == "postmarket" else "盘前简报"
    st = getattr(app, "_reports_state", None)
    if isinstance(st, dict) and st.get(kind):
        return f"{label}已经在生成中了，别重复触发。等一会儿就好。"
    with _state_lock:
        _state["runs"] += 1
        _state["last_cmd"] = f"report:{kind} @{datetime.now():%H:%M:%S}"
    threading.Thread(target=_generate_report, args=(deps, kind), daemon=True).start()
    return (f"已开始生成{label}，大约 1-3 分钟。\n"
            f"生成完会自动推到你微信，「分析报告」页也能看。\n"
            f"（这期间我还在正常收消息，不是卡住了）")


# ---------------- 分发 ----------------

# 指令别名 → 规范名。**单一真源**：wx_chat.match_command 与下面的 handle() 都
# 从这里取。早先两处各写一份别名表，结果 handle() 拿**原始文本**去查自己的表、
# 忽略了 match_command 的归一化结果 —— 于是最自然的写法「复盘」（别名）匹配不到
# 规范名「盘后复盘」，直接掉到 None，回复变成「指令没接上」。这种接缝 bug 只有
# 端到端才暴露，单测两边各自都过。
ALIASES = {
    "复盘": "盘后复盘", "盘后": "盘后复盘", "盘后复盘": "盘后复盘",
    "简报": "盘前简报", "盘前": "盘前简报", "盘前简报": "盘前简报",
    "持仓": "持仓", "自选": "自选", "状态": "状态",
    "帮助": "帮助", "?": "帮助", "？": "帮助",
}

# 带一次性确认码回来的确认消息（「盘后复盘 12345」）
_CONFIRM_RE = re.compile(r"^(盘后复盘|盘前简报|复盘|简报)\s+\S{4,12}$")


def canonical(text: str) -> str | None:
    """把原始消息归一化成规范指令名；不是指令返回 None。

    识别刻意保守：要求整条消息短且贴近指令词，否则「帮我看看复盘怎么做」
    会被误判成要执行复盘。
    """
    t = (text or "").strip()
    if not t:
        return None
    if _CONFIRM_RE.match(t):
        return t.split()[0]              # 确认消息，按原文交给 handle
    if len(t) > 12:
        return None
    return ALIASES.get(t) or ALIASES.get(t.lstrip("/").strip())


# 只读指令：直接跑，不花钱、不写文件
_READONLY = {
    "持仓": lambda d: fmt_holdings(d),
    "自选": lambda d: fmt_watchlist(d),
    "状态": lambda d: fmt_status(d),
    "帮助": lambda d: HELP_TEXT,
}

# 有代价：先要确认令牌
_COSTLY = {
    "盘后复盘": "postmarket",
    "盘前简报": "premarket",
}


def handle(deps, user_id: str, raw: str) -> str | None:
    """处理一条消息。返回回复文本；不是指令返回 None。

    **自己调 canonical 归一化**，不信任调用方已经转好 —— 上面那个 bug 就是
    因为这里跳过了归一化。
    """
    text = (raw or "").strip()
    if not text:
        return None
    cmd = canonical(text)
    if cmd is None:
        return None

    # 「盘后复盘 a12345」：带令牌回来的确认
    parts = text.split()
    if len(parts) == 2 and canonical(parts[0]) in _COSTLY:
        want = _COSTLY[canonical(parts[0])]
        got = _take_confirm(user_id, parts[1])
        if got and got == want:
            with _state_lock:
                _state["confirm_done"] += 1
            return _run_report(deps, want)
        return "确认码无效、已过期或对不上指令。重新发一次指令吧。"

    if cmd in _READONLY:
        with _state_lock:
            _state["runs"] += 1
            _state["last_cmd"] = f"{cmd} @{datetime.now():%H:%M:%S}"
        try:
            return _READONLY[cmd](deps)
        except Exception as exc:
            _log_error(f"{cmd} 执行失败: {type(exc).__name__}: {exc}")
            return f"（{cmd} 出错了：{exc}）"

    if cmd in _COSTLY:
        label = "盘后复盘" if _COSTLY[cmd] == "postmarket" else "盘前简报"
        token = _new_confirm(user_id, _COSTLY[cmd])
        return (f"生成{label}要调一次 Claude（会花 API 费），并往「分析报告」页写一份。\n"
                f"确认就发这一句：\n\n{cmd} {token}\n\n"
                f"（{CONFIRM_TTL_S // 60} 分钟内有效）")

    return None
