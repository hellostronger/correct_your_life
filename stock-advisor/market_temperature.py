# -*- coding: utf-8 -*-
"""市场温度计：37 个 A 股标的 + 7 个港股标的的「PE 十年分位数」温度。

口径（按用户提供的截图规格实现，全部有实测依据）：

    温度 = 该标的最新 PE(TTM) 在其近 10 年 PE 历史序列中的分位数 × 100
    综合温度 = 各标的温度的**等权算术平均**（非市值加权）
              —— 用截图给的 37 行温度反算过：均值 = 1834.24/37 = 49.57，
                 与截图「A股市场温度 49.57°C」逐位吻合；港股 7 行 165.60/7 = 23.66
                 同样吻合。中位数（53.24 / 15.00）对不上，所以是均值不是中位数。
    分档： <30 低温·可逐步买入 ／ 30~50 常温·持有观察 ／ >50 高温·开始卖出
    采样： 2016Q4~2025Q3 取季末、2025Q4 起取月末、再加最新交易日
    门槛： PE 历史不足 3 年（<36 个采样点）的标的，温度照常算，
           但**不进综合温度**（截图原话：新纳入标的历史不足 3 年不计入当日均值）

数据源（2026-10-28 起：**本机不直连 akshare，全部经 101 的 data_service**）：

| 数据 | 101 端点 | 背后的源 |
|---|---|---|
| 申万公布过的月末交易日 | `GET /sw-calendar` | `ak.index_analysis_week_month_sw("month")` |
| 31 个申万一级行业月末 PE/收盘 | `GET /sw-month?date=YYYY-MM-DD` | `ak.index_analysis_monthly_sw("一级行业"/"市场表征", …)`，一次返回全部 31 行 |
| 当日 PE/收盘 | `GET /sw-day?date=YYYY-MM-DD` | `ak.index_analysis_daily_sw(…, start=end=单日)`；**必须单日**，跨度大会翻到 ~1600 页 |
| 5 个宽基 + 7 个港股通的 PE 与收盘 | `GET /index-hist?code=&start=&end=` | `ak.stock_zh_index_hist_csindex`，~2400 日线含「滚动市盈率」+「收盘」，采样点在本地挑 |

服务端**不做标的过滤**（过滤权在本文件的 `A_UNIVERSE` / `UNIVERSE`，加标的不用重建镜像）；
本机仍写 `data/market_temperature/` 下的缓存（`cal.json` / `sw/*.json` / `csi/*.json`），
所以只有缺失的日期才会发请求。

创业板指 PE 取申万 **801005 申万创业**（中证/国证的 399006/399005/399673 在 csindex
全部拿不到 PE）。已实测**不可用**（不要再试）：`ak.stock_hk_valuation_comparison_em`
是个股估值不是指数；`ak.index_value_hist_funddb` 不存在；
`ak.stock_hk_index_daily_sina` 的 HSI/HSTECH 只有点位没有 PE。
⇒ **恒生指数/恒生科技/恒生消费/港股汽车 四个没有任何 PE 源**，只能用中证港股通同主题
指数**替代**（见 `HK_UNIVERSE` 的 `substitute=True`），替代口径必须显示给用户，不静默换。
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data" / "market_temperature"
SW_DIR = DATA_DIR / "sw"
CSI_DIR = DATA_DIR / "csi"

# 采样起点（近 10 年）与涨幅基期
SERIES_START = date(2016, 12, 1)
YTD_BASE = date(2025, 12, 31)      # 「今年以来」= 上一年年末收盘
GAIN924_BASE = date(2024, 9, 24)   # 「924 以来」= 2024-09-24 收盘
MIN_SAMPLES = 36                   # 3 年 × 12 个月末采样点

BANDS = (
    (30.0, "cold", "低温 · 可逐步买入", "可提高仓位"),
    (50.0, "mid", "常温 · 持有观察", "维持仓位"),
    (101.0, "hot", "高温 · 开始卖出", "不可提高仓位"),
)

DEFAULT_CONF: dict[str, Any] = {
    "enabled": True,
    "interval_minutes": 720,
    "max_chars": 1200,
}

_LOCK = threading.Lock()


# ---------------------------------------------------------------- 标的清单
# 一个标的三个名字，各司其职（2026-10-28 用户定的口径，别再混）：
#   display  —— 页面「行业名称」列，**逐字还原用户给的原始规格**。像「猪肉+农业」
#               「证券+保险」「发电厂」「电子(芯片)」这类归并/简称叫法就是要展示成
#               这样，不能为了「看起来规范」换成默认叫法。
#   official —— 申万/中证官方名。它的作用只有一个：对齐数据源（取数键、对账、
#               报告替代口径），不参与展示。
#   etf      —— 「相关标的」，纯展示。绝不作为名称匹配、计算或去重的输入。
# 规格里显示「—」的那 8 行，etf 留空串（美容护理(医美)、纺织服饰、交通运输、
# 石油石化、商贸零售、轻工制造、建筑装饰、综合）。
# ⚠️ 名册（sa_stock_roster）与本模块**完全无关**：本文件不 import app、不连库
#    （grep roster|sa_stock|get_conn|psycopg|watchlist 为 0），所以名册里的标准名
#    不可能覆盖这里的展示名。
A_UNIVERSE: list[dict] = [
    # —— 31 个申万一级行业（PE 走 index_analysis_monthly_sw("一级行业")）——
    {"key": "sw:801010", "code": "801010", "display": "猪肉+农业", "official": "农林牧渔", "etf": "159865"},
    {"key": "sw:801030", "code": "801030", "display": "基础化工", "official": "基础化工", "etf": "159870"},
    {"key": "sw:801040", "code": "801040", "display": "钢铁", "official": "钢铁", "etf": "515210"},
    {"key": "sw:801050", "code": "801050", "display": "有色金属", "official": "有色金属", "etf": "561330"},
    {"key": "sw:801080", "code": "801080", "display": "电子(芯片)", "official": "电子", "etf": "159995"},
    {"key": "sw:801110", "code": "801110", "display": "家用电器", "official": "家用电器", "etf": "159996"},
    {"key": "sw:801120", "code": "801120", "display": "食品饮料(白酒)", "official": "食品饮料", "etf": "512690"},
    {"key": "sw:801130", "code": "801130", "display": "纺织服饰", "official": "纺织服饰", "etf": ""},
    {"key": "sw:801140", "code": "801140", "display": "轻工制造", "official": "轻工制造", "etf": ""},
    {"key": "sw:801150", "code": "801150", "display": "医药+医疗", "official": "医药生物", "etf": "512010"},
    {"key": "sw:801160", "code": "801160", "display": "发电厂", "official": "公用事业", "etf": "159611"},
    {"key": "sw:801170", "code": "801170", "display": "交通运输", "official": "交通运输", "etf": ""},
    {"key": "sw:801180", "code": "801180", "display": "房地产", "official": "房地产", "etf": "512220"},
    {"key": "sw:801200", "code": "801200", "display": "商贸零售", "official": "商贸零售", "etf": ""},
    {"key": "sw:801210", "code": "801210", "display": "酒店+旅游+检测", "official": "社会服务", "etf": "159766"},
    {"key": "sw:801230", "code": "801230", "display": "综合", "official": "综合", "etf": ""},
    {"key": "sw:801710", "code": "801710", "display": "建筑材料", "official": "建筑材料", "etf": "159745"},
    {"key": "sw:801720", "code": "801720", "display": "建筑装饰", "official": "建筑装饰", "etf": ""},
    {"key": "sw:801730", "code": "801730", "display": "电力设备(电池光伏)", "official": "电力设备", "etf": "516090"},
    {"key": "sw:801740", "code": "801740", "display": "国防军工", "official": "国防军工", "etf": "512660"},
    {"key": "sw:801750", "code": "801750", "display": "计算机", "official": "计算机", "etf": "512720"},
    {"key": "sw:801760", "code": "801760", "display": "传媒(游戏)", "official": "传媒", "etf": "159869"},
    {"key": "sw:801770", "code": "801770", "display": "通信(5G)", "official": "通信", "etf": "515880"},
    {"key": "sw:801780", "code": "801780", "display": "银行", "official": "银行", "etf": "516310"},
    {"key": "sw:801790", "code": "801790", "display": "证券+保险", "official": "非银金融", "etf": "512880"},
    {"key": "sw:801880", "code": "801880", "display": "汽车", "official": "汽车", "etf": "516110"},
    {"key": "sw:801890", "code": "801890", "display": "机械设备", "official": "机械设备", "etf": "516960"},
    {"key": "sw:801950", "code": "801950", "display": "煤炭", "official": "煤炭", "etf": "515220"},
    {"key": "sw:801960", "code": "801960", "display": "石油石化", "official": "石油石化", "etf": ""},
    {"key": "sw:801970", "code": "801970", "display": "环保", "official": "环保", "etf": "512580"},
    {"key": "sw:801980", "code": "801980", "display": "美容护理(医美)", "official": "美容护理", "etf": ""},
    # —— 创业板：展示名就叫「创业板」，数据取自申万市场表征 801005 申万创业 ——
    {"key": "sw:801005", "code": "801005", "display": "创业板", "official": "申万创业(801005)", "etf": "159915", "kind": "宽基"},
    # —— 5 个中证宽基（PE + 收盘走 csindex 日线），展示名 = 官方名 ——
    {"key": "csi:000016", "code": "000016", "display": "上证50", "official": "上证50", "etf": "510050", "kind": "宽基"},
    {"key": "csi:000300", "code": "000300", "display": "沪深300", "official": "沪深300", "etf": "510300", "kind": "宽基"},
    {"key": "csi:000510", "code": "000510", "display": "中证A500", "official": "中证A500", "etf": "159338", "kind": "宽基"},
    {"key": "csi:000905", "code": "000905", "display": "中证500", "official": "中证500", "etf": "510500", "kind": "宽基"},
    {"key": "csi:000852", "code": "000852", "display": "中证1000", "official": "中证1000", "etf": "512100", "kind": "宽基"},
]

# 港股 7 个标的。substitute=True 的是**替代口径**（恒生系指数没有 PE 源），
# 页面上必须显示「替代」并说明实际用的中证指数（official），不能静默替换。
# 展示名按规格逐字：恒生消费 / 港股通非银 / 恒生科技 / 港股汽车 / 港股创新药 /
# 恒生指数 / 港股通高股息。
HK_UNIVERSE: list[dict] = [
    {"key": "csi:931027", "code": "931027", "display": "恒生消费", "official": "中证港股通大消费(931027)", "etf": "159699", "substitute": True},
    {"key": "csi:931024", "code": "931024", "display": "港股通非银", "official": "中证港股通非银(931024)", "etf": "513750", "substitute": False},
    {"key": "csi:931573", "code": "931573", "display": "恒生科技", "official": "中证港股通科技(931573)", "etf": "513010", "substitute": True},
    {"key": "csi:931239", "code": "931239", "display": "港股汽车", "official": "中证港股通汽车(931239)", "etf": "159323", "substitute": True},
    {"key": "csi:931787", "code": "931787", "display": "港股创新药", "official": "中证港股通创新药(931787)", "etf": "513120", "substitute": False},
    {"key": "csi:930930", "code": "930930", "display": "恒生指数", "official": "中证港股通综合(930930)", "etf": "159920", "substitute": True},
    {"key": "csi:930914", "code": "930914", "display": "港股通高股息", "official": "中证港股通高股息(930914)", "etf": "513820", "substitute": False},
]

UNIVERSE = A_UNIVERSE + HK_UNIVERSE

# 申万口径的代码白名单（含 801005 申万创业）。服务端返回全部行业，过滤在本机做。
_SW_CODES = {i["code"] for i in A_UNIVERSE} | {"801005"}


def conf() -> dict:
    out = dict(DEFAULT_CONF)
    try:
        import yaml
        p = BASE_DIR / "config.yaml"
        if p.exists():
            got = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("market_temperature") or {}
            out.update({k: v for k, v in got.items() if k in DEFAULT_CONF and v is not None})
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- 纯计算
def last_le(mapping: dict, d: str):
    """字典键是 ISO 日期时，取 <= d 的最大键对应的值。"""
    best = None
    for k, v in mapping.items():
        if k <= d and v is not None and (best is None or k > best[0]):
            best = (k, v)
    return best[1] if best else None


def band_of(temp: float | None) -> dict:
    """温度的分档。阈值与截图一致：<30 / 30~50 / >50。"""
    if temp is None:
        return {"key": "na", "label": "无数据", "advice": "—"}
    for hi, key, label, advice in BANDS:
        if temp < hi:
            return {"key": key, "label": label, "advice": advice}
    return {"key": "hot", "label": "高温 · 开始卖出", "advice": "不可提高仓位"}


def percentile(series: list[float], value: float) -> float | None:
    """value 在 series 里的分位数 ×100（含 value 自己所在的这一期）。

    用「<= value 的样本占比」而不是线性插值：可复算、无插值口径分歧，
    且和截图给的十年统计项（≥50 / <30 计数）同量纲。
    """
    vals = [v for v in series if v is not None and math.isfinite(v)]
    if not vals or value is None or not math.isfinite(value):
        return None
    return 100.0 * sum(1 for v in vals if v <= value) / len(vals)


def gain_pct(base: float | None, last: float | None) -> float | None:
    if not base or last is None:
        return None
    try:
        return round((float(last) / float(base) - 1.0) * 100.0, 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _sampling_dates(cal: list[str], today: date) -> list[str]:
    """季末（2016Q4~2025Q3）+ 月末（2025Q4 起）。

    cal 是申万公布过的月末交易日（`index_analysis_week_month_sw("month")`），
    形如 ['2016-12-30', ...]；季度取该季的季末月。
    """
    out: list[str] = []
    y, m = SERIES_START.year, SERIES_START.month
    while (y, m) <= (today.year, today.month):
        stop = (y, m) >= (2025, 10)
        if stop or m in (3, 6, 9, 12):
            ym = f"{y:04d}-{m:02d}"
            cands = [c for c in cal if c[:7] == ym and c <= today.isoformat()]
            if cands:
                out.append(max(cands))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def sample_csi(rows: list[dict], dates: list[str]) -> dict[str, dict]:
    """从日线里按采样日取「<= 该日的最后一根」。"""
    by_day = sorted(({"d": r["d"], "pe": r.get("pe"), "close": r.get("close")}
                     for r in rows if r.get("d")), key=lambda r: r["d"])
    out: dict[str, dict] = {}
    i = 0
    for target in dates:
        best = None
        while i < len(by_day) and by_day[i]["d"] <= target:
            if by_day[i]["pe"] is not None:
                best = by_day[i]
            i += 1
        if best is not None and best["d"] <= target:
            out[target] = best
    return out


def temperature_of(samples: dict[str, float], as_of: str) -> dict:
    """单个标的在 as_of 这天的温度。samples 是 {采样日: PE}（升序无关，内部会排）。"""
    days = sorted(k for k, v in samples.items() if v is not None)
    hist = [samples[d] for d in days if d <= as_of]
    if not hist:
        return {"temp": None, "pe": None, "samples": 0, "insufficient": True, "first": None}
    pe = hist[-1]
    temp = percentile(hist, pe)
    return {
        "temp": round(temp, 2) if temp is not None else None,
        "pe": pe,
        "samples": len(hist),
        "insufficient": len(hist) < MIN_SAMPLES,
        "first": days[0],
    }


def aggregate_at(items: list[tuple[str, dict[str, float]]], as_of: str) -> dict:
    """综合温度 = 各标的温度的等权算术平均。

    items: [(key, {采样日: PE}), ...]
    返回 mean / median / count / missing（历史不足 3 年被剔出的 key）。
    """
    temps: list[float] = []
    missing: list[str] = []
    for key, samples in items:
        t = temperature_of(samples, as_of)
        if t["temp"] is None:
            continue
        if t["insufficient"]:
            missing.append(key)
            continue
        temps.append(t["temp"])
    if not temps:
        return {"temp": None, "median": None, "count": 0, "missing": missing}
    srt = sorted(temps)
    n = len(srt)
    med = srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2.0
    return {"temp": round(sum(temps) / n, 2), "median": round(med, 2), "count": n, "missing": missing}


# ---------------------------------------------------------------- 取数
def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- 101 数据服务
# 本机不直连任何行情/估值源（用户 2026-10-28 要求）。出口只有下面两处：
# `_base_url()` 解析地址，`_get_json()` 发请求。范式与 market_data.py 逐字同构
# （trust_env=False 绕开本机代理），改一处就要两处一起改。
_session = requests.Session()
_session.trust_env = False


def _base_url() -> str:
    url = os.environ.get("SA_DATA_SERVICE_URL", "").strip()
    if not url:
        try:
            import yaml
            with open(BASE_DIR / "config.yaml", encoding="utf-8") as f:
                conf = (yaml.safe_load(f) or {}).get("sector") or {}
            url = str(conf.get("data_service_url") or "").strip()
        except FileNotFoundError:
            url = ""
        except Exception as exc:      # noqa: BLE001
            raise RuntimeError("读取 config.yaml 的 sector.data_service_url 失败: %s" % exc)
    if not url:
        raise RuntimeError("未配置行情服务地址：请设置 SA_DATA_SERVICE_URL 或 config.yaml 的 "
                           "sector.data_service_url（本机不直连任何行情源）")
    return url.rstrip("/")


def _get_json(path: str, params: dict, timeout: int = 60) -> dict:
    url = _base_url() + path
    try:
        resp = _session.get(url, params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise RuntimeError("行情服务不可达 %s: %s" % (url, str(exc)[:140]))
    if resp.status_code >= 400:
        raise RuntimeError("行情服务 %s 返回 %s: %s" % (path, resp.status_code, resp.text[:200]))
    try:
        payload = resp.json()
    except ValueError as exc:
        raise RuntimeError("行情服务 %s 返回非 JSON: %s" % (path, exc))
    if not isinstance(payload, dict):
        raise RuntimeError("行情服务 %s 返回结构异常（%s）" % (path, type(payload).__name__))
    return payload


def fetch_calendar() -> list[str]:
    """申万公布过的月末交易日（ISO 字符串，升序）。"""
    dates = _get_json("/sw-calendar", {}, timeout=90).get("dates") or []
    return sorted(str(d)[:10] for d in dates if d)


def _f(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _norm_items(items) -> dict[str, dict]:
    """服务端 items → 本地口径 {code: {pe, close, name}}，并按本文件的标的清单过滤。"""
    out: dict[str, dict] = {}
    if not isinstance(items, dict):
        return out
    for code, v in items.items():
        code = str(code)
        if not re.fullmatch(r"\d{6}", code) or code not in _SW_CODES:
            continue
        if not isinstance(v, dict):
            continue
        pe, close = _f(v.get("pe")), _f(v.get("close"))
        if pe is None and close is None:
            continue
        out[code] = {"pe": pe, "close": close, "name": str(v.get("name") or "")}
    return out


def fetch_sw_month(date_str: str) -> dict[str, dict]:
    """一个月末 → 一级行业 + 801005 申万创业的 PE/收盘（经 101 `/sw-month`）。"""
    got = _get_json("/sw-month", {"date": date_str}, timeout=120)
    return _norm_items(got.get("items"))


def fetch_sw_day(date_str: str) -> dict[str, dict]:
    """单个交易日 → 一级行业 + 801005 申万创业的当日 PE/收盘（经 101 `/sw-day`）。"""
    got = _get_json("/sw-day", {"date": date_str}, timeout=120)
    return _norm_items(got.get("items"))


def fetch_csi(code: str, start: str, end: str) -> list[dict]:
    """中证指数日线 [{d, close, pe}]（经 101 `/index-hist`）。

    失败**向上抛**而不是返回空列表：空列表与「该指数真的没有数据」无法区分，
    会把「101 挂了」显示成「这只指数没数据」（静默降级）。调用方 `refresh()`
    把原因写进 note。
    """
    got = _get_json("/index-hist", {"code": code, "start": start, "end": end}, timeout=120)
    rows: list[dict] = []
    for r in got.get("rows") or []:
        if not isinstance(r, dict):
            continue
        d = str(r.get("d") or "")[:10]
        if not d:
            continue
        rows.append({"d": d, "close": _f(r.get("close")), "pe": _f(r.get("pe"))})
    rows.sort(key=lambda x: x["d"])
    return rows


def refresh(progress=None, today: date | None = None) -> dict:
    """采样 → 落盘。每一步立刻写文件，中断后重跑只补没拿到的部分。"""
    today = today or date.today()
    note = []

    def say(msg):
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    # 1) 月末日历（决定采样日）
    cal_path = DATA_DIR / "cal.json"
    cal = _read_json(cal_path)
    if not cal or cal.get("as_of") != today.isoformat():
        try:
            cal = {"as_of": today.isoformat(), "dates": fetch_calendar()}
            if not cal["dates"]:
                raise RuntimeError("申万月末日历为空")
            _write_json(cal_path, cal)
        except Exception as e:
            note.append(f"申万月末日历取不到：{type(e).__name__}: {str(e)[:80]}")
            cal = cal or {"as_of": "", "dates": []}
    dates = _sampling_dates(cal["dates"], today)
    if not dates:
        return {"ok": False, "note": "; ".join(note) or "采样日历为空", "files": 0}

    def bad(d: str, what: str, err: str) -> None:
        """单日取不到只记 note，绝不中断整轮。

        当天（`d == today`）尚未发布属于正常态 —— 申万日报要收盘后才出，
        盘中来刷必然 502，进 note 会变成每天一条噪音。打印出来是为了
        「静默降级」不至于完全无声。
        """
        if d == today.isoformat():
            print(f"[market_temp] {what} {d} 取不到（当天可能尚未发布）：{err}", flush=True)
            return
        note.append(f"{what} {d} 取不到：{err}")

    # 2) 每个采样日一个文件（一级行业 + 市场表征 801005）
    todo = [d for d in dates if not (SW_DIR / f"{d}.json").exists()]
    for n, d in enumerate(todo, 1):
        err, got = "", {}
        try:
            got = fetch_sw_month(d)
        except RuntimeError as exc:
            err = str(exc)[:90]
        if got:
            _write_json(SW_DIR / f"{d}.json", got)
        elif err:
            bad(d, "申万月报", err)
        else:
            note.append(f"{d} 无数据")
        say(f"申万月报 {n}/{len(todo)} {d}")
        if n % 5 == 0:
            time.sleep(0.4)

    # 3) 最新交易日 + 前一交易日 + 两个涨幅基期（只走日频，一次拿全 31 行）
    day_dates = _recent_trading_days(dates, today)
    for d in day_dates:
        p = SW_DIR / f"day-{d}.json"
        if p.exists():
            continue
        err, got = "", {}
        try:
            got = fetch_sw_day(d)
        except RuntimeError as exc:
            err = str(exc)[:90]
        if got:
            _write_json(p, got)
        elif err:
            bad(d, "申万日报", err)
        else:
            note.append(f"{d} 无数据")
        say(f"申万日报 {d}（{len(got)} 行）")

    # 4) 中证指数：每个代码一次请求拿全 10 年日线（PE + 收盘都在里面）
    end = today.strftime("%Y%m%d")
    for n, item in enumerate(UNIVERSE, 1):
        if not item["key"].startswith("csi:"):
            continue
        code = item["code"]
        p = CSI_DIR / f"{code}.json"
        old = _read_json(p) or {}
        if old.get("fetched") == today.isoformat():
            continue
        err = ""
        try:
            rows = fetch_csi(code, SERIES_START.strftime("%Y%m%d"), end)
        except RuntimeError as exc:
            err = str(exc)[:90]
            rows = []
        if rows:
            _write_json(p, {"fetched": today.isoformat(), "rows": rows})
        elif err:
            note.append(f"{code} 取数失败：{err}")
        else:
            note.append(f"{code} 无数据")
        say(f"中证 {n} {code} {item['display']}（{len(rows)} 根）")
    return {"ok": True, "sampling": len(dates), "sw_new": len(todo),
            "note": "; ".join(note[:20]), "as_of": today.isoformat()}


def _recent_trading_days(dates: list[str], today: date) -> list[str]:
    """要额外发日频请求的日期：最近两个自然日窗口 + 两个涨幅基期。

    基期不是交易日也没关系 —— 取「<= 基期的最后一个已公布月末」当日近似，
    申万日报对非交易日返回空，那时退回基期之前最近的采样日。
    """
    out: list[str] = []
    probe = today
    for _ in range(9):
        d = probe.isoformat()
        if d not in out and probe.isoweekday() <= 5:
            out.append(d)
        probe -= timedelta(days=1)
        if len(out) >= 3:
            break
    for base in (YTD_BASE, GAIN924_BASE):
        d = base.isoformat()
        if d not in out:
            out.append(d)
    return out


# ---------------------------------------------------------------- 汇总
def _sw_samples() -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]], dict[str, dict[str, float]]]:
    """扫盘：{code: {采样日: PE}}, {code: {采样日: 收盘}}, {code: {日期: 当日行}}"""
    pe: dict[str, dict[str, float]] = {}
    close: dict[str, dict[str, float]] = {}
    days: dict[str, dict] = {}
    if not SW_DIR.exists():
        return pe, close, days
    for p in sorted(SW_DIR.glob("*.json")):
        d = p.name[:-5]
        body = _read_json(p) or {}
        if d.startswith("day-"):
            d2 = d[4:]
            days[d2] = body
            for code, v in body.items():
                pe.setdefault(code, {})[d2] = v.get("pe")
                close.setdefault(code, {})[d2] = v.get("close")
            continue
        for code, v in body.items():
            pe.setdefault(code, {})[d] = v.get("pe")
            close.setdefault(code, {})[d] = v.get("close")
    return pe, close, days


def _csi_rows(code: str) -> list[dict]:
    return (_read_json(CSI_DIR / f"{code}.json") or {}).get("rows") or []


def build(today: date | None = None) -> dict | None:
    """从盘上的原始数据算出整份温度计。没数据返回 None。"""
    today = today or date.today()
    cal = _read_json(DATA_DIR / "cal.json") or {}
    dates = _sampling_dates(cal.get("dates") or [], today)
    if not dates:
        return None
    sw_pe, sw_close, sw_days = _sw_samples()
    if not sw_pe:
        return None

    as_of = max(sw_days) if sw_days else dates[-1]
    prev_days = sorted(d for d in sw_days if d < as_of)
    prev_as_of = prev_days[-1] if prev_days else None

    dset = set(dates)

    def pe_at(it, d: str | None) -> float | None:
        if not d:
            return None
        if it["key"].startswith("sw:"):
            return ((sw_days.get(d) or {}).get(it["code"]) or {}).get("pe")
        got = [r for r in _csi_rows(it["code"]) if r.get("pe") is not None and r["d"] <= d]
        return got[-1]["pe"] if got else None

    items = []
    for it in UNIVERSE:
        key = it["key"]
        if key.startswith("sw:"):
            samples = {d: v for d, v in (sw_pe.get(it["code"]) or {}).items() if d in dset}
            closes = sw_close.get(it["code"]) or {}
        else:
            sampled = sample_csi(_csi_rows(it["code"]), dates)
            samples = {d: v["pe"] for d, v in sampled.items()}
            closes = {d: v["close"] for d, v in sampled.items()}
        # 采样序列止于最近一个已公布月末；当日（与前一交易日）的 PE 补进去，
        # 否则页面与综合温度会拿一个月前的估值当「今日温度」。
        for d in (prev_as_of, as_of):
            v = pe_at(it, d)
            if v is not None:
                samples[d] = v
        items.append((key, samples, it, closes))

    def close_at(it, d: str) -> float | None:
        """<= d 的最后一个收盘价。申万走日频文件，缺该日则退回采样序列。"""
        if it["key"].startswith("sw:"):
            got = ((sw_days.get(d) or {}).get(it["code"]) or {}).get("close")
            if got is not None:
                return got
            return last_le(sw_close.get(it["code"]) or {}, d)
        got = [r for r in _csi_rows(it["code"]) if r["d"] <= d and r.get("close") is not None]
        return got[-1]["close"] if got else None

    def cur_close(it) -> float | None:
        return close_at(it, as_of)

    def base_close(it, base: str) -> float | None:
        return close_at(it, base)

    rows_out = []
    for group, universe in (("a", A_UNIVERSE), ("hk", HK_UNIVERSE)):
        pairs = [(k, s) for k, s, it, _c in items if it in universe]
        cur = aggregate_at(pairs, as_of)
        prv = aggregate_at(pairs, prev_as_of) if prev_as_of else {"temp": None}
        series = []
        for d in dates:
            a = aggregate_at(pairs, d)
            series.append({"d": d, "temp": a["temp"], "count": a["count"]})
        rows_out.append({
            "group": group,
            "label": "A股" if group == "a" else "港股",
            "temp": cur["temp"],
            "prev_temp": prv.get("temp"),
            "delta": round(cur["temp"] - prv["temp"], 2) if cur["temp"] is not None and prv.get("temp") is not None else None,
            "count": cur["count"],
            "excluded": cur["missing"],
            "band": band_of(cur["temp"]),
            "series": series,
            "universe": len(universe),
        })

    in_a = {i["key"] for i in A_UNIVERSE}
    detail = []
    for key, samples, it, _c in items:
        t = temperature_of(samples, as_of)
        # name = 展示名（页面「行业名称」列），official = 数据源对齐名。
        # 两者只在展示层分工，计算与匹配一律用 key / code。
        detail.append({
            "key": key,
            "code": it["code"],
            "name": it["display"],
            "official": it["official"],
            "kind": it.get("kind") or ("行业" if key in in_a else "港股指数"),
            "etf": it.get("etf") or "",
            "substitute": bool(it.get("substitute")),
            "pe": t["pe"],
            "temp": t["temp"],
            "band": band_of(t["temp"]),
            "samples": t["samples"],
            "insufficient": t["insufficient"],
            "first": t["first"],
            "ytd": gain_pct(base_close(it, YTD_BASE.isoformat()), cur_close(it)),
            "g924": gain_pct(base_close(it, GAIN924_BASE.isoformat()), cur_close(it)),
            "group": "a" if key in in_a else "hk",
        })
    stats = {}
    for g in ("a", "hk"):
        cur = [r for r in detail if r["group"] == g and r["temp"] is not None]
        series = next((x["series"] for x in rows_out if x["group"] == g), [])
        hist = [s for s in series if s["temp"] is not None]
        vals = [s["temp"] for s in hist]
        if cur:
            hi = max(cur, key=lambda r: r["temp"])
            lo = min(cur, key=lambda r: r["temp"])
            stats[g] = {
                "max": {"v": round(max(vals), 2), "d": max(hist, key=lambda s: s["temp"])["d"]} if vals else None,
                "min": {"v": round(min(vals), 2), "d": min(hist, key=lambda s: s["temp"])["d"]} if vals else None,
                "avg": round(sum(vals) / len(vals), 2) if vals else None,
                "ge50": sum(1 for v in vals if v >= 50),
                "lt30": sum(1 for v in vals if v < 30),
                "hot_name": hi["name"], "cold_name": lo["name"],
                "hot_temp": hi["temp"], "cold_temp": lo["temp"],
            }
    return {
        "as_of": as_of,
        "prev_as_of": prev_as_of,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "markets": rows_out,
        "rows": sorted(detail, key=lambda r: (r["group"], r["temp"] if r["temp"] is not None else 999)),
        "stats": stats,
        "note": "温度 = 各自最新 PE(TTM) 在近 10 年历史序列中的分位数 ×100；综合温度 = 等权平均（非市值加权）。"
                "温度是估值位置的情绪刻度，不是点位预测。",
    }


_CACHE: dict[str, Any] = {"sig": None, "at": 0.0, "data": None}
CACHE_TTL = 300.0


def _signature() -> tuple:
    """缓存失效判据 = (文件数, 最新 mtime)。refresh 落盘后必然变化。"""
    count, newest = 0, 0.0
    try:
        for p in DATA_DIR.rglob("*.json"):
            try:
                st = p.stat()
            except OSError:
                continue
            count += 1
            if st.st_mtime > newest:
                newest = st.st_mtime
    except OSError:
        pass
    return (count, round(newest, 3))


def invalidate_cache() -> None:
    with _LOCK:
        _CACHE.update(sig=None, at=0.0, data=None)


def read(fresh: bool = False) -> dict | None:
    """带缓存的全量视图。**决策路径每只票都会调，不能每次重建 61 个 JSON。**"""
    try:
        sig = _signature()
        now = time.time()
        with _LOCK:
            if (not fresh and _CACHE["at"] > 0
                    and _CACHE["sig"] == sig and now - _CACHE["at"] < CACHE_TTL):
                return _CACHE["data"]
        data = build()
        with _LOCK:
            _CACHE.update(sig=sig, at=now, data=data)
        return data
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


INJECT_DEFAULTS = {"enabled": True, "max_chars": 1200}


def build_injection(max_chars: int = 1200) -> str:
    """决策上下文注入块。没有缓存/算不出来时返回 ""（上下文逐字节不变）。"""
    d = read()
    if not d or "error" in d or not d.get("markets"):
        return ""
    lines = ["【市场温度（PE 十年分位数，等权平均；是估值位置不是点位预测）】",
             f"数据截至 {d['as_of']}："]
    for m in d["markets"]:
        if m["temp"] is None:
            continue
        dlt = m.get("delta")
        head = (f"  · {m['label']}市场温度 {m['temp']:.1f}°C"
                f"（{m['count']}/{m['universe']} 个标的，{m['band']['label']}")
        lines.append(head + (f"，较上一交易日 {dlt:+.2f}°）" if dlt is not None else "）"))
    hot = [r for r in d["rows"] if r["temp"] is not None and r["temp"] >= 50][:6]
    cold = [r for r in sorted(d["rows"], key=lambda r: r["temp"] if r["temp"] is not None else 999)
            if r["temp"] is not None and r["temp"] < 30][:6]
    if hot:
        lines.append("  高温（>50°C，不可提高仓位）：" +
                     "、".join(f"{r['name']} {r['temp']:.0f}°" for r in hot))
    if cold:
        lines.append("  低温（<30°C，可提高仓位）：" +
                     "、".join(f"{r['name']} {r['temp']:.0f}°" for r in cold))
    subs = [r for r in d["rows"] if r["substitute"]]
    if subs:
        lines.append(f"  ⚠ 港股有 {len(subs)} 个标的是**中证港股通同主题指数替代**"
                     "（恒生系指数无任何公开 PE 源），估值口径与恒生原版不完全一致。")
    lines.append("  用法：只当「现在该不该提高总仓位」的仓位约束参考，"
                 "不得当作个股买卖的直接触发条件；PE 分位数低不代表马上涨。")
    txt = "\n".join(lines)
    if len(txt) > max_chars:
        txt = txt[:max_chars] + f"…[市场温度块超长已截断，全文 {len(txt)} 字符]"
    return txt
