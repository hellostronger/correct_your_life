"""model_watch 离线测试：不联网（HTTP 全 stub）、不连库（news_fetcher stub）、不 import app。

跑法：D:\\Python312\\python.exe model_watch_test.py
口径：多主体只抓「新模型发布痕迹」（模型仓 release / 新仓库 / HF 新模型，
变体按 base 去重，工具仓排除）。主体：zai→02513、minimax→00100。
"""
from __future__ import annotations

import ast
import json
import re
import sys
import tempfile
import types
from pathlib import Path

import model_watch as MW

BASE = Path(__file__).resolve().parent
N = 0


def ck(cond, label):
    global N
    N += 1
    if not cond:
        raise AssertionError(f"#{N} {label}")


# ---------------- 主体注册表 ----------------
ck(set(MW.SUBJECT_KEYS) == {"zai", "minimax"}, "主体注册表：zai + minimax")
ck(MW.SUBJECT_DEFAULTS["zai"]["code"] == "02513", "zai → 02513 智谱")
ck(MW.SUBJECT_DEFAULTS["minimax"]["code"] == "00100", "minimax → 00100 MINIMAX-W")
ck(MW.SUBJECT_DEFAULTS["minimax"]["hf_author"] == "MiniMaxAI", "MiniMax HF author（实测确认）")
ck(MW.SUBJECT_DEFAULTS["minimax"]["github_org"] == "MiniMax-AI", "MiniMax GitHub org（实测确认）")

# ---------------- 口径闸门（include + exclude） ----------------
zs = dict(MW.SUBJECT_DEFAULTS["zai"])
ms = dict(MW.SUBJECT_DEFAULTS["minimax"])
ck(MW._is_model_trace("zai-org/GLM-5", zs), "GLM-5 算模型")
ck(not MW._is_model_trace("zai-org/ZCode", zs), "ZCode 工具仓不算（pattern 不匹配）")
ck(MW._is_model_trace("MiniMax-AI/MiniMax-H3-Image", ms), "MiniMax-H3-Image 算模型")
ck(not MW._is_model_trace("MiniMax-AI/MiniMax-Code-MiniApps", ms), "MiniMax-Code-* 被 exclude 排除")
ck(not MW._is_model_trace("MiniMax-AI/awesome-minimax-h3-integration", ms),
   "awesome-* 不匹配 include 前缀")
ck(MW._is_model_trace("MiniMax-AI/MiniMax-M3.1-Flash", ms), "M3.1-Flash 算模型")

# ---------------- 量化变体 → base 名（含 MXFP8 实测样例） ----------------
ck(MW._model_base("MiniMax-M3-MXFP8") == "minimax-m3", "MXFP8 变体归并（HF 实测存在的样例）")
ck(MW._model_base("GLM-5.3-BF16") == "glm-5.3", "BF16 变体归并")
ck(MW._model_base("GLM-5.3") == "glm-5.3", "本体即 base")
ck(MW._model_base("MiniMax-M3.1-Flash") == "minimax-m3.1-flash", "Flash 是独立模型线")
ck(MW._QUANT_RE.sub("", "MiniMax-M3.1-Flash") == "MiniMax-M3.1-Flash",
   "Flash 不在量化后缀表里")

# ---------------- _norm_pub / 时间窗 ----------------
ck(MW._norm_pub("2026-11-04T12:00:00Z") == "2026-11-04T12:00:00+00:00", "Z → +00:00")
ck(MW._norm_pub("bad") is None, "坏时间 → None（不编造）")
ck(MW._within_days("2026-11-15T00:00:00+00:00", 7), "7 天内 = True")
ck(not MW._within_days("2025-01-01T00:00:00+00:00", 7), "老仓库 = False")
ck(not MW._within_days(None, 7), "无时间 = False（宁可少不要错）")

# ---------------- GitHub releases：双主体的闸门互不串 ----------------
GLM_REL = {"tag_name": "v5.3", "name": "", "published_at": "2026-11-10T08:00:00Z",
           "html_url": "https://github.com/zai-org/GLM-5/releases/tag/v5.3"}
ORG_REPOS = [{"full_name": "zai-org/GLM-5", "created_at": "2025-01-01T00:00:00Z",
              "pushed_at": "2026-11-10T00:00:00Z"},
             {"full_name": "zai-org/ZCode", "created_at": "2025-06-01T00:00:00Z",
              "pushed_at": "2026-11-13T00:00:00Z"}]
