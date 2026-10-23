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
import random
import re
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
# 上一次后台登录轮询线程（用户再点登录时先把它停掉，见 _kill_login_poller）
_login_poller: threading.Thread | None = None
# 轮询线程的「后代号」：每次重启登录 +1，旧线程在自己的 finally 里
# 只允许清自己那一代的 _state，不会把新一轮刚置好的状态擦掉
_login_gen = 0

# ---------------- 暂停状态（风控 / 登录失效） ----------------
# 风控和登录失效都会让抓取彻底拿不到内容，但处理动作完全不同：
#   risk  —— 小红书限流/验证码：只能等 + 降频，此时继续抓会把临时限流打成封号
#   login —— mcp 侧登录态掉了（cookies 过期/服务重启）：扫码重登即可恢复
# 所以两类各自记录，UI 分别提示不同的下一步操作。
# _pause_lock 保护这四个字段：抓取线程写、UI 线程读，不能读到半套状态。
_pause_lock = threading.Lock()
_pause = {"active": False, "kind": "", "reason": "", "since": None}

# 命中任一关键词即判定为「小红书风控」而不是普通网络错误
_RISK_PATTERNS = (
    "风控", "风险控制", "操作过于频繁", "访问频繁", "请求过于频繁", "频率",
    "验证码", "滑块", "图形认证", "安全认证", "人机",
    "captcha", "verify code", "verification", "too many requests",
    "rate limit", "ratelimit", "blocked", "forbidden", "429",
)
# 命中任一关键词判定为「登录态失效」
_LOGIN_PATTERNS = (
    "未登录", "登录失效", "登录过期", "登录已", "重新登录", "请登录",
    "not logged in", "not login", "login required", "unauthorized",
    "302", "跳转登录",
)


def _match_any(text: str, patterns: tuple[str, ...]) -> str | None:
    """返回第一个命中的关键词（小写比对），没命中返回 None。"""
    low = (text or "").lower()
    for p in patterns:
        if p.lower() in low:
            return p
    return None


def classify_failure(msg: str) -> str | None:
    """把异常/错误文案分类成 "risk" | "login" | None。

    None = 普通故障（网络抖动、服务端偶发挂死、超时），不该触发暂停 ——
    这类本来就有退避重试兜底，暂停反而让本来能成的抓取白停一轮。
    """
    hit = _match_any(msg, _RISK_PATTERNS)
    if hit:
        return "risk"
    hit = _match_any(msg, _LOGIN_PATTERNS)
    if hit:
        return "login"
    return None

# mcp 服务地址（可用环境变量覆盖）
MCP_BASE_URL = os.environ.get("XHS_MCP_URL", "http://127.0.0.1:18060")
MCP_TIMEOUT = int(os.environ.get("XHS_MCP_TIMEOUT", "180"))  # 单次请求超时（秒）

# 搜索失败重试：服务端偶发挂死（间歇性，没被封），退避重试通常能成
SEARCH_RETRIES = int(os.environ.get("XHS_SEARCH_RETRIES", "2"))
SEARCH_RETRY_COOLDOWN = float(os.environ.get("XHS_SEARCH_RETRY_COOLDOWN", "45"))
# 词条间隔（秒）+ 抖动，降低连续爬取的频率
KEYWORD_PACE_SECONDS = float(os.environ.get("XHS_KEYWORD_PACE", "8"))

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


# ---------------- 搜索词改写（基于近期新闻/公众号热点） ----------------
# 为什么改写
# ----------
# 每轮都用完全相同的「智谱」「万马股份」裸词去搜，在服务端看就是脚本行为：
# 固定词表 + 固定间隔 + 每词一次浏览器会话，风控模型一抓一个准。
# 真人搜同一家公司用的词是随时间变的 —— 今天出财报搜「智谱 业绩」，
# 明天出新闻搜「智谱 GLM-5 上车」。所以每轮从**本地已有的真实材料**
# （sa_news 近 N 天标题 + sa_mp_articles 公众号近 N 天标题）里提炼修饰词，
# 拼成当轮特有的检索词，从根本上打散「同一词反复搜」的特征。
#
# 刻意不调 LLM
# -------------
# 每轮 12 个词 = 12 次 LLM 调用：慢（几十秒）、会失败（网关退化是常态）、
# 还让词表不可复现（出问题没法查那轮到底用了什么词）。规则改写零延迟零失败，
# 且材料本身就是人写的标题，拼出来的词比 LLM 的更贴真人搜索习惯。

# 拼词模板：修饰词放在公司名之后（小红书真实搜索习惯，「智谱 业绩」比
# 「业绩 智谱」自然得多）。{m} 是从新闻标题里抽到的热点词。
_MODIFIER_TEMPLATES = [
    "{kw} {m}",
    "{kw}{m}",
    "{kw} {m} 怎么看",
    "{kw} {m} 值得关注吗",
    "{kw} 最近 {m}",
]

