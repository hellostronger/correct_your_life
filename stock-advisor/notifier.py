"""通知模块：微信推送（腾讯官方 iLink Bot）+ 邮件（SMTP），零成本。

微信通知走腾讯微信团队的官方 iLink Bot API（openclaw 的微信渠道插件
@tencent-weixin/openclaw-weixin 同款协议，腾讯官方维护）。个人微信没有
传统的 bot API，iLink Bot 是官方给 AI 助手/机器人开放的正规接入通道：

  1. 网页「通知」页点「扫码登录」→ 服务器调 ilink/bot/get_bot_qrcode
     生成二维码 → 用个人微信扫码（首次可能要输手机上显示的数字验证码）
  2. 确认后拿到 bot_token / ilink_bot_id / baseurl，存云库 sa_wx_ilink，
     并立刻调 ilink/bot/msg/notifystart 声明本客户端在线
  3. 之后调 send_wx() 推送到该微信（协议细节见 ilink_client.py）

收发模型：**不是**「必须用户先发一条消息」——2026-09-30 实测推翻了那个说法。
真正的机制是 `ilink/bot/msg/notifystart`（见 ensure_session）：绑定成功后、
以及服务每次启动后，向 iLink 声明本客户端在线，之后出站推送才有会话基础。
`context_token` 只影响消息能否挂到正确的对话上，**不是出站的必要条件**
（官方 sendMessageWeixin 在缺 context_token 时也只是 warn 一句就照发）。
不声明在线的表现是 `sendmessage ret=-2 errmsg=prepare failed`。

邮件通知走 SMTP：填发送邮箱（+授权码）和接收邮箱即可，支持 QQ/163/Gmail 等。
QQ/163 需要在邮箱设置里开启 SMTP 并生成「授权码」（不是登录密码）。

## 路由 = 渠道开关 × 事件开关（两级与）

`notify(title, content, event=...)` 不再是「按启用渠道广播」，而是查
`notify_events.EVENTS` 这张登记表决定发哪些渠道：

    要发某事件 → 该事件的渠道开关为真 **且** 该渠道总开关为真

配置文件里对应：

    notify:
      wx:   {enabled: true}
      email:{enabled: true, ...}
      events:
        mp_article: {wx: true, email: false}    # 公众号新文章：只推微信

没在 yaml 里显式配过的事件，用 notify_events 登记的默认值兜底。**事件参数
省略时按「已启用的全部渠道」广播**（保留旧行为，供 /api/notify/send 和
`python notifier.py send` 这类人工/外部调用）。

各模块过去自己持有的 `mp.notify_new` / `x.notify_new` / `sector.alert_notify`
/ `volume.notify` 仍被读取，用于「这个事件要不要触发」的粗粒度判断，与本层
的正交开关可叠加（粗粒度关掉 = 连扫描都不做；这里关掉 = 只不发）。

iLink 凭据存云库 sa_wx_ilink 表（绑定关系持久化，不落配置文件）。

用法：
    python notifier.py                          # 打印配置
    python notifier.py test wx                  # 测微信
    python notifier.py test email               # 测邮件
    python notifier.py send "标题" "内容"        # 通用发送（供 Claude 定时任务调用）
"""

import base64
import json
import smtplib
import time
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

import requests

import conf_util  # WRITE_LOCK：config.yaml 是读-改-写整文件，并发写会丢段
import ilink_client  # 腾讯 iLink Bot API 客户端（见该文件头部协议说明）
import news_fetcher  # 复用 load_config / _write_conf 的 config.yaml 读写
import notify_events

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.yaml"

# SMTP 常用服务商预设：不填 host/port 时按发件邮箱后缀推断
SMTP_PRESETS = {
    "qq.com":     ("smtp.qq.com", 465),
    "163.com":    ("smtp.163.com", 465),
    "126.com":    ("smtp.126.com", 465),
    "gmail.com":  ("smtp.gmail.com", 465),
    "outlook.com": ("smtp.office365.com", 587),
    "hotmail.com": ("smtp.office365.com", 587),
    "foxmail.com": ("smtp.qq.com", 465),
    "sina.com":   ("smtp.sina.com", 465),
    "sohu.com":   ("smtp.sohu.com", 465),
}

