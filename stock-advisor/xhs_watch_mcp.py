"""小红书舆情监控：基于 xiaohongshu-mcp REST API 实现。

与旧版 xhs_watch.py (MediaCrawler 子进程) 的区别：
- 走 HTTP 调用常驻的 xiaohongshu-mcp 服务（默认 127.0.0.1:18060），无需拉起子进程
- 登录态由 mcp 服务维护（浏览器 profile 持久化），调用方只需触发扫码
- 搜索 + 详情 + 评论全链路由 mcp 完成，返回结构化 JSON，解析更稳健
- 评论翻页/展开更多由 mcp 内部的 FeedDetailAction 处理，配置更细致
- 依赖：mcp 服务需先启动（Docker 或裸金属），且浏览器可用

数据流：
    config.yaml xhs.keywords (+ 自选股票名) 作为搜索词
      → POST /api/v1/feeds/search（逐词搜）
      → 对每条笔记 POST /api/v1/feeds/detail（拿正文+评论）
      → upsert 进 sa_xhs_notes / sa_xhs_comments
      → 匹配自选股推进 sa_news（source=xhs）
"""

import json
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests

BASE_DIR = Path(__file__).resolve().parent

_state = {"fetching": False, "last_result": None, "last_run": None, "login_ok": False, "login_waiting": False}
CRAWL_LOCK = threading.Lock()
LOGIN_LOCK = threading.Lock()

# mcp 服务地址（可用环境变量覆盖）
MCP_BASE_URL = os.environ.get("XHS_MCP_URL", "http://127.0.0.1:18060")
MCP_TIMEOUT = int(os.environ.get("XHS_MCP_TIMEOUT", "180"))  # 单次请求超时（秒）

# ---------------- 配置 ----------------

def load_xhs_conf() -> dict:
    """从 config.yaml 读 xhs 段；缺失用默认值。

    新增字段：
      - mcp_url: mcp 服务地址（环境变量 XHS_MCP_URL 优先）
      - max_notes_per_keyword: 单词条最多抓取笔记数（默认 40）
      - max_comments_per_note: 单笔记最多抓取评论数（默认 50）
      - load_all_comments: 是否尽量加载所有评论（默认 True）
    """
    defaults = {
        "enabled": True,
        "interval_minutes": 120,
        "fetch_timeout_minutes": 12,
        "max_notes_per_keyword": 40,
        "max_comments_per_note": 50,
        "load_all_comments": True,
        "use_watchlist": True,
        "keywords": [],
        "notify_new": False,
        "mcp_url": MCP_BASE_URL,
    }
    try:
        text = (BASE_DIR / "config.yaml").read_text(encoding="utf-8")
        import yaml
        data = yaml.safe_load(text) or {}
        conf = dict(data.get("xhs") or {})
    except Exception:
        conf = {}
    defaults.update({k: v for k, v in conf.items() if v is not None})
    kw = defaults.get("keywords") or []
    if isinstance(kw, str):
        kw = [k.strip() for k in kw.split(",") if k.strip()]
    defaults["keywords"] = [str(k).strip() for k in kw if str(k).strip()]
    return defaults


def _watchlist_names(limit: int = 30) -> list[str]:
    """自选股名称（去空、去重、截断），用作搜索词。库不可用时返回空。"""
    try:
        from app import get_conn
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT name FROM sa_watchlist "
                "WHERE name IS NOT NULL AND name <> '' ORDER BY name LIMIT %s",
                (limit,),
            )
            return [r[0].strip() for r in cur.fetchall() if r and r[0]]
    except Exception:
        return []


def resolve_keywords(conf: dict | None = None) -> list[str]:
    """合并 config 关键词 + 自选股名（use_watchlist 时）。"""
    conf = conf or load_xhs_conf()
    raw = conf.get("keywords") or []
    if isinstance(raw, str):
        kws: list[str] = [k.strip() for k in raw.split(",") if k.strip()]
    else:
        kws = [str(k) for k in raw]
    if conf.get("use_watchlist", True):
        kws.extend(_watchlist_names())
    seen: set[str] = set()
    out: list[str] = []
    for k in kws:
        k = (k or "").strip()
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(k)
    # 风控：单轮关键词太多必被打；上限 12
    return out[:12]


# ---------------- MCP 客户端 ----------------