# 素材标题里出现这些词，才允许当修饰词。
# 反过来（把「的」「了」这种虚词也放行）会拼出「智谱 的 怎么看」这种废词。
# ⚠️ 只放**单词**，不放「业绩预告」这种组合：jieba 会把组合切成片段
# （业绩/预告），用组合去匹配永远命中不了。要做组合请拆成单词写进来。
_MODIFIER_HINTS = (
    "业绩", "财报", "预增", "预亏", "扭亏", "年报", "季报", "快报", "预告",
    "上市", "申购", "中签", "发行价", "破发", "新股", "招股",
    "涨停", "跌停", "连板", "龙虎榜", "游资", "复盘", "异动",
    "订单", "中标", "签约", "合作", "扩产", "投产", "产能", "交付",
    "政策", "补贴", "规划", "获批", "批复", "监管", "问询", "处罚",
    "增持", "减持", "回购", "举牌", "激励", "定增", "解禁",
    "重组", "并购", "收购", "出售", "更名", "借壳",
    "关税", "涨价", "降价", "提价", "降本", "毛利", "提价",
    "发布", "亮相", "量产", "下线", "装机", "中标",
    "目标价", "评级", "看好", "看空", "风险", "问鼎", "新高", "新低",
    "算力", "芯片", "机器人", "固态电池", "储能", "光伏", "军工",
    "创新药", "医美", "白酒", "稀土", "锂矿", "人工智能", "大模型",
    "自动驾驶", "低空", "卫星", "核电", "电网", "运营商",
)
_MODIFIER_HINTS_L = tuple(h.lower() for h in _MODIFIER_HINTS)


def _keyword_material(conf: dict | None = None, days: int = 5) -> list[str]:
    """从库里取近 N 天的新闻/公众号标题，作为改写素材。

    失败一律返回空列表：改写是降险优化，不是功能依赖，
    数据库抖动不该让抓取跑不起来。
    """
    try:
        from app import get_conn
        from psycopg2.extras import RealDictCursor
        since = datetime.now(timezone.utc) - timedelta(days=days)
        titles: list[str] = []
        with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            # 新闻标题（sa_news 已有 1w+ 条，素材量最大）
            try:
                cur.execute(
                    "SELECT title FROM sa_news "
                    "WHERE publish_time >= %s AND title IS NOT NULL "
                    "ORDER BY publish_time DESC LIMIT 400",
                    (since,),
                )
                titles.extend(r["title"] for r in cur.fetchall() if r.get("title"))
            except Exception:
                pass  # 表不存在/超时都不致命，缺一类素材而已
            # 公众号标题（口径和 sa_news 不同，能补角度）
            try:
                cur.execute(
                    "SELECT title FROM sa_mp_articles "
                    "WHERE coalesce(published_at, fetched_at) >= %s "
                    "AND title IS NOT NULL "
                    "ORDER BY coalesce(published_at, fetched_at) DESC LIMIT 200",
                    (since,),
                )
                titles.extend(r["title"] for r in cur.fetchall() if r.get("title"))
            except Exception:
                pass
        return titles
    except Exception:
        return []


def _extract_modifiers(titles: list[str], base: str, limit: int = 3) -> list[str]:
    """从标题里抽能和 base 搭配的修饰词。

    做法：标题里含 base → jieba 切词 → 留下命中主题词的切分结果。
    公司名本身切出来的片段不要（那等于没改写）。

    ⚠️ jieba 的两个坑，都踩过：
      1. 组合词切不开：「业绩预告」切出来还是「业绩预告」，而 hints 里只有
         「业绩」→ `h in w` 命中不了。所以改成双向包含：`h in w or w in h`。
      2. 切太碎：「三季度净利润同比扭亏为盈」能切出「三季度」「净利润」
         「同比」「扭亏」这种 2~4 字碎片全是有效修饰词，得靠 limit 收口。
    """
    if not titles:
        return []
    mods: list[str] = []
    base_l = base.lower()
    for t in titles:
        tl = (t or "").lower()
        if base_l and base_l not in tl:
            continue
        try:
            import jieba
            words = [w.strip() for w in jieba.cut(t or "")]
        except Exception:
            words = re.split(r"[\s，。、,.:：;；/|]+", t or "")
        for w in words:
            if not w or w in ("，", "。", "、", " "):
                continue
            if len(w) < 2 or len(w) > 8:
                continue
            if base and (base in w or w in base):
                continue  # 公司名/代码片段，拼上去等于没改写
            if any(h in w or w in h for h in _MODIFIER_HINTS_L):
                if w not in mods:
                    mods.append(w)
            if len(mods) >= limit:
                return mods
    return mods[:limit]


