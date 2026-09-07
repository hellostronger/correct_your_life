"""微博博主监听模块：调 MediaCrawler 抓博主微博+评论，同步进云库 sa_wb_*。

流程：
    sa_wb_creators 里的博主列表
      → run_crawl() 子进程调 MediaCrawler CLI（uv run main.py --platform wb --type creator）
      → sync_jsonl_to_db() 读 data/wb/jsonl/creator_contents_*.jsonl、creator_comments_*.jsonl
      → upsert 进 sa_wb_posts / sa_wb_comments（首次插入标记未读）

依赖 MediaCrawler 侧补丁（2026-09-07）：
    - 微博 JSONL 带 creator_uid（真实 user_id）字段，用于归属到监听列表
    - creator 模式单博主抓取上限 + 容错（CRAWLER_MAX_NOTES_COUNT 条/博主）
"""

import json
import re
import subprocess
import threading
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
MC_DIR = BASE_DIR.parent / "MediaCrawler"
MC_DATA_DIR = MC_DIR / "data" / "wb" / "jsonl"

CRAWL_LOCK = threading.Lock()

_state = {"fetching": False, "last_result": None, "last_run": None}


# ---------------- 配置 ----------------

def load_wb_conf() -> dict:
    """从 config.yaml 读 wb 段；缺失用默认值。"""
    defaults = {"enabled": True, "interval_minutes": 60, "fetch_timeout_minutes": 15}
    try:
        text = (BASE_DIR / "config.yaml").read_text(encoding="utf-8")
        import yaml
        data = yaml.safe_load(text) or {}
        conf = dict(data.get("wb") or {})
    except Exception:
        conf = {}
    defaults.update({k: v for k, v in conf.items() if v is not None})
    return defaults


# ---------------- 子进程调 MediaCrawler ----------------

def run_crawl(creator_ids: list[str], headless: bool, timeout_min: int) -> bool:
    """拉起 MediaCrawler 抓指定博主的微博+评论。阻塞直到完成或超时。"""
    if not MC_DIR.exists():
        raise FileNotFoundError(f"MediaCrawler 目录不存在: {MC_DIR}")
    log_path = MC_DIR / "wb_crawl_last.log"
    cmd = [
        "uv", "run", "python", "main.py",
        "--platform", "wb",
        "--type", "creator",
        "--creator_id", ",".join(creator_ids),
        "--save_data_option", "jsonl",
        "--headless", "true" if headless else "false",
        "--get_comment", "true",
    ]
    # 同 bili_monitor：输出写文件，不用 PIPE（防日志写满阻塞子进程）
    with open(log_path, "w", encoding="utf-8", errors="replace") as log_f:
        proc = subprocess.Popen(
            cmd, cwd=str(MC_DIR),
            stdout=log_f, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        try:
            proc.wait(timeout=timeout_min * 60)
        except subprocess.TimeoutExpired:
            # Windows 上必须整棵进程树杀掉，否则 python/chrome 孤儿锁住 browser_data
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )
            raise TimeoutError(f"MediaCrawler 超时（{timeout_min} 分钟），已终止")
        if proc.returncode != 0:
            tail = ""
            try:
                lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                tail = "\n".join(lines[-15:])
            except OSError:
                pass
            raise RuntimeError(f"MediaCrawler 退出码 {proc.returncode}\n{tail}")
    return True


# ---------------- JSONL 同步入库 ----------------

def _read_jsonl_files(pattern: str) -> list[dict]:
    """读匹配 pattern 的所有 jsonl 文件（按 note_id/comment_id 去重，后者覆盖前者）。"""
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
                    items[str(key)] = it  # 后读到的覆盖先读到的（互动数等取最新）
        except OSError:
            continue
    return list(items.values())


def _wb_ts_to_dt(ts) -> datetime | None:
    """微博 create_time 为秒级时间戳（MediaCrawler rfc2822_to_timestamp 已转过）；异常值返回 None。"""
    try:
        ts = int(float(ts))
        if ts <= 0:
            return None
        return datetime.fromtimestamp(ts)
    except (TypeError, ValueError):
        return None


def sync_jsonl_to_db(allowed_uids: list[str] | None = None) -> dict:
    """把 JSONL 里的微博与评论 upsert 进云库。返回 {new_posts, total_posts, new_comments, total_comments}。

    allowed_uids 不为空时只导入这些博主的数据——jsonl 目录里留着历史手动爬取的
    其他博主文件，不过滤会把没监听的人的微博也灌进库。
    """
    from app import get_conn  # 延迟导入避免循环依赖

    posts = _read_jsonl_files("creator_contents_*.jsonl")
    comments = _read_jsonl_files("creator_comments_*.jsonl")
    if allowed_uids is not None:
        allow = {str(u) for u in allowed_uids}
        posts = [p for p in posts if str(p.get("creator_uid") or "") in allow]
        # 评论只保留已导入微博的（note_id 关联）
        keep = {str(p.get("note_id")) for p in posts}
        comments = [c for c in comments if str(c.get("note_id") or "") in keep]

    stats = {"new_posts": 0, "total_posts": len(posts),
             "new_comments": 0, "total_comments": len(comments)}
    if not posts and not comments:
        return stats

    with get_conn() as conn, conn.cursor() as cur:
        for p in posts:
            cur.execute(
                """
                INSERT INTO sa_wb_posts
                    (note_id, uid, author_name, text, pub_ts, stats, is_read)
                VALUES (%s,%s,%s,%s,%s,%s,FALSE)
                ON CONFLICT (note_id) DO UPDATE SET
                    stats = EXCLUDED.stats,
                    text = EXCLUDED.text
                """,
                (
                    str(p.get("note_id")),
                    str(p.get("creator_uid") or ""),
                    p.get("nickname") or "",
                    p.get("content") or "",
                    _wb_ts_to_dt(p.get("create_time")),
                    json.dumps({
                        "likes": _safe_int(p.get("liked_count")),
                        "comments": _safe_int(p.get("comments_count")),
                        "forwards": _safe_int(p.get("shared_count")),
                    }, ensure_ascii=False),
                ),
            )
            stats["new_posts"] += cur.rowcount
        for c in comments:
            cur.execute(
                """
                INSERT INTO sa_wb_comments
                    (comment_id, note_id, content, author, pub_ts, like_count)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (comment_id) DO UPDATE SET
                    like_count = EXCLUDED.like_count
                """,
                (
                    str(c.get("comment_id")),
                    str(c.get("note_id") or ""),
                    c.get("content") or "",
                    c.get("nickname") or "",
                    _wb_ts_to_dt(c.get("create_time")),
                    _safe_int(c.get("comment_like_count")),
                ),
            )
            stats["new_comments"] += cur.rowcount
    return stats