class MCPClient:
    """xiaohongshu-mcp REST API 客户端。"""

    def __init__(self, base_url: str | None = None, timeout: int = MCP_TIMEOUT):
        self.base_url = (base_url or MCP_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def _req(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.base_url}{path}"
        try:
            r = self.session.request(method, url, timeout=self.timeout, **kwargs)
            r.raise_for_status()
            resp = r.json()
            # mcp 统一返回 {"success": bool, "data": {...}, "message": str}，解包 data
            if isinstance(resp, dict) and "success" in resp and "data" in resp:
                if not resp.get("success"):
                    raise RuntimeError(f"mcp 返回失败: {resp.get('message') or resp.get('data')}")
                return resp["data"] if resp["data"] is not None else {}
            return resp
        except requests.exceptions.ConnectionError as e:
            raise ConnectionError(f"无法连接 xiaohongshu-mcp 服务 ({self.base_url})：{e}")
        except requests.exceptions.Timeout:
            raise TimeoutError(f"mcp 请求超时（{self.timeout}s）")
        except requests.exceptions.HTTPError as e:
            # 尝试读错误详情
            detail = ""
            try:
                detail = r.json().get("message", r.text[:200])
            except Exception:
                detail = r.text[:200]
            raise RuntimeError(f"mcp HTTP {r.status_code}: {detail}")

    # --- 登录相关 ---
    def get_login_qrcode(self) -> dict:
        """获取登录二维码。返回 {img: 'data:image/png;base64,...', timeout, is_logged_in}。"""
        return self._req("GET", "/api/v1/login/qrcode")

    def check_login_status(self) -> dict:
        """检查登录状态。返回 {is_logged_in: bool, username?: str}。"""
        return self._req("GET", "/api/v1/login/status")

    def delete_cookies(self) -> dict:
        """清除登录态。"""
        return self._req("POST", "/api/v1/login/logout")

    # --- 搜索 ---
    def search_feeds(self, keyword: str, filters: list[dict] | None = None) -> list[dict]:
        """搜索笔记。返回笔记列表，每项含 note_id, xsec_token, title, desc, user 等。"""
        payload = {"keyword": keyword}
        if filters:
            payload["filters"] = filters
        resp = self._req("POST", "/api/v1/feeds/search", json=payload)
        # mcp 返回 {feeds: [...], has_more: bool, cursor: str}
        return resp.get("feeds", [])

    # --- 详情 + 评论 ---
    def get_feed_detail(self, feed_id: str, xsec_token: str,
                         load_all_comments: bool = True,
                         max_comment_items: int = 50,
                         max_replies_threshold: int = 10) -> dict:
        """获取笔记详情与评论。

        参数对应 mcp 的 CommentLoadConfig。
        返回结构：{note: {...}, comments: [...]}
        """
        payload = {
            "feed_id": feed_id,
            "xsec_token": xsec_token,
            "load_all_comments": load_all_comments,
            "comment_config": {
                "max_comment_items": max_comment_items,
                "max_replies_threshold": max_replies_threshold,
                "click_more_replies": True,
                "scroll_speed": 1.0,
            },
        }
        return self._req("POST", "/api/v1/feeds/detail", json=payload)

    # --- 列表（首页流，不常用） ---
    def list_feeds(self, limit: int = 20) -> list[dict]:
        resp = self._req("GET", f"/api/v1/feeds?limit={limit}")
        return resp.get("feeds", [])


# 单例
_mcp_client: MCPClient | None = None


def get_mcp_client(conf: dict | None = None) -> MCPClient:
    global _mcp_client
    if _mcp_client is None:
        url = (conf or load_xhs_conf()).get("mcp_url") or MCP_BASE_URL
        _mcp_client = MCPClient(base_url=url)
    return _mcp_client


def reset_mcp_client():
    """切换 mcp_url 时调用。"""
    global _mcp_client
    _mcp_client = None


# ---------------- 核心抓取逻辑 ----------------

def _to_dt(ts) -> datetime | None:
    """毫秒/秒时间戳 → aware datetime。"""
    if ts is None:
        return None
    try:
        ts = int(ts)
        # mcp 返回毫秒
        if ts > 1e12:
            ts = ts / 1000
        if ts <= 0:
            return None
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def _safe_int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def fetch_notes_for_keyword(client: MCPClient, keyword: str,
                            max_notes: int = 40) -> list[dict]:
    """搜索单个关键词，返回笔记列表（已含 xsec_token，便于后续取详情）。"""
    feeds = client.search_feeds(keyword)
    # 只保留笔记类型，过滤掉视频/专题等（ntype 为 note）
    notes = []
    for f in feeds:
        if f.get("note_type") in ("note", "normal", "") or f.get("type") in ("note", "normal", ""):
            notes.append(f)
        if len(notes) >= max_notes:
            break
    return notes


def fetch_note_detail(client: MCPClient, note: dict,
                       load_all_comments: bool = True,
                       max_comments: int = 50) -> dict | None:
    """拉取单条笔记的详情与评论。"""
    feed_id = note.get("note_id") or note.get("id")
    xsec_token = note.get("xsec_token") or note.get("xsecToken")
    if not feed_id or not xsec_token:
        return None
    try:
        detail = client.get_feed_detail(
            feed_id=feed_id,
            xsec_token=xsec_token,
            load_all_comments=load_all_comments,
            max_comment_items=max_comments,
        )
        return detail
    except Exception as e:
        print(f"[xhs_mcp] 获取详情失败 note={feed_id}: {e}", flush=True)
        return None


def _normalize_note(note: dict, detail: dict | None, source_keyword: str) -> dict:
    """把 mcp 返回的原始 note + detail 归一化为入库字典。"""
    # note 基础字段
    note_id = note.get("note_id") or note.get("id") or ""
    title = (note.get("title") or "").strip()
    desc = (note.get("desc") or note.get("note_desc") or "").strip()
    # detail 里的 note 更完整（含完整正文）
    note_detail = detail.get("note") if detail else {}
    if note_detail:
        title = (note_detail.get("title") or title).strip()
        desc = (note_detail.get("desc") or note_detail.get("note_desc") or desc).strip()
    nick = (note.get("user", {}).get("nickname")
            or note.get("author", {}).get("nickname")
            or note.get("nickname")
            or "").strip()
    pub_ts = _to_dt(note.get("time") or note.get("publish_time")
                    or (note_detail.get("time") if note_detail else None))
    note_url = note.get("note_url") or note.get("link") or (
        f"https://www.xiaohongshu.com/explore/{note_id}" if note_id else ""
    )
    # 互动统计
    interact = note.get("interact_info") or note.get("stats") or {}
    if note_detail:
        interact = note_detail.get("interact_info") or note_detail.get("stats") or interact
    stats_json = json.dumps({
        "liked": _safe_int(interact.get("liked_count") or interact.get("like_count")),
        "collected": _safe_int(interact.get("collected_count") or interact.get("collect_count")),
        "comments": _safe_int(interact.get("comment_count")),
        "shares": _safe_int(interact.get("share_count")),
    }, ensure_ascii=False)
    # 标签
    tags = note.get("tag_list") or note.get("tags") or []
    if isinstance(tags, list):
        tag_str = ",".join(str(t).strip() for t in tags if t)
    else:
        tag_str = str(tags)

    return {
        "note_id": note_id,
        "ntype": note.get("note_type") or note.get("type") or "note",
        "title": title,
        "note_desc": desc,
        "nickname": nick,
        "pub_ts": pub_ts,
        "stats": stats_json,
        "note_url": note_url,
        "source_keyword": source_keyword,
        "tag_list": tag_str,
    }


def _normalize_comments(detail: dict, note_id: str) -> list[dict]:
    """把 mcp 返回的评论归一化。"""
    comments = detail.get("comments", []) if detail else []
    out = []
    for c in comments:
        cid = c.get("comment_id") or c.get("id") or ""
        if not cid:
            continue
        out.append({
            "comment_id": cid,
            "note_id": note_id,
            "content": (c.get("content") or "").strip(),
            "author": (c.get("user", {}).get("nickname") or c.get("author", "")).strip(),
            "pub_ts": _to_dt(c.get("create_time") or c.get("time")),
            "like_count": _safe_int(c.get("like_count") or c.get("liked_count")),
            "parent_comment_id": str(c.get("parent_comment_id") or c.get("root_comment_id") or ""),
        })
    return out


# ---------------- 入库同步 ----------------

def sync_notes_to_db(notes_data: list[dict], comments_data: list[dict],
                     allowed_keywords: list[str] | None = None) -> dict:
    """把归一化后的笔记与评论 upsert 进云库。

    allowed_keywords 不为空时只导入这些 source_keyword 的笔记——
    防止历史残留数据被误入库。
    """
    from app import get_conn

    if allowed_keywords is not None:
        allow = {str(k).strip() for k in allowed_keywords if str(k).strip()}
        notes_data = [n for n in notes_data if str(n.get("source_keyword") or "").strip() in allow]
        keep_ids = {n["note_id"] for n in notes_data}
        comments_data = [c for c in comments_data if c.get("note_id") in keep_ids]

    stats = {
        "new_notes": 0, "total_notes": len(notes_data),
        "new_comments": 0, "total_comments": len(comments_data),
        "notes_for_sa_news": 0,
    }
    if not notes_data and not comments_data:
        return stats

    news_items: list[dict] = []
    with get_conn() as conn, conn.cursor() as cur:
        for n in notes_data:
            note_id = n["note_id"]
            if not note_id:
                continue
            cur.execute(
                """
                INSERT INTO sa_xhs_notes
                    (note_id, ntype, title, note_desc, nickname, pub_ts, stats,
                     note_url, source_keyword, tag_list, is_read)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE)
                ON CONFLICT (note_id) DO UPDATE SET
                    title = EXCLUDED.title,
                    note_desc = EXCLUDED.note_desc,
                    stats = EXCLUDED.stats,
                    tag_list = EXCLUDED.tag_list
                """,
                (
                    note_id,
                    n.get("ntype") or "",
                    n.get("title") or "",
                    n.get("note_desc") or "",
                    n.get("nickname") or "",
                    n.get("pub_ts"),
                    n.get("stats") or "{}",
                    n.get("note_url") or "",
                    n.get("source_keyword") or "",
                    n.get("tag_list") or "",
                ),
            )
            stats["new_notes"] += cur.rowcount
            # 新插入的笔记推进 sa_news
            full = f"{n.get('title','')}\n{n.get('note_desc','')}".strip()
            if full:
                news_items.append({
                    "title": (n.get("title") or n.get("note_desc") or "")[:120],
                    "content": full,
                    "url": n.get("note_url") or "",
                    "source": "xhs",
                    "media": n.get("nickname") or "xiaohongshu",
                    "publish_time": n.get("pub_ts").isoformat() if n.get("pub_ts") else None,
                    "keyword": n.get("source_keyword") or "",
                })
        for c in comments_data:
            cid = c.get("comment_id") or ""
            if not cid:
                continue
            cur.execute(
                """
                INSERT INTO sa_xhs_comments
                    (comment_id, note_id, content, author, pub_ts,
                     like_count, parent_comment_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (comment_id) DO UPDATE SET
                    like_count = EXCLUDED.like_count
                """,
                (
                    cid,
                    c.get("note_id") or "",
                    c.get("content") or "",
                    c.get("author") or "",
                    c.get("pub_ts"),
                    c.get("like_count") or 0,
                    c.get("parent_comment_id") or "",
                ),
            )
            stats["new_comments"] += cur.rowcount

        # 匹配自选股名 → sa_news + sa_news_related
        if news_items:
            try:
                from channels_watch import sentiment_of
                matched = _match_watchlist_codes(news_items, cur)
                for it in matched:
                    codes = it.pop("related_codes", [])
                    senti = sentiment_of(it.get("content") or it.get("title") or "", codes)
                    cur.execute(
                        "INSERT INTO sa_news (code, title, url, source, media, "
                        "publish_time, sentiment) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT (url) DO NOTHING",
                        (
                            "",
                            (it.get("title") or "")[:500],
                            it["url"],
                            "xhs",
                            it.get("media") or "xiaohongshu",
                            it.get("publish_time") or None,
                            senti,
                        ),
                    )
                    for c in codes[:20]:
                        cur.execute(
                            "INSERT INTO sa_news_related (url, code) VALUES (%s,%s) "
                            "ON CONFLICT (url, code) DO NOTHING",
                            (it["url"], c),
                        )
                stats["notes_for_sa_news"] = len(matched)
            except Exception as exc:
                print(f"[xhs_mcp] 推 sa_news 失败（不影响 xhs 自身入库）: {exc}", flush=True)
    return stats


def _match_watchlist_codes(items: list[dict], cur) -> list[dict]:
    """标题/正文/搜索词命中自选股名的条目，带上 related codes。"""
    cur.execute(
        "SELECT code, name FROM sa_watchlist "
        "WHERE name IS NOT NULL AND name <> ''"
    )
    name_to_codes: dict[str, list[str]] = {}
    for code, name in cur.fetchall():
        name = (name or "").strip()
        if name:
            name_to_codes.setdefault(name, []).append(str(code))
    if not name_to_codes:
        return []
    out = []
    for it in items:
        text = f"{it.get('title','')}\n{it.get('content','')}\n{it.get('keyword','')}"
        hit_codes: list[str] = []
        for name, codes in name_to_codes.items():
            if name and name in text:
                hit_codes.extend(codes)
        if hit_codes:
            it = dict(it)
            it["related_codes"] = sorted(set(hit_codes))[:20]
            out.append(it)
    return out


# ---------------- 对外主流程 ----------------

def fetch_xhs() -> dict:
    """完整流程：解析关键词 → 搜索 → 详情+评论 → 入库 → 匹配自选推进 sa_news。"""
    if _state["fetching"]:
        return {"skipped": True, "reason": "已有抓取任务在运行"}
    # 登录时允许并发抓取（用 LOGIN_LOCK 保护，不碰 CRAWL_LOCK）
    with CRAWL_LOCK:
        _state["fetching"] = True
        try:
            conf = load_xhs_conf()
            keywords = resolve_keywords(conf)
            if not keywords:
                result = {
                    "new_notes": 0, "total_notes": 0,
                    "new_comments": 0, "total_comments": 0,
                    "keywords": [], "notes_for_sa_news": 0,
                    "message": "无关键词（config.xhs.keywords 为空且未开启/未配置自选股）",
                }
                return result

            client = get_mcp_client(conf)
            max_notes = int(conf.get("max_notes_per_keyword", 40))
            max_comments = int(conf.get("max_comments_per_note", 50))
            load_all = bool(conf.get("load_all_comments", True))

            all_notes: list[dict] = []
            all_comments: list[dict] = []

            for kw in keywords:
                try:
                    notes = fetch_notes_for_keyword(client, kw, max_notes)
                    for note in notes:
                        detail = fetch_note_detail(client, note, load_all, max_comments)
                        norm_note = _normalize_note(note, detail, kw)
                        all_notes.append(norm_note)
                        if detail:
                            all_comments.extend(_normalize_comments(detail, norm_note["note_id"]))
                except Exception as e:
                    print(f"[xhs_mcp] 关键词 {kw} 抓取失败: {e}", flush=True)
                    continue

            result = sync_notes_to_db(all_notes, all_comments, allowed_keywords=keywords)
            result["keywords"] = keywords
            return result
        finally:
            _state["fetching"] = False
            _state["last_run"] = datetime.now().isoformat(timespec="seconds")


def login_xhs() -> dict:
    """触发生成登录二维码（阻塞等待扫码完成）。

    注意：mcp 服务内部会启动浏览器等待扫码，完成后自动保存 cookies。
    此处轮询登录状态直到成功或超时。
    """
    if _state["fetching"]:
        return {"ok": False, "error": "已有抓取/登录任务在运行"}
    if not LOGIN_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "已有登录任务在进行"}
    try:
        _state["fetching"] = True  # 兼容 get_status 返回
        client = get_mcp_client()
        # 触发二维码生成
        try:
            qr_resp = client.get_login_qrcode()
            # mcp 返回 {img: 'data:image/png;base64,...', timeout, is_logged_in}
            print(f"[xhs_mcp] 登录二维码已生成，请扫码（img 长度 {len(qr_resp.get('img') or '')}）", flush=True)
        except Exception as e:
            return {"ok": False, "error": f"无法连接 mcp 服务生成二维码：{e}"}

        # 轮询登录状态（最多等 fetch_timeout_minutes）
        conf = load_xhs_conf()
        timeout_min = int(conf.get("fetch_timeout_minutes", 12))
        deadline = time.time() + timeout_min * 60
        while time.time() < deadline:
            time.sleep(3)
            try:
                status = client.check_login_status()
                if status.get("is_logged_in"):
                    print("[xhs_mcp] 登录成功", flush=True)
                    return {"ok": True}
            except Exception:
                pass
        return {"ok": False, "error": "扫码超时，请重试"}
    finally:
        _state["fetching"] = False
        LOGIN_LOCK.release()
        _state["last_run"] = datetime.now().isoformat(timespec="seconds")