def _global_modifiers(titles: list[str], limit: int = 8) -> list[str]:
    """从任意近期标题抽市场级热点修饰词（不限公司）。

    为什么必须有这一层
    ------------------
    2026-10-19 实测：sa_news 近 30 天 600 条标题里，「智谱」「万马股份」等
    自选股名 **一条都没命中** —— 新闻轮是按股票搜的，但这些票近期没上过新闻。
    只按公司名抽修饰词的话 `_extract_modifiers` 全空，改写直接失效，
    每轮还是同一批裸词，等于白做。
    所以补一层「市场级热点」：涨停/业绩/订单/获批这些全市场通用事件词，
    拼成「智谱 订单」「万马股份 涨停」—— 不一定真发生在该公司身上，
    但真人是会这么搜的（随便加个限定词 narrowing 结果），
    而且每轮词不同，降险的目的达到了。
    """
    if not titles:
        return []
    out: list[str] = []
    for t in titles[:200]:        # 只扫最近的 200 条，够随机、也别太慢
        try:
            import jieba
            words = [w.strip() for w in jieba.cut(t or "")]
        except Exception:
            words = re.split(r"[\s，。、,.:：;；/|]+", t or "")
        for w in words:
            if not w or len(w) < 2 or len(w) > 8:
                continue
            if any(h in w or w in h for h in _MODIFIER_HINTS_L):
                if w not in out:
                    out.append(w)
            if len(out) >= limit:
                return out
    return out


def rewrite_keywords(keywords: list[str], conf: dict | None = None,
                     seed_extra: int = 1) -> list[str]:
    """把基础词改写成带当日热点的变体词表（长度不变，一一对应）。

    seed_extra: 有素材时额外混入变体的比例（0~2）。0=只保留原词，
    1=默认，尽量每个词给 1 个变体，2=更激进（词表变化更大但更接近真人）。

    保证：
      - 返回长度 == len(keywords)（调用方按位置对应，不能少）
      - 原词一定在里面（改写失败/无素材时原样返回），抓不到也不丢标的
      - 不重复（风控视角下同轮重复搜同词反而更可疑）
    """
    if not keywords:
        return []
    if not conf:
        conf = load_xhs_conf()
    # 关闭改写时直接返回原词（配置开关，出事能一键退回老行为）
    if not conf.get("keyword_rewrite", True):
        return list(keywords)

    titles = _keyword_material(conf)
    if not titles:
        return list(keywords)  # 没素材就用裸词，功能优先于降险

    # 全局热点修饰词兜底：公司自己的新闻抽不到词时用它
    global_mods = _global_modifiers(titles)

    out: list[str] = []
    seen: set[str] = set()

    def _push(w: str) -> None:
        w = (w or "").strip()
        if w and w not in seen:
            seen.add(w)
            out.append(w)

    # 先按原顺序保证每个基础词至少有一个代表（原词或变体），再考虑额外变体
    for kw in keywords:
        # 优先用这个公司自己的热点；没有就退回全局热点
        mods = _extract_modifiers(titles, kw, limit=3) or global_mods
        if mods:
            # 每轮从命中的修饰词里随机挑一个模板 + 随机挑一个修饰词，
            # 同样的素材在不同轮次产出不同词表 —— 这是打散规律性的关键
            m = random.choice(mods)
            tpl = random.choice(_MODIFIER_TEMPLATES)
            variant = tpl.format(kw=kw, m=m).strip()
            variant = re.sub(r"\s+", " ", variant)[:40]
            _push(variant)
        # 原词始终在场：变体可能召回率低，原词保底
        if kw not in out:
            _push(kw)

    # 额外变体（激进档）：多加几个纯变体，让词表每轮变化更大也更像真人
    extra_budget = max(0, int(seed_extra) - 1)
    for kw in keywords:
        if len(out) >= len(keywords) + extra_budget * len(keywords):
            break
        mods = _extract_modifiers(titles, kw, limit=3) or global_mods
        if not mods:
            continue
        variant = re.sub(r"\s+", " ",
                         random.choice(_MODIFIER_TEMPLATES).format(
                             kw=kw, m=random.choice(mods))).strip()[:40]
        if variant not in seen:
            _push(variant)

    # 兜底对齐：长度必须等于基础词数，宁可少几个变体也不能丢基础词
    for kw in keywords:
        if kw not in out:
            _push(kw)
    return out[:max(len(keywords), 12)]


# ---------------- MCP 客户端 ----------------