DEFAULT_NOTIFY_CONF = {
    "wx": {
        "enabled": False,
    },
    "email": {
        "smtp_host": "",       # 留空则按 from_addr 后缀推断
        "smtp_port": 0,        # 0 = 自动（465 SSL / 587 STARTTLS）
        "from_addr": "",       # 发件邮箱
        "auth_code": "",       # 授权码（QQ/163 等不是登录密码）
        "to_addr": "",         # 接收邮箱
        "enabled": False,
    },
}


def _default_events_conf() -> dict:
    """{事件: {渠道: bool}} 的默认值（来自 notify_events 登记表）。"""
    return {ev: notify_events.event_default(ev) for ev in notify_events.EVENTS}


DEFAULT_NOTIFY_CONF["events"] = _default_events_conf()


def load_notify_conf() -> dict:
    """读 config.yaml 的 notify 段，补齐默认值。

    渠道段（wx/email）与 events 矩阵分开处理：矩阵是**两层**的
    `events.<事件>.<渠道>`，只做逐事件补齐（用户在 yaml 里写一半也不会崩）。
    """
    text = CONFIG_FILE.read_text(encoding="utf-8") if CONFIG_FILE.exists() else ""
    conf = json.loads(json.dumps(DEFAULT_NOTIFY_CONF))  # deep copy
    if not text:
        return conf
    notify = {}
    try:
        import yaml
        data = yaml.safe_load(text) or {}
        # ⚠️ except 必须兜住所有异常，不能只兜 ImportError：config.yaml 一旦
        # 因为别的原因解析失败（手改坏了缩进），这里抛 ParserError 会让
        # **每一个**读通知配置的守护线程和页面都 500 —— 通知模块本来是
        # 「坏了也不该影响主流程」的，坏成这样反而成了故障源。降级成默认值。
        if isinstance(data, dict):
            notify = data.get("notify") or {}
    except Exception:
        notify = {}
    if not isinstance(notify, dict):
        notify = {}
    for section in ("wx", "email"):
        if isinstance(notify.get(section), dict):
            conf[section].update(notify[section])
    events = notify.get("events")
    if isinstance(events, dict):
        for ev, chans in events.items():
            if not isinstance(chans, dict):
                continue
            # 未登记的事件也接受（用户可能手工加），按 notify_events 的默认补齐
            target = conf["events"].setdefault(ev, notify_events.event_default(ev))
            for ch in notify_events.CHANNELS:
                if ch in chans:
                    target[ch] = bool(chans[ch])
    return conf


def _save_notify_conf_locked(conf: dict) -> None:
    """实际写盘（调用方需已持有 conf_util.WRITE_LOCK）。"""
    text = CONFIG_FILE.read_text(encoding="utf-8") if CONFIG_FILE.exists() else ""
    # ⚠️ 写之前必须确认**旧文件是能解析的**。整段替换是破坏性操作：一旦
    # config.yaml 因为别的原因坏了（手改少了缩进、某个脚本写坏），这里读到空
    # dict → 拿默认值整段盖回去 → 用户的 SMTP 授权码被默认值（空）覆盖且无法恢复。
    # 宁可拒绝写并报错，让用户去修 yaml。
    if text.strip():
        try:
            import yaml
            if not isinstance(yaml.safe_load(text), dict):
                raise ValueError("顶层不是映射")
        except Exception as exc:
            raise RuntimeError(
                f"config.yaml 解析失败（{exc}），已拒绝写入以免覆盖 notify 段里的"
                f"真实配置。请先修好 config.yaml 再试。") from exc
    CONFIG_FILE.write_text(_render_notify_checked(conf, text), encoding="utf-8")


