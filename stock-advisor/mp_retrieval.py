"""公众号语义检索（Milvus 2.6 + bge-m3 + 服务端 BM25 混合检索）。

## 为什么要有它（实测依据，不是设想）

2026-10-13 实测近 7 天 572 篇公众号文章：

| 指标 | 值 |
|---|---|
| 正文字数 中位数 / p90 / 最大 | **630 / 2729 / 20702** |
| 超过 8000 字的 | 7 篇（1%） |
| **当前喂给 LLM 的** | 社媒块截断到 **160 字**；新闻块**只有标题** |
| 没关联到任何股票的文章 | **478 篇（84%）**——大盘/行业观点，现在等于不存在 |

所以「文章太长塞不下」是伪命题（中位数 630 字 ≈ 440 token），真问题是
**读得太少 + 挑不出该读哪几篇**。本模块做两件事：

1. **索引**：正文按段切块 → `bge-m3` 向量（网关 `/v1/embeddings`，1024 维）
   + Milvus **服务端 BM25** 稀疏向量 → 一套 collection 同时支持语义与全文检索。
2. **检索**：向量 top-K 与 BM25 top-K 用 RRF 融合，可选 rerank；
   返回**原文片段**（不是摘要）——摘要有损且会幻觉，原文片段可追溯。

## 为什么不用 agentic loop 走决策热路径

决策时的 query 是**已知且固定**的（「与这只票相关的近期观点/事实」）。
agentic 的价值在问题开放时才有，而且它有三个实打实的坏处：

- **不可复现**：同一份数据两次决策可能检索到不同片段，而决策上下文必须可复现；
- **慢**：43 只票 × 多轮检索会拖慢整轮决策；
- **抢网关**：embedding/生成模型和交易决策共用 `101.43.25.101:3000`，
  而那上面 12 个 embedding 模型里**已有 3 个 404**（NIM 部署被清理）——
  给非关键路径加压不划算。

所以：**决策注入走 `retrieve()`（确定性、单次调用）；开放问题走
`agentic_search()`**（多轮「检索 → 读 → 再检索」，带工具集，返回带引用的答案）。

## 依赖与配置

- `pymilvus 2.6.2` **本机已装**、Milvus **2.6.22 在 101**、embedding 走**已有网关**
  → **不新增任何依赖**。
- 已在网关实测可用：`bge-m3`（1024 维，中文最强，8k 长文本）、
  `Qwen3-Embedding-0.6B`（0.6s，最快）；**已死**（404）：
  `nvidia/nv-embedqa-mistral-7b-v2` / `nvidia/embed-qa-4` / `snowflake/arctic-embed-l`。
- 配置项在 `config.yaml` 的 `mp_retrieval` 段（默认全部关闭，见 `DEFAULTS`）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULTS = {
    # 默认**开**（2026-10-13 用户明确要求：公众号内容要进决策）。
    # 之前默认关的理由是「改决策口径要先问」，现已获授权；页面「公众号」标签有开关。
    "enabled": True,
    "milvus_uri": "http://101.43.25.101:19530",
    "collection": "sa_mp_chunks",
    "embed_model": "bge-m3",
    "dim": 1024,
    "chunk_chars": 600,            # 目标块长（字）
    "chunk_max": 900,              # 硬上限
    "embed_batch": 16,             # 每次 /v1/embeddings 送几条
                                #（实测 Qwen3 32 条 3.5s、bge-m3 12 条 3.6s；
                                #   32 条一批曾把网关打出 519 次
                                #   `WinError 10065 主机不可达`，16×3 更稳）
    "embed_workers": 3,            # 并发批次数（别再加，10065 就是加出来的）
    "embed_retries": 4,            # 单批失败重试次数（带退避）
    "hybrid_candidates": 30,       # 每路召回多少再融合
    "topk": 6,                     # 决策上下文里放几条
    "days": 14,                    # 检索时间窗
    "min_score": 0.0,              # 低于这个融合分就不要
    "rerank": False,               # 可选：bge-reranker-v2-m3（尚未实测）
    "rerank_model": "bge-reranker-v2-m3",
}


def load_conf(conf: dict | None = None) -> dict:
    c = dict(DEFAULTS)
    try:
        import yaml
        data = yaml.safe_load((BASE_DIR / "config.yaml").read_text(encoding="utf-8")) or {}
        for k, v in (data.get("mp_retrieval") or {}).items():
            if v is not None and k in c:
                c[k] = v
    except Exception:
        pass
    for k, v in (conf or {}).items():
        if v is not None and k in c:
            c[k] = v
    return c


# ---------------- embedding（网关） ----------------

_embed_cache: dict[str, list[float]] = {}
_embed_lock = threading.Lock()


def embed(texts: list[str], conf: dict | None = None) -> list[list[float]]:
    """批量向量化。走网关 `/v1/embeddings`，带进程内缓存。

    ⚠️ 网关上有 3 个 embedding 模型已 404（NIM 部署被删），所以**不要**因为
    `/v1/models` 里有就当能用 —— 每次失败都要把模型名和错误原样打出来。
    """
    c = load_conf(conf)
    uniq = list(dict.fromkeys(texts))
    # ⚠️ 判断方向别写反：todo = **还没缓存**的那些。
    #   我第一版写成 `if _cache_ok(t)`（= 已在缓存），于是第一次跑 todo 为空、
    #   直接返回全零向量，日志只显示「无向量 8」，差点以为网关挂了。
    with _embed_lock:
        cached = {t: _embed_cache[t] for t in uniq if t in _embed_cache}
    todo = [t for t in uniq if t not in cached]
    out: dict[str, list[float]] = dict(cached)
    if not todo:
        return [out.get(t) or [0.0] * int(c["dim"]) for t in texts]

    import llm_advisor as llm
    lc = llm.load_llm_conf()
    base = (lc.get("base_url") or "").rstrip("/")
    key = lc.get("api_key") or ""
    if not base or not key:
        print("[mpret] 网关未配置 base_url/api_key，无法向量化", flush=True)
        return [out.get(t) or [0.0] * int(c["dim"]) for t in texts]

    import urllib.request
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    batches = [todo[i:i + int(c["embed_batch"])]
               for i in range(0, len(todo), int(c["embed_batch"]))]

    def work(batch):
        payload = json.dumps({"model": c["embed_model"],
                              "input": batch}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            base + "/v1/embeddings", data=payload,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + key, "x-api-key": key},
            method="POST")
        for attempt in range(max(1, int(c.get("embed_retries") or 4))):
            try:
                with op.open(req, timeout=120) as r:
                    data = json.loads(r.read().decode("utf-8", "ignore"))
                items = data.get("data") or []
                # 网关可能不保证顺序，按 index 取
                items = sorted(items, key=lambda x: x.get("index", 0))
                for it, text in zip(items, batch):
                    vec = it.get("embedding") or []
                    if vec:
                        with _embed_lock:
                            _embed_cache[text] = vec
                return len(items)
            except Exception as exc:                 # noqa: BLE001
                if attempt == max(1, int(c.get("embed_retries") or 4)) - 1:
                    print("[mpret] embedding 失败 %s: %s"
                          % (c["embed_model"], str(exc)[:160]), flush=True)
                    return 0
                time.sleep(2.0 * (attempt + 1))
        return 0

    from concurrent.futures import ThreadPoolExecutor
    total_batches = len(batches)
    done = [0]
    t_start = time.time()

    def run_batch(batch):
        got = work(batch)
        done[0] += 1
        # ⚠️ 必须打进度：全量 2000+ 块要跑几分钟，不打进度时「还在跑」和
        # 「卡死了」在外部完全看不出区别（我就为这个白查了一轮网关和进程 CPU）。
        if done[0] % 10 == 0 or done[0] == total_batches:
            print("[mpret] 向量化 %d/%d 批（已 %.0fs）"
                  % (done[0], total_batches, time.time() - t_start), flush=True)
        return got

    with ThreadPoolExecutor(max_workers=max(1, int(c["embed_workers"]))) as ex:
        list(ex.map(run_batch, batches))

    # ⚠️ 必须**先把第一遍的结果合并进 out**，再判断谁缺失。
    #   顺序写反的话，第一遍明明全部成功，`missing` 仍等于全部 → 每批都白跑
    #   一遍「二次补齐」，API 调用直接翻倍（离线测试抓到的）。
    for t in todo:
        with _embed_lock:
            if t in _embed_cache:
                out[t] = _embed_cache[t]

    # ---- 二次补齐：第一遍因网络/限流失败的，**串行**再试一次 ----
    # 教训来自实测：32 条一批 × 6 并发把网关打出 519 次 `10065`，而重建索引
    # 是先 drop 再建 → 那些块直接消失，索引静默少 15%。所以必须补这一遍，
    # 并且「没补回来」要打印出来（静默缩小索引 = 静默降低召回）。
    missing = [t for t in uniq if t not in out]
    if missing:
        print("[mpret] 二次补齐 %d 条（串行，失败会明确报出来）" % len(missing),
              flush=True)
        fixed = 0
        for i in range(0, len(missing), int(c["embed_batch"])):
            batch2 = missing[i:i + int(c["embed_batch"])]
            if work(batch2):
                fixed += len(batch2)
        with _embed_lock:
            for t in missing:
                if t in _embed_cache:
                    out[t] = _embed_cache[t]
        still = [t for t in missing if t not in out]
        print("[mpret] 补齐 %d / %d，仍失败 %d" % (fixed, len(missing), len(still)),
              flush=True)

    dim = int(c["dim"])
    missing = sum(1 for t in texts if t not in out)
    if missing:
        print("[mpret] %d/%d 条没拿到向量（网关失败或超长），检索会漏掉它们"
              % (missing, len(texts)), flush=True)
    return [out.get(t) or [0.0] * dim for t in texts]


def _cache_ok(text: str) -> bool:
    with _embed_lock:
        return text in _embed_cache


# ---------------- 分块 ----------------

# 模板/页脚噪声：公众号正文末尾几乎必有这些，实测它们会把**不相关的文章**
# 拉进检索结果（第一次测「快手 短剧 电商」时前两条命中的是
# 「商务合作：business@yicai.com 推荐阅读…」这种纯页脚片段）。
# 在入库前剥掉，索引质量立刻变好 —— 比事后调 topk 有效得多。
_BOILERPLATE = [
    "商务合作", "推荐阅读", "扫码关注", "点击关注", "阅读原文", "点击上方",
    "编辑：", "责编：", "校对：", "审核：", "实习生", "免责声明", "版权声明",
    "本文仅代表", "风险提示", "入市需谨慎", "incopyright", "版权所有",
    "关注公众号", "更多精彩", "喜欢本文", "在看", "赞", "分享",
    "不构成投资建议", "不构成投资建议。", "END",
]


_LINK_ITEM = re.compile(r"^\s*[^\n]{0,24}[（(]\s*\d{5,6}\s*[)）]")
# 「公司名（代码）：小标题？」—— 推荐位的固定句式
_TEASER = re.compile(r"^\s*[^\s：:（()]{2,12}[（(]\s*\d{5,6}\s*[)）]\s*[：:].{0,50}[？?]\s*$")


def strip_boilerplate(text: str) -> str:
    """删掉公众号常见页脚/免责行，以及文末的「相关阅读」推荐串。

    ⚠️ 「相关阅读」实测形态有两种，**都需要处理**（第一种先做，第二种漏了）：
    1. **每条一行**、中间夹空行（宝钢那篇：全文 779 行里推荐位占 390 行）；
    2. **每条一行、但行与行之间夹着正文行**（怡合达/分众那两篇）——
       所以「连续 ≥3 行」的 run 检测会**被打断**，只抓得到第一种。

    两种判据都是**结构判据**，不写死任何关键词：
    - run 判据：连续 ≥3 行都是「名称（代码）」开头（允许夹空行）→ 整段剔。
    - 重复判据：`名称（代码）：<短问句>` 句式在全文出现 **≥3 次** → 判定为推荐位，
      全部剔掉。用「重复」而不是「行内多个代码」，是因为实测每行只有 1 个代码。
      加「以问号结尾 + 行长 ≤60」是为了不误删「招商银行（600036）今日上涨 1.2%」。

    ⚠️ 早先试过「行内含 ≥3 个代码」和「行内含代码就删」两个判据，**都抓不到**：
    每行只有 1 个代码；后一个会误删真正文。
    """
    code_re = re.compile(r"[（(]\s*\d{6}\s*[)）]")
    raw = re.split(r"\n{1,}", text or "")
    # 先标出「链接条目行」与「空行」
    is_link = [bool(_LINK_ITEM.match(s or "")) for s in raw]
    drop = [False] * len(raw)
    i = 0
    while i < len(raw):
        if not is_link[i]:
            i += 1
            continue
        j = i
        run = 0
        while j < len(raw):
            if is_link[j]:
                run += 1
                j += 1
            elif not (raw[j] or "").strip():
                j += 1              # 允许空行分隔
            else:
                break
        if run >= 3:
            for k in range(i, j):
                drop[k] = True
        i = j
    # ---- 重复句式判据：同一种「名称（代码）：问句」出现 ≥3 次 = 推荐位清单 ----
    teasers = [n for n, s in enumerate(raw)
               if not drop[n] and len((s or "").strip()) <= 60
               and _TEASER.match(s or "")]
    if len(teasers) >= 3:
        for n in teasers:
            drop[n] = True
    lines = []
    for idx, s in enumerate(raw):
        s = (s or "").strip()
        if drop[idx] or not s:
            continue
        if any(tok in s for tok in _BOILERPLATE) and len(s) <= 60:
            continue
        if len(code_re.findall(s)) >= 3:
            continue                      # 同一行挤了多个代码，也是推荐位
        if len(s) <= 40 and re.match(r"^[\w\W]*(https?://|@\S+|\d{6,})[\w\W]*$", s) \
                and any(tok in s for tok in ("http", "@", "编辑", "商务", "责编")):
            continue
        lines.append(s)
    return "\n".join(lines)


def chunk_text(text: str, conf: dict | None = None) -> list[str]:
    """按段落切块，保留重叠（多带一句上下文，避免边界把语义切断）。

    为什么不用固定字符切片：固定切片会把一句话劈成两半，检索到半句时
    LLM 判断不了它在说什么（实测长文里最常见的失败形态）。
    """
    c = load_conf(conf)
    target = int(c["chunk_chars"])
    hard = int(c["chunk_max"])
    text = strip_boilerplate(text or "")
    paras = [p.strip() for p in re.split(r"\n{1,}", text) if p.strip()]
    if not paras:
        return []
    # 超长单段：先按句末标点切，**切完仍超 hard 的再硬切**。
    # ⚠️ 少了硬切这一步就会漏：「啊」*5000 这种没有标点的段落（表格、
    # 股票代码列表）会原样变成一个 5000 字的块 —— 实测最长文章 20702 字里就有。
    expanded: list[str] = []
    for p in paras:
        if len(p) <= hard:
            expanded.append(p)
            continue
        sents = [s for s in re.split(r"(?<=[。！？；!?;])", p) if s]
        buf = ""
        for s in sents:
            if len(s) > hard:
                if buf:
                    expanded.append(buf)
                    buf = ""
                step = hard - 50                      # 留 50 字重叠
                for i in range(0, len(s), step):
                    piece = s[i:i + hard]
                    expanded.append(piece)
                    if i + hard >= len(s):
                        break
                continue
            if len(buf) + len(s) > hard and buf:
                expanded.append(buf)
                buf = s
            else:
                buf += s
        if buf:
            expanded.append(buf)
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for p in expanded:
        if size + len(p) > target and buf:
            chunks.append("\n".join(buf))
            # 带上上一块最后一句做重叠
            tail = buf[-1]
            buf = [tail] if len(tail) <= hard // 3 else []
            size = len(tail) if buf else 0
        buf.append(p)
        size += len(p)
    if buf:
        chunks.append("\n".join(buf))
    return [ch for ch in chunks if len(ch.strip()) >= 40]


def _chunk_id(url: str, no: int) -> int:
    """稳定的 int64 主键：同一篇同一块重建索引时是同一行（幂等 upsert）。"""
    h = hashlib.blake2b(("%s#%d" % (url, no)).encode("utf-8"), digest_size=8)
    return int.from_bytes(h.digest(), "big") >> 1


# ---------------- Milvus ----------------

def _client(conf: dict):
    from pymilvus import MilvusClient
    return MilvusClient(uri=conf["milvus_uri"])


def index_status(conf: dict | None = None, deps=None) -> dict:
    """索引现状（页面显示用）：collection 在不在、多少块、覆盖多少篇文章。

    为什么单独做这个端点：「开关开着但检索不出东西」有两种完全不同的原因
    —— ① 库是空的（没建索引）② 语料里真的没有相关内容。不给这个数字，
    用户只能看到「没结果」，无法判断该去建索引还是该接受现实。
    """
    c = load_conf(conf)
    out: dict = {"milvus_uri": c["milvus_uri"], "collection": c["collection"]}
    try:
        cli = _client(c)
        if not cli.has_collection(c["collection"]):
            out.update(exists=False, rows=0,
                       hint="collection 不存在，需要建一次索引")
            return out
        stats = cli.get_collection_stats(c["collection"]) or {}
        out.update(exists=True, rows=int(stats.get("row_count") or 0))
    except Exception as exc:                          # noqa: BLE001
        out.update(exists=False, rows=0, error=str(exc)[:200])
        return out
    if deps:
        try:
            conn = deps["get_conn"]()
            try:
                cur = conn.cursor()
                cur.execute("SELECT count(*) FROM sa_mp_articles "
                            "WHERE published_at_src='wx' "
                            "  AND coalesce(content_text,'') <> ''")
                out["indexable_articles"] = int(cur.fetchone()[0])
            finally:
                conn.close()
        except Exception as exc:                      # noqa: BLE001
            out["db_error"] = str(exc)[:200]
    return out


def ensure_collection(cli, conf: dict, drop: bool = False):
    """建 collection：稠密(bge-m3 1024) + 服务端 BM25 稀疏向量。

    BM25 用 Milvus 2.6 的 Function（服务端分词打分），所以中文全文检索
    不需要在 Python 里维护倒排 —— 实测本机 2.6.22 支持，且 `hybrid_search` 可用。
    """
    from pymilvus import DataType, Function, FunctionType
    name = conf["collection"]
    if drop and cli.has_collection(name):
        cli.drop_collection(name)
        print("[mpret] 已删除旧 collection %s" % name, flush=True)
    if cli.has_collection(name):
        return False
    schema = cli.create_schema(auto_id=False, enable_dynamic_field=True)
    schema.add_field("chunk_id", DataType.INT64, is_primary=True)
    schema.add_field("text", DataType.VARCHAR, max_length=8000, enable_analyzer=True)
    schema.add_field("dense", DataType.FLOAT_VECTOR, dim=int(conf["dim"]))
    schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
    schema.add_field("pub_ts", DataType.INT64)          # epoch 秒，时间窗过滤用
    schema.add_field("art_id", DataType.INT64)
    schema.add_field("chunk_no", DataType.INT32)
    schema.add_field("url", DataType.VARCHAR, max_length=512)
    schema.add_field("mp_name", DataType.VARCHAR, max_length=128)
    schema.add_field("title", DataType.VARCHAR, max_length=400)
    schema.add_function(Function(name="bm25", input_field_names=["text"],
                                 output_field_names=["sparse"],
                                 function_type=FunctionType.BM25))
    ip = cli.prepare_index_params()
    ip.add_index(field_name="dense", index_type="AUTOINDEX", metric_type="COSINE")
    ip.add_index(field_name="sparse", index_type="AUTOINDEX", metric_type="BM25")
    cli.create_collection(name, schema=schema, index_params=ip)
    print("[mpret] 已建 collection %s（dim=%d, 稠密+BM25 稀疏）"
          % (name, int(conf["dim"])), flush=True)
    return True


# ---------------- 建索引 ----------------

def index_articles(deps, conf: dict | None = None, drop: bool = False,
                   only_missing: bool = False, limit: int = 0) -> dict:
    """把 `sa_mp_articles` 的正文切块入库。

    - 只取 `published_at_src='wx'` 且有正文的行（与进入决策的口径一致）。
    - **幂等**：chunk_id 由 url+块号哈希决定，重跑只会覆盖同一批行。
    - `only_missing=True` 时跳过已经在索引里的 url（增量用，省 embedding 钱——不对，
      是省时间；按项目规矩**不要**为了省钱而少做，见 AGENTS.md）。
    """
    c = load_conf(conf)
    stat = {"articles": 0, "chunks": 0, "inserted": 0, "skipped_no_text": 0,
            "no_vector": 0, "errors": []}
    conn = deps["get_conn"]()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, url, mp_name, title, content_text, published_at "
            "FROM sa_mp_articles WHERE published_at_src = 'wx' "
            "  AND coalesce(content_text,'') <> '' "
            "ORDER BY published_at DESC")
        cols = [d[0] for d in cur.description]
        arts = [dict(zip(cols, r)) for r in cur.fetchall()]
        conn.close()
        if limit:
            arts = arts[:limit]
        stat["articles"] = len(arts)
        cli = _client(c)
        ensure_collection(cli, c, drop=drop)

        done_urls: set[str] = set()
        if only_missing:
            try:
                rows = cli.query(c["collection"],
                                 filter='chunk_id >= 0',
                                 output_fields=["url"], limit=16384)
                done_urls = {r["url"] for r in rows}
            except Exception:
                done_urls = set()
        todo = [a for a in arts if a["url"] not in done_urls] if only_missing else arts

        rows: list[dict] = []
        texts: list[str] = []
        meta: list[tuple] = []
        for a in todo:
            chunks = chunk_text(a["content_text"] or "", c)
            if not chunks:
                stat["skipped_no_text"] += 1
                continue
            for i, ch in enumerate(chunks):
                texts.append(ch)
                meta.append((a, i))
        stat["chunks"] = len(texts)
        if not texts:
            return stat
        vecs = embed(texts, c)
        zero = [0.0] * int(c["dim"])
        for (a, i), ch, v in zip(meta, texts, vecs):
            if not v or v == zero:
                stat["no_vector"] += 1
                continue
            pub = a.get("published_at")
            ts = int(pub.timestamp()) if isinstance(pub, datetime) else 0
            rows.append({
                "chunk_id": _chunk_id(a["url"], i),
                "text": ch[:8000],
                "dense": v,
                "pub_ts": ts,
                "art_id": int(a["id"]),
                "chunk_no": int(i),
                "url": (a["url"] or "")[:512],
                "mp_name": (a.get("mp_name") or "")[:128],
                "title": (a.get("title") or "")[:400],
            })
        if rows:
            for i in range(0, len(rows), 500):
                cli.insert(c["collection"], rows[i:i + 500])
            stat["inserted"] = len(rows)
        print("[mpret] 索引完成：文章 %d 篇 → 块 %d → 入库 %d（无正文 %d，无向量 %d）"
              % (stat["articles"], stat["chunks"], stat["inserted"],
                 stat["skipped_no_text"], stat["no_vector"]), flush=True)
        try:
            cli.flush(c["collection"])
        except Exception:
            pass
    except Exception as exc:                         # noqa: BLE001
        stat["errors"].append(str(exc)[:200])
        print("[mpret] 建索引失败: %s" % str(exc)[:200], flush=True)
        raise
    return stat


# ---------------- 检索 ----------------

def _rrf(rank_lists: list[list[int]], k: int = 60) -> dict[int, float]:
    """Reciprocal Rank Fusion：多路召回按名次倒数加权合并。

    不用加权分数相加：向量分与 BM25 分**量纲完全不同**（COSINE ∈[-1,1]，
    BM25 无上界），相加等于让某一路主导。RRF 只看名次，天然可比。
    """
    out: dict[int, float] = {}
    for ranks in rank_lists:
        for pos, pk in enumerate(ranks):
            out[pk] = out.get(pk, 0.0) + 1.0 / (k + pos + 1)
    return out


def retrieve(query: str, code: str = "", days: int = 0, topk: int = 0,
             conf: dict | None = None, name: str = "") -> list[dict]:
    """混合检索：向量 + BM25 → RRF 融合 → top-k 原文片段。

    返回每条都带 url / mp_name / published_at / text，**便于回溯与引用**。
    """
    c = load_conf(conf)
    topk = int(topk or c["topk"])
    days = int(days or c["days"])
    if not query or not query.strip():
        return []
    cli = _client(c)
    if not cli.has_collection(c["collection"]):
        print("[mpret] collection %s 还不存在，先跑建索引" % c["collection"],
              flush=True)
        return []

    qvec = embed([query], c)[0]
    cand = int(c["hybrid_candidates"])
    expr = ""
    if days:
        since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
        expr = "pub_ts >= %d" % since

    hits: dict[int, dict] = {}

    def collect(rows):
        for r in rows:
            hits[r["chunk_id"]] = r

    def rows_of(res):
        """pymilvus 2.6 的真实形状：`SearchResult` 是**每个 query 一个分组**的列表，
        分组（HybridHits/SingleQueryResult）本身才是命中序列，每条 Hit 有
        `chunk_id`(pk) / `distance` / `entity`(output_fields 字典)。

        我第一版把 res 当成「命中列表」直接迭代，拿到的是分组对象 →
        `'HybridHits' object has no attribute 'get'`，异常又被 except 吞成
        「降级」，于是一次真实故障看起来像「库里没数据」。
        """
        out = []
        groups = list(res) if isinstance(res, (list, tuple)) else [res]
        for g in groups:
            try:
                for h in g:
                    ent = h.get("entity") or {}
                    if isinstance(ent, str):        # 某些版本给的是 repr 串
                        try:
                            ent = json.loads(ent.replace("'", '"'))
                        except Exception:
                            ent = {}
                    out.append({"chunk_id": h.get("chunk_id"),
                                **{k: ent.get(k) for k in
                                   ("text", "url", "mp_name", "title", "pub_ts",
                                    "art_id", "chunk_no")}})
            except TypeError:
                continue
        return out

    # ---- 路 1：向量 ----
    try:
        from pymilvus import AnnSearchRequest
        # ⚠️ AnnSearchRequest 的签名只有 (data, anns_field, param, limit, expr)，
        # **不接受 output_fields** —— 那是 hybrid_search/search 的参数。
        # 给了会 TypeError，然后被我的 except 吞成「降级为纯向量」，
        # 纯向量那条路同样有这行 → 两路都炸，检索静默返回空。
        out_fields = ["text", "url", "mp_name", "title", "pub_ts", "art_id",
                      "chunk_no"]
        dense_req = AnnSearchRequest(
            data=[qvec], anns_field="dense", param={"metric_type": "COSINE"},
            limit=cand, expr=expr or None)
        sparse_req = AnnSearchRequest(
            data=[query], anns_field="sparse",
            param={"metric_type": "BM25"}, limit=cand, expr=expr or None)
        from pymilvus import RRFRanker
        res = cli.hybrid_search(c["collection"], reqs=[dense_req, sparse_req],
                                ranker=RRFRanker(), limit=topk,
                                output_fields=out_fields)
        rows = rows_of(res)
        collect(rows)
        return [_shape(r) for r in rows[:topk]]
    except Exception as exc:                          # noqa: BLE001
        print("[mpret] hybrid_search 失败，降级为纯向量：%s" % str(exc)[:160],
              flush=True)

    # ---- 兜底：纯向量 ----
    try:
        # ⚠️ `cli.search` 收的是 data + anns_field + search_params，
        #    不是 AnnSearchRequest 列表（那是 hybrid 的用法）。
        res = cli.search(c["collection"], data=[qvec], anns_field="dense",
                         search_params={"metric_type": "COSINE"}, limit=topk,
                         expr=expr or None,
                         output_fields=["text", "url", "mp_name", "title",
                                        "pub_ts", "art_id", "chunk_no"])
        return [_shape(r) for r in rows_of(res)[:topk]]
    except Exception as exc:                          # noqa: BLE001
        print("[mpret] 向量检索也失败: %s" % str(exc)[:160], flush=True)
        return []


def _shape(r: dict) -> dict:
    ts = r.get("pub_ts") or 0
    pub = ""
    if ts:
        pub = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%d")
    return {"chunk_id": r.get("chunk_id"), "text": (r.get("text") or "")[:1200],
            "url": r.get("url") or "", "mp_name": r.get("mp_name") or "",
            "title": r.get("title") or "", "published_at": pub}


# ---------------- 决策注入 ----------------

def expand_query(cur, code: str, name: str, limit: int = 3) -> str:
    """给个股查询补「行业/题材词」。

    为什么需要（实测）：纯实体名查询「招商银行 息差」召回的是房贷贴息、
    香港超高息存款这类只沾一个「银行」的文章；而**主题类**查询
    「国有大行 银行股」非常准。差别在于快讯类文章提到公司名常是顺带一笔，
    但它整篇讲的是某个题材 —— 所以要把「这只票相关的文章标题里反复出现的
    题材词」补进 query。

    词从哪来：**已经通过公司名关联到这只票的公众号文章标题**（sa_news_related
    → sa_mp_articles）。这是现成数据、零 LLM 成本、也不需要人工维护映射表。
    """
    if cur is None or not code:
        return ""
    try:
        cur.execute(
            "SELECT a.title FROM sa_news_related r "
            "  JOIN sa_mp_articles a ON a.url = r.url "
            " WHERE r.code = %s AND a.published_at > now() - interval '30 days' "
            " LIMIT 60", (code,))
        titles = [str(r[0] or "") for r in cur.fetchall()]
    except Exception:
        try:
            cur.connection.rollback()
        except Exception:
            pass
        return ""
    try:
        import jieba
        stop = {"公司", "股份", "今日", "昨日", "最新", "报道", "公告", "发布",
                "表示", "认为", "预计", "公告称", "记者", "一个", "这个", "我们"}
        words: dict[str, int] = {}
        for t in titles:
            for w in jieba.cut(t):
                w = w.strip()
                if len(w) < 2 or w in stop or w.isdigit() or name and w in name:
                    continue
                words[w] = words.get(w, 0) + 1
        ranked = sorted(words.items(), key=lambda kv: (-kv[1] * len(kv[0]), kv[0]))
        return " ".join(w for w, _ in ranked[:limit])
    except Exception:
        return ""


def build_context(cur, code: str, stock_name: str = "", conf: dict | None = None,
                  days: int = 0, topk: int = 0) -> str:
    """给某只票生成「公众号原文片段」块（决策上下文用）。

    query = 公司名 + 代码 + 该票相关文章标题里抽出的题材词（见 `expand_query`）。
    股票名去掉 -W/-U 等后缀，港股/ETF 也能命中。

    ⚠️ **必须标出可信度**：实测这些公众号是综合财经媒体（21世纪/央视/第一财经/
    中新经纬…），**很少写个股深度**。查「招商银行」召回的全是「房贷贴息」这类
    只沾一个「银行」的文章 —— 语义上确实相关，但对个股决策价值极低。
    所以逐条判断「这篇文章里**点名**了这只票吗」，分成两类渲染：
      · 直接点名（正文/标题出现公司名或代码）→ 排在前面，可作依据；
      · 仅题材相关 → 排在后面并**明确标注**，让 LLM 知道这是行业层面的信息。
    一条都不「点名」时直接说明「没有文章点名该股，只有题材层面内容」，
    而不是把行业文章伪装成个股信息。
    """
    c = load_conf(conf)
    name = (stock_name or "").strip()
    parts = [p for p in (name, code) if p]
    variants = {name} if name else set()
    for suf in ("-W", "-U", "-SW", "-B"):
        if name.endswith(suf) and len(name) - len(suf) >= 2:
            variants.add(name[: -len(suf)])
            parts.insert(0, name[: -len(suf)])
    extra = expand_query(cur, code, name)
    if extra:
        parts.append(extra)
    query = " ".join(dict.fromkeys(p for p in parts if p))
    rows = retrieve(query, code=code, days=days or int(c["days"]),
                    topk=(topk or int(c["topk"])) * 2, conf=c)

    def named(r):
        blob = "%s %s" % (r.get("title") or "", r.get("text") or "")
        return any(v and len(v) >= 3 and v in blob for v in variants) \
            or (code and code in blob)

    named_rows = [r for r in rows if named(r)]
    topic_rows = [r for r in rows if not named(r)]
    limit = topk or int(c["topk"])
    named_rows, topic_rows = named_rows[:limit], topic_rows[:max(0, limit - len(named_rows))]

    lines = ["【公众号原文片段（语义+全文检索，按相关度排序）】"]
    if not rows:
        lines.append("近 %d 天没有检索到与该股/该题材相关的公众号内容。"
                     % int(c["days"]))
        return "\n".join(lines)
    if not named_rows:
        lines.append("⚠ 近 %d 天**没有任何公众号文章点名该股**，以下仅为**题材层面**"
                     "的相关内容（行业/政策/宏观），对个股的直接参考价值有限："
                     % int(c["days"]))
    for i, r in enumerate(named_rows, 1):
        body = re.sub(r"\s+", " ", r["text"]).strip()
        lines.append("【%d·点名】%s｜%s｜%s" % (i, r["mp_name"] or "公众号",
                                          r["published_at"], r["title"][:40]))
        lines.append("    %s" % body[:500])
        lines.append("    (来源 %s)" % (r["url"][:80] or "-"))
    for j, r in enumerate(topic_rows, len(named_rows) + 1):
        body = re.sub(r"\s+", " ", r["text"]).strip()
        lines.append("【%d·题材】%s｜%s｜%s" % (j, r["mp_name"] or "公众号",
                                            r["published_at"], r["title"][:40]))
        lines.append("    %s" % body[:300])
        lines.append("    (来源 %s)" % (r["url"][:80] or "-"))
    lines.append("⚠ 以上是公众号原文摘录（自媒体口径、非权威），与新闻公告冲突时以公告为准。")
    return "\n".join(lines)


# ---------------- agentic search（开放问题用） ----------------

AGENT_SYSTEM = (
    "你是财经研究助理。可以使用工具 `search_mp` 检索公众号文章库"
    "（语义+全文混合检索）。工作方式：先用工具检索 → 阅读片段 → 需要就换关键词再检索，"
    "最多 %d 轮。\n"
    "**硬要求**：每个结论后面必须给出 [1]/[2] 形式的来源编号，对应你检索到的片段；"
    "没有检索到依据的话不要编。\n"
    "最后给一段结论（不超过 300 字）+ 一个「建议继续关注什么」的清单。"
)


def agentic_search(question: str, conf: dict | None = None, rounds: int = 3) -> dict:
    """多轮「检索 → 读 → 再检索」。**只在开放问题上用**，决策热路径不走这里。

    返回 {"answer": str, "citations": [...], "rounds": n, "queries": [...]}。
    """
    c = load_conf(conf)
    try:
        import llm_advisor as llm
    except Exception as exc:                          # noqa: BLE001
        return {"error": "llm 不可用: %s" % str(exc)[:120]}
    lc = llm.load_llm_conf()
    queries: list[str] = [question]
    passages: list[dict] = []
    transcript: list[dict] = []
    answer = ""
    for r in range(max(1, rounds)):
        block = []
        for q in queries[-2:]:
            rows = retrieve(q, topk=6, conf=c)
            for row in rows:
                if row["chunk_id"] not in {p["chunk_id"] for p in passages}:
                    passages.append(row)
        if not passages:
            answer = "库里没有检索到相关内容。"
            break
        passages = passages[:12]
        for i, p in enumerate(passages, 1):
            block.append("[%d] %s｜%s｜%s\n%s"
                         % (i, p["mp_name"], p["published_at"], p["title"][:40],
                            re.sub(r"\s+", " ", p["text"])[:600]))
        user = ("问题：%s\n\n已检索到的片段：\n%s\n\n"
                "如果这些片段已经够回答，直接给结论（带 [n] 编号）；"
                "如果还缺，请给出**新的检索关键词**（一行一个，最多 3 个）。"
                % (question, "\n\n".join(block)))
        try:
            out = llm.ask(AGENT_SYSTEM % rounds, user, conf=lc, thinking=False,
                          task="deep")
        except Exception as exc:                     # noqa: BLE001
            return {"error": "LLM 调用失败: %s" % str(exc)[:160],
                    "citations": passages}
        transcript.append({"round": r + 1, "queries": list(queries)})
        answer = out
        nxt = re.findall(r"^\s*(?:检索关键词|关键词|query)\s*[:：]\s*(.+)$",
                         out, re.M)
        if not nxt:
            break
        new_q = [x.strip() for line in nxt for x in re.split(r"[,，、]", line)
                 if x.strip()][:3]
        if not new_q:
            break
        queries.extend(new_q)
    return {"question": question, "answer": answer,
            "citations": [{"n": i + 1, "mp_name": p["mp_name"],
                           "published_at": p["published_at"],
                           "title": p["title"], "url": p["url"],
                           "text": p["text"][:400]}
                          for i, p in enumerate(passages[:12])],
            "rounds": len(transcript), "queries": queries,
            "transcript": transcript}


# ---------------- CLI ----------------

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="公众号语义检索（Milvus+bge-m3）")
    ap.add_argument("--rebuild", action="store_true", help="删库重建并全量索引")
    ap.add_argument("--index", action="store_true", help="只索引（幂等）")
    ap.add_argument("--query", default="", help="检索一句")
    ap.add_argument("--agentic", default="", help="agentic 搜索一个开放问题")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    def _deps():
        import psycopg2
        env = {}
        for line in open(os.path.join(BASE_DIR, "..", ".env"), encoding="utf-8"):
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
        return {"get_conn": lambda: psycopg2.connect(
            host=env["DB_HOST"], port=env["DB_PORT"], user=env["DB_USERNAME"],
            password=env["DB_PASSWORD"], dbname=env["DB_DATABASE"])}

    if args.rebuild or args.index:
        print(json.dumps(index_articles(_deps(), drop=args.rebuild,
                                        limit=args.limit),
                         ensure_ascii=False, default=str))
    if args.query:
        for r in retrieve(args.query):
            print("[%s] %s｜%s\n%s\n%s\n" % (r["mp_name"], r["published_at"],
                                            r["title"], r["text"][:300],
                                            r["url"]))
    if args.agentic:
        out = agentic_search(args.agentic)
        print(out.get("answer") or out.get("error"))
        for c in out.get("citations", []):
            print("  [%d] %s %s %s" % (c["n"], c["mp_name"],
                                      c["published_at"], c["title"][:40]))


__all__ = ["DEFAULTS", "load_conf", "embed", "chunk_text", "ensure_collection",
           "index_articles", "retrieve", "build_context", "agentic_search"]