class MCPClient:
    """xiaohongshu-mcp REST API 客户端。"""

    def __init__(self, base_url: str | None = None, timeout: int = MCP_TIMEOUT,
                 search_timeout: int = 60):
        self.base_url = (base_url or MCP_BASE_URL).rstrip("/")
        self.timeout = timeout
        # 搜索单独用短超时：服务端（rod 等页面状态）偶发挂死时 180s 太长，
        # 60s 内不回就早失败，由调用方退避重试（挂死是间歇性的，重试通常能成）。
        self.search_timeout = int(os.environ.get("XHS_SEARCH_TIMEOUT", search_timeout))
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def _req(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.base_url}{path}"
        try:
            per_call = kwargs.pop("timeout", self.timeout)
            r = self.session.request(method, url, timeout=per_call, **kwargs)
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
            # 注意用 per_call 而不是 self.timeout —— 搜索走 60s 短超时，
            # 报成 180s 会误导排查
            raise TimeoutError(f"mcp 请求超时（{per_call}s）")
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
        resp = self._req("POST", "/api/v1/feeds/search", json=payload,
                         timeout=self.search_timeout)
        # mcp 返回 {feeds: [...], has_more: bool, cursor: str}
        return resp.get("feeds", [])

    # --- 详情 + 评论 ---
    def get_feed_detail(self, feed_id: str, xsec_token: str,
                         load_all_comments: bool = True,
                         max_comment_items: int = 50,
                         max_replies_threshold: int = 10,
                         scroll_speed: str = "normal") -> dict:
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
                # ⚠️ 服务端是 Go string 枚举 slow/normal/fast；传数字会 400
                "scroll_speed": scroll_speed,
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
    """单例 client，但 URL 必须跟随本次 conf。

    ⚠️ 旧版「只在第一次创建」有个真 bug：app 启动早期（config 里还没有
    mcp_url 时）第一次用到 client 就会把 127.0.0.1:18060 永久缓存进单例，
    之后每轮抓取（虽每次现读 config）全都连着旧地址 —— 2026-10-19 11:31
    自动抓取 12 词全「连接拒绝」0 条，而 /api/xhs/status 显示的 URL 却是对的
    （它不走单例），两处行为不一致即此因。
    """
    global _mcp_client
    url = str((conf or load_xhs_conf()).get("mcp_url") or MCP_BASE_URL).rstrip("/")
    if _mcp_client is None or _mcp_client.base_url != url:
        _mcp_client = MCPClient(base_url=url)
    return _mcp_client


def reset_mcp_client():
    """切换 mcp_url 时调用。"""
    global _mcp_client
    _mcp_client = None


# ---------------- 暂停状态 ----------------

def is_paused() -> bool:
    """当前是否处于暂停抓取状态。定时循环和手动抓取都先问这个。"""
    with _pause_lock:
        return bool(_pause["active"])


def get_pause() -> dict:
    """暂停详情，UI 用。active=False 时返回空壳而不是 None，前端少一层判空。"""
    with _pause_lock:
        return dict(_pause)


def pause_fetch(kind: str, reason: str) -> None:
    """进入暂停。kind ∈ {"risk", "login"}。

    同 kind 重复触发不刷新 since —— 否则一次持续的风控会因为每轮都命中
    而把"已暂停多久"永远显示成刚开始，看不出到底卡了多久。
    """
    global _pause
    with _pause_lock:
        _pause = {"active": True, "kind": kind, "reason": reason,
                  "since": datetime.now().isoformat(timespec="seconds")}


def _login_state_is_bad(client: MCPClient | None = None) -> bool:
    """问一次 mcp 登录态；明确未登录才算 bad。

    连不上 mcp 时返回 False（那是服务挂了，不是登录问题）——
    不能因为服务端抖动就顺手把抓取停掉。
    """
    try:
        st = (client or get_mcp_client()).check_login_status()
        return not st.get("is_logged_in", False)
    except Exception:
        return False


def resume_fetch(verify_login: bool = True) -> dict:
    """解除暂停。verify_login=True 时先确认登录态真的回来了才放行。

    返回 {ok, error?}：风控的冷却期是小红书单方决定的，如果用户点"解除暂停"
    时其实还在限流，放行只会立刻再停一次；所以默认要验一下登录态。
    注意这只验登录态，不验风控 —— 风控只能在用户确认冷却够了之后由用户放行，
    服务端没有可查的"你现在还被限流吗"接口。
    """
    if verify_login and _login_state_is_bad():
        return {"ok": False, "error": "登录态仍未恢复，请先完成扫码登录再解除暂停"}
    with _pause_lock:
        global _pause
        _pause = {"active": False, "kind": "", "reason": "", "since": None}
    print(f"[xhs_mcp] 暂停已解除（verified={verify_login}）", flush=True)
    return {"ok": True}


def _pause_reason(kind: str) -> str:
    """给 UI 的短理由，说明是谁判定出来的。"""
    if kind == "risk":
        return "疑似触发小红书风控（连续搜索结果为空 / 命中限流关键词）"
    if kind == "login":
        return "mcp 服务登录态失效（需重新扫码）"
    return kind or "unknown"


def _pause_message(p: dict) -> str:
    """给用户看的完整一句话。两类分开写，因为下一步动作完全不同。"""
    kind = p.get("kind")
    if kind == "risk":
        return ("⛔ 已触发小红书风控，已自动暂停后续抓取。"
                "请等待冷却（建议 30 分钟以上）、期间不要手动重试，"
                "恢复后在页面点「解除暂停」再抓。")
    if kind == "login":
        return ("🔑 小红书登录态已失效，已自动暂停后续抓取。"
                "请点「登录小红书」重新扫码，登录成功后会自动恢复抓取。")
    return p.get("reason") or "抓取已暂停"


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
    # ⚠️ MCP v2.5.5 返回 modelType + noteCard 嵌套结构，不是扁平的 note_type/type
    notes = []
    for f in feeds:
        mtype = str(f.get("modelType") or f.get("note_type") or f.get("type") or "")
        card = f.get("noteCard") or {}
        ctype = str(card.get("type") or "")
        # ⚠️ 热搜表等杂项没有 noteCard（实测 modelType=hot_query 之类），
        # 放过去取详情必失败且白占 max_notes 名额
        if not card:
            continue
        if mtype in ("note", "normal", "") or ctype in ("note", "normal", ""):
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


