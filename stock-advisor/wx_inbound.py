"""微信入站消息长轮询（wx_inbound.py）——让 bot 能"对话"，而不只是单向推送。

## 为什么单独一个模块

出站（notifier.send_wx）与入站是**两件独立的事**，之前只有出站：
`ilink_client.get_updates()` 和 `notifier.record_inbound_context()` 早就写好了，
但没有任何调用方，于是你在微信里给 bot 发消息，服务完全没反应。

入站还多两样出站不需要的东西：
1. **游标 `get_updates_buf`** 必须持久化。不带游标 = 每次都从队首拉，会把
   历史消息**重复**消费一遍并重复回复；游标丢了则漏收。官方存本地 JSON 文件，
   本项目存云库 `sa_wx_ilink`（KV 表加个字段，不用改表结构，也不怕本地文件丢）。
2. **会话保活**。长轮询本身就在向 iLink 证明"这个客户端还活着"。第三方 bridge
   的 README 明确写着：Gateway 停掉后 token 几小时内失效。也就是说没有这个
   线程，光有 `notifystart` 也只是把失效时间从"几小时"缩到"一重启"。

## 抄自官方

`@tencent-weixin/openclaw-weixin` 2.4.9 `src/monitor/monitor.ts`：

| 官方 | 这里 |
|---|---|
| `DEFAULT_LONG_POLL_TIMEOUT_MS = 35_000` | 同 |
| `MAX_CONSECUTIVE_FAILURES = 3` → 退避 30s | 同 |
| 否则失败 → 2s 后重试 | 同 |
| `errcode/ret === -14` → `pauseSession()` 冷却 1 小时 | 同（照抄 `session-guard.ts`） |
| 服务端回 `longpolling_timeout_ms` → 采用它 | 同 |
| 拿到 `get_updates_buf` 立刻落盘 | 同 |

## 回复策略放哪

本模块只管**收**（解析 + 落库 + 缓存 context_token），不管**怎么回**。
回复交给注入的 `reply_fn(text, user_id, context_token) -> str | None`。
这样 A（回显）/ B（LLM 对话）/ C（遥控指令）只是换一个 reply_fn，
传输层一行都不用改 —— app.py 默认注入 LLM 对话实现。
"""

import threading
import time

import ilink_client

# 官方 monitor.ts 的常量（2026-09-30 对齐 2.4.9）
DEFAULT_LONG_POLL_TIMEOUT_S = 35
MAX_CONSECUTIVE_FAILURES = 3
BACKOFF_DELAY_S = 30
RETRY_DELAY_S = 2
STALE_TOKEN_ERRCODE = -14       # session-guard.ts: bot token 过期/失效
STALE_TOKEN_PAUSE_S = 3600      # session-guard.ts: 冷却一小时，别硬刚

# 游标在 sa_wx_ilink 里的 KV 键
CURSOR_KEY = "get_updates_buf"

_state_lock = threading.Lock()
_state = {
    "last_poll": None, "last_error": None, "poll_count": 0,
    "msg_count": 0, "reply_count": 0, "paused_until": 0,
    "last_inbound": None,
}


def get_status() -> dict:
    with _state_lock:
        s = dict(_state)
    s["paused"] = s["paused_until"] > time.time()
    s["paused_left_s"] = max(0, int(s["paused_until"] - time.time()))
    return s


def _set(**kw) -> None:
    with _state_lock:
        _state.update(kw)


# ---------------- 游标持久化（云库 KV，存本地文件会丢） ----------------

def get_cursor() -> str:
    try:
        from notifier import _db_exec
        row = _db_exec("SELECT value FROM sa_wx_ilink WHERE field = %s",
                       (CURSOR_KEY,), fetch="one")
        return (row[0] if row else "") or ""
    except Exception:
        return ""