def _render_notify_checked(conf: dict, text: str) -> str:
    """把新块替换进原文件文本，返回**已验证可被 YAML 解析**的整份内容。

    为什么要有这一步：`notify:` 段里同时塞着邮箱授权码、SMTP 参数和一张
    事件矩阵，是这个项目里最容易写出「缩进错位 / 同名键出现两次」的地方，
    而 config.yaml 一旦坏掉，**全项目所有配置一起失效**（守护线程读不到
    就静默停摆，页面报「未知键」）。写盘前先在内存里解析一遍，不合法就抛，
    磁盘上那份仍然是好��。
    """
    block = _render_notify_block(conf)
    lines = text.splitlines() if text else []
    start = None
    for i, line in enumerate(lines):
        if line.rstrip() == "notify:":
            start = i
            break
    if start is not None:
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if lines[j] and not lines[j][0].isspace() and not lines[j].startswith("#"):
                end = j
                break
        while start > 0 and lines[start - 1].startswith("#"):
            start -= 1
        lines[start:end] = block.splitlines()
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(block.splitlines())
    out = "\n".join(lines) + "\n"
    try:
        import yaml
        data = yaml.safe_load(out)
        if not isinstance(data, dict) or "notify" not in data:
            raise ValueError("渲染结果缺少 notify 段")
        got = (data["notify"] or {}).get("events")
        want = conf.get("events") or {}
        if not isinstance(got, dict):
            raise ValueError(f"events 不是映射（{type(got).__name__}）")
        # 比对**键集合**而不只是「是不是 dict」：缩进错位时 YAML 仍可能解析
        # 成功，只是把某个事件挪到了顶层去（实测 mp_article 就这样跑到了
        # 顶层，events 变成空 —— 只查 isinstance 完全发现不了）。
        if set(got) != set(want):
            raise ValueError(f"事件键集合对不上：多 {sorted(set(got) - set(want))}，"
                             f"少 {sorted(set(want) - set(got))}")
        for ev, chans in want.items():
            for ch in notify_events.CHANNELS:
                if bool(chans.get(ch)) != bool(got[ev].get(ch)):
                    raise ValueError(f"{ev}.{ch} 路由回读不一致")
    except Exception as exc:
        raise RuntimeError(f"notify 段渲染结果不合法（{exc}），已放弃写入") from exc
    return out


def save_notify_conf(conf: dict) -> None:
    """把 notify 段写回 config.yaml（保留文件里其余内容，含注释）。

    整段替换 = 读-改-写整文件，必须在 conf_util.WRITE_LOCK 内做，否则与 news
    段、调度段、事件矩阵的写操作互相丢段。锁由本函数自己取，调用方不要重复
    持有（threading.Lock 不可重入）。
    """
    with conf_util.WRITE_LOCK:
        _save_notify_conf_locked(conf)


def save_notify_events(events: dict) -> None:
    """只改事件矩阵，渠道段（wx/email/SMTP）原样保留。

    **刻意不用 conf_util.set_nested_key 逐键写**：那个函数是行级手术，往
    `notify` 段里新建 `events.<事件>.<渠道>` 这样的**三层**路径时，第一次调用
    插入 `events:` 空壳，第二次调用在其内部再插一层 —— 实测把 config.yaml
    写成了「同名键出现两次 + 缩进错位」，YAML 随即解析失败，**整个配置文件
    读不出来**（38 个事件要调 38 次，每次都失败）。

    整段重渲染是一次原子操作、只读写一遍文件，顺带把 38 次文件 I/O 降到 1 次
    （之前「只留微信」那个批量按钮 60 秒都没返回）。
    """
    def _do() -> None:
        conf = load_notify_conf()
        conf["events"] = events
        _save_notify_conf_locked(conf)
    with conf_util.WRITE_LOCK:
        _do()