def _detail_inner(detail: dict | None) -> dict:
    """详情响应可能包一层：{feed_id, data:{note, comments}} 或直接 {note, comments}。"""
    d = detail or {}
    inner = d.get("data")
    if isinstance(inner, dict) and ("note" in inner or "comments" in inner):
        return inner
    return d


def _normalize_note(note: dict, detail: dict | None, source_keyword: str) -> dict:
    """把 mcp 返回的原始 note + detail 归一化为入库字典。

    ⚠️ MCP 真实结构（v2.x 实测，与旧版扁平假设不同）：
      - 搜索项：{id, xsecToken, modelType, noteCard:{type, displayTitle,
                 user:{nickname,nickName}, interactInfo:{likedCount,...}}}
      - 详情：{feed_id, data:{note:{noteId,title,desc,type,time,user,interactInfo},
                 comments:{list:[...]}}}（有的版本不包 data 层）
    """
    card = note.get("noteCard") or {}                       # 搜索项卡片（嵌套）
    note_detail = _detail_inner(detail).get("note") or {}   # 详情里的 note（更完整）

    note_id = note.get("note_id") or note.get("id") or note_detail.get("noteId") or ""
    title = (note.get("title") or card.get("displayTitle") or "").strip()
    desc = (note.get("desc") or note.get("note_desc") or "").strip()
    if note_detail:
        title = (note_detail.get("title") or title).strip()
        desc = (note_detail.get("desc") or note_detail.get("note_desc") or desc).strip()
    user_obj = note.get("user") or card.get("user") or note_detail.get("user") or {}
    nick = (user_obj.get("nickname") or user_obj.get("nickName")
            or note.get("nickname") or "").strip()
    pub_ts = _to_dt(note.get("time") or note.get("publish_time")
                    or note_detail.get("time"))
    note_url = note.get("note_url") or note.get("link") or (
        f"https://www.xiaohongshu.com/explore/{note_id}" if note_id else ""
    )
    # 互动统计（计数是 camelCase 字符串，如 likedCount="3"）
    interact = (note.get("interact_info") or note.get("stats")
                or card.get("interactInfo") or {})
    if note_detail:
        interact = (note_detail.get("interact_info")
                    or note_detail.get("interactInfo") or interact)
    stats_json = json.dumps({
        "liked": _safe_int(interact.get("liked_count") or interact.get("like_count")
                           or interact.get("likedCount")),
        "collected": _safe_int(interact.get("collected_count") or interact.get("collect_count")
                               or interact.get("collectedCount")),
        "comments": _safe_int(interact.get("comment_count") or interact.get("commentCount")),
        "shares": _safe_int(interact.get("share_count") or interact.get("sharedCount")),
    }, ensure_ascii=False)
    # 标签
    tags = note.get("tag_list") or note.get("tags") or []
    if isinstance(tags, list):
        tag_str = ",".join(str(t).strip() for t in tags if t)
    else:
        tag_str = str(tags)

    return {
        "note_id": note_id,
        "ntype": str(note.get("modelType") or note.get("note_type") or note.get("type")
                     or card.get("type") or note_detail.get("type") or "note")[:16],
        "title": title,
        "note_desc": desc,
        "nickname": nick[:128],
        "pub_ts": pub_ts,
        "stats": stats_json,
        "note_url": note_url,
        "source_keyword": source_keyword,
        "tag_list": tag_str,
    }