def set_cursor(buf: str) -> None:
    """必须在**处理完消息之后**才写游标（官方也是这个顺序）。

    反过来做（先存游标再处理）会丢消息：进程崩在两者之间，那条消息既被游标
    划过去、又没处理，永久丢失。先处理后存最坏只多收一次（幂等由 reply_fn 自己兜）。
    """
    if not buf:
        return
    try:
        from notifier import _db_exec
        _db_exec(
            "INSERT INTO sa_wx_ilink (field, value) VALUES (%s, %s) "
            "ON CONFLICT (field) DO UPDATE SET value = EXCLUDED.value,"
            " updated_at = now()", (CURSOR_KEY, buf))
    except Exception as exc:
        print(f"[wx-in] 游标落库失败（下一轮会重复收，已处理的消息不会重复回）: {exc}",
              flush=True)


def clear_cursor() -> None:
    """重置游标（解绑 / 换了微信号时用，否则新账号会拿旧游标去问服务端）。"""
    try:
        from notifier import _db_exec
        _db_exec("DELETE FROM sa_wx_ilink WHERE field = %s", (CURSOR_KEY,))
    except Exception:
        pass


# ---------------- 消息解析 ----------------

def extract_text(msg: dict) -> str:
    """从一条入站消息里抽纯文本（只取 type=1 TEXT，媒体/工具调用项忽略）。

    官方 MessageItemType：1=TEXT 2=IMAGE 3=VOICE 4=FILE 5=VIDEO 11/12=工具调用。
    非文本消息这里返回空串 —— 明确不猜：把「[图片]」当文本喂给 LLM 只会产生幻觉。
    """
    out = []
    for item in (msg.get("item_list") or []):
        if not isinstance(item, dict):
            continue
        if int(item.get("type") or 0) == ilink_client.ITEM_TYPE_TEXT:
            text = (item.get("text_item") or {}).get("text") or ""
            if text.strip():
                out.append(text.strip())
    return "\n".join(out)


def describe_media(msg: dict) -> str:
    """媒体消息的占位描述（只用于日志和页面展示，不进 LLM）。"""
    kinds = {2: "图片", 3: "语音", 4: "文件", 5: "视频"}
    seen = []
    for item in (msg.get("item_list") or []):
        if not isinstance(item, dict):
            continue
        t = int(item.get("type") or 0)
        if t in kinds and kinds[t] not in seen:
            seen.append(kinds[t])
    return "、".join(seen)


# ---------------- 轮询主循环 ----------------

def send_text(user_id: str, text: str, context_token: str = "") -> bool:
    """发一条纯文本（公开给 wx_chat 的补发逻辑用）。

    单独抽出来是因为长轮询回复与「窗口到期补发」是两处调用点，凭据读取和
    错误处理不该各写一遍。
    """
    from notifier import ilink_creds
    creds = ilink_creds()
    token = (creds.get("bot_token") or "").strip()
    if not token or not user_id:
        return False
    base = (creds.get("baseurl") or "").strip() or ilink_client.BASE_URL
    try:
        ilink_client.send_message(token, base, user_id, str(text)[:4000],
                                  context_token=context_token)
        return True
    except Exception as exc:
        print(f"[wx-in] 发送失败 to={user_id}: {exc}", flush=True)
        return False