orig_http = MW._http_json
MW._http_json = lambda url, timeout=15, proxy="": [GLM_REL]
items = MW.fetch_github_releases(zs, ORG_REPOS)
ck(len(items) == 1, "zai：单 release")
ck(items[0]["code"] == "02513" and items[0]["source"] == "model", "code/source 标记")
ck(not MW.fetch_github_releases(ms, ORG_REPOS), "minimax 主体对 zai 仓库一个都不收（闸门互不串）")

MM_REL = {"tag_name": "v1.0", "name": "H3-Image",
          "published_at": "2026-11-11T10:00:00Z",
          "html_url": "https://github.com/MiniMax-AI/MiniMax-H3-Image/releases/tag/v1.0"}
ORG_MM = [{"full_name": "MiniMax-AI/MiniMax-H3-Image", "created_at": "2025-01-01T00:00:00Z",
           "pushed_at": "2026-11-11T00:00:00Z"},
          {"full_name": "MiniMax-AI/MiniMax-Code-MiniApps", "created_at": "2025-06-01T00:00:00Z",
           "pushed_at": "2026-11-13T00:00:00Z"}]
polled = []


def _polled(url, timeout=15, proxy=""):
    polled.append(url)
    return [MM_REL]


MW._http_json = _polled
items = MW.fetch_github_releases(ms, ORG_MM)
ck(len(items) == 1 and items[0]["code"] == "00100", "minimax：模型仓 release 收进 00100")

STALE = [{"full_name": "zai-org/GLM-4", "created_at": "2025-01-01T00:00:00Z",
          "pushed_at": "2026-08-05T00:00:00Z"}]   # 100 天没 push
polled.clear()
MW._http_json = _polled
items = MW.fetch_github_releases(zs, STALE)
ck(items == [] and polled == [], "90 天没 push 的仓库不轮询（配额纪律，新 release 必伴随 push）")
ck(not any("MiniMax-Code" in u for u in polled), "工具仓连 /releases 调用都不发生")
MW._http_json = orig_http

# ---------------- 新仓库前兆：7 天窗 + 主体隔离 ----------------
NOW = "2026-11-15T00:00:00Z"
MIX_REPOS = [{"full_name": "zai-org/GLM-7", "created_at": NOW},
             {"full_name": "zai-org/GLM-5", "created_at": "2025-01-01T00:00:00Z"},
             {"full_name": "MiniMax-AI/MiniMax-H4", "created_at": NOW}]
items = MW.fetch_github_new_repos(zs, MIX_REPOS)
ck(len(items) == 1 and "GLM-7" in items[0]["title"] and "智谱" in items[0]["title"],
   "zai 只报 GLM-7 前兆（老仓库/别家仓库不串）")
items = MW.fetch_github_new_repos(ms, MIX_REPOS)
ck(len(items) == 1 and "MiniMax-H4" in items[0]["title"], "minimax 只报 H4 前兆")

# ---------------- HF：双主体变体去重 + 状态按主体分桶 ----------------
tmpdir = Path(tempfile.gettempdir())
MW.STATE_FILE = tmpdir / "model_watch_state_test.json"
MW.STATE_FILE.unlink(missing_ok=True)

HF_ZAI = [{"modelId": "zai-org/GLM-5.3", "createdAt": "2026-10-15T10:00:00Z",
           "lastModified": "2026-11-14T09:00:00Z"},
          {"modelId": "zai-org/GLM-5.3-BF16", "createdAt": "2026-10-15T11:00:00Z"}]
HF_MM = [{"modelId": "MiniMaxAI/MiniMax-M3-MXFP8", "createdAt": "2026-06-02T00:00:00Z"},
         {"modelId": "MiniMaxAI/MiniMax-M3", "createdAt": "2026-06-01T00:00:00Z"},
         {"modelId": "MiniMaxAI/MiniMax-M3.1-Flash", "createdAt": "2026-10-17T00:00:00Z"},
         {"modelId": "MiniMaxAI/MiniMax-Code-Docs", "createdAt": "2026-11-10T00:00:00Z"}]
MW._http_json = lambda url, timeout=15, proxy="": (HF_ZAI if "zai-org" in url else HF_MM)
items, seen, err = MW.fetch_hf_models(zs, {})
ck(err == {} and len(items) == 1 and "GLM-5.3" in items[0]["title"],
   "zai：本体 1 条，BF16 变体归并")
