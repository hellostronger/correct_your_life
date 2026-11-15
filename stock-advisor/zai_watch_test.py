"""zai_watch 离线测试：不联网（HTTP 全 stub）、不连库（news_fetcher stub）、不 import app。

跑法：D:\\Python312\\python.exe zai_watch_test.py
口径：只抓「新模型发布痕迹」（GLM* release / GLM* 新仓库 / HF 新模型，变体按 base 去重）。
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import types
from pathlib import Path

import zai_watch as ZW

BASE = Path(__file__).resolve().parent
N = 0


def ck(cond, label):
    global N
    N += 1
    if not cond:
        raise AssertionError(f"#{N} {label}")


# ---------------- 口径闸门：什么算「模型痕迹」 ----------------
pat = ZW.DEFAULT_CONF["model_name_re"]
ck(ZW._is_model_trace("zai-org/GLM-5", pat), "GLM-5 算模型")
ck(ZW._is_model_trace("zai-org/GLM-5.3-Flash", pat), "GLM-5.3-Flash 算模型")
ck(ZW._is_model_trace("zai-org/glm-ocr", pat), "小写也命中（IGNORECASE）")
ck(not ZW._is_model_trace("zai-org/ZCode", pat), "ZCode 工具仓不算（用户纠正的瞎监控）")
ck(not ZW._is_model_trace("zai-org/zcode-plugins", pat), "插件仓不算")
ck(not ZW._is_model_trace("zai-org/feedback", pat), "反馈仓不算")
ck(not ZW._is_model_trace("zai-org/CogAgent", pat), "非 GLM 命名默认不算（可扩 pattern）")
ck(not ZW._is_model_trace("zai-org/GLM-5", ""), "pattern 空 → 一律不算（宁缺毋滥）")
ck(not ZW._is_model_trace("zai-org/GLM-5", "([broken"), "坏正则容错 → False")

# ---------------- 量化变体 → base 名 ----------------
ck(ZW._model_base("GLM-5.3-BF16") == "glm-5.3", "BF16 变体归并")
ck(ZW._model_base("GLM-5.3-FP8") == "glm-5.3", "FP8 变体归并")
ck(ZW._model_base("GLM-5.3-GGUF") == "glm-5.3", "GGUF 变体归并")
ck(ZW._model_base("glm-5.3-Int4") == "glm-5.3", "Int4 小写变体归并")
ck(ZW._model_base("GLM-5.3") == "glm-5.3", "本体即 base")
ck(ZW._model_base("GLM-5.3-Flash") == "glm-5.3-flash", "Flash 是独立模型线，不并进本体")

# ---------------- _norm_pub ----------------
ck(ZW._norm_pub("2026-11-04T12:00:00Z") == "2026-11-04T12:00:00+00:00", "Z → +00:00")
ck(ZW._norm_pub(None) is None and ZW._norm_pub("") is None, "空 → None")
ck(ZW._norm_pub("not-a-date") is None, "坏时间 → None（不编造）")

# ---------------- GitHub releases：模式闸门在 fetch 前生效 ----------------
GLM_REL = {"tag_name": "v5.3", "name": "GLM-5.3 release",
           "published_at": "2026-11-10T08:00:00Z", "created_at": "2026-11-10T07:00:00Z",
           "html_url": "https://github.com/zai-org/GLM-5/releases/tag/v5.3"}
ORG_REPOS = [
    {"full_name": "zai-org/GLM-5", "created_at": "2025-01-01T00:00:00Z"},
    {"full_name": "zai-org/ZCode", "created_at": "2025-06-01T00:00:00Z"},   # 工具仓
]
CONF = dict(ZW.DEFAULT_CONF)

polled = []
def _stub_github(url, timeout=15):
    polled.append(url)
    if "/orgs/" in url:
        return ORG_REPOS
    return [GLM_REL]
ZW._http_json = _stub_github
items = ZW.fetch_github_releases(CONF, ORG_REPOS)
ck(len(items) == 1, f"只有 GLM-5 被轮询（ZCode 被闸掉）——实际 {len(items)}")
ck(not any("ZCode" in u for u in polled), "ZCode 的 /releases 调用都没发生")
it = items[0]
for k in ("code", "title", "url", "source", "media", "publish_time"):
    ck(k in it and it[k], f"item 字段 {k}（save_to_db 契约）")
ck(it["code"] == "02513" and it["source"] == "zai" and it["media"] == "GitHub",
   "code/source/media 标记")
ck(it["title"].count("v5.3") == 1, "name==tag 时标题不重复")
ck(it["publish_time"].startswith("2026-11-10"), "publish_time 取 published_at")
ck(not ZW.fetch_github_releases({**CONF, "code": ""}, ORG_REPOS), "code 空 → 不产 items")
ck(not ZW.fetch_github_releases({**CONF, "model_name_re": ""}, ORG_REPOS),
   "pattern 空 → 不产 items（宁缺毋滥）")

# ---------------- GitHub 新仓库：只报 created_at ≤7 天的 GLM* ----------------
NOW_REPOS = [
    {"full_name": "zai-org/GLM-6", "created_at": "2026-11-15T00:00:00Z"},   # 新模型前兆
    {"full_name": "zai-org/GLM-5", "created_at": "2025-01-01T00:00:00Z"},   # 老仓库
    {"full_name": "zai-org/ZCode", "created_at": "2026-11-15T00:00:00Z"},   # 新但非模型
]
items = ZW.fetch_github_new_repos(CONF, NOW_REPOS)
ck(len(items) == 1 and "GLM-6" in items[0]["title"] and "前兆" in items[0]["title"],
   "新 GLM 仓库 = 发布前兆；老仓库/新工具仓都不报")

# ---------------- HF：模式闸门 + 变体按 base 去重（状态侧车） ----------------
tmpdir = Path(tempfile.gettempdir())
ZW.STATE_FILE = tmpdir / "zai_watch_state_test.json"
ZW.STATE_FILE.unlink(missing_ok=True)

HF_MODELS = [
    {"modelId": "zai-org/GLM-5.3", "createdAt": "2026-10-15T10:00:00.000Z",
     "lastModified": "2026-11-14T09:00:00.000Z"},
    {"modelId": "zai-org/GLM-5.3-BF16", "createdAt": "2026-10-15T11:00:00.000Z"},
    {"modelId": "zai-org/GLM-5.3-Flash", "createdAt": "2026-10-15T12:00:00.000Z"},
    {"modelId": "zai-org/ZCode-Tool", "createdAt": "2026-11-13T00:00:00Z"},  # 非 GLM 命名
    {"modelId": "", "createdAt": "2026-11-01T00:00:00Z"},                    # 坏行
]
ZW._http_json = lambda url, timeout=15: HF_MODELS
items, st = ZW.fetch_hf_models(CONF)
ck(len(items) == 2, f"本体+Flash=2；BF16 变体归并、非 GLM 排除、坏行跳过——实际 {len(items)}")
ck(items[0]["publish_time"].startswith("2026-10-15"), "createdAt 优先于 lastModified"
   "（lastModified 是 push 时间，当发布时间会每次 push 都假报新发布）")
ck(len(st["seen_hf"]) == 2 and "glm-5.3" in st["seen_hf"], "状态记录 base 首见")
# 第二轮：同 base（含变体）不再报
ZW._http_json = lambda url, timeout=15: HF_MODELS
items2, st2 = ZW.fetch_hf_models(CONF, st)
ck(items2 == [], "第二轮同 base 全部去重（跨 URL，sa_news 管不到这层）")
# 变体先行也算首见
items3, st3 = ZW.fetch_hf_models(CONF, {})
ZW._http_json = lambda url, timeout=15: [
    {"modelId": "zai-org/GLM-9-INT4", "createdAt": "2026-11-15T00:00:00Z"}]
items4, _ = ZW.fetch_hf_models(CONF, st3)
ck(len(items4) == 1 and "GLM-9-INT4" in items4[0]["title"],
   "量化变体先行 = 该模型首见，照报（base 归并只去重后续）")

# ---------------- run_once（stub news_fetcher 整个模块 + 固定 conf） ----------------
fake_nf = types.ModuleType("news_fetcher")
_saved = {"items": None}


def _fake_save(items, related_map=None, senti_words=None):
    _saved["items"] = items
    for it in items:
        for k in ("code", "title", "url", "source", "media", "publish_time"):
            assert k in it and it[k], f"item 缺 {k}"
    return 2


fake_nf.save_to_db = _fake_save
sys.modules["news_fetcher"] = fake_nf

def _router(url, timeout=15):
    if "/orgs/" in url:
        return NOW_REPOS
    if "huggingface" in url:
        return HF_MODELS
    return [GLM_REL]

ZW._http_json = _router
FIX = {**CONF, "notify_new": False}
ZW.load_conf = lambda: dict(FIX)
notified = []
res = ZW.run_once({"get_conn": None, "notify_fn": lambda m: notified.append(m)})
ck(res.get("ok") is True, f"run_once ok（实际 {res}）")
ck(_saved["items"], "items 进入 save_to_db")
ck(all(i["code"] == "02513" for i in _saved["items"]), "全部关联 02513")
ck(not any("ZCode" in i["title"] for i in _saved["items"]),
   "工具仓条目被口径闸门挡在 save_to_db 之外")
ck(notified == [], "notify_new=False 不推")
ck(json.loads(ZW.STATE_FILE.read_text(encoding="utf-8")).get("seen_hf"),
   "落库成功才写状态侧车")

# notify_new=True：新痕迹推微信
ZW.load_conf = lambda: {**FIX, "notify_new": True, "github_org": "zai-org",
                        "hf_author": "x-none"}   # hf 拉失败也要能推 github 部分
def _gh_only(url, timeout=15):
    if "/orgs/" in url:
        return NOW_REPOS
    if "huggingface" in url:
        raise ConnectionError("hf down")
    return [GLM_REL]
ZW._http_json = _gh_only
notified.clear()
res = ZW.run_once({"get_conn": None, "notify_fn": lambda m: notified.append(m)})
ck(res.get("ok") is True, "单源失败不炸整体")
ck(len(notified) >= 1 and notified[0].startswith("[智谱动态]"), "推送带前缀")

# save_to_db 异常 → ok False + last_error，且状态侧车不更新
before = ZW.STATE_FILE.read_text(encoding="utf-8") if ZW.STATE_FILE.exists() else ""
class _Broken:
    def save_to_db(self, *a, **k):
        raise RuntimeError("db down")
sys.modules["news_fetcher"] = _Broken()
ZW._http_json = _gh_only
res = ZW.run_once({"get_conn": None})
ck(res.get("ok") is False and "db down" in res.get("error", ""), "落库异常不炸")
ck("db down" in (ZW.get_status().get("last_error") or ""), "last_error 记录")
ck(ZW.STATE_FILE.read_text(encoding="utf-8") == before, "落库失败不改状态（下轮重试同批）")
sys.modules["news_fetcher"] = fake_nf

# skipped 路径
ZW.load_conf = lambda: {**FIX, "enabled": False}
ck(ZW.run_once({}) == {"ok": False, "skipped": "disabled"}, "enabled=False → skipped")
ZW.load_conf = lambda: {**FIX, "code": ""}
ck(ZW.run_once({}) == {"ok": False, "skipped": "code empty"}, "code 空 → skipped")
ZW.load_conf = lambda: dict(FIX)

# ---------------- load_conf（默认回退） ----------------
conf = ZW.load_conf()
ck(conf.get("enabled") is True and conf.get("code") == "02513", "load_conf 默认值")
ck(isinstance(conf.get("model_name_re"), str) and conf["model_name_re"], "pattern 默认")

# ---------------- 静态接线 ----------------
app_src = (BASE / "app.py").read_text(encoding="utf-8")
ck("import zai_watch" in app_src, "app.py import zai_watch")
ck('_start_daemon(_zai_loop, "sa_zai")' in app_src, "守护线程已注册")
ck("zai_watch.run_once(_zai_deps())" in app_src, "daemon 调 run_once")
ck('"notify_fn": notifier.notify' in app_src, "notify_fn 注入 notifier.notify")

schema_src = (BASE / "config_schema.py").read_text(encoding="utf-8")
ck('"zai": {' in schema_src, "config_schema 有 zai 段")
for f in ("enabled", "interval_minutes", "github_org", "hf_author", "model_name_re",
          "code", "notify_new", "per_source_limit"):
    ck(f'{{"key": "{f}"' in schema_src, f"schema 字段 {f}")

ex = (BASE / "config.example.yaml").read_text(encoding="utf-8")
ck(re.search(r"^zai:\n  enabled: true", ex, re.M) is not None, "example yaml 有 zai 段")
ck("model_name_re" in ex and "github_repos:" not in ex,
   "example yaml 用新键（github_org/model_name_re，非旧 github_repos）")

# schema 与模块 DEFAULTS 一致（副本漂移钉死）
import config_schema as CS
sec = CS.SCHEMAS["zai"]
defaults = {f["key"]: f.get("d") for f in sec["fields"]}
for k in ("interval_minutes", "github_org", "hf_author", "model_name_re", "code",
          "notify_new", "per_source_limit", "proxy"):
    ck(defaults.get(k) == ZW.DEFAULT_CONF.get(k), f"默认值一致：{k}")

# 红线：模块不 import app、无写操作（AST 级断言——数子串会撞 docstring 里的「不 import app」）
zw_src = (BASE / "zai_watch.py").read_text(encoding="utf-8")
import ast as _ast
_tree = _ast.parse(zw_src)
_top_imports = {n.names[0].name.split(".")[0]
                for n in _tree.body if isinstance(n, _ast.Import)}
ck("app" not in _top_imports, "不 import app（AST 级）")
ck(re.search(r"\.(post|put|delete)\(", zw_src, re.I) is None, "无写 HTTP（纯 GET）")
ck("sa_paper" not in zw_src and "sa_trades" not in zw_src, "不碰交易表")

print(f"zai_watch_test: {N}/{N} 全过")
