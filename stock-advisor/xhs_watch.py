"""小红书舆情监控：调 MediaCrawler 抓关键词搜索结果+评论，同步进云库 sa_xhs_*。

流程：
    config.yaml xhs.keywords（+ 自选股票名）作为搜索词
      → run_crawl() 子进程调 MediaCrawler CLI（uv run main.py --platform xhs --type search）
      → sync_jsonl_to_db() 读 data/xhs/jsonl/search_contents_*.jsonl、search_comments_*.jsonl
      → upsert 进 sa_xhs_notes / sa_xhs_comments（首次插入标记未读）
      → 匹配到自选股的笔记推进 sa_news（source=xhs），供模拟盘舆情引用

选型（2026-10-13）：复用本仓库已有 MediaCrawler 子模块（hellostronger/MediaCrawler fork，
xhs 搜索+笔记详情+一级/二级评论全链路已内建），与 bili_monitor/wb_monitor 同一接入姿势。
未采用 xiaohongshu-mcp 等独立 MCP 服务：多一个常驻进程/端口，且评论翻页能力不比 MediaCrawler 强。

⚠️ 依赖：
    - MediaCrawler browser_data/xhs/ 里有小红书登录态（POST /api/xhs/login 弹浏览器扫码）
    - MediaCrawler 非商业学习许可（NON-COMMERCIAL LEARNING LICENSE 1.1），仅个人研究用
    - 小红书风控严：interval_minutes 默认 120，单轮关键词数与笔记数有上限
"""

import json
import os
import re
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
MC_DIR = BASE_DIR.parent / "MediaCrawler"
MC_DATA_DIR = MC_DIR / "data" / "xhs" / "jsonl"
XHS_USER_DATA = MC_DIR / "browser_data" / "xhs_user_data_dir"

CRAWL_LOCK = threading.Lock()

_state = {"fetching": False, "last_result": None, "last_run": None}


# ---------------- 配置 ----------------

