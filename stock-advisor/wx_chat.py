"""微信对话的大脑（wx_chat.py）——收到消息后拿 Claude 回一句话。

## 定位

`wx_inbound.py` 只负责**收**（长轮询 + 解析 + 落库 + 缓存 context_token）。
本模块负责**怎么回**，作为 `reply_fn` 注入，传输层完全不感知内容。

## 为什么先做"通用助手"而不是别的

`DESIGN.md`（LifeReflector）规划了三条回复路线：

- **B 通用助手**（本模块先做）—— 用现成的 `llm` 段就能跑，不依赖别的模块
- **C 遥控指令**（"复盘"→ 触发盘后报告、"持仓"→ 查持仓）—— 需要注入一堆回调
- **D 日记/情绪记录** —— 隐私落点、词典规则、冷静期机制，是独立子系统

B 先落地是因为它是 C 和 D 的**共同底座**：先把"消息进 → LLM → 消息出"这条
链路跑通且省钱省心，后面接指令路由或情绪分析只是替换/包装 `reply_fn`。
如果反过来先做 C，`reply_fn` 会被业务逻辑占死，B 就得重写一遍。

## 省钱省心

- **同一用户 60 秒内的多条消息合并成一次调用**。人在微信里打字常分三条发，
  逐条调 LLM 既慢又贵；合并后只回一条，也更像真人。
- 历史带最近 N 轮（默认 6 轮），超长截断。单条入站上限 2000 字。
- LLM 挂了**不影响接收**：回一句兜底文案并记日志，context_token 照样缓存，
  出站推送不受影响。这一点比"对话好不好用"重要 —— 聊天是锦上添花，
  通知才是主功能。
"""

import threading
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.yaml"

DEFAULT_CHAT_CONF = {
    "enabled": True,        # 入站长轮询总开关
    "reply": True,          # 是否用 LLM 自动回（关掉则只收不回，便于观察）
    "history_turns": 6,     # 带最近几轮历史进上下文
    "merge_seconds": 60,    # 同一用户 N 秒内的消息合并成一次调用
    "max_chars": 2000,      # 单条入站字数上限（超了截断，防超长拖垮上下文）
    "max_reply_chars": 1200,
    "cooldown_seconds": 20,  # 同一用户两次回复的最小间隔，防刷屏
}

SYSTEM = """\
你在一个本地股票/自选管理系统里，是用户通过微信联系的私人助理。

怎么回：

1. **短。** 微信是对话框，不是报告。默认 3~6 句、200 字以内。他问细节再展开。
2. **直接答。** 先给结论，再给理由。不要复述他的问题，不要"这是个好问题"。
3. **不确定就说不确定。** 你看不到他的持仓、行情页面和系统状态 —— 除非他
   在消息里告诉你。**绝对不要编造持仓、股价、涨跌幅、成交数据**。
   需要真实数据时，让他去对应的页面看，或让他用系统的指令（见下）。
4. **不主动输出 Markdown。** 微信不渲染，标题符号和表格会显示成乱码。
   用短段落、必要时用「·」开头列点。
5. 如果他问的是需要查系统数据的事（持仓、行情、报告），告诉他用指令而不是猜。

可以识别的指令（其余当作闲聊）：
  复盘 / 盘后       → 生成盘后复盘报告
  简报 / 盘前       → 生成盘前简报
  持仓              → 列出当前持仓与浮动盈亏
  自选              → 列出自选股
  情绪 / 日记 / 记一下 → 记录今天的心情（情绪洞察）
  帮助 / ?          → 列出上面这些指令

语气：平实、简短、像同事。不要 emoji 堆砌，不要过度热情。
"""

_state_lock = threading.Lock()
_state = {"last_reply": None, "last_error": None, "replies": 0, "skipped": 0}

# 同一用户待合并的消息：{user_id: {"texts": [...], "ctx": str, "ts": float}}
_pending: dict[str, dict] = {}
_pending_lock = threading.Lock()
_last_reply_at: dict[str, float] = {}


def load_chat_conf() -> dict:
    conf = dict(DEFAULT_CHAT_CONF)
    try:
        text = CONFIG_FILE.read_text(encoding="utf-8") if CONFIG_FILE.exists() else ""
        if text:
            import yaml
            data = yaml.safe_load(text) or {}
            conf.update({k: v for k, v in (data.get("wx_chat") or {}).items()
                         if v is not None})
    except Exception:
        pass
    return conf


def get_status() -> dict:
    with _state_lock:
        s = dict(_state)
    with _pending_lock:
        s["pending_users"] = len(_pending)
    return s


# ---------------- 落库（供页面回看） ----------------