def _normalize_comments(detail: dict, note_id: str) -> list[dict]:
    """把 mcp 返回的评论归一化（二级回复也展平入库，带 parent_comment_id）。

    ⚠️ MCP 真实结构：detail["data"]["comments"]（或顶层 comments）= {list:[{
       id, content, likeCount, createTime(ms), userInfo:{nickname},
       subComments:[...]}], cursor, hasMore}。兼容直接给 list 的旧形态。
    """
    raw = _detail_inner(detail).get("comments") or []
    if isinstance(raw, dict):
        raw = raw.get("list") or []
    out: list[dict] = []

    def _take(c: dict, parent_id: str = "") -> None:
        cid = c.get("comment_id") or c.get("id") or ""
        if not cid:
            return
        user_obj = c.get("userInfo") or c.get("user") or {}
        out.append({
            "comment_id": str(cid),
            "note_id": note_id,
            "content": (c.get("content") or "").strip(),
            "author": (user_obj.get("nickname") or user_obj.get("nickName")
                       or c.get("author") or "").strip()[:128],
            "pub_ts": _to_dt(c.get("createTime") or c.get("create_time") or c.get("time")),
            "like_count": _safe_int(c.get("likeCount") or c.get("like_count")
                                    or c.get("liked_count")),
            "parent_comment_id": str(c.get("parent_comment_id")
                                     or c.get("root_comment_id") or parent_id),
        })
        for sub in (c.get("subComments") or []):
            if isinstance(sub, dict):
                _take(sub, str(cid))

    for c in raw:
        if isinstance(c, dict):
            _take(c)
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
    paused = get_pause()
    if paused["active"]:
        # 暂停期间不发起任何浏览器操作：风控时继续打会把临时限流夯成封号，
        # 登录失效时也只会拿到空结果白跑 12 个词。
        return {"skipped": True, "reason": "paused", "paused": paused,
                "message": _pause_message(paused)}
    # 登录时允许并发抓取（用 LOGIN_LOCK 保护，不碰 CRAWL_LOCK）
    with CRAWL_LOCK:
        _state["fetching"] = True
        try:
            conf = load_xhs_conf()
            base_keywords = resolve_keywords(conf)
            if not base_keywords:
                result = {
                    "new_notes": 0, "total_notes": 0,
                    "new_comments": 0, "total_comments": 0,
                    "keywords": [], "notes_for_sa_news": 0,
                    "message": "无关键词（config.xhs.keywords 为空且未开启/未配置自选股）",
                }
                return result

            # 每轮基于近期新闻/公众号热点改写词表：固定裸词 + 固定间隔是脚本特征，
            # 改写后每轮词表都不同，从根上打散「同一词反复搜」。改写失败自动退回裸词。
            keywords = rewrite_keywords(base_keywords, conf)
            print(f"[xhs_mcp] 本轮检索词（改写后 {len(keywords)} 个）：{'、'.join(keywords)}", flush=True)

            client = get_mcp_client(conf)
            max_notes = int(conf.get("max_notes_per_keyword", 40))
            max_comments = int(conf.get("max_comments_per_note", 50))
            load_all = bool(conf.get("load_all_comments", True))

            all_notes: list[dict] = []
            all_comments: list[dict] = []
            # 判定用：连续多少词颗粒无收才怀疑出事（第 1 个词空可能就是没内容）
            empty_streak = 0
            blocked_kind: str | None = None

            for idx, kw in enumerate(keywords):
                try:
                    # 搜索挂死是服务端偶发的，退避重试通常能成；全部失败才跳过该词
                    notes = None
                    last_err: Exception | None = None
                    for attempt in range(1, SEARCH_RETRIES + 1):
                        if attempt > 1:
                            print(f"[xhs_mcp] 关键词 {kw} 第 {attempt}/{SEARCH_RETRIES} 次重试（等待 {SEARCH_RETRY_COOLDOWN:.0f}s）", flush=True)
                            time.sleep(SEARCH_RETRY_COOLDOWN)
                        try:
                            notes = fetch_notes_for_keyword(client, kw, max_notes)
                            break
                        except Exception as e:
                            last_err = e
                            print(f"[xhs_mcp] 关键词 {kw} 第 {attempt}/{SEARCH_RETRIES} 次搜索失败: {e}", flush=True)
                    if notes is None:
                        # 重试全灭：先看报错文案像不像风控/登录失效，纯网络故障不暂停
                        kind = classify_failure(str(last_err))
                        if kind:
                            blocked_kind = blocked_kind or kind
                            print(f"[xhs_mcp] 关键词 {kw} 失败命中 {kind} 判定，本轮不再继续抓", flush=True)
                            break
                        # 顺手确认一下不是登录态悄悄掉了（文案里没写的那种）
                        if _login_state_is_bad(client):
                            blocked_kind = "login"
                            print("[xhs_mcp] 搜索失败且登录态已失效，本轮停止", flush=True)
                            break
                        print(f"[xhs_mcp] 关键词 {kw} 搜索重试全部失败，跳过: {last_err}", flush=True)
                        continue

                    if not notes:
                        empty_streak += 1
                        print(f"[xhs_mcp] 关键词 {kw} 无结果（连续 {empty_streak} 词为空）", flush=True)
                        if empty_streak >= 2:
                            # 两个词都 0 条就不再试剩下的了。此时真相只有两种：
                            # 登录态掉了（空结果 + 未登录），或风控。问一次服务端即可分流。
                            if _login_state_is_bad(client):
                                blocked_kind = "login"
                            else:
                                blocked_kind = "risk"
                            print(f"[xhs_mcp] 连续空结果 → 判定为 {blocked_kind}，本轮停止", flush=True)
                            break
                    else:
                        empty_streak = 0
                        for note in notes:
                            detail = fetch_note_detail(client, note, load_all, max_comments)
                            norm_note = _normalize_note(note, detail, kw)
                            all_notes.append(norm_note)
                            if detail:
                                all_comments.extend(_normalize_comments(detail, norm_note["note_id"]))
                except Exception as e:
                    kind = classify_failure(str(e))
                    if kind:
                        blocked_kind = kind
                        print(f"[xhs_mcp] 关键词 {kw} 抓取异常命中 {kind} 判定，本轮不再继续抓: {e}", flush=True)
                        break
                    print(f"[xhs_mcp] 关键词 {kw} 抓取失败: {e}", flush=True)
                    continue
                finally:
                    # 词条间隔 + 抖动：连续高频搜索容易触发风控（每词一次浏览器会话）
                    if idx < len(keywords) - 1:
                        time.sleep(KEYWORD_PACE_SECONDS + random.uniform(0, 4))

            # 判定成立 → 暂停后续所有抓取（定时 + 手动都进不去），直到用户处理
            if blocked_kind:
                reason = (_pause_reason(blocked_kind))
                pause_fetch(blocked_kind, reason)
                result = {
                    "new_notes": len(all_notes), "total_notes": len(all_notes),
                    "new_comments": 0, "total_comments": 0,
                    "keywords": keywords, "notes_for_sa_news": 0,
                    "blocked": True, "blocked_kind": blocked_kind,
                    "aborted_at_keyword": kw,
                    "message": _pause_message(get_pause()),
                }
                # 本轮已经抓到的还是要入库，不能因为后半场出事就把前半场丢掉
                if all_notes or all_comments:
                    try:
                        saved = sync_notes_to_db(all_notes, all_comments, allowed_keywords=keywords)
                        result.update({k: v for k, v in saved.items() if k != "keywords"})
                    except Exception as e:
                        print(f"[xhs_mcp] 暂停前入库失败: {e}", flush=True)
                return result

            result = sync_notes_to_db(all_notes, all_comments, allowed_keywords=keywords)
            result["keywords"] = keywords
            return result
        finally:
            _state["fetching"] = False
            _state["last_run"] = datetime.now().isoformat(timespec="seconds")


