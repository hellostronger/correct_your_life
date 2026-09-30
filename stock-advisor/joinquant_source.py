# -*- coding: utf-8 -*-
"""聚宽社区策略采集：列表 -> 详情 -> 源码/评论区 -> 落库（防重复爬取）。

接口与坑（全部 2026-09-30 实测，不是照文档猜的）
--------------------------------------------------
- **「通用参数」= cookie 里的 token**。之前匿名访问 /api/* 一律返回
  `{"code":4,"msg":"缺失必要的通用参数"}`，试过 token/source/platform/cookie
  等 20 多种组合都试不出来 —— 因为它就是浏览器 cookie 里的 token。
  真实端点也不是 /api/ 前缀，而是 /community/ 开头：
      列表  GET /community/post/listV2?limit=20&page=1&cate=3&type=isNew
      详情  GET /community/post/detailV2?postId=<id>
      评论  GET /community/post/replyList?postId=<id>&limit=50&page=1
  （探测过 /api/community/list、/community/post/detail、/replies、/getReplies、
    /commentList、/listReply —— 全部不存在或报「报表配置不存在」，只有上面三个真）

- **列表的 content 是截断摘要**（实测 123~7576 字符，5 篇里 0 篇带代码块），
  **完整正文和代码只在 detailV2**。所以必须逐篇拉详情，不能只看列表。

- **数值字段是字符串**：`backtestCloneCount: '201'`、`type: '1'`。
  排序/比较时必须 int() 兜底，否则 `-(str)` 直接 TypeError。

- **详情里的源码字段**：`notebookPath`/`fileKey`/`backtestId` 在社区帖
  通常是空串（那是「研究笔记/克隆」的付费/关联内容）。**真源码在正文的
  ``` 代码块里**，实测 60 篇里 5 篇有（最多 9 块）。评论区的 `backtestId`
  也指向别的账号的策略，拿不到代码。

- 代码块内容**不都是源码**：有的是回测日志（带时间戳、订单流水），
  有的是 SQL/CSS/HTML 片段。靠 `is_code_block()` 判定：Python 关键字
  （def / import / set_order_cost / g.*）或含 `initialize` 才算策略代码。

**postId 是个假 id（这条坑最隐蔽，也最费时间）**
------------------------------------------
聚宽的 `listV2` 和 `detailV2` 返回的 **`postId` 每次请求都不一样** ——
同一篇「聚宽新手指南」，连续两次列表拿到
`f8e26b567ae09e1aba710a2b797593d9` 和 `dc2dd1017ec456c35cb488936e35f846`，
交集为 0；`userId`、`lastReplyId` 同样每次变（应该是服务端每次重新签发）。
**只有 `uniqueKey` 稳定**（32 位 hex，列表和详情一致、跨请求不变）。

所以：
1. **一切主键/去重都用 `uniqueKey`**，用 postId 算 hash 等于没有防重 ——
   我第一版就是这么写的，实测同一页连抓两次 new=22/dup=0，44 行全是重复。
2. 幸运的是 `detailV2?postId=<uniqueKey>` **能直接查详情**（实测有效），
   所以不必依赖列表给的临时 postId。
3. 旧 postId 不会失效（实测隔 20 秒、夹别的请求都还能用），但没理由依赖它。

防重复爬取
----------
靠 `sa_crawl_queue.url_hash` **唯一键**，不是「先查再插」—— 那是竞态，
两个进程同时插都会成功。命中 ON CONFLICT 即视为已见过，不再发详情请求。
另外详情响应里没有 ETag/Last-Modified 可用，改用 `content_hash`（正文 sha1）
判断「内容是否变了」：没变就不重新抽取，省一次 LLM 调用。
"""
import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime

BASE = "https://www.joinquant.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
LIST_PATH = "/community/post/listV2"
DETAIL_PATH = "/community/post/detailV2"
REPLY_PATH = "/community/post/replyList"

