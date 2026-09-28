"""X(Twitter) 指定用户发言监控（x_monitor.py）。

## 为什么用 twscrape（2026-09-26 调研结论）

| 方案 | 结论 |
|---|---|
| Nitter | ❌ 2026-08-24 X Corp 发律师函要求永久下架，仓库 2026-09-11 归档只读 |
| snscrape / Twint | ❌ 依赖匿名 guest token，X 已关闭该通道，一并失效 |
| RSSHub `/twitter/user/:id` | ⚠️ 主体还活着，但 Twitter 路由是全项目最不稳的一批，且同样要 cookie |
| **twscrape**（`vladkens/twscrape`） | ✅ MIT、2.8k star、2026-09-22 仍在提交、PyPI v0.20.1 |

twscrape 用**你自己 X 账号的 cookie**（`auth_token` + `ct0`）轮换请求。带这两个
cookie 的账号「立即激活，不需要 login_accounts 步骤」（上游 README 原话）。所以本
模块的账号配置就是一段 cookie 字符串，网页上粘贴即可：

    x.com → F12 → Application → Cookies → 复制 auth_token 与 ct0
    → 拼成 `auth_token=xxx; ct0=yyy`

也可以用上游推荐的 unjar 一键导出：
`unjar x.com -f header | twscrape add_cookie my_account`

**强烈建议装 curl 后端**：X 会做 TLS 指纹识别，httpx 的指纹会被拒。

    pip install "twscrape[curl]"

代码里默认 `TWS_HTTP_BACKEND=curl`（curl-cffi 做浏览器级指纹伪装）。

## 本模块的表

| 表 | 内容 |
|---|---|
| `sa_x_accounts` | X 账号 cookie（名称/cookie 串/开关/上轮状态） |
| `sa_x_watch` | 监听的用户（username/user_id/显示名/是否含回复/点赞门槛） |
| `sa_x_tweets` | 推文（tweet_id 去重、正文、互动数、是否回复/转发、未读） |

本模块不 import app（避免循环依赖）：`get_conn` / `notify_fn` 由 app.py 注入。
"""

import asyncio
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
ACCOUNTS_DB = DATA_DIR / "x_accounts.db"      # twscrape 自己的账号池（SQLite）

# X 会做 TLS 指纹识别，httpx 指纹会被拒 → 默认走 curl-cffi 伪装。必须在 import
# twscrape 之前设好：_detect_backend() 在 make_client() 时才读，但让环境变量
# 尽早就位更稳。上游同款做法（README: TWS_HTTP_BACKEND=curl twscrape …）。
os.environ.setdefault("TWS_HTTP_BACKEND", "curl")

DEFAULT_X_CONF = {
    "enabled": True,
    "interval_minutes": 30,    # 抓取周期（分钟），0 = 关闭
    "notify_new": True,        # 新推文推微信
    "max_tweets": 40,          # 每人每轮最多取多少条
    "keep_days": 90,
    "include_retweets": False, # 是否收录转发（默认不收，噪音大）
    "wait_timeout": 25,        # 等空闲账号的秒数（账号池被限流时）
    "max_text": 2000,          # 正文入库截断长度
}

UNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")

_state_lock = threading.Lock()
_state = {"fetching": False, "last_run": None, "last_result": None}


# ---------------- 配置 ----------------

def load_x_conf() -> dict:
    """读 config.yaml 的 x 段，补齐默认（每轮现读，改配置即生效）。"""
    conf = dict(DEFAULT_X_CONF)
    try:
        import yaml
        data = yaml.safe_load((BASE_DIR / "config.yaml").read_text(encoding="utf-8")) or {}
        conf.update({k: v for k, v in (data.get("x") or {}).items() if v is not None})
    except Exception:
        pass
    return conf