def load_xhs_conf() -> dict:
    """从 config.yaml 读 xhs 段；缺失用默认值。

    keywords 可以是 list[str] 或逗号分隔字符串，统一归一成 list[str]。
    """
    defaults = {
        "enabled": True,
        "interval_minutes": 120,
        "fetch_timeout_minutes": 12,
        "max_notes": 40,
        "max_comments": 20,
        "use_watchlist": True,
        "keywords": [],
        "notify_new": False,
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
    """合并 config 关键词 + 自选股名（use_watchlist 时）。

    keywords 可以是 list[str] 或逗号分隔字符串（与 load_xhs_conf 同口径）——
    直接 list("ab") 会把字符串拆成单字符，必须先分流。
    """
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
        # MediaCrawler 用英文逗号拼 KEYWORDS，词内不能含逗号
        k = k.replace(",", " ")
        if not k:
            continue
        seen.add(k)
        out.append(k)
    # 风控：单轮关键词太多必被打；上限 12
    return out[:12]


# ---------------- 子进程调 MediaCrawler ----------------

def _uv_cmd() -> str:
    return os.environ.get("UV_BIN", "uv")


def run_crawl(keywords: list[str], headless: bool, timeout_min: int,
              max_notes: int = 40, max_comments: int = 20) -> bool:
    """按关键词搜索小红书笔记+评论。阻塞直到完成或超时。"""
    if not MC_DIR.exists():
        raise FileNotFoundError(f"MediaCrawler 目录不存在: {MC_DIR}")
    if not keywords:
        raise ValueError("关键词为空，无法搜索")
    log_path = MC_DIR / "xhs_crawl_last.log"
    cmd = [
        _uv_cmd(), "run", "python", "main.py",
        "--platform", "xhs",
        "--type", "search",
        "--keywords", ",".join(keywords),
        "--save_data_option", "jsonl",
        "--headless", "true" if headless else "false",
        "--get_comment", "true",
        "--get_sub_comment", "true",
        "--crawler_max_notes_count", str(max(20, int(max_notes))),  # xhs 搜索页固定 20/页，下限 20
        "--max_comments_count_singlenotes", str(max(1, int(max_comments))),
    ]
    # 同 bili/wb：输出写文件不用 PIPE（日志写满会堵死子进程）
    with open(log_path, "w", encoding="utf-8", errors="replace") as log_f:
        proc = subprocess.Popen(
            cmd, cwd=str(MC_DIR),
            stdout=log_f, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            start_new_session=not hasattr(subprocess, "CREATE_NO_WINDOW"),
        )
        try:
            proc.wait(timeout=timeout_min * 60)
        except subprocess.TimeoutExpired:
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            raise TimeoutError(f"MediaCrawler 超时（{timeout_min} 分钟），已终止")
        if proc.returncode != 0:
            tail = ""
            text = ""
            try:
                text = log_path.read_text(encoding="utf-8", errors="replace")
                tail = "\n".join(text.splitlines()[-15:])
            except OSError:
                pass
            # ⚠️ 「未登录」是最常见的失败，而它的**原始报错完全指不到真因**：
            #   用户看到的是「退出码 1 + 一段日志」，不知道要做什么。
            #   实测（2026-10-15）这条路径的判据是爬虫自己打印的
            #   `Login state result: False` —— 那是**我们拉的子进程的输出**，
            #   不是猜测关键词（AGENTS.md 反对的是拿关键词猜新闻语义）。
            if "Login state result: False" in text or "login_by_qrcode" in text:
                raise RuntimeError(
                    "小红书未登录：请先点「登录小红书」扫码（会弹出浏览器窗口）。"
                    "browser_data 目录存在但**未登录**时，抓取会以 headless 运行 —— "
                    "扫码窗口不显示，必然失败。\n" + tail)
            raise RuntimeError(f"MediaCrawler 退出码 {proc.returncode}\n{tail}")
    return True


# ---------------- JSONL 同步入库 ----------------

def _read_jsonl_files(pattern: str) -> list[dict]:
    """读匹配 pattern 的所有 jsonl（按 note_id/comment_id 去重，后者覆盖前者）。"""
    items: dict[str, dict] = {}
    if not MC_DATA_DIR.exists():
        return []
    for f in sorted(MC_DATA_DIR.glob(pattern)):
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    it = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = it.get("note_id") or it.get("comment_id")
                if key:
                    items[str(key)] = it
        except OSError:
            continue
    return list(items.values())


def _to_dt(ts) -> datetime | None:
    """秒级时间戳 → aware datetime；空/0 返回 None。"""
    try:
        ts = int(ts)
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


def sync_jsonl_to_db(allowed_keywords: list[str] | None = None) -> dict:
    """把 JSONL 里的笔记与评论 upsert 进云库。

    allowed_keywords 不为空时只导入这些 source_keyword 的笔记——jsonl 目录会残留
    历史手动爬取文件，不过滤会把别的词的数据也灌进库。

    返回 {new_notes, total_notes, new_comments, total_comments, notes_for_sa_news}。
    """
    from app import get_conn

    notes = _read_jsonl_files("search_contents_*.jsonl")
    comments = _read_jsonl_files("search_comments_*.jsonl")
    if allowed_keywords is not None:
        allow = {str(k).strip() for k in allowed_keywords if str(k).strip()}
        notes = [n for n in notes if str(n.get("source_keyword") or "").strip() in allow]
        keep = {str(n.get("note_id")) for n in notes}
        comments = [c for c in comments if str(c.get("note_id") or "") in keep]

    stats = {
        "new_notes": 0, "total_notes": len(notes),
        "new_comments": 0, "total_comments": len(comments),
        "notes_for_sa_news": 0,
    }
    if not notes and not comments:
        return stats

    news_items: list[dict] = []
    with get_conn() as conn, conn.cursor() as cur:
        for n in notes:
            note_id = str(n.get("note_id") or "")
            if not note_id:
                continue
            title = (n.get("title") or "").strip()
            desc = (n.get("desc") or "").strip()
            nick = n.get("nickname") or ""
            tags = n.get("tag_list") or ""
            pub = _to_dt(n.get("time"))
            note_url = n.get("note_url") or (
                f"https://www.xiaohongshu.com/explore/{note_id}"
            )
            stats_json = json.dumps({
                "liked": _safe_int(n.get("liked_count")),
                "collected": _safe_int(n.get("collected_count")),
                "comments": _safe_int(n.get("comment_count")),
                "shares": _safe_int(n.get("share_count")),
            }, ensure_ascii=False)
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
                    n.get("type") or "",
                    title,
                    desc,
                    nick,
                    pub,
                    stats_json,
                    note_url,
                    n.get("source_keyword") or "",
                    tags if isinstance(tags, str) else ",".join(
                        str(t) for t in tags if t
                    ),
                ),
            )
            stats["new_notes"] += cur.rowcount
            # 新插入的笔记推进 sa_news（模拟盘舆情流）；ON CONFLICT 自然去重
            full = f"{title}\n{desc}".strip()
            if full:
                news_items.append({
                    "title": (title or desc)[:120],
                    "content": full,
                    "url": note_url,
                    "source": "xhs",
                    "media": nick,
                    "publish_time": pub.isoformat() if pub else None,
                    "keyword": n.get("source_keyword") or "",
                })
        for c in comments:
            cid = str(c.get("comment_id") or "")
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
                    str(c.get("note_id") or ""),
                    c.get("content") or "",
                    c.get("nickname") or "",
                    _to_dt(c.get("create_time")),
                    _safe_int(c.get("like_count")),
                    str(c.get("parent_comment_id") or ""),
                ),
            )
            stats["new_comments"] += cur.rowcount

        # 匹配自选股名 → sa_news + sa_news_related。
        # 不用 channels_watch.save_to_sa_news：它硬编码 SOURCE="wx_channels"，
        # 会把小红书条目错标成视频号（source 字段会误导下游过滤）。
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
                print(f"[xhs] 推 sa_news 失败（不影响 xhs 自身入库）: {exc}", flush=True)
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
    """完整流程：解析关键词 → 爬取 → 入库 → 匹配自选推进 sa_news。"""
    if _state["fetching"]:
        return {"skipped": True, "reason": "已有抓取任务在运行"}
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
            else:
                login_ok = _browser_data_ready()
                try:
                    run_crawl(
                        keywords,
                        headless=login_ok,
                        timeout_min=int(conf.get("fetch_timeout_minutes", 12)),
                        max_notes=int(conf.get("max_notes", 40)),
                        max_comments=int(conf.get("max_comments", 20)),
                    )
                except Exception as exc:
                    # 超时/非零退出：盘上已写的 JSONL 仍入库，否则整轮白跑
                    result = sync_jsonl_to_db(allowed_keywords=keywords)
                    result["keywords"] = keywords
                    result["warning"] = f"爬虫异常退出，仅同步已写盘数据：{exc}"
                    return result
                result = sync_jsonl_to_db(allowed_keywords=keywords)
                result["keywords"] = keywords
            return result
        finally:
            _state["fetching"] = False
            _state["last_run"] = datetime.now().isoformat(timespec="seconds")