def _safe_int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _sync_creator_names() -> None:
    """把 JSONL 里出现过的 creator_uid → nickname 回填 sa_wb_creators（网页不用手动填名字）。"""
    from app import get_conn
    posts = _read_jsonl_files("creator_contents_*.jsonl")
    names: dict[str, str] = {}
    for p in posts:
        uid, name = str(p.get("creator_uid") or ""), p.get("nickname") or ""
        if uid and name:
            names[uid] = name
    if not names:
        return
    with get_conn() as conn, conn.cursor() as cur:
        for uid, name in names.items():
            cur.execute(
                "UPDATE sa_wb_creators SET name = %s WHERE uid = %s "
                "AND (name IS NULL OR name = '')",
                (name, uid),
            )


# ---------------- 对外主流程 ----------------

def fetch_wb() -> dict:
    """完整流程：读博主列表 → 爬取 → 同步入库。返回结果摘要。"""
    from app import get_conn
    if _state["fetching"]:
        return {"skipped": True, "reason": "已有抓取任务在运行"}
    with CRAWL_LOCK:
        _state["fetching"] = True
        try:
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT uid FROM sa_wb_creators ORDER BY added_at")
                uids = [r[0] for r in cur.fetchall()]
            if not uids:
                result = {"new_posts": 0, "total_posts": 0,
                          "new_comments": 0, "total_comments": 0, "creators": 0,
                          "message": "未配置博主"}
            else:
                conf = load_wb_conf()
                login_ok = _browser_data_ready()
                try:
                    run_crawl(uids, headless=login_ok,
                              timeout_min=int(conf.get("fetch_timeout_minutes", 15)))
                except Exception:
                    # 超时/非零退出：子进程边爬边写 JSONL，盘上已有的数据仍然入库
                    result = sync_jsonl_to_db(allowed_uids=uids)
                    _sync_creator_names()
                    result["creators"] = len(uids)
                    result["warning"] = "爬虫超时或异常退出，仅同步了已写盘的数据"
                    return result
                result = sync_jsonl_to_db(allowed_uids=uids)
                _sync_creator_names()
                result["creators"] = len(uids)
            return result
        finally:
            _state["fetching"] = False
            _state["last_run"] = datetime.now().isoformat(timespec="seconds")


def _browser_data_ready() -> bool:
    """browser_data 里已有 wb 登录态才允许 headless，否则弹出浏览器扫码。"""
    bd = MC_DIR / "browser_data" / "wb_user_data_dir"
    return bd.exists() and any(bd.iterdir())


def login_wb() -> dict:
    """弹出浏览器让用户扫码登录微博（headless=false）。阻塞直到完成。"""
    conf = load_wb_conf()
    with CRAWL_LOCK:
        _state["fetching"] = True
        try:
            # 用占位 UID 触发完整启动→登录流程；登录态保存在 browser_data/wb_user_data_dir
            run_crawl(["5756404150"], headless=False,
                      timeout_min=int(conf.get("fetch_timeout_minutes", 15)))
            return {"ok": _browser_data_ready()}
        finally:
            _state["fetching"] = False
            _state["last_run"] = datetime.now().isoformat(timespec="seconds")


def get_status() -> dict:
    return {"fetching": _state["fetching"], "last_run": _state["last_run"],
            "last_result": _state["last_result"],
            "browser_data_ready": _browser_data_ready(),
            "mediacrawler_exists": MC_DIR.exists()}


# ---------------- 报告引用 ----------------

def digest_markdown(hours: int = 24) -> str:
    """近 N 小时新微博的 markdown 汇总，供盘前/盘后报告引用。无数据返回空串。"""
    from app import get_conn
    from psycopg2.extras import RealDictCursor
    since = datetime.now() - _hours_delta(hours)
    with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT uid, author_name, text, pub_ts, stats
            FROM sa_wb_posts WHERE pub_ts >= %s ORDER BY pub_ts DESC LIMIT 50
            """,
            (since,),
        )
        rows = cur.fetchall()
    if not rows:
        return ""
    lines = [f"### 微博动态（近 {hours} 小时，{len(rows)} 条）", ""]
    for r in rows:
        ts = r["pub_ts"].strftime("%m-%d %H:%M") if r.get("pub_ts") else ""
        author = r.get("author_name") or f"UID {r.get('uid')}"
        text = re.sub(r"\s+", " ", (r.get("text") or "").strip())[:120]
        st = r.get("stats") or {}
        lines.append(f"- **{author}**（{ts}）{text}")
        lines.append(f"  赞 {st.get('likes', 0)} / 评 {st.get('comments', 0)} / 转 {st.get('forwards', 0)}")
    return "\n".join(lines)


def _hours_delta(hours: int):
    from datetime import timedelta
    return timedelta(hours=hours)