def twscrape_available() -> tuple[bool, str]:
    """(能否用, 原因)。没装要在页面上明确告诉用户装什么，而不是抛栈。"""
    try:
        import twscrape  # noqa: F401
    except ImportError:
        return False, '未安装 twscrape。请执行：pip install "twscrape[curl]"'
    if os.environ.get("TWS_HTTP_BACKEND") == "curl":
        try:
            import curl_cffi  # noqa: F401
        except ImportError:
            return False, ('已装 twscrape 但缺 curl 后端（X 会做 TLS 指纹识别）。'
                           '请执行：pip install "twscrape[curl]"')
    return True, ""


# ---------------- 账号 / 监听用户 / 推文的读写 ----------------

def _ensure_tables(deps) -> None:
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sa_x_accounts (
                id           BIGSERIAL PRIMARY KEY,
                name         VARCHAR(64) NOT NULL UNIQUE,
                cookies      TEXT        NOT NULL,
                enabled      BOOLEAN     NOT NULL DEFAULT TRUE,
                last_check   TIMESTAMPTZ,
                last_status  VARCHAR(16) NOT NULL DEFAULT '',
                last_error   TEXT        NOT NULL DEFAULT '',
                created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sa_x_watch (
                id           BIGSERIAL PRIMARY KEY,
                username     VARCHAR(32) NOT NULL UNIQUE,
                display_name VARCHAR(64) NOT NULL DEFAULT '',
                user_id      VARCHAR(24) NOT NULL DEFAULT '',
                note         TEXT        NOT NULL DEFAULT '',
                enabled      BOOLEAN     NOT NULL DEFAULT TRUE,
                with_replies BOOLEAN     NOT NULL DEFAULT FALSE,
                min_likes    INTEGER     NOT NULL DEFAULT 0,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sa_x_tweets (
                id          BIGSERIAL PRIMARY KEY,
                tweet_id    VARCHAR(32) NOT NULL UNIQUE,
                watch_id    BIGINT      NOT NULL,
                username    VARCHAR(32) NOT NULL DEFAULT '',
                display_name VARCHAR(64) NOT NULL DEFAULT '',
                text        TEXT        NOT NULL DEFAULT '',
                url         TEXT        NOT NULL DEFAULT '',
                created_at  TIMESTAMPTZ,
                stats       JSONB       NOT NULL DEFAULT '{}'::jsonb,
                is_reply    BOOLEAN     NOT NULL DEFAULT FALSE,
                is_retweet  BOOLEAN     NOT NULL DEFAULT FALSE,
                is_read     BOOLEAN     NOT NULL DEFAULT FALSE,
                fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sa_x_tweets_watch "
                    "ON sa_x_tweets (watch_id, id DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sa_x_tweets_pub "
                    "ON sa_x_tweets (created_at DESC NULLS LAST, id DESC)")


def list_accounts(deps, reveal: bool = False) -> list[dict]:
    """账号列表。默认**不返回** cookie 原文（页面上只显示「已配置」+ 掩码）。"""
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, name, cookies, enabled, last_check, last_status, "
                    "last_error, created_at FROM sa_x_accounts ORDER BY id")
        rows = cur.fetchall()
    out = []
    for r in rows:
        ck = r[2] or ""
        out.append({
            "id": r[0], "name": r[1], "enabled": r[3],
            "has_cookies": bool(ck),
            "cookies_masked": _mask(ck),
            "cookies": ck if reveal else "",
            "last_check": r[4].isoformat(timespec="seconds") if r[4] else None,
            "last_status": r[5], "last_error": r[6],
            "created_at": r[7].isoformat(timespec="seconds") if r[7] else None,
        })
    return out


def _mask(cookie: str) -> str:
    """只露 auth_token / ct0 的头尾，中间打码。"""
    if not cookie:
        return ""
    parts = []
    for kv in cookie.split(";"):
        kv = kv.strip()
        if not kv:
            continue
        if "=" not in kv:
            parts.append(kv)
            continue
        k, v = kv.split("=", 1)
        parts.append(f"{k}={v[:4]}…{v[-4:]}" if len(v) > 10 else f"{k}={v[:4]}…")
    return "; ".join(parts)


def save_account(deps, name: str, cookies: str) -> dict:
    """新增/更新一个 X 账号。cookie 串必须同时含 auth_token 与 ct0。"""
    name = (name or "").strip()
    ck = (cookies or "").strip()
    if not name:
        raise ValueError("请填写账号备注名（本地标识，任意）")
    if "auth_token" not in ck or "ct0" not in ck:
        raise ValueError("cookie 必须同时包含 auth_token 与 ct0，"
                         "格式如：auth_token=xxx; ct0=yyy")
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO sa_x_accounts (name, cookies) VALUES (%s,%s)
                       ON CONFLICT (name) DO UPDATE SET
                           cookies=EXCLUDED.cookies, last_status='', last_error=''
                       RETURNING id""", (name, ck))
        sid = cur.fetchone()[0]
    return {"id": sid, "name": name, "has_cookies": True, "cookies_masked": _mask(ck)}


def remove_account(deps, sid: int) -> dict:
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM sa_x_accounts WHERE id = %s RETURNING name", (sid,))
        row = cur.fetchone()
    if not row:
        raise ValueError(f"账号 {sid} 不存在")
    return {"ok": True, "name": row[0]}


def list_watch(deps) -> list[dict]:
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, username, display_name, user_id, note, enabled, "
                    "with_replies, min_likes, created_at FROM sa_x_watch ORDER BY id")
        rows = cur.fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r[0], "username": r[1], "display_name": r[2], "user_id": r[3],
            "note": r[4], "enabled": r[5], "with_replies": r[6], "min_likes": r[7],
            "created_at": r[8].isoformat(timespec="seconds") if r[8] else None,
        })
    return out


def add_watch(deps, username: str, note: str = "", with_replies: bool = False,
              min_likes: int = 0) -> dict:
    u = (username or "").strip().lstrip("@")
    # 允许粘 x.com/xxx 的链接
    m = re.search(r"(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,15})", u)
    if m:
        u = m.group(1)
    if not UNAME_RE.match(u):
        raise ValueError("请填 X 用户名（@jack 或 https://x.com/jack）")
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO sa_x_watch (username, note, with_replies, min_likes)
                       VALUES (%s,%s,%s,%s)
                       ON CONFLICT (username) DO UPDATE SET
                           note=EXCLUDED.note, with_replies=EXCLUDED.with_replies,
                           min_likes=EXCLUDED.min_likes, enabled=TRUE
                       RETURNING id""",
                    (u, note or "", bool(with_replies), max(0, int(min_likes or 0))))
        sid = cur.fetchone()[0]
    return {"id": sid, "username": u}