# Python/聚宽策略代码的判别特征
PY_HINTS = ("def ", "import ", "from ", "initialize(", "set_order_cost(",
             "handle_data(", "before_trading_start", "after_code_changed",
             "g.", "order_target", "context.portfolio", "attribute(",
             "run_daily", "run_weekly", "run_monthly", "history(")
NON_CODE_LANGS = {"sql", "css", "html", "json", "bash", "sh", "text", "yaml"}


def _i(v, default=0):
    """聚宽的数值字段是字符串（'201'），排序/比较前必须转 int。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


class JqError(Exception):
    pass


class Client:
    """聚宽社区客户端。token 从 deps 注入，不写死在代码里。"""

    def __init__(self, token: str, timeout: int = 25, retries: int = 3,
                 min_interval: float = 0.6):
        self.token = (token or "").strip()
        self.timeout = timeout
        self.retries = retries
        self.min_interval = min_interval
        self._last = 0.0

    def _throttle(self):
        """限速。社区站没有公告的频率上限，但别当它没有 —— 打太快会被限。"""
        dt = time.time() - self._last
        if dt < self.min_interval:
            time.sleep(self.min_interval - dt)
        self._last = time.time()

    def get(self, path: str, params: dict | None = None) -> dict:
        if not self.token:
            raise JqError("没有 token：先在网页登录聚宽，然后用 kimi-webbridge "
                          "抓 cookie 里的 token 填到 config.yaml 的 jq.token")
        url = BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        last = None
        for attempt in range(self.retries):
            self._throttle()
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": UA,
                    "Referer": BASE + "/view/community/list?listType=1",
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json, text/plain, */*",
                    "Cookie": "token=%s" % self.token,
                })
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    d = json.loads(r.read().decode("utf-8", "replace"))
                # 返回体是 {status, code, msg, data}；code 非 00000 即失败
                code = str(d.get("code") or "")
                if code not in ("00000", "0", "200"):
                    raise JqError("%s: %s" % (d.get("msg") or code, path))
                return d
            except JqError:
                raise
            except Exception as exc:      # noqa: BLE001
                last = exc
                time.sleep(1.0 * (attempt + 1))
        raise JqError("请求失败 %s: %s" % (path, str(last)[:120]))

    # ---- 业务 ----

    def list_posts(self, page: int = 1, limit: int = 20, cate: int = 3,
                   type_: str = "isNew") -> dict:
        d = self.get(LIST_PATH, {"limit": limit, "page": page,
                                 "cate": cate, "type": type_})
        data = d.get("data") or {}
        return {"total": _i(data.get("totalCount")),
                "page": page,
                "posts": [_norm_post(x) for x in (data.get("list") or [])]}

    def detail(self, post_id: str) -> dict:
        d = self.get(DETAIL_PATH, {"postId": post_id})
        return _norm_post(d.get("data") or {})

    def replies(self, post_id: str, page: int = 1, limit: int = 50) -> list[dict]:
        d = self.get(REPLY_PATH, {"postId": post_id, "limit": limit,
                                  "page": page})
        return [_norm_reply(x) for x in
                ((d.get("data") or {}).get("replyArr") or [])]


# ---------------- 归一化 ----------------

def _norm_post(x: dict) -> dict:
    """把站点 JSON 归一成我们内部用的形状。数值字段一律 int()。

    **post_id 一律用 uniqueKey**，不用站点返回的 postId —— 后者每次请求
    都会变（详见模块 docstring）。listV2 用 `user` 对象带作者，detailV2 用
    `author`，两个都要认，否则列表阶段作者全是空。
    """
    au = x.get("author")
    if not isinstance(au, dict):
        au = x.get("user") if isinstance(x.get("user"), dict) else {}
    ukey = str(x.get("uniqueKey") or "").strip()
    pid = str(x.get("postId") or "").strip()
    return {
        # uniqueKey 才是真主键；拿不到就退回 postId（宁可退化也不能空）
        "post_id": ukey or pid,
        "ukey": ukey,
        "ephemeral_id": pid,          # 会变的那个，只留个记录，不做主键
        "title": (x.get("title") or "").strip(),
        "author": (au.get("userName") or au.get("name")
                   or au.get("nickname") or "").strip(),
        "author_id": str(x.get("userId") or au.get("_userId") or "").strip(),
        "content": x.get("content") or "",
        "add_time": x.get("addTime") or "",
        "mod_time": x.get("modTime") or "",
        "last_active": x.get("lastActiveTime") or x.get("lastReplyTime") or "",
        "view_count": _i(x.get("viewCount")),
        "like_count": _i(x.get("likeCount")),
        "reply_count": _i(x.get("replyCount")),
        "collect_count": _i(x.get("collectionCount")),
        "clone_count": _i(x.get("backtestCloneCount")),
        "tags": [t.get("name") for t in (x.get("tagInfo") or [])
                 if isinstance(t, dict) and t.get("name")],
        "notebook_path": x.get("notebookPath") or "",
        "file_key": x.get("fileKey") or "",
        "backtest_id": str(x.get("backtestId") or ""),
    }


def _norm_reply(x: dict) -> dict:
    u = x.get("user") if isinstance(x.get("user"), dict) else {}
    return {
        "reply_id": str(x.get("replyId") or ""),
        "post_id": str(x.get("postId") or ""),
        "author": (u.get("userName") or u.get("name") or "").strip(),
        "content": x.get("content") or "",
        "add_time": x.get("addTime") or "",
        "backtest_id": str(x.get("backtestId") or ""),
        "backtest_name": (x.get("backtestName") or "").strip(),
        "is_owner": _i(x.get("isOwner")),
    }


# ---------------- 源码抽取 ----------------

FENCE_RE = re.compile(r"```([a-zA-Z+#]*)\s*\n(.*?)```", re.S)


def code_blocks(md: str) -> list[dict]:
    """抽出 markdown 里的所有代码块，标出语言与行数。"""
    out = []
    for m in FENCE_RE.finditer(md or ""):
        lang = (m.group(1) or "").strip().lower()
        body = m.group(2)
        out.append({"lang": lang, "code": body,
                    "lines": body.count("\n") + 1,
                    "chars": len(body)})
    return out


def is_code_block(blk: dict) -> bool:
    """判断这个代码块是不是**策略源码**（而不是回测日志 / 纯文字说明）。

    为什么要分三类：实测社区帖的代码块里
      ① 策略代码   —— def / order_* / g.* ……
      ② 参数配置   —— `lgb_params = {'objective': 'regression', ...}`
      ③ 回测日志   —— 带时间戳、订单流水
      ④ 中文说明   —— 「训练集：2018-2022年，155个截面」
    ①②要进源码，③④只进 other_blocks（③没用，④对 LLM 抽「适用场景」
    很有用但不是代码）。

    第一版只认 PY_HINTS（def/import/order_*…），结果把 ② 判成了非代码 ——
    实测《多因子LightGBM选股策略》正文 9 块，**LightGBM 的超参配置块
    （lgb_params / num_leaves / max_depth …）被扔了**，只剩 27 行骨架。
    那是这篇策略最值钱的部分之一。

    所以判据放宽成：作者标了 python 就信；没标就看有没有 Python 赋值/
    字典字面量形态（`xxx = {`）。中文说明块不含 `=` 和 `{`，仍然会被排除。
    """
    if blk["lang"] in NON_CODE_LANGS:
        return False
    body = blk["code"]
    if len(body.strip()) < 20:
        return False
    # 回测日志特征：大量时间戳行且没有任何代码结构
    log_lines = len(re.findall(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s+-",
                               body, re.M))
    has_code_shape = bool(re.search(r"^\s*\w+\s*=\s*[\{\[(]", body, re.M))
    if log_lines >= 3 and not has_code_shape and not any(
            k in body for k in ("def ", "import ")):
        return False
    # 作者标了 python 就当代码（哪怕没有 def，比如纯参数配置块）
    if blk["lang"] == "python":
        return True
    # 没标语言：要么命中聚宽/Python 关键字，要么是赋值/字典字面量形态
    if any(k in body for k in PY_HINTS):
        return True
    return has_code_shape


def extract_source(md: str, replies: list[dict] | None = None) -> dict:
    """从正文 + 评论区里抽出策略源码。

    返回 {has_code, source, blocks, other_blocks}
    - `blocks`：**所有**判定为策略代码的块，按行数降序
    - `source.code`：**所有代码块拼接后的完整源码**（不是一个块！）

    为什么必须收全部块
    ------------------
    第一版我只留「最长的那一个块」，结果实测把最有价值的一篇毁了：
    《多因子LightGBM选股策略》正文有 **9 个代码块、11279 字**，
    而单独的每个块都只有 12 行 —— 因为作者把一个完整策略拆成了
    initialize / 因子计算 / 选股 / 调仓 几段贴。只留一个块 = 只拿到
    12 行骨架，选股逻辑整段丢失。
    聚宽策略本来就是「一个文件」，块之间有前后依赖，拼起来才对。

    正文没有就翻评论区（你提的那点：不少帖子源码只在评论区）。

    **redacted=True 表示作者把核心逻辑用 `...` 省略了**（实测社区里不少
    「复现年化XXX%」的帖子是这样：给了函数签名和变量名，函数体是 `...`）。
    这种情况照样入库 —— 变量名和选股思路本身就有参考价值 —— 但必须打标记，
    否则 LLM 抽取「实现步骤」时会照着空壳脑补出一套不存在的策略。
    """
    blocks: list[dict] = []
    for blk in code_blocks(md or ""):
        if is_code_block(blk):
            blocks.append(dict(blk, origin="body"))
    if replies:
        for rp in replies:
            for blk in code_blocks(rp.get("content") or ""):
                if is_code_block(blk):
                    blocks.append(dict(blk, origin="reply",
                                       author=rp.get("author"),
                                       reply_id=rp.get("reply_id")))
    blocks.sort(key=lambda b: -(b["lines"] * 1000 + b["chars"] // 100))
    other = [b for b in code_blocks(md or "") if not is_code_block(b)]

    if not blocks:
        return {"has_code": False, "source": None, "blocks": [],
                "other_blocks": other}

    for i, b in enumerate(blocks, 1):
        b["idx"] = i
        b["lines"] = b["code"].count("\n") + 1
        n, why = stub_score(b["code"])
        b["redacted"] = n > 0
        b["stub_sites"] = n
        b["stub_reasons"] = why

    # 拼成一份完整源码。用显式分隔标记而不是空行，这样回填/二次抽取时
    # 还能看出块边界；也方便将来把每个块单独喂给 LLM。
    merged = []
    for b in blocks:
        head = "# ===== 代码块 %d/%d（%s，%d 行%s）%s" % (
            b["idx"], len(blocks), b.get("origin", "body"), b["lines"],
            "，作者省略了 %d 处" % b["stub_sites"] if b["redacted"] else "",
            ("by " + b["author"]) if b.get("author") else "")
        merged.append(head + "\n" + b["code"].rstrip())
    merged_text = "\n\n".join(merged) + "\n"

    # 整体脱敏标记：任一块被省略就算（因为拼接后是一份源码，缺哪块都跑不了）
    redacted = any(b["redacted"] for b in blocks)
    why_all: list[str] = []
    for b in blocks:
        for w in b["stub_reasons"]:
            why_all.append("块%d: %s" % (b["idx"], w))
    return {"has_code": True,
            "source": {"lang": blocks[0].get("lang", ""),
                       "code": merged_text,
                       "lines": merged_text.count("\n") + 1,
                       "origin": blocks[0].get("origin", "body"),
                       "author": blocks[0].get("author", ""),
                       "reply_id": blocks[0].get("reply_id", ""),
                       "redacted": redacted,
                       "stub_sites": sum(b["stub_sites"] for b in blocks),
                       "stub_reasons": why_all[:8],
                       "n_blocks": len(blocks),
                       "raw_lines": sum(b["lines"] for b in blocks)},
            "blocks": blocks, "other_blocks": other}


def stub_score(body: str) -> tuple[int, list[str]]:
    """检测代码是不是**脱敏/省略版**（社区里很常见，作者防抄袭）。

    实测案例《实测复现年化526%的低吸连阳首板策略》整个代码块长这样：
        def before_open(context):
            ...
            g.today_pick = g.target_list.copy()
    `...` 是 Python 的 Ellipsis，语法上合法（当表达式语句），所以**编译得过**，
    但函数体是空的 —— 作者把真正的选股逻辑藏了。如果不识别，LLM 抽取
    「实现步骤」时就会照着空壳脑补出一套根本不存在的策略。

    返回 (省略处数量, 命中原因列表)。>=1 就当脱敏版。
    """
    reasons: list[str] = []
    n = 0
    # 1) 函数体只有一个 ... / pass（省略号的典型形态）
    for m in re.finditer(r"^([ \t]*)def\s+\w+\s*\([^)]*\)\s*(?:->[^:]+)?:\s*\n"
                         r"((?:[ \t]*\n)*)[ \t]*(\.\.\.|pass)\s*(?:#.*)?$",
                         body, re.M):
        n += 1
        reasons.append("函数 %s 的函数体是 %s" % (m.group(1).strip() or "def",
                                              m.group(3)))
    # 2) 独立成行的 ... （连续的省略块）
    n2 = len(re.findall(r"^[ \t]*\.\.\.\s*(?:#.*)?$", body, re.M))
    if n2:
        n += n2
        reasons.append("%d 处独立省略号" % n2)
    # 3) 注释里明说省略了
    for pat in (r"#\s*(其余|后续|中间|剩下).*(省略|略|略去|同上)",
                r"#\s*(省略|略去|不展示|已删除|自行补全|自己实现)",
                r"\b(todo|your code here|fill in|待补充|自行实现)\b"):
        for m in re.finditer(pat, body, re.I):
            n += 1
            reasons.append("注释声明省略: %s" % m.group(0).strip()[:40])
    return n, reasons[:6]


def md_to_text(md: str, limit: int = 20000) -> str:
    """markdown -> 纯文本，专供 LLM 抽取。

    刻意**保留代码块内容**（去掉 ``` 围栏）：策略的选股/择时逻辑经常就写在
    注释和代码里，剥掉等于丢掉最关键的信息。但去掉图片和裸 URL ——
    `![](https://...)` 一条能占几百字符，纯浪费。
    """
    if not md:
        return ""
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", md)          # 图片
    t = re.sub(r"\[([^\]]*)\]\((https?://[^)]*)\)", r"\1", t)  # 链接留文字
    t = re.sub(r"```[a-zA-Z+#]*\n", "\n【代码】\n", t)   # 代码块去围栏留内容
    t = re.sub(r"```", " ", t)
    t = re.sub(r"https?://\S+", " ", t)                     # 裸 URL
    t = re.sub(r"<[^>]+>", " ", t)                          # HTML 标签
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()[:limit]


def url_hash(url: str) -> str:
    return hashlib.sha1((url or "").encode("utf-8")).hexdigest()


def content_hash(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()


def post_url(post_id: str) -> str:
    """社区详情页。用 uniqueKey 拼 —— 实测这个 id 是稳定的，用站点那个
    会变的 postId 拼出来的 URL 下次打开就指不到同一篇了。"""
    return "%s/view/community/detail?id=%s" % (BASE, post_id)
