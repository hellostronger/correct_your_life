# -*- coding: utf-8 -*-
"""离线断言：行情代理 data_service.market（不联网，所有 HTTP 都被 Fake 掉）。

跑法（在仓库根目录）：
    python tests/test_market_proxy.py
"""
import importlib.util
import os
import sys
from pathlib import Path

os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

# 仓库根目录**就是** data_service 包的内容（部署时平铺到 /opt/sa-data-service），
# 所以克隆出来的目录名可能不是 data_service。这里显式注册包名，
# 保证与部署环境 `python -m uvicorn data_service.api:app` 的 import 路径一致。
ROOT = Path(__file__).resolve().parents[1]
if "data_service" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "data_service", ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)])
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["data_service"] = _mod
    _spec.loader.exec_module(_mod)

from data_service import market as M  # noqa: E402

PASS = FAIL = 0


def chk(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print("FAIL:", msg)


class FakeResp:
    def __init__(self, *, text="", content=b"", payload=None):
        self.text = text
        self.content = content
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        return None


# ---------- A. 符号映射 ----------
chk(M.secid_to_tx("1.600519") == "sh600519", "secid 1.")
chk(M.secid_to_tx("0.000158") == "sz000158", "secid 0.")
chk(M.secid_to_tx("116.02513") == "hk02513", "secid 116 补零")
chk(M.secid_to_tx("100.HSI") == "hkHSI", "secid 100 -> 指数")
chk(M.tx_to_secid("sh600519") == "1.600519", "tx sh")
chk(M.tx_to_secid("sz000158") == "0.000158", "tx sz")
chk(M.tx_to_secid("bj830799") == "0.830799", "tx bj -> 0.")
chk(M.tx_to_secid("hk00100") == "116.00100", "tx hk 补零")
chk(M.tx_to_secid("hkHSI") == "100.HSI", "tx hk 指数")
try:
    M.secid_to_tx("83.1")
    chk(False, "未知市场应 ValueError")
except ValueError:
    chk(True, "")
try:
    M.parse_secid("600519")
    chk(False, "裸代码应 ValueError")
except ValueError:
    chk(True, "")
chk(M._is_star("1.688795") is True, "star sh")
chk(M._is_star("0.688795") is True, "star sz")
chk(M._is_star("1.600519") is False, "star 非 688")

# ---------- B. 腾讯日K -> 东财 CSV ----------
BARS = [
    ["2026-10-21", "100", "101", "102", "99", "1000", {}, "1.23", "1010", ""],
    ["2026-10-22", "101", "103", "104", "100", "2000", {}, "2.34", "2060", ""],
]

captured = {}


def fake_get_tx(url, *, params=None, timeout=15, referer=None):
    captured["url"] = url
    captured["params"] = dict(params or {})
    return FakeResp(payload={"code": 0, "data": {"sh600519": {"qfqday": BARS}}})


M._get = fake_get_tx
rows = M._tx_kline("1.600519", 60, "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61", "", "")
chk(len(rows) == 2, "腾讯日K 2 根, got %s" % len(rows))
p0 = rows[0].split(",")
p1 = rows[1].split(",")
chk(p0[0] == "2026-10-21" and p0[1] == "100.00" and p0[2] == "101.00", "首根 OHLC")
chk(p0[5] == "1000.00", "A股 f56 手 原样, got %s" % p0[5])
chk(p0[6] == "10100000.00", "f57 = 万元*1e4, got %s" % p0[6])
chk(p0[10] == "1.23", "f61 换手率, got %s" % p0[10])
chk(p0[8] == "-" and p0[9] == "-", "首根涨跌幅/额无前收应 '-'")
chk(abs(float(p1[8]) - (103 - 101) / 101 * 100) < 0.005, "第二根涨跌幅")
chk(float(p1[9]) == 2.0, "第二根涨跌额")
chk("newfqkline" in captured["url"], "走 newfqkline")
chk(captured["params"]["param"] == "sh600519,day,,,60,qfq", "腾讯 param")

# 科创板：volume 是股 -> f56 手
captured.clear()
M._get = lambda url, *, params=None, timeout=15, referer=None: FakeResp(
    payload={"data": {"sh688795": {"qfqday": [
        ["2026-10-22", "10", "11", "12", "9", "12345", {}, "3.0", "0", ""]]}}})
r = M._tx_kline("1.688795", 10, "f51,f56,f57", "", "")
chk(r[0].split(",")[1] == "123.45", "科创板 f56 = 股/100, got %s" % r[0])

# 港股：volume 是股，原样
M._get = lambda url, *, params=None, timeout=15, referer=None: FakeResp(
    payload={"data": {"hk02513": {"day": [
        ["2026-10-22", "10", "11", "12", "9", "12345", {}, "3.0", "0", ""]]}}})
r = M._tx_kline("116.02513", 10, "f51,f56,f57", "", "")
chk(r[0].split(",")[1] == "12345.00", "港股 f56 原样, got %s" % r[0])
chk(r[0].split(",")[2] == "135795.00", "成交额缺失时 量*价 估算(股), got %s" % r[0])

# beg/end 过滤
M._get = lambda url, *, params=None, timeout=15, referer=None: FakeResp(
    payload={"data": {"sh600519": {"qfqday": BARS}}})
r = M._tx_kline("1.600519", 60, "f51,f53", "2026-10-22", "")
chk(len(r) == 1 and r[0].startswith("2026-10-22"), "beg 过滤")
r = M._tx_kline("1.600519", 60, "f51,f53", "", "2026-10-21")
chk(len(r) == 1 and r[0].startswith("2026-10-21"), "end 过滤")

# ---------- C. 实时行情 ----------
def build_tencent(fields_count, overrides):
    f = [""] * fields_count
    for i, v in overrides.items():
        f[i] = str(v)
    return "~".join(f)


a_txt = ('v_sh600519="%s";\n' % build_tencent(60, {
    1: "贵州茅台", 3: "1500.5", 4: "1490", 5: "1495", 6: "12345",
    30: "20261022150000", 31: "10.5", 32: "0.70", 33: "1510", 34: "1488",
    35: "1500/123/185000000", 37: "18500", 45: "18800", 49: "1.25"})
    + 'v_whHKDCNY="%s";\n' % build_tencent(6, {1: "港币人民币", 3: "0.9123"})
    + 'v_usAAPL="%s";\n' % build_tencent(40, {
        1: "苹果", 3: "230.1", 4: "228", 32: "0.92"}))


def fake_get_q(url, *, params=None, timeout=15, referer=None):
    if "hq.sinajs.cn" in url:
        return FakeResp(content='var hq_str_rt_hk00100="00100,MINIMAX-W,25.0,24.0,'
                                '25.5,23.8,25.2,1.2,5.0,25.1,24.9,123456789,'
                                '5000000,,,,,,2026/10/22,15:59:59";'
                                .encode("gbk"))
    return FakeResp(content=a_txt.encode("gbk"))


M._get = fake_get_q
q = M.quotes(["sh600519", "hk00100", "whHKDCNY", "usAAPL", "hkHSI"])
chk(q["quotes"]["sh600519"]["name"] == "贵州茅台", "腾讯 A 名称")
chk(q["quotes"]["sh600519"]["price"] == 1500.5, "腾讯 A 价格")
chk(q["quotes"]["sh600519"]["amount"] == 185000000.0, "f35 第三段=绝对额")
chk(q["quotes"]["sh600519"]["ratio"] == 1.25, "f49 量比")
chk(q["quotes"]["sh600519"]["market"] == "a", "A 市场")
chk(q["quotes"]["whHKDCNY"]["price"] == 0.9123, "汇率短字段可用, got %r"
    % q["quotes"]["whHKDCNY"])
chk(q["quotes"]["whHKDCNY"]["market"] == "fx", "汇率 market=fx")
chk(q["quotes"]["usAAPL"]["price"] == 230.1, "美股价格")
chk(q["quotes"]["usAAPL"]["change_pct"] == 0.92, "美股涨跌幅")
chk(q["quotes"]["usAAPL"]["market"] == "us", "美股 market")
chk(q["quotes"]["hk00100"]["price"] == 25.2, "港股新浪价格")
chk(q["quotes"]["hk00100"]["amount"] == 123456789.0, "港股成交额")
chk(q["quotes"]["hk00100"]["market"] == "hk", "港股 market")
chk(isinstance(q["quotes"]["hkHSI"], dict) and "error" in q["quotes"]["hkHSI"],
    "hkHSI 走腾讯（本 fixture 未给 -> error）")

# 港股缺市值 -> 腾讯补（第二次调用）
seen = {}


def fake_get_cap(url, *, params=None, timeout=15, referer=None):
    seen.setdefault("calls", []).append(url)
    if "qt.gtimg.cn" in url:
        return FakeResp(content=('v_hk00100="%s";'
                                 % build_tencent(50, {1: "MINIMAX-W", 45: "780.5"}))
                        .encode("gbk"))
    return fake_get_q(url, params=params, timeout=timeout, referer=referer)


M._get = fake_get_cap
M._hk_cap_cache.clear()
q2 = M.quotes(["hk00100"])
chk(q2["quotes"]["hk00100"]["market_cap"] == 780.5, "港股 f45 市值补齐")
chk(any("qt.gtimg.cn" in u for u in seen["calls"]), "市值确实走了腾讯")

# 空 symbols / 超 300
try:
    M.quotes([])
    chk(True, "")
except Exception:
    chk(False, "空列表不该抛")
try:
    M.quotes(["sh%06d" % i for i in range(301)])
    chk(False, "超 300 应 ValueError")
except ValueError:
    chk(True, "")

# ---------- D. 分时 ----------
M._get = lambda url, *, params=None, timeout=15, referer=None: FakeResp(
    payload={"data": {"sh600519": {"data": {
        "date": "20261022",
        "data": ["0930 100.0 10 1000.0", "0931 101.0 20 2020.0"]}}}})
i = M.intraday("sh600519", period="m5", count=10)
chk(i["has_ohlc"] is False or i["has_ohlc"] is True, "intraday 结构")
chk(abs(i["vwap"] - (1000 + 2020) / ((10 + 20) * 100)) < 1e-6, "A 股 VWAP 手*100")
chk(i["vwap_is_true"] is True, "分时 VWAP 可信标记")

# 港股：无 mkline，用分时合成 bars，且量是股
M._get = lambda url, *, params=None, timeout=15, referer=None: FakeResp(
    payload={"data": {"hk02513": {"data": {
        "date": "20261022",
        "data": ["0930 10.0 100 1000.0", "0931 12.0 100 1200.0"]}}}})
i = M.intraday("hk02513", period="m5", count=10)
chk(i["is_hk"] is True and i["source"] == "tx_trend", "港股走分时合成")
chk(abs(i["vwap"] - 2200 / 200) < 1e-6, "港股 VWAP 不乘 100, got %r" % i["vwap"])
chk(len(i["bars"]) == 2 and i["bars"][0]["datetime"] == "202610220930", "合成 bar 时间")

# 科创板 VWAP 也不乘 100（volume 已是股）
M._get = lambda url, *, params=None, timeout=15, referer=None: FakeResp(
    payload={"data": {"sh688795": {"data": {
        "date": "20261022",
        "data": ["0930 10.0 1000 10000.0", "0931 12.0 1000 12000.0"]}}}})
i = M.intraday("688795", period="m5", count=10)
chk(abs(i["vwap"] - 22000 / 2000) < 1e-6, "科创板 VWAP 不乘 100, got %r" % i["vwap"])

print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
