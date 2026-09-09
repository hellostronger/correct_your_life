"""B站UP主动态监听模块：调 MediaCrawler 抓动态+评论，同步进云库 sa_bili_*。

流程：
    sa_bili_creators 里的 UP 主列表
      → run_crawl() 子进程调 MediaCrawler CLI（uv run main.py --platform bili --type creator）
      → sync_jsonl_to_db() 读 data/bili/jsonl/creator_dynamics_*.jsonl、creator_comments_*.jsonl
      → upsert 进 sa_bili_dynamics / sa_bili_comments（首次插入标记未读）

依赖 MediaCrawler 侧补丁（2026-09-06）：
    - 动态 JSONL 带 creator_uid / aid / bvid / title 字段
    - get_dynamics 后 best-effort 抓每条动态评论
"""

import json
import os
import re
import signal
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
MC_DIR = BASE_DIR.parent / "MediaCrawler"
MC_DATA_DIR = MC_DIR / "data" / "bili" / "jsonl"

# 防止并发拉起爬虫/重复 fetch（app 侧 fetch API 与自动线程共用）
CRAWL_LOCK = threading.Lock()

_state = {"fetching": False, "last_result": None, "last_run": None}


# ---------------- 配置 ----------------

def load_bili_conf() -> dict:
    """从 config.yaml 读 bili 段；缺失用默认值。"""
    defaults = {"enabled": True, "interval_minutes": 30, "fetch_timeout_minutes": 10}
    try:
        text = (BASE_DIR / "config.yaml").read_text(encoding="utf-8")
        import yaml
        data = yaml.safe_load(text) or {}
        conf = dict(data.get("bili") or {})
    except Exception:
        conf = {}
    defaults.update({k: v for k, v in conf.items() if v is not None})
    return defaults


# ---------------- 子进程调 MediaCrawler ----------------

def _uv_cmd() -> str:
    """Windows 上 uv 通常是 uv.exe，直接用 'uv' 走 PATH；
    Docker 里 uv 装在固定路径，可用环境变量覆盖。"""
    return os.environ.get("UV_BIN", "uv")


def run_crawl(creator_ids: list[str], headless: bool, timeout_min: int) -> bool:
    """拉起 MediaCrawler 抓指定 UP 主的全部动态+评论。阻塞直到完成或超时。"""
    if not MC_DIR.exists():
        raise FileNotFoundError(f"MediaCrawler 目录不存在: {MC_DIR}")
    log_path = MC_DIR / "bili_crawl_last.log"
    cmd = [
        _uv_cmd(), "run", "python", "main.py",
        "--platform", "bili",
        "--type", "creator",
        "--creator_id", ",".join(creator_ids),
        "--save_data_option", "jsonl",
        "--headless", "true" if headless else "false",
        "--get_comment", "true",
    ]
    # 输出重定向到文件而不是 PIPE：MediaCrawler 日志量大，PIPE 无人读会被写满，
    # 子进程阻塞在 write 上永远不退出（表现为整轮超时）。文件同时留作排查现场。
    # start_new_session 仅 POSIX 生效：子进程自成进程组，超时可整组杀掉。
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
            # Windows 上 proc.kill() 只杀 uv 外壳，python/chrome 孤儿会残留并锁住
            # browser_data，下一轮抓取会卡死。必须整棵进程树杀掉。
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                # POSIX（Docker/Linux）：os.killpg 一并杀掉 uv→python→chrome 进程组
                # （子进程用 start_new_session 拉起，自成进程组）
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
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
    """读匹配 pattern 的所有 jsonl 文件（按 dynamic_id/comment_id 去重，后者覆盖前者）。"""
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
                key = it.get("dynamic_id") or it.get("comment_id")
                if key:
                    items[str(key)] = it  # 后读到的覆盖先读到的（互动数等取最新）
        except OSError:
            continue
    return list(items.values())


def _to_dt(ts) -> datetime | None:
    """动态/评论的时间戳（秒）转 datetime；空值返回 None。"""
    try:
        ts = int(ts)
        if ts <= 0:
            return None
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def sync_jsonl_to_db(allowed_uids: list[str] | None = None) -> dict:
    """把 JSONL 里的动态与评论 upsert 进云库。返回 {new_dynamics, total_dynamics, new_comments, total_comments}。

    allowed_uids 不为空时只导入这些 UP 主的数据——jsonl 目录里留着历史手动
    爬取的其他 UP 主文件，不过滤会把没监听的人的动态也灌进库。
    """
    from app import get_conn  # 延迟导入避免循环依赖

    dynamics = _read_jsonl_files("creator_dynamics_*.jsonl")
    comments = _read_jsonl_files("creator_comments_*.jsonl")
    if allowed_uids is not None:
        allow = {str(u) for u in allowed_uids}
        dynamics = [d for d in dynamics if str(d.get("creator_uid") or "") in allow]
        # 评论挂在动态上（video_id 字段存 aid 或 dynamic_id），只留已导入动态的评论
        keep = {str(d.get("dynamic_id")) for d in dynamics} | \
               {str(d.get("aid")) for d in dynamics if d.get("aid")}
        comments = [c for c in comments if str(c.get("video_id") or "") in keep]

    stats = {"new_dynamics": 0, "total_dynamics": len(dynamics),
             "new_comments": 0, "total_comments": len(comments)}
    if not dynamics and not comments:
        return stats

    with get_conn() as conn, conn.cursor() as cur:
        for d in dynamics:
            cur.execute(
                """
                INSERT INTO sa_bili_dynamics
                    (dynamic_id, uid, author_name, dtype, title, text, bvid, aid,
                     pub_ts, stats, is_read)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE)
                ON CONFLICT (dynamic_id) DO UPDATE SET
                    stats = EXCLUDED.stats
                """,
                (
                    str(d.get("dynamic_id")),
                    str(d.get("creator_uid") or ""),
                    d.get("user_name") or "",
                    d.get("type") or "",
                    d.get("title") or "",
                    d.get("text") or "",
                    d.get("bvid") or "",
                    str(d.get("aid") or ""),
                    _to_dt(d.get("pub_ts")),
                    json.dumps({
                        "comments": d.get("total_comments") or 0,
                        "forwards": d.get("total_forwards") or 0,
                        "likes": d.get("total_liked") or 0,
                    }, ensure_ascii=False),
                ),
            )
            stats["new_dynamics"] += cur.rowcount
        for c in comments:
            cur.execute(
                """
                INSERT INTO sa_bili_comments
                    (comment_id, dynamic_id, content, author, pub_ts, like_count)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (comment_id) DO UPDATE SET
                    like_count = EXCLUDED.like_count
                """,
                (
                    str(c.get("comment_id")),
                    str(c.get("video_id") or ""),  # MediaCrawler 评论表 video_id 字段存 aid 或 dynamic_id
                    c.get("content") or "",
                    c.get("nickname") or "",
                    _to_dt(c.get("create_time")),
                    int(c.get("like_count") or 0),
                ),
            )
            stats["new_comments"] += cur.rowcount
    return stats


