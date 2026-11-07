#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线测试入口（不联网、不烧外部配额，可反复跑）。

用法::

    python run_tests.py

仓库目录名是 ``sa-data-service``，而包名必须是 ``data_service``（部署时
``/opt/sa-data-service`` 里的文件直接就是包内容，容器里跑的是
``python -m uvicorn data_service.api:app``）。所以先按路径把本目录注册成包，
否则 ``import data_service`` 和 ``from . import`` 都解析不了。

覆盖（全部离线，HTTP 全被 Fake 掉）：

  - selftest.py                 字段规范化 / 请求预算记账 / 跨源合并 / 评分
  - tests/test_budget.py        预算耗尽后自动降级而不是继续捶源
  - tests/test_market_proxy.py  行情代理 /kline /quote /intraday 的解析与单位
  - tests/test_rates.py         现金基准（LPR/货基）采集与快照缓存

不在本入口里的三套（原因见 DEPLOY.md「测试」一节）：

  - tests/test_pipeline.py            依赖主仓库 ``sector.py`` 的回落逻辑
  - tests/test_sector_integration.py  同上，且要打网络
  - tests/test_service_api.py         打真实服务（默认 8901 / 101:8080）
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PKG = "data_service"

if PKG not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        PKG, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = pkg
    spec.loader.exec_module(pkg)

asserts = importlib.import_module(PKG + ".tests.asserts")

SUITES = [
    ("selftest", "包内自测"),
    ("tests.test_budget", "请求预算限流"),
    ("tests.test_market_proxy", "行情代理"),
    ("tests.test_rates", "现金基准 LPR/货基"),
]


def _run_one(name: str) -> tuple:
    """跑一个套件，返回 (通过数, 失败数, 是否通过)。"""
    full = PKG + "." + name
    p0, f0 = len(asserts.PASS), len(asserts.FAIL)
    ok = True
    # 按路径加载而不是 import_module：套件结尾都 sys.exit()，
    # 导入失败时 importlib 会把模块从 sys.modules 里删掉，就拿不到它自己的计数了。
    spec = importlib.util.spec_from_file_location(
        full, ROOT / (name.replace(".", "/") + ".py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    try:
        spec.loader.exec_module(mod)
    except SystemExit as exc:
        ok = exc.code in (0, None)
    except BaseException:
        ok = False
        traceback.print_exc()

    own_pass = getattr(mod, "PASS", None) if mod else None
    own_fail = getattr(mod, "FAIL", None) if mod else None
    if isinstance(own_pass, int) and isinstance(own_fail, int):
        passed, failed = own_pass, own_fail
    else:
        passed = len(asserts.PASS) - p0
        failed = len(asserts.FAIL) - f0
    return passed, failed, (ok and failed == 0)


def main() -> int:
    rows = []
    for name, desc in SUITES:
        print()
        print("#" * 74)
        print("# %s —— %s" % (name, desc))
        print("#" * 74)
        passed, failed, ok = _run_one(name)
        rows.append((name, passed, failed, ok))

    print()
    print("=" * 74)
    total_pass = sum(r[1] for r in rows)
    total_fail = sum(r[2] for r in rows)
    for name, passed, failed, ok in rows:
        print("  %-26s %4d 通过 / %3d 失败   %s"
              % (name, passed, failed, "OK" if ok else "FAIL"))
    print("-" * 74)
    print("  合计 %d 通过 / %d 失败" % (total_pass, total_fail))
    print("=" * 74)
    return 0 if all(r[3] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
