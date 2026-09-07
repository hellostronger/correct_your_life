"""腾讯微信 iLink Bot API 客户端（纯 Python）。

iLink Bot 是腾讯微信团队提供的官方机器人接入通道（npm 包
@tencent-weixin/openclaw-weixin，维护者均为 @tencent.com 邮箱），但官方只发
了 Node 版插件、没有 Python 包。本模块按该插件（2.4.8）的线上协议直接实现
HTTP 调用，字段与官方插件对齐，零第三方依赖（仅 requests）。

协议要点（逆向自插件 dist/src/api/api.js、auth/login-qr.js、messaging/send.js）：
- 基址 https://ilinkai.weixin.qq.com；扫码确认时可能按用户 IDC 重定向
  （get_qrcode_status 返回 scaned_but_redirect + redirect_host，之后请求换基址）
- 登录：POST ilink/bot/get_bot_qrcode?bot_type=3 → 用户微信扫码 →
  GET ilink/bot/get_qrcode_status?qrcode=...（服务端长轮询约 35s）
  状态机：wait / scaned / need_verifycode / expired / verify_code_blocked /
  binded_redirect / scaned_but_redirect / confirmed
  confirmed 返回 bot_token / ilink_bot_id / baseurl / ilink_user_id
- 收消息：POST ilink/bot/getupdates 长轮询，响应里的 get_updates_buf 是
  游标，必须持久化并在下次请求带回
- 发消息：POST ilink/bot/sendmessage；出站消息应回传该用户最近一条入站
  消息携带的 context_token（官方插件按用户缓存并持久化到磁盘）
- 公共头：iLink-App-Id / iLink-App-ClientVersion；带 token 的 POST 另加
  AuthorizationType: ilink_bot_token + Authorization: Bearer + X-WECHAT-UIN
"""

import base64
import os
import random
import time

import requests

# 对齐 @tencent-weixin/openclaw-weixin@2.4.8 的协议版本与 appid
PLUGIN_VERSION = "2.4.8"
APP_ID = "bot"                # npm 包 package.json 里的 ilink_appid
BASE_URL = "https://ilinkai.weixin.qq.com"
BOT_AGENT = "OpenClaw"        # 官方插件默认 bot_agent（相当于 UA 兜底值）

# 枚举（dist/src/api/types.js）
MSG_TYPE_BOT = 2              # MessageType.BOT
MSG_STATE_FINISH = 2          # MessageState.FINISH
ITEM_TYPE_TEXT = 1            # MessageItemType.TEXT


class IlinkError(RuntimeError):
    """iLink 接口返回 ret != 0 之类的业务错误。"""


def _client_version(version: str) -> int:
    """版本字符串 → (major&0xff)<<16 | (minor&0xff)<<8 | patch，官方同款编码。"""
    parts = (version.split(".") + ["0", "0", "0"])[:3]
    major, minor, patch = (int(p) if p.isdigit() else 0 for p in parts)
    return ((major & 0xFF) << 16) | ((minor & 0xFF) << 8) | (patch & 0xFF)


_CLIENT_VERSION = _client_version(PLUGIN_VERSION)


def _common_headers() -> dict:
    """GET 与 POST 共用的头（官方 buildCommonHeaders）。"""
    return {
        "iLink-App-Id": APP_ID,
        "iLink-App-ClientVersion": str(_CLIENT_VERSION),
    }


def _headers(token: str | None = None) -> dict:
    """POST 头：公共头 + AuthorizationType + 随机 X-WECHAT-UIN + 可选 Bearer。

    X-WECHAT-UIN：随机 uint32 的十进制字符串再 base64（官方 randomWechatUin）。
    """
    uin = base64.b64encode(str(random.getrandbits(32)).encode()).decode()
    h = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": uin,
        **_common_headers(),
    }
    if token and token.strip():
        h["Authorization"] = f"Bearer {token.strip()}"
    return h


def _base_info() -> dict:
    """请求体里的 base_info（官方 buildBaseInfo）。"""
    return {"channel_version": PLUGIN_VERSION, "bot_agent": BOT_AGENT}


def _post(base_url: str, endpoint: str, body: dict, token: str | None = None,
          timeout: float = 20) -> dict:
    r = requests.post(f"{base_url.rstrip('/')}/{endpoint}", json=body,
                      headers=_headers(token), timeout=timeout)
    r.raise_for_status()
    return r.json()


def get_bot_qrcode(local_token_list: list[str] | None = None,
                   bot_type: str = "3") -> dict:
    """取登录二维码。返回 {qrcode, qrcode_img_content}：前者是轮询句柄，
    后者是要编码成二维码的字符串（不是图片本身）。bot_type=3 是官方插件
    当前构建的默认值，走 query string 传（服务端从 URL 读）。"""
    return _post(BASE_URL, f"ilink/bot/get_bot_qrcode?bot_type={bot_type}",
                 {"local_token_list": local_token_list or []}, timeout=15)


def get_qrcode_status(qrcode: str, verify_code: str = "",
                      base_url: str = BASE_URL, timeout_s: float = 35) -> dict:
    """长轮询扫码状态（服务端挂起约 35s）。need_verifycode 时带上手机上
    显示的数字验证码重发。"""
    params = {"qrcode": qrcode}
    if verify_code:
        params["verify_code"] = verify_code
    r = requests.get(f"{base_url.rstrip('/')}/ilink/bot/get_qrcode_status",
                     params=params, headers=_common_headers(),
                     timeout=timeout_s + 10)
    r.raise_for_status()
    return r.json()


def get_updates(token: str, base_url: str, get_updates_buf: str = "",
                timeout_s: float = 35) -> dict:
    """长轮询收消息，返回 {ret, msgs, get_updates_buf}；buf 游标需持久化带回。
    客户端超时（无新消息）正常返回空 msgs。"""
    return _post(base_url, "ilink/bot/getupdates",
                 {"get_updates_buf": get_updates_buf or "",
                  "base_info": _base_info()},
                 token=token, timeout=timeout_s + 10)


def send_message(token: str, base_url: str, to_user_id: str, text: str,
                 context_token: str = "", client_id: str = "") -> dict:
    """发文本消息：message_type=2(BOT)、message_state=2(FINISH)、item type=1(TEXT)。

    context_token 来自该用户最近一条入站消息（官方插件按用户缓存）；
    没有缓存时官方插件也会不带 context 直接发，这里同样处理。
    """
    msg = {
        "from_user_id": "",
        "to_user_id": to_user_id,
        "client_id": client_id or
                     f"stock-advisor-{int(time.time() * 1000)}-{os.urandom(4).hex()}",
        "message_type": MSG_TYPE_BOT,
        "message_state": MSG_STATE_FINISH,
        "item_list": [{"type": ITEM_TYPE_TEXT, "text_item": {"text": text}}],
    }
    if context_token:
        msg["context_token"] = context_token
    resp = _post(base_url, "ilink/bot/sendmessage",
                 {"msg": msg, "base_info": _base_info()}, token=token)
    ret = resp.get("ret")
    if ret and ret != 0:
        raise IlinkError(
            f"sendmessage ret={ret} errmsg={resp.get('errmsg') or '(none)'}")
    return resp
