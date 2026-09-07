"""通知模块：微信推送（腾讯官方 iLink Bot）+ 邮件（SMTP），零成本。

微信通知走腾讯微信团队的官方 iLink Bot API（openclaw 的微信渠道插件
@tencent-weixin/openclaw-weixin 同款协议，腾讯官方维护）。个人微信没有
传统的 bot API，iLink Bot 是官方给 AI 助手/机器人开放的正规接入通道：

  1. 网页「通知」页点「扫码登录」→ 服务器调 ilink/bot/get_bot_qrcode
     生成二维码 → 用个人微信扫码（首次可能要输手机上显示的数字验证码）
  2. 确认后拿到 bot_token / ilink_bot_id / baseurl，存云库 sa_wx_ilink
  3. 之后调 send_wx() 推送到该微信（协议细节见 ilink_client.py）

收发模型：iLink bot 是被动会话制——用户先给 bot 发过一条消息后，出站推送
才有着落（context_token 按用户缓存于 sa_wx_users）。所以绑定后先在微信里
随便给 bot 发一句，之后就能收到报告推送。

邮件通知走 SMTP：填发送邮箱（+授权码）和接收邮箱即可，支持 QQ/163/Gmail 等。
QQ/163 需要在邮箱设置里开启 SMTP 并生成「授权码」（不是登录密码）。

配置存在 config.yaml 的 notify 段（与新闻配置同一文件，网页可改），
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

import ilink_client  # 腾讯 iLink Bot API 客户端（见该文件头部协议说明）
import news_fetcher  # 复用 load_config / _write_conf 的 config.yaml 读写

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


def load_notify_conf() -> dict:
    """读 config.yaml 的 notify 段，补齐默认值。"""
    text = CONFIG_FILE.read_text(encoding="utf-8") if CONFIG_FILE.exists() else ""
    conf = json.loads(json.dumps(DEFAULT_NOTIFY_CONF))  # deep copy
    if not text:
        return conf
    try:
        import yaml
        data = yaml.safe_load(text) or {}
    except ImportError:
        data = {}
    notify = (data.get("notify") or {}) if isinstance(data, dict) else {}
    for section in ("wx", "email"):
        if isinstance(notify.get(section), dict):
            conf[section].update(notify[section])
    return conf


def save_notify_conf(conf: dict) -> None:
    """把 notify 段写回 config.yaml（保留文件里其余内容，含注释）。

    简单做法：按行扫描，已存在 notify: 块则整体替换，否则追加到文件尾。
    """
    text = CONFIG_FILE.read_text(encoding="utf-8") if CONFIG_FILE.exists() else ""
    block = _render_notify_block(conf)
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.rstrip() == "notify:":
            start = i
            break
    if start is not None:  # 删掉旧 notify: 块（含上方紧邻注释行，到下一个顶层 key）
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
    CONFIG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _render_notify_block(conf: dict) -> str:
    wx, email = conf["wx"], conf["email"]
    return "\n".join([
        "# 通知配置（微信走腾讯官方 iLink Bot，凭据存云库；邮件走 SMTP）",
        "# 网页「通知」标签页可改；auth_code 是邮箱授权码，注意保管",
        "notify:",
        "  wx:",
        f"    enabled: {str(bool(wx.get('enabled'))).lower()}",
        "  email:",
        f"    smtp_host: {email.get('smtp_host', '')}",
        f"    smtp_port: {int(email.get('smtp_port') or 0)}",
        f"    from_addr: {email.get('from_addr', '')}",
        f"    auth_code: {email.get('auth_code', '')}",
        f"    to_addr: {email.get('to_addr', '')}",
        f"    enabled: {str(bool(email.get('enabled'))).lower()}",
    ])


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


def ilink_creds_clear() -> int:
    """解绑：清空云库凭据与用户缓存。"""
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
        ilink_creds_save({
            "bot_token": status["bot_token"],
            "ilink_bot_id": status["ilink_bot_id"],
            "baseurl": status.get("baseurl") or base_url,
            "ilink_user_id": status.get("ilink_user_id") or "",
        })
        return {"status": "confirmed", "bot_id": status["ilink_bot_id"]}
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

    收件人 = 绑定后给 bot 发过消息的用户（sa_wx_users）；
    一次都没发过消息的用户收不到推送（iLink 被动会话制，绑定后请先发一句）。
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
    # 没有用户记录时兜底：绑定流程里 ilink_user_id 就是绑定者本人
    if not recips and creds.get("ilink_user_id"):
        recips = [creds["ilink_user_id"]]
    if not recips:
        return {"ok": False, "channel": "wx",
                "error": "还没有微信用户和 bot 建立会话，请在微信里先给 bot 发一条消息"}
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
    return {"ok": ok_any, "channel": "wx", "results": results}


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

def notify(title: str, content: str = "", conf: dict | None = None,
           channels: list[str] | None = None) -> dict:
    """按启用的渠道广播通知（可传 channels 强制指定 ["wx","email"]）。

    各渠道独立成败，互不影响；两个渠道都未启用时返回 enabled=False。
    """
    conf = conf or load_notify_conf()
    enabled = [ch for ch in ("wx", "email")
               if (conf.get(ch) or {}).get("enabled")]
    if channels:
        enabled = [ch for ch in enabled if ch in channels] or list(channels)
    if not enabled:
        return {"sent": False, "reason": "未启用任何通知渠道（config.yaml notify 段）",
                "results": []}
    results = []
    for ch in enabled:
        fn = send_wx if ch == "wx" else send_email
        try:
            results.append(fn(title, content or title, conf))
        except Exception as exc:
            results.append({"ok": False, "channel": ch,
                            "error": f"{type(exc).__name__}: {exc}"})
    return {"sent": any(r.get("ok") for r in results), "results": results}


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
