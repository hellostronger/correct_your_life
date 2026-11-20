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
import json
import re
import threading
import time
import uuid
from datetime import datetime
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


def describe_image(data: bytes, conf: dict | None = None,
                   system_prompt: str | None = None, max_chars: int | None = None,
                   max_tokens: int = 3000) -> tuple[str, str] | tuple[None, None]:
    """用网关上的视觉模型看图。返回 (描述文本, 模型名)；全失败返回 (None, None)。

    实测口径（2026-11-08）：两个可用模型都是**推理模型**，max_tokens 不足 3000
    会被思考花光、content 为空 —— 所以这里固定给大预算，不读配置的回复长度。
    失败不缓存不编造：返回 None 由调用方如实提示。

    2026-11-19 加三个可选参数，用于「银证转账截图 → 结构化 JSON」：
    system_prompt 换专用 prompt、max_chars 放宽（通用描述的 1200 字装不下一张
    多行流水表）、max_tokens 可加大。**不传时行为与原来逐字一致**。
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
            {"type": "text", "text": system_prompt or _SYSTEM_PROMPT},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64," + b64}},
        ]}], "max_tokens": max_tokens}
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
                cap = int(max_chars if max_chars is not None
                          else (conf.get("img_note_chars") or 1200))
                return out[:cap], mid
            except Exception as exc:
                print(f"[web-chat] 视觉 {mid} 请求失败: {type(exc).__name__}: {exc}",
                      flush=True)
                continue
    except Exception as exc:
        print(f"[web-chat] 图片识别异常: {type(exc).__name__}: {exc}", flush=True)
    return None, None


# ---------------- 银证转账截图 → 结构化候选行（2026-11-19） ----------------
#
# 与 describe_image 共用同一套网关/密钥/视觉模型 fallback，只换 prompt 与输出上限。
# 红线不变：本模块**只读**，产出的只是一堆候选 dict；真正写台账由 app.py 的
# /api/chat/transfer-commit 负责，且必须经用户在确认表里点头才发生。
# 不复用通用描述的原因：它截断到 1200 字、输出自由文本，装不下也解析不出一张
# 多行流水表；把描述再塞给 LLM 抽字段会多烧一次调用，还更容易漏数字。

TRANSFER_SYSTEM_PROMPT = (
    "这是券商 App 的「转账记录 / 银证转账」截图。请逐行读取明细，"
    "只输出一个 JSON 数组，不要解释文字、不要 Markdown 代码块。\n"
    "每个元素固定字段：\n"
    '{"date":"YYYY-MM-DD","side":"in或out","amount":正数,'
    '"currency":"CNY或HKD或USD","account":"账户或币种页签名",'
    '"note":"类型+时间，如 证券转银行 14:14:15",'
    '"status":"成功或失败或撤单或看不清"}\n'
    "规则：\n"
    "1. 银行转证券 / 转入 = in；证券转银行 / 转出 = out。"
    "金额一律给正数，不要负号、不要千分位、不要货币符号。\n"
    "2. 日期必须补全为 YYYY-MM-DD；截图只有月日时补当前年份；"
    "截图里能看到年份就用截图年份。年份拿不准时按当前年份填并在 note 标「年份不确定」。\n"
    "3. currency 取顶部当前选中的币种页签（人民币=CNY、港币=HKD、美元=USD）；"
    "看不清给 CNY 并在 note 标「币种不确定」。\n"
    "4. 失败 / 撤单 / 未成交的行也要输出，status 照填；成功写「成功」。\n"
    "5. 顶部汇总（转入总额、转出总额、净转入）不是明细行，不要输出。\n"
    "6. 底部被截断、日期或金额看不全的行：date 填空字符串、amount 填 0、"
    "status 填「看不清」。\n"
    "7. 数字逐字转写，不四舍五入、不推算、不编造。\n"
    "只输出 JSON 数组。"
)

_TRANSFER_NOUNS = ("银证转账", "转账记录", "转账流水", "出入金", "入金", "出金",
                   "银行转证券", "证券转银行", "转账")
_TRANSFER_VERBS = ("录入", "导入", "补录", "记账", "记一下", "记一笔", "记入",
                   "入库", "加到台账", "存进", "写进", "自动录", "记上",
                   "补上", "录进去", "存起来", "加进去", "录到台账",
                   "记到", "加到", "录进", "存到")


def is_transfer_intent(text: str) -> bool:
    """「转账类名词 + 录入类动作词」同时命中才触发，避免「帮我看看这张转账截图」
    被误当成记账指令。必须同时带图片由调用方判断。"""
    t = (text or "").strip().lower()
    if not t:
        return False
    return (any(n in t for n in _TRANSFER_NOUNS)
            and any(v in t for v in _TRANSFER_VERBS))


def parse_transfer_json(text: str) -> list[dict]:
    """从视觉模型输出里 robust 地抠出 JSON 数组；解析不出来返回 []（不编）。

    实测模型常包 ```json 代码块、前后加一句解释、用单引号或末尾多逗号，
    所以逐层兜底；任何一步失败都交给调用方如实提示「识别失败」。
    """
    s = (text or "").strip()
    if not s:
        return []
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.S | re.I)
    if m:
        s = m.group(1).strip()
    i, j = s.find("["), s.rfind("]")
    if i < 0 or j <= i:
        return []
    frag = s[i:j + 1]
    dq = frag.replace("'", '"')
    cands = [frag, dq,
             re.sub(r",\s*([}\]])", r"\1", frag),
             re.sub(r",\s*([}\]])", r"\1", dq)]
    for cand in cands:
        try:
            v = json.loads(cand)
        except Exception:
            continue
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
    return []


def _pick(row: dict, *keys):
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return v
    return None


def _norm_side(side_raw: str, note: str) -> str:
    """side 用精确值、note 用关键词：避免 "in" 命中 MINIMAX 这类子串。"""
    s = (side_raw or "").strip().lower()
    n = (note or "").lower()
    if s in ("in", "buy", "转入", "入金") or "银行转证券" in n:
        return "in"
    if s in ("out", "sell", "转出", "出金") or "证券转银行" in n:
        return "out"
    return ""


def normalize_transfer_rows(raw_rows, default_year: int | None = None,
                            existing=None) -> list[dict]:
    """把模型输出的松散 dict 归一化成台账字段，并标记问题 / 重复 / 应跳过。

    纯函数、不写库、不联网 —— 离线测试可全量覆盖。每一行都保留，
    由前端确认表决定勾哪些；绝不因为「看不清」就静默丢行。
    """
    year = int(default_year or datetime.now().year)
    existing = existing or []
    out = []
    for r in raw_rows or []:
        if not isinstance(r, dict):
            continue
        issues = []

        # 日期：容忍 YYYY/MM/DD、M-D、带时间后缀
        date = str(_pick(r, "date", "日期", "trade_date") or "").strip()
        date = date.replace("/", "-").replace(".", "-")
        m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", date)
        if m:
            date = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        else:
            m2 = re.search(r"(?:^|\D)(\d{1,2})-(\d{1,2})(?:\D|$)", date)
            date = (f"{year:04d}-{int(m2.group(1)):02d}-{int(m2.group(2)):02d}"
                    if m2 else "")
        if not date:
            issues.append("日期缺失或看不清")
        else:
            try:
                datetime.strptime(date, "%Y-%m-%d")
            except ValueError:
                issues.append("日期不合法")
                date = ""

        side_raw = str(_pick(r, "side", "方向", "type", "类型") or "").strip()
        note = str(_pick(r, "note", "备注", "desc") or "").strip()[:120]
        side = _norm_side(side_raw, note)
        if not side:
            issues.append("方向不明")
        account = str(_pick(r, "account", "账户") or "").strip()[:40]

        # 时刻（2026-11-20 时间戳升级）：原始 date 串或备注尾部提取 HH:MM[:SS]，
        # 与日期拼成完整时间戳入库；解析不出就保持纯日期（台账侧归一成 00:00:00）。
        _tm_src = str(_pick(r, "date", "日期", "trade_date") or "") + " " + note
        _tm = re.search(r"(?:^|[\s,，])([0-2]?\d):([0-5]\d)(?::([0-5]\d))?(?=$|[\s,，])",
                        _tm_src)
        if date and _tm and int(_tm.group(1)) <= 23:
            date = f"{date} {int(_tm.group(1)):02d}:{_tm.group(2)}:{_tm.group(3) or '00'}"

        try:
            amount = abs(float(str(_pick(r, "amount", "金额", "amt") or 0)
                               .replace(",", "").replace("−", "-")))
        except (TypeError, ValueError):
            amount = 0.0
        amount = round(amount, 2)
        if amount <= 0:
            issues.append("金额缺失或看不清")

        ccy = str(_pick(r, "currency", "币种") or "CNY").strip().upper()
        ccy = {"人民币": "CNY", "RMB": "CNY", "港币": "HKD", "港元": "HKD",
               "美元": "USD"}.get(ccy, ccy)
        # 侧车台账只收 CNY/HKD（broker_cash.CURRENCIES）。美元页签的行保留原值
        # 并标问题，由用户在确认表里决定 —— **绝不静默改成 CNY**，那会写错账。
        if ccy not in ("CNY", "HKD"):
            issues.append("币种不支持（台账只收 CNY/HKD）")

        status = str(_pick(r, "status", "状态") or "成功").strip()
        skip = any(k in status for k in ("失败", "撤单", "未成交", "看不清", "作废"))
        if skip:
            issues.append("状态：" + status)

        # 查重（2026-11-20 时间戳升级）：日期+方向+金额+币种；两侧都带时刻时
        # 再比时刻 —— 同日不同时刻的两笔（如 10:22 与 13:34 各 20000）不再误判重复。
        row_d10 = date[:10] if date else ""
        row_tm = date[11:] if " " in date else ""
        duplicate = bool(row_d10 and side and amount > 0 and any(
            str(f.get("date", ""))[:10] == row_d10
            and f.get("side") == side
            and abs(float(f.get("amount", 0) or 0) - amount) < 0.005
            and str(f.get("currency", "CNY")).upper() == ccy
            and (not row_tm
                 or " " not in str(f.get("date", ""))
                 or str(f.get("date", ""))[11:] == row_tm)
            for f in existing))
        if duplicate:
            issues.append("疑似已录入")

        out.append({"date": date, "side": side, "amount": amount,
                    "currency": ccy, "account": account, "note": note,
                    "status": status, "issues": issues,
                    "duplicate": duplicate, "skip": skip,
                    "force": bool(r.get("force"))})
    return out


def extract_transfer_flows(data: bytes, conf: dict | None = None):
    """调视觉模型，返回 (原始 JSON dict 列表, 模型名)；全失败返回 ([], None)。"""
    text, model = describe_image(data, conf or load_conf(),
                                 system_prompt=TRANSFER_SYSTEM_PROMPT,
                                 max_chars=12000, max_tokens=4000)
    if not text:
        return [], model
    return parse_transfer_json(text), model


def prepare_transfer_rows(data: bytes, conf: dict | None = None,
                          existing=None):
    """一步到位：看图 → 解析 → 归一化并标重复。返回 (候选行, 模型名)。"""
    raw, model = extract_transfer_flows(data, conf)
    return normalize_transfer_rows(raw, existing=existing), model


def validate_transfer_rows(rows, existing=None):
    """commit 前的服务端复核（前端传来的值一律不可信，重新归一化）。

    返回 (accepted, rejected)。重复行默认拦下；前端确认表里用户若坚持勾选，
    会带 force=True 放行 —— 侧车设计上「同额同日两笔」是合法的，不该硬禁。
    """
    norm = normalize_transfer_rows(rows, existing=existing)
    accepted, rejected = [], []
    for src, r in zip(rows or [], norm):
        r["force"] = bool(src.get("force")) if isinstance(src, dict) else False
        hard = [x for x in r["issues"] if x != "疑似已录入"]
        if r["skip"] or hard or not r["date"] or not r["side"] or r["amount"] <= 0:
            rejected.append({"row": r, "why": "、".join(r["issues"]) or "字段不完整"})
        elif r["duplicate"] and not r["force"]:
            rejected.append({"row": r, "why": "疑似已录入（date+side+amount+币种 已存在）"})
        else:
            accepted.append(r)
    return accepted, rejected