def ensure_table(deps) -> None:
    """对话留档。表很小（一天几条），但没有它就没法排查"它到底回了什么"——
    而这恰恰是 LLM 对话最容易出问题的地方。"""
    ddl = """
    CREATE TABLE IF NOT EXISTS sa_wx_chat (
        id         BIGSERIAL PRIMARY KEY,
        user_id    VARCHAR(128) NOT NULL,
        direction  VARCHAR(4) NOT NULL,            -- in | out
        text       TEXT NOT NULL,
        merged     INTEGER NOT NULL DEFAULT 1,     -- 几条合并的
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS idx_wx_chat_created ON sa_wx_chat (created_at DESC);
    """
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        for stmt in [s.strip() for s in ddl.split(";") if s.strip()]:
            stmt = "\n".join(l for l in stmt.splitlines()
                             if not l.strip().startswith("--")).strip()
            if stmt:
                cur.execute(stmt)
    conn.commit()


def _log(deps, user_id: str, direction: str, text: str, merged: int = 1) -> None:
    try:
        with deps["get_conn"]() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO sa_wx_chat (user_id, direction, text, merged)"
                        " VALUES (%s,%s,%s,%s)", (user_id, direction, text, merged))
        conn.commit()
    except Exception:
        pass


def recent(deps, limit: int = 30) -> list[dict]:
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, user_id, direction, text, merged, created_at"
                    " FROM sa_wx_chat ORDER BY created_at DESC, id DESC LIMIT %s",
                    (max(1, min(100, limit)),))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------------- 指令路由（为 C 预留） ----------------
# 现在只回一句「功能在建」，不做实际动作 —— 但入口先留好，因为 C 落地时
# 就挂在这里，而不是去改 wx_inbound 或 notifier。
COMMANDS = {
    "复盘": "盘后复盘", "盘后": "盘后复盘",
    "简报": "盘前简报", "盘前": "盘前简报",
    "持仓": "持仓", "自选": "自选",
    "情绪": "情绪", "日记": "情绪", "记一下": "情绪",
    "帮助": "帮助", "?": "帮助",
}


def match_command(text: str) -> str | None:
    """识别开头的指令词。刻意要求「短且贴近整条消息」——
    否则"帮我看看复盘怎么做"这种问法会被误判成要执行复盘。"""
    t = (text or "").strip()
    if len(t) > 12:
        return None
    return COMMANDS.get(t) or COMMANDS.get(t.lstrip("/").strip())


# ---------------- 回复 ----------------

def _history(deps, user_id: str, turns: int) -> str:
    rows = recent(deps, limit=max(4, turns * 2))
    picked = [r for r in rows if r["user_id"] == user_id][:turns * 2]
    picked.reverse()
    if not picked:
        return "（这是第一次对话）"
    lines = []
    for r in picked:
        who = "他" if r["direction"] == "in" else "你"
        lines.append(f"{who}：{r['text'][:300]}")
    return "\n".join(lines)


def build_reply(deps, user_id: str, text: str) -> str | None:
    """生成一条回复。返回 None 表示「不回」（静默）。

    这是给 wx_inbound 用的 reply_fn 之外的同步版本，测试也直接调它。
    """
    conf = load_chat_conf()
    if not conf.get("reply"):
        return None
    cmd = match_command(text)
    if cmd:
        return (f"「{cmd}」指令还没接上（C 阶段做）。\n\n"
                f"现在能做的：直接用文字跟我说就行，比如问我行情、问某只票的逻辑、"
                f"或者让我帮你想事情。\n\n发送「帮助」随时能看当前支持什么。")
    try:
        import llm_advisor
    except Exception:
        return "（对话功能暂时不可用）"
    llm_conf = llm_advisor.load_llm_conf()
    if not llm_conf.get("enabled") or not llm_conf.get("api_key"):
        return ("（LLM 没配好，所以我暂时没法说话。\n"
                "请在「🔔 通知」页的完整配置里填 llm.api_key，或设置环境变量 "
                "ANTHROPIC_API_KEY。）")
    body = (f"最近的对话：\n{_history(deps, user_id, int(conf.get('history_turns') or 6))}\n"
            f"\n---\n他现在说：\n{text[:int(conf.get('max_chars') or 2000)]}")
    try:
        out = llm_advisor.ask(SYSTEM, body, conf=llm_conf,
                              max_tokens=1024, thinking=True)
    except Exception as exc:
        with _state_lock:
            _state["last_error"] = f"{type(exc).__name__}: {exc}"
        print(f"[wx-chat] LLM 失败: {exc}", flush=True)
        return f"（我这会儿想不出来：{str(exc)[:120]}）\n\n你的消息我收到了，稍后再问我一次。"
    return str(out)[:int(conf.get("max_reply_chars") or 1200)]