def poll_once(deps: dict, reply_fn=None) -> dict:
    """一轮长轮询。返回 {ok, msgs, ret, errcode, error, paused}。

    只拉一次、只处理一次就返回 —— 主循环的退避/暂停判断在 `run_forever` 里，
    这样单轮可以独立测试。
    """
    from notifier import ilink_creds, record_inbound_context
    creds = ilink_creds()
    token = (creds.get("bot_token") or "").strip()
    if not token:
        return {"ok": False, "error": "未登录微信", "msgs": []}
    base = (creds.get("baseurl") or "").strip() or ilink_client.BASE_URL
    cursor = get_cursor()            # 一次读，末尾比对也复用它，别再查一遍库
    try:
        resp = ilink_client.get_updates(token, base, cursor,
                                        timeout_s=DEFAULT_LONG_POLL_TIMEOUT_S)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "msgs": []}

    ret = resp.get("ret")
    errcode = resp.get("errcode")
    if (ret not in (None, 0)) or (errcode not in (None, 0)):
        return {"ok": False, "ret": ret, "errcode": errcode,
                "errmsg": resp.get("errmsg") or "",
                "error": f"ret={ret} errcode={errcode} errmsg={resp.get('errmsg')}",
                "msgs": []}

    msgs = resp.get("msgs") or []
    handled = 0
    for msg in msgs:
        if not isinstance(msg, dict):
            continue
        user_id = (msg.get("from_user_id") or "").strip()
        ctx = msg.get("context_token") or ""
        # 只有用户消息才处理：message_type=1 是用户消息（types.ts 注释）
        if int(msg.get("message_type") or 1) != 1 or not user_id:
            continue
        if ctx:
            record_inbound_context(user_id, ctx)      # 缓存了，出站就能挂对对话
        text = extract_text(msg)
        media = describe_media(msg)
        if not text:
            if media:
                _set(last_inbound={"user": user_id, "text": f"[{media}]", "ts":
                                   time.strftime("%m-%d %H:%M:%S")})
                print(f"[wx-in] 收到 {media}（暂不支持解析）from={user_id}", flush=True)
            continue
        _set(last_inbound={"user": user_id, "text": text[:200],
                           "ts": time.strftime("%m-%d %H:%M:%S")})
        handled += 1
        if reply_fn is None:
            continue
        try:
            reply = reply_fn(text, user_id, ctx)
        except Exception as exc:
            print(f"[wx-in] reply_fn 抛错: {type(exc).__name__}: {exc}", flush=True)
            continue
        if not reply:
            continue
        if send_text(user_id, str(reply)[:4000], ctx):
            _set(reply_count=_state["reply_count"] + 1)

    # 处理完再存游标（顺序理由见 set_cursor 注释）
    new_buf = resp.get("get_updates_buf") or ""
    if new_buf and new_buf != cursor:
        set_cursor(new_buf)
    return {"ok": True, "msgs": len(msgs), "handled": handled, "ret": 0}


def run_forever(deps: dict, reply_fn=None, tick_fn=None) -> None:
    """常驻轮询线程体。退避/暂停策略抄官方 monitor.ts。

    tick_fn 每轮成功后调一次（wx_chat 用它补发合并窗口已过期的消息）。
    """
    fails = 0
    while True:
        try:
            r = poll_once(deps, reply_fn)
            _set(last_poll=time.strftime("%Y-%m-%d %H:%M:%S"), last_error=None)
            if not r.get("ok"):
                # -14 = token 过期。官方直接冷却一小时，别硬刚（每分钟失败一次的
                # 后果是账号被风控，而重新扫码本来也需要人来操作）。
                if r.get("errcode") == STALE_TOKEN_ERRCODE or r.get("ret") == STALE_TOKEN_ERRCODE:
                    _set(paused_until=time.time() + STALE_TOKEN_PAUSE_S,
                         last_error=f"token 过期(errcode -14)，冷却 1 小时；"
                                    f"需在「通知」页重新扫码登录")
                    print(f"[wx-in] token 过期，冷却 {STALE_TOKEN_PAUSE_S // 60} 分钟", flush=True)
                    time.sleep(STALE_TOKEN_PAUSE_S)
                    fails = 0
                    continue
                fails += 1
                _set(last_error=r.get("error"))
                if fails >= MAX_CONSECUTIVE_FAILURES:
                    print(f"[wx-in] 连续失败 {fails} 次，退避 {BACKOFF_DELAY_S}s："
                          f"{r.get('error')}", flush=True)
                    fails = 0
                    time.sleep(BACKOFF_DELAY_S)
                else:
                    time.sleep(RETRY_DELAY_S)
                continue
            fails = 0
            _set(poll_count=_state["poll_count"] + 1,
                 msg_count=_state["msg_count"] + (r.get("handled") or 0))
            if tick_fn is not None:
                try:
                    tick_fn()
                except Exception as exc:
                    print(f"[wx-in] tick_fn 异常: {type(exc).__name__}: {exc}", flush=True)
            # 长轮询本身已经阻塞了 ~35s，这里不再额外 sleep
        except Exception as exc:                      # 兜底，别让线程死掉
            _set(last_error=f"{type(exc).__name__}: {exc}")
            print(f"[wx-in] 轮询异常: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(BACKOFF_DELAY_S)