def login_xhs() -> dict:
    """触发生成登录二维码（阻塞等待扫码完成）。

    注意：mcp 服务内部会启动浏览器等待扫码，完成后自动保存 cookies。
    此处轮询登录状态直到成功或超时。前端现在走非阻塞的 request_login_qr，
    本函数保留给脚本/调试用，同样遵守「锁被占就回收、不拒绝」原则。
    """
    _kill_login_poller()
    if not LOGIN_LOCK.acquire(timeout=10):
        return {"ok": False, "error": "登录锁被占用超过 10 秒，请重试"}
    try:
        _state["fetching"] = True  # 兼容 get_status 返回
        client = get_mcp_client()
        # 触发二维码生成
        try:
            qr_resp = client.get_login_qrcode()
            img = qr_resp.get("img") or ""
            print(f"[xhs_mcp] 登录二维码已生成，请扫码（img 长度 {len(img)}）", flush=True)
            if not img:
                # 没图就别干等 12 分钟了：要么已登录，要么服务端没给全
                if qr_resp.get("is_logged_in"):
                    return {"ok": True, "already_logged_in": True}
                return {"ok": False, "error": f"mcp 未返回二维码图片：{qr_resp}"}
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
                    _state["login_ok"] = True
                    if get_pause()["kind"] == "login":
                        resume_fetch(verify_login=False)
                    return {"ok": True}
            except Exception:
                pass
        return {"ok": False, "error": "扫码超时，请重试"}
    finally:
        _state["fetching"] = False
        LOGIN_LOCK.release()
        _state["last_run"] = datetime.now().isoformat(timespec="seconds")


def _kill_login_poller(timeout: float = 3.0) -> None:
    """停掉上一次残留的后台登录轮询（如果有），清掉它留下的状态。

    为什么需要
    ----------
    二维码是登录态失效时**唯一**的恢复入口。旧实现里它被两道锁看着：
      - `login_waiting` 残留 True（上一次扫码超时、或 app 重启前的假状态）
      - LOGIN_LOCK 被上一次的轮询线程占着没释放
    任一出现，用户再点「登录小红书」就只会得到「已有任务在运行」，
    而真实情况是根本没人等他扫码 —— 2026-10-19 实测卡死在这里。

    这里做的是**回收**而不是**拒绝**：把旧轮询停掉、把锁强制拿回来。
    代价是上一次轮询可能少看一两眼登录态，但它已经超时/失效了，无所谓。
    """
    global _login_poller, _login_gen
    _login_gen += 1                      # 旧线程在 finally 里发现世代不符就闭嘴
    old = _login_poller
    if old and old.is_alive():
        print("[xhs_mcp] 停掉上一次残留的登录轮询线程", flush=True)
        old.join(timeout=timeout)        # 最多等几秒，别把接口卡住
        if old.is_alive():
            # JOIN_TIMEOUT 了：轮询在 sleep(3) 里，最多再等 3s 也会退出。
            # 这里不暴力夺取 LOGIN_LOCK（Python 的锁没有 owner，强夺会双泄漏），
            # 直接进入下一步 —— 下面拿不到锁时走 acquire(timeout=...) 让它自然落地。
            print("[xhs_mcp] 旧轮询线程未在超时内退出，继续尝试拿锁", flush=True)
    _state["login_waiting"] = False
    _login_poller = None