def _flush(deps, user_id: str, slot: dict) -> str | None:
    """把攒下的消息合并成一次 LLM 调用并回一条。

    ⚠️ 两个顺序上的坑：

    1. 必须**取走并清空** texts，否则已回过的那几条会被下一次合并重复带上 ——
       表现为「bot 把你十几条旧消息又念一遍」，且每次都白烧一次 LLM。
    2. 入站落库必须在 build_reply **之后**。build_reply 里的 _history 是从库里
       读最近对话的；先落库的话，这条消息会同时出现在「最近的对话」和
       「他现在说」两处，模型看到重复内容（还会以为你在强调它）。
    """
    texts = slot.get("texts") or []
    if not texts:
        return None
    merged = "\n".join(texts)
    with _pending_lock:
        slot["texts"] = []              # 先取走，避免下面抛异常时重复计入
    try:
        out = build_reply(deps, user_id, merged)
    finally:
        # finally：LLM/配置出错也要把"他说了什么"记下来 —— 排查对话问题
        # 时最缺的就是这个。
        _log(deps, user_id, "in", merged, merged=len(texts))
    if not out:
        return None
    _last_reply_at[user_id] = time.time()
    _log(deps, user_id, "out", out)
    with _state_lock:
        _state["replies"] += 1
        _state["last_reply"] = {"user": user_id, "text": out[:120],
                                "ts": datetime.now().strftime("%m-%d %H:%M:%S")}
        _state["last_error"] = None
    return out


def reply_fn(deps) -> callable:
    """给 wx_inbound 用的 reply_fn：消息合并（debounce）。

    语义是**首条立即回、后续在窗口内合并成一条补充回**，而不是「攒够窗口再回」。
    后者会让第一条消息白等一个合并窗口（默认 60s）—— 实测正是这么写的，
    结果是「你在微信发消息它收到了但一直不回」。

    窗口的关闭靠两个时机（缺一不可）：
      1. 新消息到达时，若距上次回复已超过窗口 → 立刻合并回
      2. 每轮长轮询结束后调 tick()，把静默期内攒下的补上
    只有第 1 条的话，安静下来的那一批永远不会被回。
    """
    def _reply(text: str, user_id: str, context_token: str = "") -> str | None:
        conf = load_chat_conf()
        if not conf.get("enabled", True) or not conf.get("reply"):
            return None
        now = time.time()
        window = float(conf.get("merge_seconds") or 0)
        with _pending_lock:
            slot = _pending.setdefault(user_id, {"texts": [], "ctx": context_token,
                                                 "ts": now, "last_sent": 0.0})
            slot["texts"].append(text)
            slot["ctx"] = context_token or slot["ctx"]
            slot["ts"] = now
            if window > 0 and now - slot["last_sent"] < window:
                # 还在窗口内：只入队，不调 LLM
                _log(deps, user_id, "in", text, merged=0)
                return None
            slot["last_sent"] = now
        return _flush(deps, user_id, slot)
    return _reply


def tick(deps) -> int:
    """每轮长轮询结束后调一次：把窗口已过期的队列补发掉。

    没有这一步，「说完一句话就不动了」的那一批会被永远卡在队列里 ——
    长轮询 35s 一轮，正好当成天然的定时器。
    """
    now = time.time()
    window = float(load_chat_conf().get("merge_seconds") or 0)
    if window <= 0:
        return 0
    ready = []
    with _pending_lock:
        for uid, slot in list(_pending.items()):
            if slot["texts"] and now - slot["ts"] >= window:
                slot["last_sent"] = now
                ready.append((uid, slot))
    sent = 0
    for uid, slot in ready:
        try:
            out = _flush(deps, uid, slot)
        except Exception as exc:
            print(f"[wx-chat] tick 补发失败 {uid}: {exc}", flush=True)
            continue
        if not out:
            with _pending_lock:
                _pending.pop(uid, None)
            continue
        try:
            import wx_inbound
            wx_inbound.send_text(uid, out, slot.get("ctx", ""))
            sent += 1
        except Exception as exc:
            print(f"[wx-chat] tick 补发推送失败 {uid}: {exc}", flush=True)
        finally:
            with _pending_lock:
                _pending.pop(uid, None)
    return sent


def run_forever(deps) -> None:
    """入站守护线程。enabled=false 且未登录时退避 5 分钟再查，不空转。"""
    import wx_inbound
    fn = reply_fn(deps)
    tick_fn = lambda: tick(deps)          # noqa: E731  （长轮询 35s 一轮，当定时器用）
    while True:
        try:
            if not load_chat_conf().get("enabled", True):
                import notifier
                if not notifier.ilink_creds().get("bot_token"):
                    time.sleep(300)
                    continue
            wx_inbound.run_forever(deps, fn, tick_fn)
        except Exception as exc:
            print(f"[wx-chat] 入站线程异常: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(60)
