"""网页对话（web_chat.py）—— ChatGPT 风格对话 tab 的后端。

## 与微信侧的关系（2026-11-08 用户裁决）

**只共用 agent 与数据**：网页对话与微信对话共用本机 nanobot agent（wx_chat 里的
`agent_chat()`）和同一批只读 MCP 工具，但 session 完全隔离：

- 微信 session id = `wx:<uid>`（wx_chat 内部拼，逐字节不变）
- 网页 session id = `web:<sid>`，**存库时就是完整串**，直接传给 agent 做 session key
- 微信侧 `AGENT_STYLE` 等任何代码不动（test_wx_chat_agent.py 钉死）

## 历史

落库 `sa_chat_messages`（2026-11-08 用户明确选了改 schema，等于授权建表）。
agent 自己按 session 存对话（nanobot sessions），这张表是**页面回看用**的镜像，
两边各自存并不会重复进 prompt —— web_chat 的历史**只用于页面渲染**，不会塞进
agent 请求（同 wx_chat「不拼历史」的道理）。

## 图片

网关主力是文本模型，实测（2026-11-08，真实 PNG 「BLUE-42」，须 max_tokens>=3000，
推理模型会把小预算花在思考上 —— AGENTS.md 老坑）可读图的只有两个：

    nvidia/nemotron-3.5-super-vl-120b-a3b   → 3.6s  正确读出
    nvidia/nemotron-3-nano-omni-30b-a3b-reasoning → 6.2s 正确读出

（已驳回：llama-3.2-11b/90b-vision（410 EOL / >200s 超时）、phi-3-vision 与
gemma-3-4b/12b（NIM deployment 404）、embed-vl 两个（是 embedding 不是对话）。）

`describe_image()` 按列表顺序逐个试，拿到非空且非错误信封的内容为止；全部失败
返回 None，调用方如实写「无法自动识别」，**不编**。识别结果只当「vision 描述」
注入 agent 的文本，标注模型名 —— 它可能看错，不冒充事实。

红线：只读。本模块不 import app、不下单、不写任何交易表。
"""

import base64
import io
import re
import threading
import time
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.yaml"
UPLOAD_DIR = BASE_DIR / "data" / "chat_uploads"

DEFAULTS = {
    "enabled": True,           # 对话 tab 总开关
    "max_reply_chars": 8000,   # 网页可长回答（与微信 1200 不同；这是网页自己的口径）
    "history_limit": 200,      # 每个会话最多拉多少条历史渲染
    "max_upload_mb": 10,       # 单张图片上限
    # 逗号分隔；留空 = 不看图（上传仍可以，agent 提示里写「请描述图片内容」）
    "vision_models": "nvidia/nemotron-3.5-super-vl-120b-a12b,nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
    "img_note_chars": 1200,    # 图片描述截断长度
}

DEFAULTS_LOCK = threading.Lock()  # 防副本漂移占位（本模块无模块级可变状态，忽略可删）

_IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_NAME_RE = re.compile(r"[A-Za-z0-9_.\-]{1,120}")
_SID_PREFIX = "web:"


def load_conf() -> dict:
    conf = dict(DEFAULTS)
    try:
        if CONFIG_FILE.exists():
            import yaml
            data = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
            conf.update({k: v for k, v in (data.get("web_chat") or {}).items()
                         if v is not None})
    except Exception:
        pass
    return conf


def vision_list(conf: dict) -> list[str]:
    """配置里的视觉模型有序列表（去空白、去重、保序）。"""
    raw = str(conf.get("vision_models") or "").strip()
    out, seen = [], set()
    for item in raw.replace(";", ",").split(","):
        m = item.strip()
        if m and not m.lower().startswith("#") and m not in seen:
            out.append(m)
            seen.add(m)
    return out


def new_session_id() -> str:
    """web:<短随机>。存库与 nanobot session 都用这个完整串。"""
    return _SID_PREFIX + uuid.uuid4().hex[:12]


def check_sid(sid: str) -> bool:
    """只认我们自己发的会话 id：web 前缀 + 小写十六进制，防任意串进 agent。"""
    return bool(re.fullmatch(r"web:[0-9a-f]{6,32}", sid or ""))


# ---------------- DDL 与存取 ----------------

DDL = """
CREATE TABLE IF NOT EXISTS sa_chat_messages (
    id         BIGSERIAL PRIMARY KEY,
    sid        VARCHAR(64) NOT NULL,          -- 会话 id（web:xxxx，即 nanobot session）
    role       VARCHAR(12) NOT NULL,          -- user | assistant
    text       TEXT NOT NULL DEFAULT '',
    img_name   VARCHAR(160),                  -- user 行的图片附件文件名（可空）
    src        VARCHAR(16) NOT NULL DEFAULT 'agent',  -- assistant 来源 agent|llm
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_sid ON sa_chat_messages (sid, id DESC);
"""


def ensure_table(deps) -> None:
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        for stmt in [s.strip() for s in DDL.split(";") if s.strip()]:
            stmt = "\n".join(l for l in stmt.splitlines()
                             if not l.strip().startswith("--")).strip()
            if stmt:
                cur.execute(stmt)
    conn.commit()


def save_message(deps, sid: str, role: str, text: str,
                 img_name: str = "", src: str = "agent") -> None:
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sa_chat_messages (sid, role, text, img_name, src)"
            " VALUES (%s,%s,%s,%s,%s)", (sid, role, text or "", img_name or "", src))
    conn.commit()


