"""rates.py 离线测试：全 Fake（不联网、不装 akshare 也能跑）。

跑法：``python -m data_service.tests.test_rates``（或直接 python 本文件，
parents[2] 已把仓库根加进 sys.path）。
"""
import os
import statistics
import sys
from pathlib import Path

os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from data_service import rates
from data_service.tests.asserts import FAIL, PASS, check, report


class FakeDf:
    """最小 DataFrame 形状：columns + __getitem__（pandas 不必在场）。"""

    def __init__(self, columns, cols_data):
        self.columns = list(columns)
        self._data = {c: list(v) for c, v in cols_data.items()}

    def __getitem__(self, col):
        return self._data[col]


def _mf_df(n=40, valid_ratio=1.0):
    """n 条 7 日年化，valid_ratio 控制多少条可解析（'---' 不可解析）。"""
    vals = [f"{1.0 + i * 0.01:.4f}%" if i < int(n * valid_ratio) else "---"
            for i in range(n)]
    return FakeDf(["基金代码", "2026-11-06-7日年化%", "2026-11-05-7日年化%"],
                  {"2026-11-06-7日年化%": vals})


def _lpr_row(lpr1y=3.0, lpr5y=3.5, date="2026-10-20"):
    return {"TRADE_DATE": date, "LPR1Y": lpr1y, "LPR5Y": lpr5y}


print("=" * 78)
print("1. _num 数值闸门")
print("=" * 78)
check("'1.5410%' -> 1.541", rates._num("1.5410%") == 1.541, note=f"= {rates._num('1.5410%')}")
check("'---' -> None", rates._num("---") is None)
check("'' -> None", rates._num("") is None)
check("'3.0' -> 3.0", rates._num("3.0") == 3.0)
check("None -> None", rates._num(None) is None)
check("NaN -> None（f != f 闸门）", rates._num(float("nan")) is None)
check("0 不被 falsy 吞掉", rates._num("0%") == 0.0, note=f"= {rates._num('0%')}")
check("垃圾字符串 -> None", rates._num("abc") is None)

print("=" * 78)
print("2. fetch_lpr")
print("=" * 78)
out = rates.fetch_lpr(fn=lambda: _lpr_row())
check("lpr1y/lpr5y/date 都在", out == {"date": "2026-10-20", "lpr1y": 3.0, "lpr5y": 3.5},
      note=f"= {out}")
out = rates.fetch_lpr(fn=lambda: _lpr_row(lpr5y="---"))
check("无效 LPR5Y 被省略", out == {"date": "2026-10-20", "lpr1y": 3.0}, note=f"= {out}")
out = rates.fetch_lpr(fn=lambda: _lpr_row(lpr1y=None, lpr5y=None))
check("两个利率全无效 -> None", out is None)

print("=" * 78)
print("3. fetch_money_fund")
print("=" * 78)
out = rates.fetch_money_fund(fn=lambda: _mf_df(40))
exp_median = statistics.median([1.0 + i * 0.01 for i in range(40)])
check("中位数对", out["median"] == round(exp_median, 3), note=f"= {out}")
check("count=40", out["count"] == 40)
check("min/max 对", out["min"] == 1.0 and out["max"] == 1.39, note=f"= {out}")
check("date 取列名日期", out["date"] == "2026-11-06")
out = rates.fetch_money_fund(fn=lambda: _mf_df(60, valid_ratio=0.5))
check("'---' 不计入（count=30）", out["count"] == 30, note=f"= {out}")
try:
    rates.fetch_money_fund(fn=lambda: _mf_df(10))
    check("样本<30 抛 ValueError", False)
except ValueError as exc:
    check("样本<30 抛 ValueError", "疑似源异常" in str(exc), note=str(exc))
try:
    rates.fetch_money_fund(fn=lambda: FakeDf(["基金代码"], {"基金代码": ["1"]}))
    check("缺 7日年化列抛 ValueError", False)
except ValueError as exc:
    check("缺 7日年化列抛 ValueError", "没有 7 日年化" in str(exc))

print("=" * 78)
print("4. snapshot：TTL 缓存 / 单源失败 / 全失败沿用旧快照")
print("=" * 78)


def _boom():
    raise ConnectionError("net down")


rates._reset_cache()
calls = {"lpr": 0, "mf": 0}


def _lpr_ok():
    calls["lpr"] += 1
    return _lpr_row()


def _mf_ok():
    calls["mf"] += 1
    return _mf_df(40)


out = rates.snapshot(force=True, lpr_fn=_lpr_ok, mf_fn=_mf_ok)
check("两源都在", out["lpr"] and out["money_fund"] and not out["errors"], note=f"= {out['notes']}")
check("fetched_at 是 iso 时间", len(out["fetched_at"]) >= 19)
n1, n2 = calls["lpr"], calls["mf"]
out = rates.snapshot(lpr_fn=_lpr_ok, mf_fn=_mf_ok)
check("TTL 内不重打源", (calls["lpr"], calls["mf"]) == (n1, n2),
      note=f"calls={calls}")
out = rates.snapshot(force=True, lpr_fn=_lpr_ok, mf_fn=_mf_ok)
check("force=True 重打源", calls["lpr"] == n1 + 1 and calls["mf"] == n2 + 1,
      note=f"calls={calls}")

rates._reset_cache()
out = rates.snapshot(force=True, lpr_fn=_lpr_ok, mf_fn=_boom)
check("单源失败不拖垮另一源", out["lpr"] is not None and out["money_fund"] is None,
      note=f"= {out['errors']}")
check("errors 里有钱基原因", "ConnectionError" in out["errors"]["money_fund"])
check("notes 提示部分失败", any("部分源失败" in n for n in out["notes"]))

out = rates.snapshot(force=True, lpr_fn=_boom, mf_fn=_boom)
check("全失败沿用旧快照 stale=True", out.get("stale") is True and out["lpr"] is not None,
      note=f"= {out['notes']}")
check("沿用快照仍带本次 errors", "ConnectionError" in out["errors"]["lpr"])

rates._reset_cache()
try:
    rates.snapshot(force=True, lpr_fn=_boom, mf_fn=_boom)
    check("无旧快照全失败抛 UpstreamError", False)
except rates.UpstreamError as exc:
    check("无旧快照全失败抛 UpstreamError", "两个源都失败" in str(exc))

print("=" * 78)
print("5. api 路由注册 + 版本号")
print("=" * 78)
from data_service import api as ds_api  # noqa: E402
paths = {r.path for r in ds_api.app.routes}
check("/cash-baseline 已注册", "/cash-baseline" in paths)
check("GET / 根端点列表提到 /cash-baseline", "/cash-baseline" in ds_api.root()["endpoints"])
from data_service import __version__ as ds_ver  # noqa: E402
check("版本已升 1.0.6", ds_ver == "1.0.6", note=f"= {ds_ver}")

sys.exit(report())