def update_watch(deps, wid: int, **fields) -> dict:
    allowed = {"note", "enabled", "with_replies", "min_likes", "display_name"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            raise ValueError(f"不支持字段 {k}")
        sets.append(f"{k}=%s")
        vals.append(v)
    if not sets:
        raise ValueError("没有要改的字段")
    vals.append(wid)
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE sa_x_watch SET {','.join(sets)} WHERE id=%s "
                    f"RETURNING id, username", vals)
        row = cur.fetchone()
    if not row:
        raise ValueError(f"监听用户 {wid} 不存在")
    return {"id": row[0], "username": row[1]}


def remove_watch(deps, wid: int) -> dict:
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM sa_x_tweets WHERE watch_id = %s", (wid,))
        n = cur.rowcount
        cur.execute("DELETE FROM sa_x_watch WHERE id = %s RETURNING username", (wid,))
        row = cur.fetchone()
    if not row:
        raise ValueError(f"监听用户 {wid} 不存在")
    return {"ok": True, "username": row[0], "removed_tweets": n}


def list_tweets(deps, watch_id: int | None = None, unread_only: bool = False,
                limit: int = 50) -> list[dict]:
    limit = max(1, min(limit, 200))
    where, params = [], []
    if watch_id:
        where.append("t.watch_id = %s")
        params.append(watch_id)
    if unread_only:
        where.append("NOT t.is_read")
    sql = ("SELECT t.id, t.tweet_id, t.watch_id, t.username, t.display_name, t.text, "
           "t.url, t.created_at, t.stats, t.is_reply, t.is_retweet, t.is_read, "
           "w.username FROM sa_x_tweets t JOIN sa_x_watch w ON w.id = t.watch_id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += (" ORDER BY COALESCE(t.created_at, t.fetched_at) DESC NULLS LAST, t.id DESC "
            "LIMIT %s")
    params.append(limit)
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    out = []
    for r in rows:
        try:
            stats = json.loads(r[8] or "{}")
        except json.JSONDecodeError:
            stats = {}
        out.append({
            "id": r[0], "tweet_id": r[1], "watch_id": r[2], "username": r[3],
            "display_name": r[4], "text": r[5], "url": r[6],
            "created_at": r[7].isoformat(timespec="minutes") if r[7] else None,
            "stats": stats, "is_reply": r[9], "is_retweet": r[10], "is_read": r[11],
            "watch_username": r[12],
        })
    return out


