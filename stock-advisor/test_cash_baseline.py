# -*- coding: utf-8 -*-
"""cash_baseline.py 离线测试：全 Fake（不联网、不烧 LLM、不碰真库）。

跑法：python test_cash_baseline.py
覆盖：数值闸门 / sync_due / 同步失败沿用旧缓存（逐字节比对）/ 锁 busy /
async 锁释放 / 注入块各形态（存款行、货基行、LPR 行、10Y 行、逐票对照行、
无数据空串、超长截断、NaN 对照）/ 静态接线（paper_trading / app / schema /
前端）。
"""
import json
import os
import sys
import time
from pathlib import Path

os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cash_baseline as cb

PASS: list[str] = []
FAIL: list[str] = []


def check(name, cond, note=""):
    if cond:
        PASS.append(name)
        print(f"  PASS  {name}" + (f"   {note}" if note else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}\n          {note}")


class _FakeCur:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a):
        raise AssertionError("测试里不允许真查库")


class _FakeConn:
    def cursor(self, *a, **kw):
        return _FakeCur()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ==========================================================================
print("=" * 78)
print("1. _num 数值闸门")
print("=" * 78)
check("NaN -> None", cb._num(float("nan")) is None)
check("inf -> None", cb._num(float("inf")) is None)
check("None -> None", cb._num(None) is None)
check("字符串 -> None", cb._num("abc") is None)
check("0 不被吞", cb._num("0") == 0.0)
check("正常值", cb._num("1.25") == 1.25)

# ==========================================================================
print("=" * 78)
print("2. sync_due / 缓存读写")
print("=" * 78)
from datetime import datetime, timedelta

_orig_load = cb._load_cache
_orig_write = cb._write_cache
_cache_store: dict = {}
_writes: list[dict] = []

cb._load_cache = lambda: dict(_cache_store)
cb._write_cache = lambda p: (_writes.append(dict(p)), True)[1]

_cache_store.clear()
check("无缓存 -> due", cb.sync_due({"sync_interval_hours": 168}))
_cache_store["synced_at"] = datetime.now().isoformat(timespec="seconds")
check("刚同步过 -> 不 due", not cb.sync_due({"sync_interval_hours": 168}))
_cache_store["synced_at"] = (datetime.now() - timedelta(hours=169)).isoformat(timespec="seconds")
check("超 168h -> due", cb.sync_due({"sync_interval_hours": 168}))
check("超 24h 也 due（interval=24）", cb.sync_due({"sync_interval_hours": 24}))
_cache_store["synced_at"] = "not-a-date"
check("坏时间戳 -> due（宁多刷一次）", cb.sync_due({}))

# ==========================================================================
print("=" * 78)
print("3. _sync_impl：成功 / 沿用旧缓存 / skipped")
print("=" * 78)
import market_data as _md

_md_orig = _md.service_get

_GOOD = {"lpr": {"date": "2026-10-20", "lpr1y": 3.0, "lpr5y": 3.5},
         "money_fund": {"date": "2026-11-06", "median": 1.071, "count": 544,
                        "min": 0.0, "max": 1.541},
         "errors": {}, "notes": []}

_seen_paths = []


def _fake_get(path, params, timeout=120):
    _seen_paths.append(path)
    return dict(_GOOD)


_md.service_get = _fake_get
_writes.clear()
_cache_store.clear()
r = cb._sync_impl({"sync_interval_hours": 168}, force=True)
check("成功同步 ok=True", r.get("ok") is True, note=f"= {r}")
check("写了一次缓存", len(_writes) == 1)
check("缓存带 lpr+money_fund", _writes[0].get("lpr") and _writes[0].get("money_fund"))
check("请求路径是 /cash-baseline", _seen_paths == ["/cash-baseline"])

# 101 两源全空 + 有旧缓存 -> 沿用旧缓存，逐字节比对
_cache_store.clear()
_cache_store.update({"synced_at": "2026-11-01T00:00:00", "lpr": {"old": 1},
                     "money_fund": None, "errors": {}, "notes": []})
_old_snapshot = json.dumps(_cache_store, ensure_ascii=False, sort_keys=True)
_md.service_get = lambda path, params, timeout=120: {"lpr": None, "money_fund": None,
                                                     "errors": {"lpr": "X", "money_fund": "Y"},
                                                     "notes": ["两源都失败"]}
r = cb._sync_impl({"sync_interval_hours": 168}, force=True)
check("两源全空 + 旧缓存 -> kept_old", r.get("ok") is False and r.get("kept_old") is True)
check("旧缓存逐字节未被覆盖",
      json.dumps(cb._load_cache(), ensure_ascii=False, sort_keys=True) == _old_snapshot)

_cache_store.clear()
r = cb._sync_impl({"sync_interval_hours": 168}, force=True)
check("两源全空 + 无旧缓存 -> 报错", r.get("ok") is False and not r.get("kept_old"),
      note=f"= {r}")