def _yaml_scalar(value) -> str:
    """渲染一个 YAML 标量。空串必须写成 ''（裸空会让该行变成 null）。

    **交给 yaml.safe_dump 决定要不要加引号，不自己判断。** 手写规则一定会漏：
    最初漏了「以 `- ` 开头」这一类，于是 `from_addr: - dash` 被解析成序列，
    整个 config.yaml 从那一行开始报废（本人邮箱字段实测踩到）。
    safe_dump 的转义规则就是解析器的逆运算，没有漏的可能。
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    # 换行的值无法表示成「一行一个标量」的段内字段，硬塞会把后面整段顶到
    # 错误位置。直接拒绝（邮箱地址/主机名/授权码里出现换行本就是 bug）。
    if "\n" in s or "\r" in s:
        raise RuntimeError(f"notify 段不接受含换行的值：{s[:40]!r}")
    import yaml
    out = yaml.safe_dump(s, default_flow_style=True, allow_unicode=True, width=10 ** 6)
    # safe_dump 对**纯文本**标量（如「中文:冒号」）会在末尾补文档结束标记
    # `...`，直接用会把它写成新的一行，后面所有键的缩进就都错了。
    # 带引号的那种不带，所以只切这一种后缀。
    line = out.split("\n...\n", 1)[0].strip()
    return line or "''"


def _render_notify_block(conf: dict) -> str:
    wx, email = conf["wx"], conf["email"]
    events = conf.get("events") or {}
    # 已登记事件按登记表顺序在前，用户自建事件追加在后，保证 yaml 可读
    ordered = [ev for ev in notify_events.EVENTS if ev in events]
    ordered += [ev for ev in sorted(events) if ev not in notify_events.EVENTS]
    lines = [
        "# 通知配置（微信走腾讯官方 iLink Bot，凭据存云库；邮件走 SMTP）",
        "# 网页「🔔 通知」标签页可改：上面是渠道，下面 events 是「事件→渠道」矩阵。",
        "# 实际发送条件 = 渠道总开关 AND 该事件的该渠道开关（两级与）。",
        "# auth_code 是邮箱授权码，注意保管。",
        "notify:",
        "  wx:",
        f"    enabled: {str(bool(wx.get('enabled'))).lower()}",
        "  email:",
        f"    smtp_host: {_yaml_scalar(email.get('smtp_host', ''))}",
        f"    smtp_port: {int(email.get('smtp_port') or 0)}",
        f"    from_addr: {_yaml_scalar(email.get('from_addr', ''))}",
        f"    auth_code: {_yaml_scalar(email.get('auth_code', ''))}",
        f"    to_addr: {_yaml_scalar(email.get('to_addr', ''))}",
        f"    enabled: {str(bool(email.get('enabled'))).lower()}",
    ]
    if ordered:
        lines.append("  # 事件 → 渠道开关矩阵（网页可勾选；缺省项用 notify_events.py 的默认值）")
        lines.append("  events:")
        for ev in ordered:
            chans = events[ev] or {}
            meta = notify_events.EVENTS.get(ev)
            lines.append(f"    {ev}:")
            for ch in notify_events.CHANNELS:
                lines.append(f"      {ch}: {str(bool(chans.get(ch))).lower()}")
    return "\n".join(lines)


# ---------------- 微信（腾讯官方 iLink Bot API） ----------------

# 云库里的凭据 KV 表：bot_token / ilink_bot_id / baseurl / ilink_user_id
ILINK_FIELDS = ("bot_token", "ilink_bot_id", "baseurl", "ilink_user_id")


def _db_exec(sql: str, params: tuple = (), fetch: str | None = None):
    """云库短连接执行（库偶发不稳，调用方自行 try/except 兜底）。"""
    from app import get_conn
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        if fetch == "one":
            return cur.fetchone()
        if fetch == "all":
            return cur.fetchall()
        conn.commit()
    return None


def ilink_creds() -> dict:
    """从云库读已登录的 iLink 凭据；库不可用或未登录返回空 dict。"""
    try:
        rows = _db_exec(
            "SELECT field, value FROM sa_wx_ilink WHERE field = ANY(%s)",
            (list(ILINK_FIELDS),), fetch="all") or []
        return {k: v for k, v in rows if v}
    except Exception:
        return {}


def ilink_creds_save(creds: dict) -> None:
    """确认登录成功后把凭据写进云库（UPSERT，字段级）。"""
    for field in ILINK_FIELDS:
        value = (creds.get(field) or "").strip()
        if not value:
            continue
        _db_exec(
            "INSERT INTO sa_wx_ilink (field, value) VALUES (%s, %s) "
            "ON CONFLICT (field) DO UPDATE SET value = EXCLUDED.value, "
            "updated_at = now()", (field, value))


def ensure_session(creds: dict | None = None) -> dict:
    """向 iLink 声明本客户端在线（notifystart），让出站推送具备会话基础。

    **每次绑定成功后、以及服务启动时都该调一次。** 没有这一步，iLink 侧不认为
    存在活跃客户端，出站 sendmessage 会一直 `ret=-2 prepare failed`，
    而真实原因（没声明在线）在任何一处报错里都看不到。

    抄自官方 @tencent-weixin/openclaw-weixin 2.4.9 的
    `src/channel.ts::startAccount` —— 它在启动 monitor 长轮询之前无条件调
    `notifyStart`，失败也只 warn 不阻塞。实测本机 ret=0。

    返回 {ok, ret, errmsg}。**不抛异常**：它是尽力而为的增强，失败不该
    阻断「已经登录成功」这个事实。
    """
    creds = creds or ilink_creds()
    token = (creds.get("bot_token") or "").strip()
    if not token:
        return {"ok": False, "error": "没有 bot_token，未登录"}
    base = (creds.get("baseurl") or "").strip() or ilink_client.BASE_URL
    try:
        resp = ilink_client.notify_start(token, base)
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        print(f"[wx] notifystart 失败: {err}", flush=True)
        return {"ok": False, "error": err}
    ret = resp.get("ret")
    ok = ret in (None, 0)
    if not ok:
        print(f"[wx] notifystart ret={ret} errmsg={resp.get('errmsg')}", flush=True)
    return {"ok": ok, "ret": ret, "errmsg": resp.get("errmsg") or ""}


def ilink_creds_clear() -> int:
    """解绑：清空云库凭据与用户缓存。"""
    creds = ilink_creds()
    token = (creds.get("bot_token") or "").strip()
    if token:
        # 先告知服务端下线再清凭据：否则服务端仍认为这个 bot 在线，
        # 下一个绑定同一个微信号的实例要等它自己超时。
        try:
            ilink_client.notify_stop(token,
                                     (creds.get("baseurl") or "").strip()
                                     or ilink_client.BASE_URL)
        except Exception as exc:
            print(f"[wx] notifystop 失败（忽略）: {exc}", flush=True)
    n1 = _db_exec("DELETE FROM sa_wx_ilink")
    n2 = _db_exec("DELETE FROM sa_wx_users")
    return int(n1 or 0) + int(n2 or 0)


def create_login_qrcode() -> dict:
    """发起 iLink 扫码登录：生成二维码。

    返回 {qrcode, qrcode_png_b64, valid_seconds}：
    - qrcode 是后续 get_qrcode_status 轮询用的句柄（勿泄露给前端）
    - qrcode_png_b64 是二维码图片的 base64 PNG，前端 <img src="data:image/png;base64,...">
    """
    import qrcode
    resp = ilink_client.get_bot_qrcode()
    if resp.get("ret") not in (None, 0):
        raise RuntimeError(f"获取二维码失败: {resp.get('err_msg') or resp}")
    link = resp["qrcode_img_content"]          # 要编码成二维码的 URL
    img = qrcode.make(link)
    png = img.pil_image if hasattr(img, "pil_image") else img
    import io
    buf = io.BytesIO()
    png.save(buf, format="PNG")
    return {
        "qrcode": resp["qrcode"],
        "qrcode_png_b64": base64.b64encode(buf.getvalue()).decode(),
        "valid_seconds": 300,   # 官方登录会话 5 分钟 TTL，过期需重新生成
    }


def poll_login_status(qrcode: str, verify_code: str = "") -> dict:
    """长轮询一次扫码状态（服务端挂起约 35s；前端别频繁调，一次调用等它返回）。

    返回 {status, ...}：wait=还没扫 / scaned=已扫码验证中 /
    need_verifycode=要求输手机上的数字（前端收集后带 verify_code 再调） /
    expired=二维码过期需重新生成 / confirmed=成功（此时凭据已落库） /
    scaned_but_redirect=已扫码且要切 IDC（内部自动跟随，前端当 wait 处理）。
    """
    base_url = ilink_client.BASE_URL
    status = None
    # scaned_but_redirect：扫码确认的 IDC 重定向，后续轮询/请求换基址（官方同款处理）
    # 最多跟 3 次防循环
    for _ in range(3):
        status = ilink_client.get_qrcode_status(
            qrcode, verify_code=verify_code, base_url=base_url)
        if status.get("status") == "scaned_but_redirect" and status.get("redirect_host"):
            base_url = f"https://{status['redirect_host']}"
            verify_code = ""
            continue
        break
    st = status.get("status")
    if st == "confirmed":
        if not status.get("ilink_bot_id") or not status.get("bot_token"):
            return {"status": "error", "error": "服务器未返回完整凭据（bot_token/ilink_bot_id 缺失）"}
        saved = {
            "bot_token": status["bot_token"],
            "ilink_bot_id": status["ilink_bot_id"],
            "baseurl": status.get("baseurl") or base_url,
            "ilink_user_id": status.get("ilink_user_id") or "",
        }
        ilink_creds_save(saved)
        # 绑定成功即声明在线 —— 否则用户不主动发消息的话出站会一直
        # ret=-2 prepare failed。抄官方 channel.ts::startAccount 的 notifyStart。
        s = ensure_session(saved)
        return {"status": "confirmed", "bot_id": status["ilink_bot_id"],
                "session": s}
    out = {"status": st or "wait"}
    if st == "scaned_but_redirect":
        out["status"] = "wait"  # 前端无需感知 IDC 切换
    return out


def _context_token_for(user_id: str) -> str:
    """读某用户最近一次入站消息的 context_token（iLink 出站要回传它）。"""
    if not user_id:
        return ""
    try:
        row = _db_exec(
            "SELECT context_token FROM sa_wx_users WHERE ilink_user_id = %s",
            (user_id,), fetch="one")
        return (row[0] if row else "") or ""
    except Exception:
        return ""


def record_inbound_context(user_id: str, context_token: str) -> None:
    """收到该用户的入站消息时缓存 context_token（供之后主动推送用）。"""
    if not user_id or not context_token:
        return
    try:
        _db_exec(
            "INSERT INTO sa_wx_users (ilink_user_id, context_token) VALUES (%s, %s) "
            "ON CONFLICT (ilink_user_id) DO UPDATE SET context_token = EXCLUDED.context_token",
            (user_id, context_token))
    except Exception:
        pass


def send_wx(title: str, content: str, conf: dict | None = None) -> dict:
    """推送到已绑定的微信（iLink Bot）。content 纯文本（微信不支持 Markdown 渲染）。

    收件人优先级：
      1. `sa_wx_users` 里给 bot 发过消息的用户（有 context_token，出站最稳）
      2. 绑定者本人的 `ilink_user_id`（**兜底即可**：2026-09-30 实测，
         没有 context_token 也能送达，前提是 notifystart 已声明过在线）

    全失败时**必须带顶层 `error`**：原来只把错误塞在 results[] 里，
    而 `/api/notify/test` 只读顶层 error，于是对外只显示「发送失败」，
    把 `sendmessage ret=-2 errmsg=prepare failed` 整个吞掉 ——
    2026-09-30 排查这个问题时在这上面白绕了很久。
    """
    creds = ilink_creds()
    if not creds.get("bot_token"):
        return {"ok": False, "channel": "wx",
                "error": "尚未登录微信，请先在「通知」页扫码登录"}
    try:
        rows = _db_exec(
            "SELECT ilink_user_id FROM sa_wx_users ORDER BY bound_at",
            fetch="all") or []
    except Exception:
        rows = []
    recips = [r[0] for r in rows if r[0]]
    fallback = False
    if not recips and creds.get("ilink_user_id"):
        recips = [creds["ilink_user_id"]]
        fallback = True
    if not recips:
        return {"ok": False, "channel": "wx",
                "error": "没有可推送的收件人：扫码登录返回里没有 ilink_user_id，"
                         "请重新扫码绑定"}
    text = f"{title}\n\n{content}" if content else title
    results, ok_any = [], False
    for uid in recips:
        try:
            ilink_client.send_message(
                creds["bot_token"], creds.get("baseurl") or ilink_client.BASE_URL,
                uid, text, context_token=_context_token_for(uid))
            results.append({"uid": uid, "ok": True})
            ok_any = True
        except Exception as exc:
            results.append({"uid": uid, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    out = {"ok": ok_any, "channel": "wx", "results": results}
    if not ok_any:
        first = next((r["error"] for r in results if r.get("error")), "未知错误")
        out["error"] = first
        # 把"为什么"说清楚。ret=-2 prepare failed 在 iLink 侧的含义是
        # 「这个 bot 客户端没有活跃会话」—— 绝大多数情况是没调 notifystart
        # （服务重启后没人重新声明在线），而不是消息内容有问题。
        if "prepare failed" in first:
            out["hint"] = ("iLink 返回 prepare failed：该 bot 当前没有活跃会话。"
                           "点「重建会话」重新声明在线即可；仍失败则需在微信里"
                           "给 bot 发一条消息（微信侧要求绑定后至少有一次交互）。")
        elif fallback:
            out["hint"] = ("收件人是绑定者兜底（没收到过入站消息）。"
                           "在微信里给 bot 发一条后会记下 context_token，更稳。")
    return out


# ---------------- 邮件（SMTP） ----------------

def _smtp_params(email_conf: dict) -> tuple[str, int]:
    """推断 SMTP host/port：显式配置优先，否则按发件邮箱后缀查预设表。"""
    host = (email_conf.get("smtp_host") or "").strip()
    port = int(email_conf.get("smtp_port") or 0)
    from_addr = (email_conf.get("from_addr") or "").strip()
    if not host:
        domain = from_addr.rsplit("@", 1)[-1].lower() if "@" in from_addr else ""
        host, port = SMTP_PRESETS.get(domain, ("", 0))
    if not host:
        raise RuntimeError(f"无法推断 {from_addr} 的 SMTP 服务器，请填写 smtp_host")
    if not port:
        port = 465
    return host, port


def send_email(subject: str, body: str, conf: dict | None = None,
               html: bool = False) -> dict:
    """发邮件到配置的接收邮箱。auth_code 是授权码，不是邮箱登录密码。"""
    conf = conf or load_notify_conf()
    email = conf.get("email") or {}
    from_addr = (email.get("from_addr") or "").strip()
    to_addr = (email.get("to_addr") or "").strip()
    auth_code = (email.get("auth_code") or "").strip()
    if not (from_addr and to_addr and auth_code):
        missing = [k for k, v in (("from_addr", from_addr), ("to_addr", to_addr),
                                  ("auth_code", auth_code)) if not v]
        return {"ok": False, "channel": "email", "error": f"邮件配置缺字段: {', '.join(missing)}"}
    try:
        host, port = _smtp_params(email)
    except RuntimeError as exc:
        return {"ok": False, "channel": "email", "error": str(exc)}

    subtype = "html" if html else "plain"
    msg = MIMEMultipart()
    msg.attach(MIMEText(body, subtype, "utf-8"))
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr(("Stock Advisor", from_addr))
    msg["To"] = to_addr

    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=20)
        else:  # 587 等走 STARTTLS
            server = smtplib.SMTP(host, port, timeout=20)
            server.starttls()
        with server:
            server.login(from_addr, auth_code)
            server.sendmail(from_addr, [to_addr], msg.as_string())
        return {"ok": True, "channel": "email", "to": to_addr}
    except smtplib.SMTPAuthenticationError:
        return {"ok": False, "channel": "email",
                "error": "SMTP 认证失败：请确认用的是授权码而非登录密码（QQ/163 在邮箱设置里开启 SMTP 生成）"}
    except Exception as exc:
        return {"ok": False, "channel": "email", "error": f"{type(exc).__name__}: {exc}"}


# ---------------- 通用发送 ----------------

def resolve_channels(event: str | None = None, conf: dict | None = None,
                     channels: list[str] | None = None) -> list[str]:
    """算出这次通知该走哪些渠道。

    规则（从松到紧，后一条覆盖前一条）：
      1. 渠道总开关开着哪些 → 候选集
      2. 传了 event → 与该事件在 events 矩阵里打开的渠道取交集
      3. 传了 channels → 再与它取交集（`/api/notify/send` 的手动指定）

    event=None 时不查矩阵（按候选集广播），保留旧的通用入口行为。
    交集为空**不**回退到全渠道 —— 交集为空正是「用户明确关掉了这个渠道」，
    回退会让页面上刚点的关闭开关完全失效。
    """
    conf = conf or load_notify_conf()
    enabled = [ch for ch in notify_events.CHANNELS
               if (conf.get(ch) or {}).get("enabled")]
    if event:
        ev = (conf.get("events") or {}).get(event) or notify_events.event_default(event)
        allowed = [ch for ch in notify_events.CHANNELS if ev.get(ch)]
        enabled = [ch for ch in enabled if ch in allowed]
    if channels:
        enabled = [ch for ch in enabled if ch in channels]
    return enabled


def event_enabled(event: str, conf: dict | None = None) -> bool:
    """该事件至少有一个可用渠道（用来让调用方跳过昂贵的组装/扫描）。"""
    return bool(resolve_channels(event, conf))


def notify(title: str, content: str = "", conf: dict | None = None,
           channels: list[str] | None = None, event: str | None = None) -> dict:
    """发一条通知。

    event 传了就按 events 矩阵决定渠道（见 resolve_channels）；不传则按已启用的
    全部渠道广播（`POST /api/notify/send` 与 `python notifier.py send` 用这条）。

    各渠道独立成败，互不影响；没有可用渠道时 sent=False，reason 说明是
    「渠道总开关没开」还是「该事件的渠道都被关了」—— 排查时这两种要分得清。
    """
    conf = conf or load_notify_conf()
    enabled = resolve_channels(event, conf, channels)
    if not enabled:
        if event:
            matrix = (conf.get("events") or {}).get(event) or notify_events.event_default(event)
            turned_off = [ch for ch in notify_events.CHANNELS
                          if not (conf.get(ch) or {}).get("enabled")]
            reason = (f"事件「{event}」在 notify.events 里打开的渠道"
                      f"（{', '.join(ch for ch in notify_events.CHANNELS if matrix.get(ch)) or '无'}）"
                      f"与已启用渠道（{', '.join(turned_off) or '无'}）没有交集")
        else:
            reason = "未启用任何通知渠道（config.yaml notify 段）"
        return {"sent": False, "event": event, "reason": reason, "results": []}
    results = []
    for ch in enabled:
        fn = send_wx if ch == "wx" else send_email
        try:
            results.append(fn(title, content or title, conf))
        except Exception as exc:
            results.append({"ok": False, "channel": ch,
                            "error": f"{type(exc).__name__}: {exc}"})
    return {"sent": any(r.get("ok") for r in results), "event": event,
            "channels": enabled, "results": results}


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if args[:1] == ["test"] and args[1:2]:
        channel = args[1]
        res = send_wx("Stock Advisor 测试", "微信通知配置成功 ✓\n收到此消息说明绑定生效。") \
            if channel == "wx" else \
            send_email("Stock Advisor 测试", "邮件通知配置成功 ✓\n收到此邮件说明 SMTP 配置正确。")
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif args[:1] == ["send"]:
        print(json.dumps(notify(args[1], args[2] if len(args) > 2 else ""),
                         ensure_ascii=False, indent=2))
    else:
        print(json.dumps(load_notify_conf(), ensure_ascii=False, indent=2))
