"""智谱动态监控 zai_watch.py —— 只抓「新模型发布痕迹」→ sa_news。

## 口径（2026-11-16 用户定：「监控的是是否有发布新模型的痕迹，而不是瞎监控」）

什么算「痕迹」（三道，全部按 `model_name_re` 匹配 basename，默认 GLM 系命名）：

1. **GitHub 模型仓新 release**：`zai-org/GLM-5` 这类模型仓出 tag（= 正式发布/开源）。
   ZCode / zcode-plugins / feedback 这类**工具仓的 release 不算**（默认排除——
   第一版把 ZCode 的 v3.17.2 也收了，被用户纠正为「瞎监控」）。
2. **GitHub 新模型仓库上线**：org 下新出现 GLM* 仓库 = 新模型发布前兆（只报
   created_at 在 7 天内的，更旧的没有任何决策价值）。
3. **HuggingFace 新模型上传**：author 下的新 modelId；**同一模型的量化变体
   （-BF16/-FP8/-GGUF…）按 base 名去重**——那是同一份权重的重打包，不是新发布。

什么不算：工具仓 release、非 GLM 命名的仓库/模型（可在 config 扩 model_name_re）、
超过 7 天的旧条目（save_to_db 时效闸门）。

## 落库契约

`news_fetcher.save_to_db(items)`：URL 唯一去重、**必须有真实 publish_time**（铁律），
`code` 取配置（默认 02513 锚定票）→ 进 `_paper_news_rows` 决策上下文与新闻 tab。
首跑只收最近 7 天痕迹（旧的不补，避免灌一屏旧闻）。

## 状态侧车

`data/zai_watch_state.json`（锁 + tmp 原子写）：HF 的 base 名 → 首见 URL，
用于量化变体去重（跨 URL，sa_news 的 URL 去重管不到）。坏文件容错为空。
GitHub 侧**不需要**状态：release 靠 URL 去重，新仓库靠 created_at 7 天窗。

## 红线

只读公开 API（github.com / huggingface.co）；不 import app；不写任何交易表；
`get_conn` / `notify_fn` 由 app.py 注入。
"""

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.yaml"
STATE_FILE = BASE_DIR / "data" / "zai_watch_state.json"

DEFAULT_CONF = {
    "enabled": True,
    "interval_minutes": 60,      # 0 = 关闭
    "github_org": "zai-org",     # org 仓库发现 + 各模型仓 release，各一次调用
    "hf_author": "zai-org",      # HuggingFace 作者页（一次调用覆盖全部上传）
    # 什么算「模型」：匹配**basename**（仓库名 / modelId 去掉 org 前缀），大小写不敏感。
    # 默认 GLM 系命名；CogAgent/SCAIL-2/ZCode 这类不匹配 → 不算模型痕迹。
    "model_name_re": r"GLM[-_A-Za-z0-9.]*",
    "code": "02513",             # 新闻关联的股票代码（进 _paper_news_rows 决策上下文）
    "notify_new": True,          # 过滤后剩下的都是高信号（新模型发布），默认推
    "per_source_limit": 8,       # 每源单轮最多取多少条
    # HF 本机直连超时（2026-11-16 真机实测：github.com 直连 OK，huggingface.co 不通），
    # 直连失败自动走这个代理重试一次。代理不常驻（会挂），所以只做回退不做主路。
    "proxy": "http://127.0.0.1:6478",
}

# 量化/再打包后缀：同一份权重的变体，不是新发布（大小写不敏感，贴在 basename 尾部）
_QUANT_RE = re.compile(r"[-_. ](BF16|FP8|FP16|INT8|INT4|GGUF|AWQ|GPTQ|4BIT|8BIT)$",
                       re.IGNORECASE)

LOCK = threading.Lock()
_state = {"last_run": None, "last_error": None, "last_inserted": 0}


def load_conf() -> dict:
    conf = dict(DEFAULT_CONF)
    try:
        if CONFIG_FILE.exists():
            import yaml
            data = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
            conf.update({k: v for k, v in (data.get("zai") or {}).items()
                         if v is not None})
    except Exception:
        pass
    return conf


def get_status() -> dict:
    with LOCK:
        return dict(_state)


# ---------------- 状态侧车（HF 变体去重） ----------------