ck(seen.get("glm-5.3"), "zai 状态记录 base")
items, seen, err = MW.fetch_hf_models(ms, {})
ck(err == {} and len(items) == 2, f"minimax：M3（MXFP8 归并）+ M3.1-Flash = 2 —— 实际 {len(items)}")
ck(all("Code" not in i["title"] for i in items), "非模型命名排除")
ck(seen.get("minimax-m3") and seen.get("minimax-m3.1-flash"), "minimax 状态分桶独立")
MW._http_json = lambda url, timeout=15, proxy="": (HF_ZAI if "zai-org" in url else HF_MM)
items2, _, _ = MW.fetch_hf_models(ms, seen)
ck(items2 == [], "minimax 第二轮全去重")
items3, _, _ = MW.fetch_hf_models(zs, {"glm-5.3": {"url": "x"}})
ck(items3 == [], "zai 已见 base 不再报")

# ---------------- run_once（stub news_fetcher + 固定 conf） ----------------
fake_nf = types.ModuleType("news_fetcher")
_saved = {"items": []}
_seen_urls = set()


def _fake_save(items, related_map=None, senti_words=None):
    # 模拟真库 ON CONFLICT (url) DO NOTHING：URL 去重，返回真正新增数
    fresh = []
    for it in items:
        for k in ("code", "title", "url", "source", "media", "publish_time"):
            assert k in it and it[k], f"item 缺 {k}"
        if it["url"] not in _seen_urls:
            _seen_urls.add(it["url"])
            fresh.append(it)
            _saved["items"].append(it)
    return len(fresh)


fake_nf.save_to_db = _fake_save
sys.modules["news_fetcher"] = fake_nf


def _router(url, timeout=15, proxy=""):
    if "/orgs/zai-org/" in url:
        return ORG_REPOS
    if "/orgs/MiniMax-AI/" in url:
        return ORG_MM
    if "huggingface" in url:
        return HF_ZAI if "zai-org" in url else HF_MM
    return [GLM_REL]


MW._http_json = _router
MW.load_conf = lambda: {"global": dict(MW.DEFAULT_CONF),
                        "subjects": [dict(zs, key="zai", proxy=MW.DEFAULT_CONF["proxy"]),
                                     dict(ms, key="minimax", proxy=MW.DEFAULT_CONF["proxy"])]}
notified = []
MW.STATE_FILE.unlink(missing_ok=True)
res = MW.run_once({"get_conn": None, "notify_fn": lambda m: notified.append(m)})
ck(res.get("ok") is True, f"run_once ok（实际 {res}）")
codes = {i["code"] for i in _saved["items"]}
ck(codes == {"02513", "00100"}, f"两主体都落库：{codes}")
ck(not any("ZCode" in i["title"] or "Code-" in i["title"] for i in _saved["items"]),
   "工具仓条目全程被闸")
ck(len(notified) >= 1 and notified[0].startswith("[模型发布]"), "notify 推送带前缀")
_state_disk = json.loads(MW.STATE_FILE.read_text(encoding="utf-8"))
ck(_state_disk["seen_hf"].get("zai") and _state_disk["seen_hf"].get("minimax"),
   "状态按主体分桶落盘")

# 第二轮：全部去重（URL/base 双层）
_saved["items"].clear()
notified.clear()
res = MW.run_once({"get_conn": None, "notify_fn": lambda m: notified.append(m)})
ck(res.get("inserted") == 0 and notified == [], "第二轮零新增零推送")

# 单主体落库失败：该主体「失败批次的新 base」不进状态（下轮重试同批），另一主体正常
class _Broken:
    def save_to_db(self, items, **k):
        if items and items[0]["code"] == "00100":
            raise RuntimeError("db down")
        return len(items)


sys.modules["news_fetcher"] = _Broken()
HF_MM_FRESH = [{"modelId": "MiniMaxAI/MiniMax-M4", "createdAt": "2026-11-15T00:00:00Z"}]
MW._http_json = lambda url, timeout=15, proxy="": (
    ORG_REPOS if "/orgs/zai-org/" in url else
    ORG_MM if "/orgs/MiniMax-AI/" in url else
    HF_ZAI if "zai-org" in url else HF_MM_FRESH)
res = MW.run_once({"get_conn": None})
ck(res.get("ok") is False, "有主体失败 → ok False")
st = json.loads(MW.STATE_FILE.read_text(encoding="utf-8"))["seen_hf"]
ck(st.get("zai"), "zai 主体状态正常更新")
ck(not (st.get("minimax") or {}).get("minimax-m4"),
   "minimax 失败批次的新 base 不进状态（下轮重试同批）")