def list_sessions(deps, limit: int = 50) -> list[dict]:
    """会话列表：每组一条（最后一条消息 + 消息数），按最近活动排序。"""
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT s.sid, s.n, s.first_ts, m.text, m.created_at AS last_ts"
            " FROM (SELECT sid, COUNT(*) AS n, MIN(created_at) AS first_ts"
            "        FROM sa_chat_messages GROUP BY sid) s"
            " JOIN LATERAL (SELECT text, created_at"
            "                 FROM sa_chat_messages WHERE sid = s.sid"
            "                ORDER BY created_at DESC, id DESC LIMIT 1) m ON true"
            " ORDER BY m.created_at DESC, s.sid LIMIT %s",
            (max(1, min(200, limit)),))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def history(deps, sid: str, limit: int = 0, conf: dict | None = None) -> list[dict]:
    """按时间正序返回某会话的消息（渲染顺序）。limit<=0 用配置默认。"""
    if limit <= 0:
        limit = int((conf or load_conf()).get("history_limit") or 200)
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, sid, role, text, img_name, src, created_at"
            " FROM sa_chat_messages WHERE sid = %s"
            " ORDER BY created_at DESC, id DESC LIMIT %s",
            (sid, max(1, min(1000, limit))))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    rows.reverse()
    return rows


def delete_session(deps, sid: str) -> int:
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM sa_chat_messages WHERE sid = %s", (sid,))
        n = cur.rowcount
    conn.commit()
    return n


# ---------------- 图片上传与描述 ----------------

def save_upload(data: bytes, ext: str, conf: dict | None = None) -> str:
    """落盘一张图片，返回可放 URL 的文件名。格式/大小都在这里闸住。"""
    conf = conf or load_conf()
    ext = (ext or "").lower()
    if ext not in _IMG_EXT:
        raise ValueError("不支持的图片格式（只收 png/jpg/jpeg/gif/webp/bmp）")
    cap = float(conf.get("max_upload_mb") or 10) * 1024 * 1024
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ValueError("空文件")
    if len(data) > cap:
        raise ValueError(f"图片超过 {float(conf.get('max_upload_mb') or 10):g} MB 上限")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{int(time.time())}-{uuid.uuid4().hex[:8]}{ext}"
    path = UPLOAD_DIR / name
    path.write_bytes(data)
    return name


def upload_path(name: str):
    """文件名白名单 → 绝对路径；不合法/不存在返回 None。"""
    if not _NAME_RE.fullmatch(name or "") or ".." in name:
        return None
    p = (UPLOAD_DIR / name)
    if not p.is_file():
        return None
    return p


_SYSTEM_PROMPT = (
    "这是用户发给股票助理系统的一张截图。请先用一两句说明这张图大概是什么"
    "（如 app/行情页/表格/文章截图），然后：① 图里若有文字，把关键文字与数字"
    "逐条转写出来（不要遗漏数字）；② 若有表格，简要给出列名与几行要点。"
    "只用图片里实际出现的内容，看不清就标「看不清」。中文回答，不要 Markdown。"
)


def describe_image(data: bytes, conf: dict | None = None) -> tuple[str, str] | tuple[None, None]:
    """用网关上的视觉模型看图。返回 (描述文本, 模型名)；全失败返回 (None, None)。

    实测口径（2026-11-08）：两个可用模型都是**推理模型**，max_tokens 不足 3000
    会被思考花光、content 为空 —— 所以这里固定给大预算，不读配置的回复长度。
    失败不缓存不编造：返回 None 由调用方如实提示。
    """
    conf = conf or load_conf()
    models = vision_list(conf)
    if not models:
        return None, None
    try:
        import requests
        conf_llm = None
        try:
            import llm_advisor
            conf_llm = llm_advisor.load_llm_conf()
        except Exception:
            conf_llm = None
        base_url = str((conf_llm or {}).get("base_url") or "").strip().rstrip("/")
        api_key = str((conf_llm or {}).get("api_key") or "")
        if not (base_url and api_key):
            print("[web-chat] 无网关配置（llm.base_url/api_key），跳过图片识别", flush=True)
            return None, None
        b64 = base64.b64encode(data).decode()
        url = base_url + "/v1/chat/completions"
        headers = {"Authorization": "Bearer " + api_key}
        body = {"messages": [{"role": "user", "content": [
            {"type": "text", "text": _SYSTEM_PROMPT},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64," + b64}},
        ]}], "max_tokens": 3000}
        for mid in models:
            body["model"] = mid
            try:
                r = requests.post(url, json=body, headers=headers, timeout=180,
                                  proxies={"http": None, "https": None})
                if r.status_code != 200:
                    print(f"[web-chat] 视觉 {mid} HTTP {r.status_code}: {str(r.text)[:120]}",
                          flush=True)
                    continue
                j = r.json()
                out = str((((j.get("choices") or [{}])[0]).get("message") or {})
                          .get("content") or "").strip()
                usage = j.get("usage") or {}
                try:
                    tok = int(usage.get("total_tokens") or 0)
                except (TypeError, ValueError):
                    tok = 0
                if not out:
                    print(f"[web-chat] 视觉 {mid} 空内容（usage={tok}），试下一个模型",
                          flush=True)
                    continue
                if out.startswith("Error:"):
                    print(f"[web-chat] 视觉 {mid} 返回错误信封: {out[:120]}", flush=True)
                    continue
                cap = int(conf.get("img_note_chars") or 1200)
                return out[:cap], mid
            except Exception as exc:
                print(f"[web-chat] 视觉 {mid} 请求失败: {type(exc).__name__}: {exc}",
                      flush=True)
                continue
    except Exception as exc:
        print(f"[web-chat] 图片识别异常: {type(exc).__name__}: {exc}", flush=True)
    return None, None