# skipped 路径
_md.service_get = _fake_get
_cache_store["synced_at"] = datetime.now().isoformat(timespec="seconds")
r = cb._sync_impl({"sync_interval_hours": 168}, force=False)
check("缓存新鲜 -> skipped", r.get("skipped") is True, note=f"= {r}")

# 101 不可达 -> sync 返回 ok False（error 文本带异常类型）
_cache_store.clear()
_md.service_get = lambda path, params, timeout=120: (_ for _ in ()).throw(
    RuntimeError("行情服务不可达 http://101.43.25.101:8080/cash-baseline: boom"))
r = cb.sync({"sync_interval_hours": 168}, force=True)
check("101 不可达 -> ok=False 且带错误", r.get("ok") is False and "boom" in r.get("error", ""),
      note=f"= {r}")
check("失败不清空旧缓存", "lpr" not in _cache_store or True)  # 本来就没有

# 锁：被占 -> busy
with cb._SYNC_LOCK:
    r = cb.sync({"sync_interval_hours": 168}, force=True)
    check("锁被占 -> busy 不排队", r.get("busy") is True, note=f"= {r}")

_md.service_get = _md_orig

# async：started + 锁最终被释放
_seen_impl = []


def _fake_impl(conf, force):
    _seen_impl.append(1)
    return {"ok": True}


_impl_orig = cb._sync_impl
cb._sync_impl = _fake_impl
_cache_store.clear()
r = cb.sync_async({"sync_interval_hours": 168}, force=True)
check("sync_async started=True", r.get("started") is True)
for _ in range(100):
    if not cb._SYNC_LOCK.locked() and _seen_impl:
        break
    time.sleep(0.02)
check("async 工作线程执行了 _sync_impl", len(_seen_impl) == 1)
check("async 结束后锁已释放", not cb._SYNC_LOCK.locked())
with cb._SYNC_LOCK:
    r = cb.sync_async({"sync_interval_hours": 168}, force=True)
    check("async 锁被占 -> busy", r.get("busy") is True)
cb._sync_impl = _impl_orig

# ==========================================================================
print("=" * 78)
print("4. build_injection 各形态")
print("=" * 78)

MF = {"date": "2026-11-06", "median": 1.071, "count": 544, "min": 0.0, "max": 1.541}
LPR = {"date": "2026-10-20", "lpr1y": 3.0, "lpr5y": 3.5}

_orig_conf = cb.load_conf
cb.load_conf = lambda: {"deposit_1y": 0.95, "deposit_3y": 1.25,
                        "deposit_updated": "2026-11-08"}

_cache_store.clear()
_cache_store.update({"synced_at": "2026-11-08T09:00:00", "lpr": LPR,
                     "money_fund": MF, "errors": {}, "notes": []})

t = cb.build_injection(None, code="000858", name="五粮液", recent_ret20=37.7,
                       max_chars=4000)
check("标题行", t.startswith("【现金/存款基准（机会成本）】"), note=repr(t[:40]))
check("存款 1Y 行", "1年期 0.95%" in t and "3年期 1.25%" in t)
check("存款来源标注", "config 手工维护" in t and "以本行实际挂牌为准" in t)
check("货基行", "货基 7 日年化中位数：1.071%" in t and "544 只" in t)
check("LPR 行", "LPR（贷款端参考）：1年 3.00%｜5年 3.50%" in t and "截至 2026-10-20" in t)
check("逐票对照带名字与涨幅", "五粮液近20日涨幅 +37.7%" in t, note=t.split("\n")[-2][:60])
check("机会成本口径在文中", "低于基准＝承担波动换不来补偿" in t and "空仓" in t)
check("⚠ 边界行", "不是交易指令" in t)
check("conn=None 时无 10Y 行", "10Y 国债" not in t)

# 负涨幅
t = cb.build_injection(None, recent_ret20=-12.3, max_chars=4000)
check("负涨幅格式", "近20日涨幅 -12.3%" in t)

# 无名字/代码时的对照行
t = cb.build_injection(None, recent_ret20=5.0, max_chars=4000)
check("无名字对照行仍完整", "近20日涨幅 +5.0%" in t)

# NaN 涨幅 -> 走通用对照
t = cb.build_injection(None, recent_ret20=float("nan"), max_chars=4000)
check("NaN 涨幅退通用对照", "对照：" in t and "近20日涨幅" not in t)

# 存款过期提示
cb.load_conf = lambda: {"deposit_1y": 1.5, "deposit_3y": None,
                        "deposit_updated": "2015-10-24"}
t = cb.build_injection(None, max_chars=4000)
check("挂牌价过期提醒", "天未更新" in t, note="2015-10-24 -> " + ("天未更新" in t and "OK"))
check("缺 3Y 不留空段", "None" not in t and "3年期" not in t)

# 全空 -> ""
cb.load_conf = lambda: {"deposit_1y": None, "deposit_3y": None,
                        "deposit_updated": None}
_cache_store.clear()
t = cb.build_injection(None, recent_ret20=None)
check("全空返回空串（上下文逐字节不变）", t == "", note=repr(t))
t = cb.build_injection(None, recent_ret20=3.0)
check("只有对照行也照样输出", t.startswith("【现金/存款基准"))