def unread_count(deps) -> int:
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM sa_x_tweets WHERE NOT is_read")
        return cur.fetchone()[0]


def mark_read(deps, ids: list[int] | None = None, all_: bool = False) -> int:
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        if all_:
            cur.execute("UPDATE sa_x_tweets SET is_read = TRUE WHERE NOT is_read")
        elif ids:
            cur.execute("UPDATE sa_x_tweets SET is_read = TRUE "
                        "WHERE id = ANY(%s) AND NOT is_read", (ids,))
        else:
            return 0
        return cur.rowcount


# ---------------- 抓取（twscrape 异步，外部用 asyncio.run 包一层） ----------------

def _tweet_row(deps, w: dict, t, conf: dict) -> tuple | None:
    """一条 Tweet -> 入库参数；按开关/门槛判定为 None 表示不收。"""
    is_retweet = getattr(t, "retweetedTweet", None) is not None
    if is_retweet and not conf.get("include_retweets", False):
        return None
    is_reply = getattr(t, "inReplyToTweetIdStr", None) is not None
    if getattr(t, "likeCount", 0) < int(w.get("min_likes") or 0):
        return None
    text = re.sub(r"[ \t]+\n", "\n", (getattr(t, "rawContent", "") or "")).strip()
    return (
        str(getattr(t, "id_str", "") or getattr(t, "id", "")),
        w["id"],
        (getattr(t.user, "username", "") or w["username"]),
        (getattr(t.user, "displayname", "") or w.get("display_name", ""))[:64],
        text[:int(conf.get("max_text") or 2000)],
        getattr(t, "url", "") or f"https://x.com/{w['username']}/status/{getattr(t, 'id_str', '')}",
        getattr(t, "date", None),
        json.dumps({
            "like": int(getattr(t, "likeCount", 0) or 0),
            "retweet": int(getattr(t, "retweetCount", 0) or 0),
            "reply": int(getattr(t, "replyCount", 0) or 0),
            "quote": int(getattr(t, "quoteCount", 0) or 0),
            "view": int(getattr(t, "viewCount", 0) or 0),
            "lang": getattr(t, "lang", "") or "",
        }, ensure_ascii=False),
        is_reply, is_retweet,
    )


