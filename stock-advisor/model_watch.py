"""模型发布监控 model_watch.py —— 多主体「新模型发布痕迹」→ sa_news。

## 口径（2026-11-16 用户定：「监控的是是否有发布新模型的痕迹，而不是瞎监控」；
2026-11-16 追加 MiniMax 主体后泛化为多主体）

每个主体（subjects，见下）三道痕迹，全部按该主体的 `model_name_re` 匹配 basename、
再过 `exclude_re` 排除：

1. **GitHub 模型仓新 release**：GLM-5 / MiniMax-M3 这类模型仓出 tag（= 正式发布/开源）。
   工具仓不算：ZCode（智谱，include 不匹配）、MiniMax-Code-*（MiniMax，exclude 排除）。
2. **GitHub 新模型仓库上线**：org 下新出现模型命名仓库 = 新模型发布前兆（只报
   created_at 在 7 天内的，更旧的没有任何决策价值，因此无需 seen 状态）。
3. **HuggingFace 新模型上传**：author 下的新 modelId；**量化变体（-BF16/-FP8/-MXFP8/
   -GGUF…）按 base 名去重**——那是同一份权重的重打包，不是新发布。实测样例：
   MiniMax-M3-MXFP8（base=MiniMax-M3）不该单独报。

## 主体与代码映射

| key | GitHub org | HF author | 股票 | pattern | exclude |
|---|---|---|---|---|---|
| zai | zai-org | zai-org | 02513 智谱 | GLM* | — |
| minimax | MiniMax-AI | MiniMaxAI | 00100 MINIMAX-W | MiniMax* | MiniMax-Code-* |

## 落库契约

`news_fetcher.save_to_db(items)`：URL 唯一去重、**必须有真实 publish_time**（铁律），
时效闸门 7 天（普通条目）。`code` 取主体配置 → 进 `_paper_news_rows` 决策上下文
与新闻 tab。首跑只收最近 7 天痕迹（旧的不补，避免灌一屏旧闻）。

## 状态侧车

`data/model_watch_state.json`（锁 + tmp 原子写）：按主体分桶的 HF base 名 → 首见
URL，用于量化变体去重（跨 URL，sa_news 的 URL 去重管不到）。坏文件容错为空。
**落库成功才写回**——落库失败下轮重试同一批。GitHub 侧不需要状态：release 靠
URL 去重，新仓库靠 created_at 7 天窗。

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
STATE_FILE = BASE_DIR / "data" / "model_watch_state.json"

# 全局（一次轮询一套）与主体（每个公司一套）分开。主体注册表是**代码级真源**，
# config.yaml 的同名段只覆盖字段值（改 org/pattern/code 不用动代码）。
SUBJECT_DEFAULTS = {
    "zai": {"label": "智谱", "github_org": "zai-org", "hf_author": "zai-org",
            "model_name_re": r"GLM[-_A-Za-z0-9.]*", "exclude_re": "",
            "code": "02513"},
    "minimax": {"label": "MiniMax", "github_org": "MiniMax-AI", "hf_author": "MiniMaxAI",
                "model_name_re": r"MiniMax[-_A-Za-z0-9.]*",
                "exclude_re": r"MiniMax-Code[-_A-Za-z0-9.]*",
                "code": "00100"},
}
SUBJECT_KEYS = tuple(SUBJECT_DEFAULTS)

DEFAULT_CONF = {
    "enabled": True,
    "interval_minutes": 60,      # 0 = 关闭
    "notify_new": True,          # 过滤后剩下的都是高信号（新模型发布），默认推
    "per_source_limit": 8,       # 每源单轮最多取多少条
    # HF 本机直连超时（2026-11-16 真机实测：github.com 直连 OK，huggingface.co 不通），
    # 直连失败自动走这个代理重试一次。代理不常驻（会挂），所以只做回退不做主路。
    "proxy": "http://127.0.0.1:6478",
    # GitHub 免鉴权 60 次/时/IP 被打爆时的救援通道（2026-11-16 用户要求）：
    #   auto   = 直连优先，403 rate-limit 时经 kimi-webbridge 用登录态抓 releases.atom
    #   always = 直接走桥（浏览器登录态会话鉴权，不吃 API 配额）
    #   off    = 不用桥
    "github_bridge": "auto",
}

# 量化/再打包后缀：同一份权重的变体，不是新发布（大小写不敏感，贴在 basename 尾部）
_QUANT_RE = re.compile(
    r"[-_. ](BF16|FP8|FP16|FP4|MXFP8|MXFP4|NVFP4|INT8|INT4|GGUF|AWQ|GPTQ|4BIT|8BIT)$",
    re.IGNORECASE)

LOCK = threading.Lock()
_state = {"last_run": None, "last_error": None, "last_inserted": 0}


def load_conf() -> dict:
    """读 config.yaml：全局段 model_watch + 各主体段（按 SUBJECT_DEFAULTS 合并）。"""
    raw: dict = {}
    try:
        if CONFIG_FILE.exists():
            import yaml
            data = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
            raw = data or {}
    except Exception:
        pass
    g = dict(DEFAULT_CONF)
    g.update({k: v for k, v in (raw.get("model_watch") or {}).items()
              if v is not None})
    subjects = []
    for key in SUBJECT_KEYS:
        s = dict(SUBJECT_DEFAULTS[key])
        s.update({k: v for k, v in (raw.get(key) or {}).items()
                  if v is not None and k != "label"})
        s["key"] = key
        s.setdefault("proxy", g.get("proxy"))   # 主体继承全局代理（HF 直连超时的回退）
        subjects.append(s)
    return {"global": g, "subjects": subjects}


def get_status() -> dict:
    with LOCK:
        return dict(_state)


# ---------------- 状态侧车（HF 变体去重，按主体分桶） ----------------

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
        print(f"[model-watch] 状态侧车写失败: {type(exc).__name__}: {exc}", flush=True)


def _basename(name: str) -> str:
    """org/repo 或 org/model → basename（最后一段）。"""
    return (name or "").rstrip("/").split("/")[-1]


def _is_model_trace(name: str, subject: dict) -> bool:
    """basename 命中 include pattern 且不命中 exclude_re 才算痕迹。"""
    b = _basename(name)
    inc = str(subject.get("model_name_re") or "")
    exc = str(subject.get("exclude_re") or "")
    if not inc:
        return False
    try:
        if re.match(inc, b, re.IGNORECASE) is None:
            return False
        if exc and re.search(exc, b, re.IGNORECASE):
            return False
        return True
    except re.error:
        return False


def _model_base(basename: str) -> str:
    """模型 base 名：剥掉量化变体后缀，小写。MiniMax-M3-MXFP8 → minimax-m3。"""
    return _QUANT_RE.sub("", basename.strip()).lower()


# ---------------- HTTP ----------------

def _http_json(url: str, timeout: int = 15, proxy: str = ""):
    """GET JSON：先直连，失败走代理重试一次。

    三种失败都要代理兜底（代理不常驻，只做回退）：
    - ConnectError/Timeout：HF 本机直连超时（2026-11-16 真机实测）；
    - GitHub 403 rate limit：免鉴权 60 次/时/IP，走代理 = 换 IP 配额，能救回整轮。"""
    import requests
    hdrs = {"Accept": "application/json", "User-Agent": "sa-model-watch/1.0"}
    attempts = [{"http": None, "https": None}]
    if proxy:
        attempts.append({"http": proxy, "https": proxy})
    last_exc: Exception | None = None
    for px in attempts:
        via = "direct" if px.get("http") is None else "proxy"
        try:
            r = requests.get(url, timeout=timeout, headers=hdrs, proxies=px)
            if r.status_code == 403 and "rate limit" in r.text.lower()                     and via == "direct" and len(attempts) > 1:
                last_exc = requests.exceptions.HTTPError(
                    f"403 rate limited (direct): {url}")
                continue  # 换代理 IP 重试
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as exc:
            last_exc = exc
            continue  # 直连网络失败 → 代理重试
        except requests.exceptions.HTTPError as exc:
            # HTTP 层错误（404/500 等）不重试——换 IP 也一样，直接抛给上层软处理
            raise
    print(f"[model-watch] 全部尝试失败: {url} ({type(last_exc).__name__}: {last_exc})",
          flush=True)
    raise last_exc if last_exc else RuntimeError(f"unreachable: {url}")


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


# ---------------- GitHub 会话鉴权（kimi-webbridge） ----------------

def gh_bridge_fetch_atoms(repos: list[str], pace_ms: int = 300) -> dict[str, str | None]:
    """经 kimi-webbridge 在用户**已登录**的 github.com 页面里批量抓 releases.atom。

    鉴权原理（与雪球桥 xq_bridge._JS 同模式）：GitHub 会话 cookie 是 HttpOnly，
    JS 读不到、也**不该**提取落盘；但在 github.com 页面上下文里
    `fetch(..., {credentials:'include'})` 会自动带上会话 —— 登录态本身就是鉴权配置，
    永远新鲜、零存储。返回 {full_name: atom_xml 或 None}。"""
    if not repos:
        return {}
    try:
        import xq_bridge
    except Exception as exc:
        print(f"[model-watch] xq_bridge 不可用: {exc}", flush=True)
        return {}
    conf = dict(xq_bridge.DEFAULTS)
    conf.update({"session": "github", "seed_url": "https://github.com/", "timeout": 90})
    js = """(async () => {
      const repos = %s;
      const pace = %d;
      const out = {};
      for (const r of repos) {
        try {
          const resp = await fetch('/' + r + '/releases.atom', {credentials: 'include'});
          out[r] = {http: resp.status, body: resp.ok ? (await resp.text()) : null};
        } catch (e) { out[r] = {http: 0, error: String(e).slice(0, 120)}; }
        await new Promise(s => setTimeout(s, pace));
      }
      return JSON.stringify(out);
    })()""" % (json.dumps(repos, ensure_ascii=False), pace_ms)
    bases = [str(conf["base_url"])]
    if conf.get("fallback_url"):
        bases.append(str(conf["fallback_url"]))
    last: xq_bridge.BridgeError | None = None
    for base in bases:
        for attempt in range(int(conf["retries"]) + 1):
            try:
                xq_bridge.ensure_page(base, conf)
                data = xq_bridge._post(base, {"action": "evaluate",
                                              "args": {"code": js},
                                              "session": conf["session"]},
                                       conf["timeout"])
                rows = json.loads(data.get("value") or "{}")
                return {r: (rows.get(r) or {}).get("body")
                        if (rows.get(r) or {}).get("http") == 200 else None
                        for r in repos}
            except xq_bridge.BridgeError as exc:
                last = exc
                xq_bridge.reset_page(base, conf["session"])
                if exc.reason in ("bridge_down", "captcha"):
                    break      # 换下一个端点 / 重试无用
        if last is None:
            break
    if last:
        print(f"[model-watch] github bridge 拉取失败: {last}", flush=True)
    return {}


def _parse_releases_atom(xml: str, repo: str, code: str,
                         limit: int = 8) -> list[dict]:
    """releases.atom → items（entry 的 title/link/updated）。解析失败返回 []。"""
    import xml.etree.ElementTree as ET
    ns = "{http://www.w3.org/2005/Atom}"
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        print(f"[model-watch] {repo} atom 解析失败: {exc}", flush=True)
        return []
    items = []
    base = _basename(repo)
    for entry in root.findall(ns + "entry")[:max(1, limit)]:
        title = (entry.findtext(ns + "title") or "").strip()
        url = ""
        for link in entry.findall(ns + "link"):
            if (link.get("rel") or "alternate") == "alternate":
                url = link.get("href") or ""
                break
        pub = _norm_pub(entry.findtext(ns + "updated"))
        if not url or not pub:
            continue
        items.append({"code": code,
                      "title": f"[GitHub] {base} {title}".strip()[:200],
                      "url": url, "source": "model", "media": "GitHub",
                      "publish_time": pub})
    return items


# ---------------- 三道痕迹（按主体） ----------------

def fetch_github_releases(subject: dict, org_repos: list[dict],
                          bridge: str = "auto") -> list[dict]:
    """模型仓最近 releases → items。org_repos 由 run_once 传入（org 调用共用）。

    配额纪律（2026-11-16 真机 403 逮到）：免鉴权 GitHub API = 60 次/小时/IP，
    zai-org 的 GLM* 仓库比预想多（GLM-4/5/TTS/ASR/Image/OCR/skills...），
    全轮询一发就顶到墙。三道闸：
    ① 只轮询 **pushed_at ≤90 天** 的仓库（三个月没动静的仓不会突然出 release）；
    ② 403 rate-limit → `github_bridge != "off"` 时经 kimi-webbridge 用登录态抓
       releases.atom（web 端点不吃 API 配额，会话鉴权），"always" 则全程走桥；
    ③ 其余 HTTP 错误软跳过（单行日志 + 计数）。"""
    code = str(subject.get("code") or "").strip()
    limit = max(1, int(subject.get("per_source_limit") or 8))
    pending = []
    for repo in org_repos or []:
        full = repo.get("full_name") or ""
        if not _is_model_trace(full, subject):
            continue
        # 90 天没 push 的仓库跳过（省配额；新 release 必然伴随 push 更新 pushed_at）
        pushed = _norm_pub(repo.get("pushed_at"))
        if pushed and not _within_days(pushed, 90):
            continue
        pending.append(full)
    if not pending:
        return []
    bridge = str(bridge or "auto").lower()
    if bridge == "always":
        atoms = gh_bridge_fetch_atoms(pending)
        out = []
        for full, xml in atoms.items():
            out += _parse_releases_atom(xml or "", full, code, limit)
        return out

    items = []
    rate_limited = False
    failed = 0
    done: list[str] = []
    for full in pending:
        try:
            rels = _http_json(
                f"https://api.github.com/repos/{full}/releases?per_page={limit}",
                proxy=str(subject.get("proxy") or ""))
        except Exception as exc:
            if "rate limit" in str(exc).lower():
                rate_limited = True
                break                      # 换桥把剩余仓库一次拉完
            failed += 1
            if failed == 1:
                print(f"[model-watch] {full} releases 拉取失败（后续同类只计数）: "
                      f"{type(exc).__name__}: {exc}", flush=True)
            continue
        done.append(full)
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
                              "url": url, "source": "model", "media": "GitHub",
                              "publish_time": pub})
    if failed > 1:
        print(f"[model-watch] 另有 {failed - 1} 个仓库 releases 拉取失败", flush=True)
    if rate_limited and bridge == "auto":
        remaining = [r for r in pending if r not in done]
        print(f"[model-watch] GitHub API 403 rate limit → 经 webbridge 登录态抓 "
              f"{len(remaining)} 个仓库的 releases.atom", flush=True)
        atoms = gh_bridge_fetch_atoms(remaining)
        for full, xml in atoms.items():
            items += _parse_releases_atom(xml or "", full, code, limit)
    return items


def fetch_github_new_repos(subject: dict, org_repos: list[dict]) -> list[dict]:
    """新模型仓库上线（created_at ≤7 天且命中 pattern）= 新模型发布前兆。
    created_at 更旧的仓库没有任何决策价值，不报（因此无需 seen 状态）。"""
    code = str(subject.get("code") or "").strip()
    org = str(subject.get("github_org") or "").strip()
    items = []
    for repo in org_repos or []:
        full = repo.get("full_name") or ""
        if not _is_model_trace(full, subject):
            continue
        pub = _norm_pub(repo.get("created_at"))
        if not _within_days(pub, 7):
            continue
        base = _basename(full)
        label = str(subject.get("label") or org)
        items.append({"code": code,
                      "title": f"[GitHub] {label} 新模型仓库上线：{base}（发布前兆）"[:200],
                      "url": f"https://github.com/{full}",
                      "source": "model", "media": "GitHub", "publish_time": pub})
    return items


def fetch_hf_models(subject: dict, seen: dict) -> tuple[list[dict], dict, dict]:
    """HF 新模型上传 → items。量化变体按 base 名去重（状态按主体分桶）。
    返回 (items, new_seen, stats)：**只有在 save_to_db 成功后才写回 new_seen**，
    落库失败下轮自然重试。"""
    code = str(subject.get("code") or "").strip()
    author = str(subject.get("hf_author") or "").strip()
    limit = max(1, int(subject.get("per_source_limit") or 8))
    seen = dict(seen or {})
    if not author or not code:
        return [], seen, {}
    try:
        models = _http_json(
            f"https://huggingface.co/api/models?author={author}&sort=lastModified&limit={limit}",
            proxy=str(subject.get("proxy") or ""))
    except Exception as exc:
        print(f"[model-watch] HF {author} 拉取失败: {type(exc).__name__}: {exc}", flush=True)
        return [], seen, {"error": str(exc)}
    items = []
    for m in models if isinstance(models, list) else []:
        mid = m.get("modelId") or ""
        base_name = _basename(mid)
        if not _is_model_trace(mid, subject):
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
                      "url": url, "source": "model", "media": "HuggingFace",
                      "publish_time": pub})
        seen[base] = {"url": url, "date": pub}
    return items, seen, {}


def run_once(deps: dict) -> dict:
    """逐主体拉三道痕迹 → news_fetcher.save_to_db。返回统计。"""
    conf = load_conf()
    g = conf["global"]
    if not g.get("enabled", True):
        return {"ok": False, "skipped": "disabled"}

    state = _load_state()
    results = []
    total_inserted = 0
    total_notified = 0
    ok = True
    for s in conf["subjects"]:
        key = s["key"]
        code = str(s.get("code") or "").strip()
        if not code:
            results.append({"key": key, "skipped": "code empty"})
            continue
        # org repos 拉一次，release 发现与新仓库发现共用（省 API 配额）
        org = str(s.get("github_org") or "").strip()
        org_repos = None
        if org:
            try:
                org_repos = _http_json(
                    f"https://api.github.com/orgs/{org}/repos?per_page=100&sort=pushed",
                    proxy=str(g.get("proxy") or ""))
                if not isinstance(org_repos, list):
                    org_repos = None
            except Exception as exc:
                print(f"[model-watch] {org} repos 拉取失败: {type(exc).__name__}: {exc}",
                      flush=True)

        items = fetch_github_releases(s, org_repos,
                                      bridge=str(g.get("github_bridge") or "auto")) + \
            fetch_github_new_repos(s, org_repos)
        hf_items, new_seen, hf_err = fetch_hf_models(
            s, (state.get("seen_hf") or {}).get(key) or {})
        items += hf_items
        sres = {"key": key, "items": len(items), "inserted": 0, "notified": 0}
        if hf_err:
            sres["error"] = f"hf: {hf_err}"
            ok = False
        if items:
            try:
                import news_fetcher
                inserted = news_fetcher.save_to_db(items)
                sres["inserted"] = inserted
                total_inserted += inserted
                # 落库成功才更新该主体的 HF 变体状态（失败下轮重试同一批）
                bucket = state.setdefault("seen_hf", {})
                bucket[key] = new_seen
                _save_state(state)
            except Exception as exc:
                sres["error"] = f"{type(exc).__name__}: {exc}"
                ok = False
                with LOCK:
                    _state["last_error"] = f"{key}: {type(exc).__name__}: {exc}"
                print(f"[model-watch] {key} save_to_db 失败: "
                      f"{type(exc).__name__}: {exc}", flush=True)
            else:
                if sres["inserted"] and g.get("notify_new") and \
                        callable(deps.get("notify_fn")):
                    for it in items[:sres["inserted"]]:
                        try:
                            deps["notify_fn"](f"[模型发布] {it['title']} {it['url']}")
                            sres["notified"] += 1
                            total_notified += 1
                        except Exception:
                            pass
        results.append(sres)

    with LOCK:
        _state["last_run"] = datetime.now().isoformat(timespec="seconds")
        _state["last_error"] = None if ok else "见 subjects.error"
        _state["last_inserted"] = total_inserted
    return {"ok": ok, "inserted": total_inserted, "notified": total_notified,
            "subjects": results}