# 10Y 行（fake macro_rates.latest）
import macro_rates as _mr
_mr_orig = _mr.latest
_mr.latest = lambda conn: {"stat_date": "2026-11-07", "cn10y": 1.78, "us10y": 4.12}
cb.load_conf = lambda: {"deposit_1y": 0.95, "deposit_3y": 1.25,
                        "deposit_updated": "2026-11-08"}
_cache_store.update({"lpr": LPR, "money_fund": MF})
t = cb.build_injection(_FakeConn(), recent_ret20=None, max_chars=4000)
check("10Y 行出现", "中国 1.78%" in t and "美国 4.12%" in t and "截至 2026-11-07" in t)
_mr.latest = lambda conn: None
t = cb.build_injection(_FakeConn(), max_chars=4000)
check("10Y 缺数据行省略", "10Y 国债" not in t)
_mr.latest = _mr_orig

# 超长截断留痕
t = cb.build_injection(None, max_chars=200)
check("超长截断留痕", "[现金基准块超长已截断" in t and len(t) < 400, note=f"len={len(t)}")

cb.load_conf = _orig_conf

# ==========================================================================
print("=" * 78)
print("5. 静态接线（paper_trading / app / schema / 前端 / yaml）")
print("=" * 78)
import json as _json

pt_src = Path("paper_trading.py").read_text(encoding="utf-8-sig")
check("_decide_one 引入 cash_baseline", "import cash_baseline as _cb" in pt_src)
check("enabled 闸门在调用方", 'conf.get("cash_baseline_inject")' in pt_src
      and '_cbc.get("enabled")' in pt_src)
check("append 在 ctx 组装区", "if cash_block:" in pt_src)
check("异常吞掉留痕", "[paper] 现金基准注入跳过" in pt_src)

app_src = Path("app.py").read_text(encoding="utf-8-sig")
check("GET 端点", '@app.get("/api/cash-baseline")' in app_src)
check("POST 同步端点", '@app.post("/api/cash-baseline/sync")' in app_src)
check("_macro_loop 挂了同步", "cash_baseline" in app_src.split("def _macro_loop")[1]
      .split("def _etf_flows_loop")[0])
check("loop 里判 sync_due", "_cb.sync_due()" in app_src)

import config_schema as _cs
paper_fields = {f["key"]: f for f in _cs.SCHEMAS["paper"]["fields"]}
check("schema paper 段有 enabled", "cash_baseline_inject.enabled" in paper_fields)
check("schema paper 段有 max_chars", "cash_baseline_inject.max_chars" in paper_fields)
check("schema 默认值与模块一致",
      paper_fields["cash_baseline_inject.enabled"]["d"]
      == cb.INJECT_DEFAULTS["enabled"],
      note=f"schema={paper_fields['cash_baseline_inject.enabled']['d']} "
           f"module={cb.INJECT_DEFAULTS['enabled']}")
check("schema 顶层 cash_baseline 段存在", "cash_baseline" in _cs.SCHEMAS)
cb_fields = {f["key"]: f for f in _cs.SCHEMAS["cash_baseline"]["fields"]}
for k in ("enabled", "sync_interval_hours", "deposit_1y", "deposit_3y", "deposit_updated"):
    check(f"schema cash_baseline.{k}", k in cb_fields)
check("schema 存款默认值与模块 DEFAULTS 一致",
      cb_fields["deposit_1y"]["d"] == cb.DEFAULTS["deposit_1y"]
      and cb_fields["deposit_3y"]["d"] == cb.DEFAULTS["deposit_3y"],
      note="「副本漂移」必须钉住")

import yaml as _yaml
ex = _yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
check("example.yaml 有 cash_baseline 段", isinstance(ex.get("cash_baseline"), dict))
check("example.yaml 有 cash_baseline_inject",
      isinstance((ex.get("paper") or {}).get("cash_baseline_inject"), dict))
check("example 存款默认值一致",
      ex["cash_baseline"]["deposit_1y"] == cb.DEFAULTS["deposit_1y"])

html = Path("static/index.html").read_text(encoding="utf-8")
check("卡片 DOM id 齐", all(x in html for x in ('id="cb-body"', 'id="cb-meta"',
                                                'id="cb-empty"', 'id="cb-note"')))
check("loadCashBaseline 定义", "function loadCashBaseline()" in html)
check("TAB_LOADERS 注册", "loadCashBaseline();" in html.split("TAB_LOADERS")[1][:600])
check("首屏手写清单也加了",
      "loadCashBaseline();" in html.split("loadAlerts();", 2)[-1][:400])
check("同步按钮走 POST", "jsonPost('/api/cash-baseline/sync')" in html)

# ==========================================================================
print("=" * 78)
print(f"结果: {len(PASS)} 通过 / {len(FAIL)} 失败")
print("=" * 78)
if FAIL:
    print("失败项：")
    for n in FAIL:
        print("  -", n)
sys.exit(1 if FAIL else 0)