async def _fetch_watch(api, deps, w: dict, conf: dict) -> dict:
    from twscrape import gather
    out = {"watch_id": w["id"], "username": w["username"], "new": 0,
           "total": 0, "fresh": [], "display_name": w.get("display_name", "")}
    # user_id 缓存住：解析一次就不用每轮再发一次 UserByScreenName
    uid = int(w["user_id"]) if (w.get("user_id") or "").isdigit() else 0
    if not uid:
        user = await api.user_by_login(w["username"])
        if user is None:
            raise RuntimeError("查不到该用户：可能已改名、账号私密，"
                               "或当前 X 账号被限流（换个账号或等几分钟再试）")
        uid = int(user.id)
        with deps["get_conn"]() as conn, conn.cursor() as cur:
            cur.execute("UPDATE sa_x_watch SET user_id=%s, display_name=%s WHERE id=%s",
                        (str(uid), (getattr(user, "displayname", "") or "")[:64], w["id"]))
        w["user_id"] = str(uid)
        w["display_name"] = getattr(user, "displayname", "") or w.get("display_name", "")
    out["display_name"] = w.get("display_name", "")
    limit = max(1, int(conf.get("max_tweets") or 40))
    gen = (api.user_tweets_and_replies(uid, limit=limit) if w.get("with_replies")
           else api.user_tweets(uid, limit=limit))
    tweets = await gather(gen)
    out["total"] = len(tweets)
    rows = [r for r in (_tweet_row(deps, w, t, conf) for t in tweets) if r]
    if rows:
        with deps["get_conn"]() as conn, conn.cursor() as cur:
            for r in rows:
                cur.execute(
                    """
                    INSERT INTO sa_x_tweets
                        (tweet_id, watch_id, username, display_name, text, url,
                         created_at, stats, is_reply, is_retweet)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (tweet_id) DO NOTHING
                    """, r)
                if cur.rowcount:            # 1 = 真插入（冲突时 0）
                    out["new"] += 1
                    out["fresh"].append(r)
    out["status"] = "ok"
    return out


async def _fetch_all(deps, accounts: list[dict], watches: list[dict], conf: dict) -> list[dict]:
    from twscrape import API
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    api = API(str(ACCOUNTS_DB), raise_when_no_account=True,
              wait_timeout=float(conf.get("wait_timeout") or 25))
    for a in accounts:
        # 幂等：同名账号重复 add 只是替换会话，保留 stats/locks/proxy
        await api.pool.add_account_cookies(a["name"], a["cookies"])
    out = []
    for w in watches:
        try:
            out.append(await _fetch_watch(api, deps, w, conf))
        except Exception as exc:
            out.append({"watch_id": w["id"], "username": w["username"],
                        "new": 0, "total": 0, "status": "error", "error": str(exc)[:500]})
    return out


async def _probe(cookies: str, name: str) -> dict:
    """校验 cookie 是否还能用：拿一个公开号跑一次最轻的接口。"""
    from twscrape import API
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    api = API(str(ACCOUNTS_DB), raise_when_no_account=True, wait_timeout=15)
    await api.pool.add_account_cookies(name, cookies)
    user = await api.user_by_login("xdevelopers")
    if user is None:
        return {"ok": False,
                "error": "cookie 可能已失效（X 会话过期），请重新导出 auth_token/ct0"}
    return {"ok": True, "message": f"cookie 有效（探测账号 @xdevelopers 关注数 "
                                   f"{getattr(user, 'followersCount', '?')}）"}