def request_login_qr() -> dict:
    """非阻塞登录：取二维码立即返回，后台线程轮询登录态。

    返回 {ok, img: 'data:image/png;base64,...'} 或 {ok: False, error}。
    如果已经登录，直接返回 {ok: True, already_logged_in: True}。
    轮询期间 _state["login_waiting"]=True，由 /api/xhs/login-state 查询。
    """
    # 先检查是否已登录
    client = get_mcp_client()
    try:
        st = client.check_login_status()
        if st.get("is_logged_in"):
            _state["login_ok"] = True
            _state["login_waiting"] = False
            return {"ok": True, "already_logged_in": True}
    except Exception:
        pass  # 连不上 mcp 时继续尝试获取二维码
    
    # 只阻止并发登录，不阻止并发抓取
    if _state.get("login_waiting"):
        return {"ok": False, "error": "已有登录任务在等待扫码"}
    if not LOGIN_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "已有登录任务在进行"}
    client = get_mcp_client()
    try:
        qr = client.get_login_qrcode()
    except Exception as e:
        LOGIN_LOCK.release()
        return {"ok": False, "error": f"无法连接 mcp 服务生成二维码：{e}"}
    img = qr.get("img") or ""
    # mcp 返回 is_logged_in=True + timeout=0s 表示已经登录，此时不返回二维码
    if qr.get("is_logged_in") and not img:
        LOGIN_LOCK.release()
        _state["login_ok"] = True
        _state["login_waiting"] = False
        return {"ok": True, "already_logged_in": True}
    if not img:
        LOGIN_LOCK.release()
        return {"ok": False, "error": f"mcp 未返回二维码图片：{qr}"}
    _state["login_ok"] = False
    _state["login_waiting"] = True

    def _wait():
        try:
            conf = load_xhs_conf()
            timeout_min = int(conf.get("fetch_timeout_minutes", 12))
            deadline = time.time() + timeout_min * 60
            while time.time() < deadline:
                time.sleep(3)
                try:
                    if client.check_login_status().get("is_logged_in"):
                        print("[xhs_mcp] 登录成功", flush=True)
                        _state["login_ok"] = True
                        return
                except Exception:
                    pass
            print("[xhs_mcp] 扫码超时", flush=True)
        finally:
            _state["login_waiting"] = False
            LOGIN_LOCK.release()
            _state["last_run"] = datetime.now().isoformat(timespec="seconds")

    threading.Thread(target=_wait, daemon=True).start()
    return {"ok": True, "img": img}