def request_login_qr() -> dict:
    """非阻塞登录：取二维码立即返回，后台线程轮询登录态。

    返回 {ok, img: 'data:image/png;base64,...'} 或 {ok: False, error}。
    如果已经登录，直接返回 {ok: True, already_logged_in: True}。
    轮询期间 _state["login_waiting"]=True，由 /api/xhs/login-state 查询。

    ⚠️ 设计原则：**二维码必须无条件尝试生成**。登录态失效时这是唯一出口，
    所以不设任何「已有任务在运行」的前置拒绝 —— 那等于把恢复通道焊死。
    旧的并发轮询在这里回收，不是拒绝。
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
        pass  # 连不上 mcp 时继续尝试获取二维码 —— 也许只是这一下慢

    # 回收上一次残留轮询，保证下面能拿到锁（不因「已有任务」拒绝本次请求）
    _kill_login_poller()
    got_lock = LOGIN_LOCK.acquire(timeout=10)     # 旧线程最多再占几秒，等得起
    if not got_lock:
        # 极端情况：旧轮询卡死。锁没拿到也不能就此失败 ——
        # 二维码还是先生成出来给用户看，轮询由新的 try 兜底（拿不到锁时跳过轮询）。
        print("[xhs_mcp] ⚠️ LOGIN_LOCK 获取超时，仍先生成二维码", flush=True)
    client = get_mcp_client()
    try:
        qr = client.get_login_qrcode()
    except Exception as e:
        if got_lock:
            LOGIN_LOCK.release()
        return {"ok": False, "error": f"无法连接 mcp 服务生成二维码：{e}"}
    img = qr.get("img") or ""
    # mcp 返回 is_logged_in=True + timeout=0s 表示已经登录，此时不返回二维码
    if qr.get("is_logged_in") and not img:
        if got_lock:
            LOGIN_LOCK.release()
        _state["login_ok"] = True
        _state["login_waiting"] = False
        return {"ok": True, "already_logged_in": True}
    if not img:
        if got_lock:
            LOGIN_LOCK.release()
        return {"ok": False, "error": f"mcp 未返回二维码图片：{qr}"}
    _state["login_ok"] = False
    _state["login_waiting"] = True

    my_gen = _login_gen

    def _wait():
        try:
            conf = load_xhs_conf()
            timeout_min = int(conf.get("fetch_timeout_minutes", 12))
            deadline = time.time() + timeout_min * 60
            while time.time() < deadline:
                # 用户又点了一次登录 → 新一代接管，本线程立刻闭嘴退出，
                # 绝不清/改新一轮的状态（不然会把新的 login_waiting 擦掉）
                if my_gen != _login_gen:
                    print("[xhs_mcp] 登录轮询已被新一轮取代，退出", flush=True)
                    return
                time.sleep(3)
                try:
                    if client.check_login_status().get("is_logged_in"):
                        print("[xhs_mcp] 登录成功", flush=True)
                        _state["login_ok"] = True
                        # 登录态回来了 = "login" 类暂停自动解除。风控类不动：
                        # 风控不等同于掉登录，用户得自己确认冷却够了再放行。
                        if get_pause()["kind"] == "login":
                            resume_fetch(verify_login=False)
                        return
                except Exception:
                    pass
            print("[xhs_mcp] 扫码超时", flush=True)
        finally:
            # 只清自己这一代的状态：被顶替时 my_gen != _login_gen，
            # 新线程刚置的 login_waiting=True 不能被这里擦掉
            if my_gen == _login_gen:
                _state["login_waiting"] = False
                _state["last_run"] = datetime.now().isoformat(timespec="seconds")
            if got_lock:
                LOGIN_LOCK.release()
            global _login_poller
            if _login_poller is threading.current_thread():
                _login_poller = None

    t = threading.Thread(target=_wait, daemon=True)
    _login_poller = t
    t.start()
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
    # 健康检查用短超时避免卡死；但 mcp 在远端（如 101）时跨机 RTT 中位已 ~2s，
    # 2 秒会让健康检查必然超时（服务明明是好的却报 mcp_service_ok=false）
    try:
        # 登录状态检查要开浏览器（实测空闲 2~6s、忙时 >8s）；8s 会把本来就好的
        # 检查变成超时暂退 → 服务端把断开转成 panic 500（12:01:33 实录）。20s 稳妥。
        client = MCPClient(base_url=conf.get("mcp_url") or MCP_BASE_URL, timeout=20)
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
        # 暂停状态：paused/pause_kind/pause_reason/pause_since 摊平，
        # 前端不用再解一层嵌套；message 已经是给用户的完整话术
        "paused": get_pause()["active"],
        "pause_kind": get_pause()["kind"],
        "pause_reason": get_pause()["reason"],
        "pause_since": get_pause()["since"],
        "pause_message": _pause_message(get_pause()),
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