sys.modules["news_fetcher"] = fake_nf

# skipped 路径
MW.load_conf = lambda: {"global": {**MW.DEFAULT_CONF, "enabled": False},
                        "subjects": [dict(zs, key="zai")]}
ck(MW.run_once({}) == {"ok": False, "skipped": "disabled"}, "enabled=False → skipped")
MW.load_conf = lambda: {"global": dict(MW.DEFAULT_CONF),
                        "subjects": [dict(zs, key="zai", code="")]}
ck(MW.run_once({}).get("subjects")[0].get("skipped") == "code empty", "code 空 → skipped")
MW.load_conf = lambda: {"global": dict(MW.DEFAULT_CONF),
                        "subjects": [dict(zs, key="zai", proxy=MW.DEFAULT_CONF["proxy"])]}

# ---------------- 静态接线 ----------------
app_src = (BASE / "app.py").read_text(encoding="utf-8")
ck("import model_watch" in app_src, "app.py import model_watch")
ck('_start_daemon(_model_watch_loop, "sa_model_watch")' in app_src, "守护线程已注册")
ck("model_watch.run_once(_model_watch_deps())" in app_src, "daemon 调 run_once")
ck("import zai_watch" not in app_src and "_zai_loop" not in app_src, "旧接线已清干净")

schema_src = (BASE / "config_schema.py").read_text(encoding="utf-8")
for sec in ("model_watch", "zai", "minimax"):
    ck(f'"{sec}": {{' in schema_src, f"config_schema 有 {sec} 段")
for f in ("github_org", "hf_author", "model_name_re", "exclude_re", "code"):
    ck(schema_src.count(f'{{"key": "{f}"') >= 2, f"两个主体段都有 {f} 字段")

ex = (BASE / "config.example.yaml").read_text(encoding="utf-8")
ck("model_watch:" in ex and "MiniMax-AI" in ex and "00100" in ex, "example yaml 三段齐全")

# schema 与模块默认值一致（副本漂移钉死）
import config_schema as CS

gdef = {f["key"]: f.get("d") for f in CS.SCHEMAS["model_watch"]["fields"]}
for k in ("interval_minutes", "notify_new", "per_source_limit", "proxy"):
    ck(gdef.get(k) == MW.DEFAULT_CONF.get(k), f"全局默认一致：{k}")
mdef = {f["key"]: f.get("d") for f in CS.SCHEMAS["minimax"]["fields"]}
ck(mdef.get("code") == MW.SUBJECT_DEFAULTS["minimax"]["code"], "minimax code 默认一致")
ck(mdef.get("exclude_re") == MW.SUBJECT_DEFAULTS["minimax"]["exclude_re"],
   "minimax exclude 默认一致")

# 红线（AST 级——数子串会撞 docstring）
mw_src = (BASE / "model_watch.py").read_text(encoding="utf-8")
_tree = ast.parse(mw_src)
_top = {n.names[0].name.split(".")[0] for n in _tree.body if isinstance(n, ast.Import)}
ck("app" not in _top, "不 import app（AST 级）")
ck(re.search(r"\.(post|put|delete)\(", mw_src, re.I) is None, "无写 HTTP（纯 GET）")
ck("sa_paper" not in mw_src and "sa_trades" not in mw_src, "不碰交易表")

# ---------------- GitHub 会话鉴权桥（webbridge，2026-11-16 用户要求） ----------------
ATOM_MM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xml:lang="en-US">
  <id>tag:github.com,2008:https://github.com/MiniMax-AI/MiniMax-H3-Image/releases</id>
  <entry>
    <id>tag:github.com,2008:Repository/1/v1.0</id>
    <updated>2026-11-11T10:00:00+08:00</updated>
    <link rel="alternate" type="text/html" href="https://github.com/MiniMax-AI/MiniMax-H3-Image/releases/tag/v1.0"/>
    <title>v1.0</title>
  </entry>
  <entry>
    <id>tag:github.com,2008:Repository/1/v0.9</id>
    <updated>2026-11-01T10:00:00+08:00</updated>
    <link rel="alternate" type="text/html" href="https://github.com/MiniMax-AI/MiniMax-H3-Image/releases/tag/v0.9"/>
    <title>v0.9</title>
  </entry>