def run_once(deps) -> dict:
    """一轮：所有启用账号 + 所有启用监听用户 → 抓推文 → 去重入库 → 新文推微信。"""
    ok, why = twscrape_available()
    if not ok:
        return {"error": why}
    conf = load_x_conf()
    with _state_lock:
        if _state["fetching"]:
            return {"skipped": True, "reason": "已有抓取任务在运行"}
        _state["fetching"] = True
    try:
        with deps["get_conn"]() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, name, cookies, enabled FROM sa_x_accounts "
                        "WHERE enabled AND cookies <> '' ORDER BY id")
            acols = ("id", "name", "cookies", "enabled")
            accounts = [dict(zip(acols, r)) for r in cur.fetchall()]
            cur.execute("SELECT id, username, display_name, user_id, note, enabled, "
                        "with_replies, min_likes FROM sa_x_watch WHERE enabled ORDER BY id")
            wcols = ("id", "username", "display_name", "user_id", "note", "enabled",
                     "with_replies", "min_likes")
            watches = [dict(zip(wcols, r)) for r in cur.fetchall()]
        if not accounts:
            return {"skipped": True, "reason": "未配置 X 账号 cookie（网页「X」页添加）",
                    "items": []}
        if not watches:
            return {"skipped": True, "reason": "未添加监听用户", "items": []}
        items = asyncio.run(_fetch_all(deps, accounts, watches, conf))
        with deps["get_conn"]() as conn, conn.cursor() as cur:
            for it in items:
                cur.execute("UPDATE sa_x_watch SET display_name = COALESCE(NULLIF(%s,''), "
                            "display_name) WHERE id = %s",
                            (it.get("display_name", ""), it["watch_id"]))
        fresh = [f for it in items for f in it.get("fresh", [])]
        pushed = 0
        if fresh and conf.get("notify_new", True):
            try:
                deps["notify_fn"](_notify_title(len(fresh)), _notify_body(fresh))
                pushed = 1
            except Exception as exc:
                print(f"[x] notify failed: {exc}", flush=True)
        _cleanup(deps, conf)
        result = {"items": items, "new": sum(i["new"] for i in items),
                  "pushed": pushed,
                  "ts": datetime.now().isoformat(timespec="seconds")}
        _state["last_result"] = result
        return result
    except Exception as exc:
        return {"error": f"X 抓取失败：{exc}"}
    finally:
        _state["fetching"] = False
        _state["last_run"] = datetime.now().isoformat(timespec="seconds")


def test_account(deps, name: str, cookies: str) -> dict:
    """网页「测试」按钮：校验 cookie。不入库。"""
    ok, why = twscrape_available()
    if not ok:
        return {"ok": False, "error": why}
    try:
        return asyncio.run(_probe((cookies or "").strip(), (name or "probe").strip() or "probe"))
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:400]}


def _cleanup(deps, conf: dict) -> None:
    keep = int(conf.get("keep_days") or 90)
    if keep <= 0:
        return
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM sa_x_tweets "
                    "WHERE fetched_at < now() - make_interval(days => %s)", (keep,))


def _notify_title(n: int) -> str:
    return f"🐦 X 新推文 {n} 条" if n > 1 else "🐦 X 新推文"


def _notify_body(fresh: list) -> str:
    by_user: dict[str, list] = {}
    for r in fresh:
        by_user.setdefault(r[3] or r[2], []).append(r)
    lines = []
    for name, rows in by_user.items():
        lines.append(f"【@{name}】")
        for r in rows[:4]:
            body = re.sub(r"\s+", " ", r[4])[:160]
            try:
                like = json.loads(r[7] or "{}").get("like", 0)
            except json.JSONDecodeError:
                like = 0
            lines.append(f"- {body}　❤️{like}")
        if len(rows) > 4:
            lines.append(f"- …另有 {len(rows) - 4} 条")
    lines.append("\n（打开 stock-advisor「🐦 X」页看全文）")
    return "\n".join(lines)


def get_status(deps) -> dict:
    ok, why = twscrape_available()
    return {"fetching": _state["fetching"], "last_run": _state["last_run"],
            "last_result": _state["last_result"],
            "available": ok, "reason": why,
            "accounts": sum(1 for a in list_accounts(deps) if a["enabled"]),
            "watching": sum(1 for w in list_watch(deps) if w["enabled"]),
            "unread": unread_count(deps)}


def digest_lines(deps, hours: int = 24) -> list[str]:
    """近 N 小时新推文的一行摘要，给盘前/盘后报告引用。"""
    try:
        rows = list_tweets(deps, limit=40)
    except Exception:
        return []
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    out = []
    for r in rows:
        ts = r.get("created_at")
        dt = datetime.fromisoformat(ts).astimezone(timezone.utc) if ts else None
        if dt and dt < since:
            continue
        mark = "🔴 " if not r["is_read"] else ""
        who = r["display_name"] or r["username"]
        text = re.sub(r"\s+", " ", r["text"])[:140]
        out.append(f"- {mark}@{r['username']}（{who}）{text}")
        if len(out) >= 20:
            break
    return out