def _load_state() -> dict:
    try:
        if STATE_FILE.exists():
            d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def _save_state(d: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception as exc:
        # 写失败必须打印（AGENTS.md：静默丢状态 = 变体去重悄悄失效）
        print(f"[zai-watch] 状态侧车写失败: {type(exc).__name__}: {exc}", flush=True)


def _basename(name: str) -> str:
    """org/repo 或 org/model → basename（最后一段）。"""
    return (name or "").rstrip("/").split("/")[-1]


def _is_model_trace(name: str, pattern: str) -> bool:
    """basename 命中模型命名 pattern 才算痕迹（ZCode/feedback 等工具仓排除在外）。"""
    if not pattern:
        return False
    try:
        return re.match(pattern, _basename(name), re.IGNORECASE) is not None
    except re.error:
        return False


def _model_base(basename: str) -> str:
    """模型 base 名：剥掉量化变体后缀，小写。GLM-5.3-BF16 → glm-5.3。"""
    return _QUANT_RE.sub("", basename.strip()).lower()


# ---------------- HTTP ----------------

def _http_json(url: str, timeout: int = 15):
    """GET JSON：先直连，ConnectError/Timeout 时走代理重试一次。

    实测（2026-11-16）：api.github.com 本机直连 OK，huggingface.co 直连超时 ——
    所以不能写死直连也不能写死代理（代理不常驻），按「直连优先、代理兜底」来。"""
    import requests
    proxies = {"http": None, "https": None}
    try:
        r = requests.get(url, timeout=timeout,
                         headers={"Accept": "application/json",
                                  "User-Agent": "sa-zai-watch/1.0"},
                         proxies=proxies)
        r.raise_for_status()
        return r.json()
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as first:
        proxy = str(load_conf().get("proxy") or "").strip()
        if not proxy:
            raise
        try:
            r = requests.get(url, timeout=timeout,
                             headers={"Accept": "application/json",
                                      "User-Agent": "sa-zai-watch/1.0"},
                             proxies={"http": proxy, "https": proxy})
            r.raise_for_status()
            return r.json()
        except Exception as second:
            print(f"[zai-watch] 直连与代理都失败: {url} "
                  f"(direct: {type(first).__name__}, proxy: {type(second).__name__}: {second})",
                  flush=True)
            raise second from first


def _norm_pub(s: str | None) -> str | None:
    """GitHub/HF 的时间串 → ISO（带偏移）。save_to_db 会再规范化一次。"""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        return None


def _within_days(pub_iso: str | None, days: int = 7) -> bool:
    if not pub_iso:
        return False
    try:
        dt = datetime.fromisoformat(pub_iso)
        return (datetime.now(timezone.utc) - dt).days <= days
    except (ValueError, TypeError):
        return False


# ---------------- 三道痕迹 ----------------

def fetch_github_releases(conf: dict, org_repos: list[dict] | None = None) -> list[dict]:
    """模型仓最近 releases → items。org_repos 由 fetch_github_org 传入（复用那次调用）；
    单独调用时自己拉 org repos。非模型仓（ZCode 等）在这里就被模式闸掉。"""
    code = str(conf.get("code") or "").strip()
    limit = max(1, int(conf.get("per_source_limit") or 8))
    org = str(conf.get("github_org") or "").strip()
    pattern = str(conf.get("model_name_re") or "")
    if not org or not code or not pattern:
        return []
    if org_repos is None:
        try:
            org_repos = _http_json(
                f"https://api.github.com/orgs/{org}/repos?per_page=100&sort=pushed")
        except Exception as exc:
            print(f"[zai-watch] {org} repos 拉取失败: {type(exc).__name__}: {exc}",
                  flush=True)
            return []
        if not isinstance(org_repos, list):
            return []
    items = []
    for repo in org_repos:
        full = repo.get("full_name") or ""
        if not _is_model_trace(full, pattern):
            continue
        try:
            rels = _http_json(f"https://api.github.com/repos/{full}/releases?per_page={limit}")
        except Exception as exc:
            print(f"[zai-watch] {full} releases 拉取失败: {type(exc).__name__}: {exc}",
                  flush=True)
            continue
        if not isinstance(rels, list):
            continue
        base = _basename(full)
        for rel in rels:
            url = rel.get("html_url") or ""
            pub = _norm_pub(rel.get("published_at") or rel.get("created_at"))
            tag = (rel.get("tag_name") or "").strip()
            name = (rel.get("name") or "").strip()
            title_bits = [f"[GitHub] {base}"]
            if tag:
                title_bits.append(tag)
            if name and name != tag:
                title_bits.append(name)
            if url and pub:
                items.append({"code": code, "title": " ".join(title_bits)[:200],
                              "url": url, "source": "zai", "media": "GitHub",
                              "publish_time": pub})
    return items


def fetch_github_new_repos(conf: dict, org_repos: list[dict] | None = None) -> list[dict]:
    """新模型仓库上线（created_at ≤7 天且命中 pattern）= 新模型发布前兆。
    created_at 更旧的仓库没有任何决策价值，不报（因此无需 seen 状态）。"""
    code = str(conf.get("code") or "").strip()
    org = str(conf.get("github_org") or "").strip()
    pattern = str(conf.get("model_name_re") or "")
    if not org or not code or not pattern:
        return []
    if org_repos is None:
        try:
            org_repos = _http_json(
                f"https://api.github.com/orgs/{org}/repos?per_page=100&sort=pushed")
        except Exception as exc:
            print(f"[zai-watch] {org} repos 拉取失败: {type(exc).__name__}: {exc}",
                  flush=True)
            return []
        if not isinstance(org_repos, list):
            return []
    items = []
    for repo in org_repos:
        full = repo.get("full_name") or ""
        if not _is_model_trace(full, pattern):
            continue
        pub = _norm_pub(repo.get("created_at"))
        if not _within_days(pub, 7):
            continue
        base = _basename(full)
        items.append({"code": code,
                      "title": f"[GitHub] zai-org 新模型仓库上线：{base}（发布前兆）"[:200],
                      "url": f"https://github.com/{full}",
                      "source": "zai", "media": "GitHub", "publish_time": pub})
    return items


def fetch_hf_models(conf: dict, state: dict | None = None) -> tuple[list[dict], dict]:
    """HF 新模型上传 → items。量化变体按 base 名去重（状态侧车）。
    返回 (items, new_state)：**只有在 save_to_db 成功后才写回 new_state**，
    落库失败下轮自然重试。"""
    code = str(conf.get("code") or "").strip()
    author = str(conf.get("hf_author") or "").strip()
    pattern = str(conf.get("model_name_re") or "")
    limit = max(1, int(conf.get("per_source_limit") or 8))
    if not author or not code or not pattern:
        return [], dict(state or {})
    st = dict(state or {})
    seen = st.get("seen_hf") if isinstance(st.get("seen_hf"), dict) else {}
    try:
        models = _http_json(
            f"https://huggingface.co/api/models?author={author}&sort=lastModified&limit={limit}")
    except Exception as exc:
        print(f"[zai-watch] HF {author} 拉取失败: {type(exc).__name__}: {exc}", flush=True)
        return [], st
    items = []
    for m in models if isinstance(models, list) else []:
        mid = m.get("modelId") or ""
        base_name = _basename(mid)
        if not _is_model_trace(mid, pattern):
            continue
        # createdAt=首次创建（发布时间）；lastModified 是 push 时间，不能当发布时间
        pub = _norm_pub(m.get("createdAt") or m.get("lastModified"))
        if not mid or not pub:
            continue
        base = _model_base(base_name)
        if base in seen:
            continue  # 同一模型的量化变体/重复上报 —— 不是新发布
        url = f"https://huggingface.co/{mid}"
        items.append({"code": code, "title": f"[HuggingFace] 新模型：{mid}"[:200],
                      "url": url, "source": "zai", "media": "HuggingFace",
                      "publish_time": pub})
        seen[base] = {"url": url, "date": pub}
    st["seen_hf"] = seen
    return items, st


def run_once(deps: dict) -> dict:
    """三道痕迹 → news_fetcher.save_to_db。返回统计。"""
    conf = load_conf()
    if not conf.get("enabled", True):
        return {"ok": False, "skipped": "disabled"}
    code = str(conf.get("code") or "").strip()
    if not code:
        return {"ok": False, "skipped": "code empty"}

    # org repos 拉一次，release 发现与新仓库发现共用（省 API 配额）
    org = str(conf.get("github_org") or "").strip()
    org_repos = None
    if org:
        try:
            org_repos = _http_json(
                f"https://api.github.com/orgs/{org}/repos?per_page=100&sort=pushed")
            if not isinstance(org_repos, list):
                org_repos = None
        except Exception as exc:
            print(f"[zai-watch] {org} repos 拉取失败: {type(exc).__name__}: {exc}",
                  flush=True)

    state = _load_state()
    gh_items = fetch_github_releases(conf, org_repos)
    new_repo_items = fetch_github_new_repos(conf, org_repos)
    hf_items, new_state = fetch_hf_models(conf, state)
    items = gh_items + new_repo_items + hf_items

    inserted = 0
    notified = 0
    if items:
        try:
            import news_fetcher
            inserted = news_fetcher.save_to_db(items)
        except Exception as exc:
            with LOCK:
                _state["last_error"] = f"{type(exc).__name__}: {exc}"
            print(f"[zai-watch] save_to_db 失败: {type(exc).__name__}: {exc}", flush=True)
            return {"ok": False, "error": str(exc), "items": len(items)}
        # 落库成功才更新 HF 变体状态（失败下轮重试同一批）
        _save_state(new_state)
        if inserted and conf.get("notify_new") and callable(deps.get("notify_fn")):
            for it in items[:inserted]:
                try:
                    deps["notify_fn"](f"[智谱动态] {it['title']} {it['url']}")
                    notified += 1
                except Exception:
                    pass
    with LOCK:
        _state["last_run"] = datetime.now().isoformat(timespec="seconds")
        _state["last_error"] = None
        _state["last_inserted"] = inserted
    return {"ok": True, "items": len(items), "inserted": inserted,
            "notified": notified}