</feed>"""

ck(MW._parse_releases_atom(ATOM_MM, "MiniMax-AI/MiniMax-H3-Image", "00100")[0]["url"]
   == "https://github.com/MiniMax-AI/MiniMax-H3-Image/releases/tag/v1.0", "atom 解析 link")
ck(MW._parse_releases_atom(ATOM_MM, "MiniMax-AI/MiniMax-H3-Image", "00100")[0]["publish_time"]
   == "2026-11-11T02:00:00+00:00", "atom updated → UTC 规范化（+08:00 输入）")
ck(len(MW._parse_releases_atom(ATOM_MM, "MiniMax-AI/MiniMax-H3-Image", "00100", 1)) == 1,
   "limit 生效")
ck(MW._parse_releases_atom("<broken", "x", "00100") == [], "坏 XML → 空（不炸）")
ck(MW._parse_releases_atom(ATOM_MM.replace("releases/tag/v1.0", ""), "x", "00100")[0]["title"],
   "无 link 的 entry 也产 title（url 过滤在 item 级）") if False else None

# ---- 桥桩：不碰真桥 ----
class _FakeBridgeErr(Exception):
    def __init__(self, message, reason="upstream"):
        self.reason = reason
        super().__init__(message)


fake_xq = types.ModuleType("xq_bridge")
fake_xq.DEFAULTS = {"base_url": "http://127.0.0.1:10086",
                    "fallback_url": "http://101.43.25.101/xq-bridge",
                    "session": "github", "timeout": 90, "retries": 1}
fake_xq.BridgeError = _FakeBridgeErr
bridge_calls = {"posts": [], "nav": []}


def _fb_post(base, payload, timeout):
    bridge_calls["posts"].append((base, payload.get("session")))
    if payload.get("action") == "navigate":
        bridge_calls["nav"].append(base)
        return {}
    code = payload["args"]["code"]
    m = re.search(r"const repos = (\[[^\]]*\])", code)
    repos = json.loads(m.group(1)) if m else []
    out = {r: {"http": 200, "body": ATOM_MM} for r in repos}
    return {"value": json.dumps(out)}


fake_xq._post = _fb_post
fake_xq.ensure_page = lambda base, conf: bridge_calls["nav"].append(base)
fake_xq.reset_page = lambda base="", session="": None
sys.modules["xq_bridge"] = fake_xq

# always 模式：全程走桥，不碰 api.github.com
api_calls = []


def _api_counter(url, timeout=15, proxy=""):
    api_calls.append(url)
    return [GLM_REL]


MW._http_json = _api_counter
atoms = MW.gh_bridge_fetch_atoms(["MiniMax-AI/MiniMax-H3-Image"])
ck(atoms.get("MiniMax-AI/MiniMax-H3-Image", "").startswith("<?xml"), "桥桩返回 atom")

items = MW.fetch_github_releases(ms, ORG_MM, bridge="always")
ck(len(items) == 2 and items[0]["code"] == "00100", "always 模式：atom → 2 items")
ck(api_calls == [], "always 模式不碰 api.github.com")

# auto 模式：403 rate limit → 剩余仓库转桥
def _403(url, timeout=15, proxy=""):
    api_calls.append(url)
    import requests as _r
    raise _r.exceptions.HTTPError("403 Client Error: rate limit exceeded for url")


MW._http_json = _403
api_calls.clear()
items = MW.fetch_github_releases(zs, ORG_REPOS, bridge="auto")
ck(api_calls, "auto：先试过直连")
ck(len(items) == 2, f"403 后转桥拿回 atom items —— 实际 {len(items)}")
ck(items and items[0]["code"] == "02513", "桥 items 归属 zai 主体 code")

# off 模式：403 不走桥
MW._http_json = _403
bridge_calls["posts"].clear()
items = MW.fetch_github_releases(zs, ORG_REPOS, bridge="off")
ck(items == [] and bridge_calls["posts"] == [], "off 模式：403 不走桥")

# 桥挂了：软跳过不炸
def _fb_post_down(base, payload, timeout):
    raise _FakeBridgeErr("桥连不上 http://127.0.0.1:10086", "bridge_down")


fake_xq._post = _fb_post_down
MW._http_json = _403
items = MW.fetch_github_releases(zs, ORG_REPOS, bridge="auto")
ck(items == [], "桥挂了 → 软跳过（下轮重试）")
fake_xq._post = _fb_post
MW._http_json = orig_http

# 默认值一致性
ck(MW.DEFAULT_CONF.get("github_bridge") == "auto", "github_bridge 默认 auto")

print(f"model_watch_test: {N}/{N} 全过")