def _sync_creator_names() -> None:
    """把 JSONL 里出现过的 creator_uid → user_name 回填 sa_bili_creators（网页不用手动填名字）。"""
    from app import get_conn
    dynamics = _read_jsonl_files("creator_dynamics_*.jsonl")
    names: dict[str, str] = {}
    for d in dynamics:
        uid, name = str(d.get("creator_uid") or ""), d.get("user_name") or ""
        if uid and name:
            names[uid] = name
    if not names:
        return
    with get_conn() as conn, conn.cursor() as cur:
        for uid, name in names.items():
            cur.execute(
                "UPDATE sa_bili_creators SET name = %s WHERE uid = %s "
                "AND (name IS NULL OR name = '')",
                (name, uid),
            )


# ---------------- 对外主流程 ----------------

def fetch_bili() -> dict:
    """完整流程：读 UP 主列表 → 爬取 → 同步入库。返回结果摘要。"""
    from app import get_conn
    if _state["fetching"]:
        return {"skipped": True, "reason": "已有抓取任务在运行"}
    with CRAWL_LOCK:
        _state["fetching"] = True
        try:
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT uid FROM sa_bili_creators ORDER BY added_at")
                uids = [r[0] for r in cur.fetchall()]
            if not uids:
                result = {"new_dynamics": 0, "total_dynamics": 0,
                          "new_comments": 0, "total_comments": 0, "creators": 0,
                          "message": "未配置 UP 主"}
            else:
                conf = load_bili_conf()
                login_ok = _browser_data_ready()
                try:
                    run_crawl(uids, headless=login_ok,
                              timeout_min=int(conf.get("fetch_timeout_minutes", 10)))
                except Exception:
                    # 超时/非零退出：子进程边爬边写 JSONL，盘上已有的数据仍然入库，
                    # 否则整轮白跑（表现为"监控没生效"——数据在文件里、库里却空的）
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
    """browser_data 里已有 bili 登录态才允许 headless，否则弹出浏览器扫码。"""
    bd = MC_DIR / "browser_data"
    return bd.exists() and any(bd.iterdir())


def login_bili() -> dict:
    """弹出浏览器让用户扫码登录B站（headless=false）。阻塞直到完成。"""
    conf = load_bili_conf()
    with CRAWL_LOCK:
        _state["fetching"] = True
        try:
            # 用一个占位 UID 触发完整启动→登录流程；登录态保存在 browser_data/ 后续复用
            run_crawl(["1"], headless=False,
                      timeout_min=int(conf.get("fetch_timeout_minutes", 10)))
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
    """近 N 小时新动态的 markdown 汇总，供盘前/盘后报告引用。无数据返回空串。"""
    from app import get_conn
    from psycopg2.extras import RealDictCursor
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT uid, author_name, dtype, title, text, bvid, pub_ts, stats, is_read
            FROM sa_bili_dynamics WHERE pub_ts >= %s ORDER BY pub_ts DESC LIMIT 50
            """,
            (since,),
        )
        rows = cur.fetchall()
    if not rows:
        return ""
    lines = [f"### B站动态（近 {hours} 小时，{len(rows)} 条）", ""]
    for r in rows:
        ts = r["pub_ts"].astimezone().strftime("%m-%d %H:%M") if r.get("pub_ts") else ""
        author = r.get("author_name") or f"UID {r.get('uid')}"
        content = (r.get("title") or "").strip()
        body = (r.get("text") or "").strip()
        text = content if content else body
        if content and body:
            text = f"{content}：{body}"
        text = re.sub(r"\s+", " ", text)[:120]
        st = r.get("stats") or {}
        kind = {"DYNAMIC_TYPE_VIDEO": "视频", "DYNAMIC_TYPE_WORD": "文字",
                "DYNAMIC_TYPE_DRAW": "图文"}.get(r.get("dtype") or "", "动态")
        link = f"https://www.bilibili.com/video/{r['bvid']}" if r.get("bvid") else \
               f"https://t.bilibili.com/{r.get('dynamic_id') or ''}"
        lines.append(f"- **{author}**（{kind}，{ts}）{text}")
        lines.append(f"  赞 {st.get('likes', 0)} / 评 {st.get('comments', 0)}")
    return "\n".join(lines)