def login_state() -> dict:
    """前端轮询用：登录是否完成、是否还在等待扫码。"""
    # 如果等待中，直接返回状态
    if _state.get("login_waiting"):
        return {"logged_in": False, "waiting": True}
    # 如果标记已登录，返回已登录
    if _state.get("login_ok"):
        return {"logged_in": True, "waiting": False}
    # 否则实时查 mcp 服务
    client = get_mcp_client()
    try:
        st = client.check_login_status()
        return {"logged_in": bool(st.get("is_logged_in")), "waiting": False}
    except Exception:
        return {"logged_in": False, "waiting": False}


def get_status() -> dict:
    conf = load_xhs_conf()
    mcp_ok = False
    login_ok = False
    # 健康检查用极短超时，避免卡死
    try:
        client = MCPClient(base_url=conf.get("mcp_url") or MCP_BASE_URL, timeout=2)
        st = client.check_login_status()
        mcp_ok = True
        login_ok = st.get("is_logged_in", False)
    except Exception:
        pass
    return {
        "fetching": _state["fetching"],
        "last_run": _state["last_run"],
        "last_result": _state["last_result"],
        "mcp_service_ok": mcp_ok,
        "login_ok": login_ok,
        "mcp_url": conf.get("mcp_url") or MCP_BASE_URL,
        "keywords": resolve_keywords(conf),
    }


def digest_markdown(hours: int = 24) -> str:
    """近 N 小时新笔记的 markdown 汇总。"""
    from app import get_conn
    from psycopg2.extras import RealDictCursor
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT note_id, title, note_desc, nickname, pub_ts, stats, source_keyword, note_url
            FROM sa_xhs_notes WHERE pub_ts >= %s ORDER BY pub_ts DESC LIMIT 30
            """,
            (since,),
        )
        rows = cur.fetchall()
    if not rows:
        return ""
    lines = [f"### 小红书笔记（近 {hours} 小时，{len(rows)} 条）", ""]
    for r in rows:
        ts = r["pub_ts"].astimezone().strftime("%m-%d %H:%M") if r.get("pub_ts") else ""
        title = (r.get("title") or r.get("note_desc") or "").strip()
        title = " ".join(title.split())[:100]
        nick = r.get("nickname") or ""
        st = r.get("stats") or {}
        kw = r.get("source_keyword") or ""
        lines.append(f"- **{nick}**（{ts}，词「{kw}」）{title}")
        lines.append(
            f"  赞 {st.get('liked', 0)} / 收藏 {st.get('collected', 0)} / 评 {st.get('comments', 0)}"
        )
    return "\n".join(lines)