def _browser_data_ready() -> bool:
    """browser_data/xhs_user_data_dir 已有登录态才允许 headless。"""
    return XHS_USER_DATA.exists() and any(XHS_USER_DATA.iterdir())


def login_xhs() -> dict:
    """弹出浏览器扫码登录小红书（headless=false）。阻塞直到完成。"""
    conf = load_xhs_conf()
    with CRAWL_LOCK:
        _state["fetching"] = True
        try:
            run_crawl(
                ["股票"],  # 占位词触发完整启动→登录流程
                headless=False,
                timeout_min=int(conf.get("fetch_timeout_minutes", 12)),
                max_notes=20,
                max_comments=1,
            )
            return {"ok": _browser_data_ready()}
        finally:
            _state["fetching"] = False
            _state["last_run"] = datetime.now().isoformat(timespec="seconds")


def get_status() -> dict:
    return {
        "fetching": _state["fetching"],
        "last_run": _state["last_run"],
        "last_result": _state["last_result"],
        "browser_data_ready": _browser_data_ready(),
        "mediacrawler_exists": MC_DIR.exists(),
        "keywords": resolve_keywords(),
    }


# ---------------- 报告引用 ----------------

def digest_markdown(hours: int = 24) -> str:
    """近 N 小时新笔记的 markdown 汇总。无数据返回空串。"""
    from app import get_conn
    from psycopg2.extras import RealDictCursor
    from datetime import timedelta
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT note_id, title, desc, nickname, pub_ts, stats, source_keyword, note_url
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
        title = re.sub(r"\s+", " ", (r.get("title") or r.get("desc") or "").strip())[:100]
        nick = r.get("nickname") or ""
        st = r.get("stats") or {}
        kw = r.get("source_keyword") or ""
        lines.append(f"- **{nick}**（{ts}，词「{kw}」）{title}")
        lines.append(
            f"  赞 {st.get('liked', 0)} / 收藏 {st.get('collected', 0)} / 评 {st.get('comments', 0)}"
        )
    return "\n".join(lines)
