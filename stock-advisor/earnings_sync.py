"""财报排期自动同步：持仓 + 所属板块 A股/美股龙头 的财报披露时间。

与 econ_calendar 的 CPI 同步同模式（2026-10-17 加）：JSON 缓存、不进数据库、
并入财经日历 upcoming_events()，页面与盘前提醒自动可见。

数据源（全部实测，2026-10-17，别再换）：
- A股定期报告预约披露：akshare `stock_yysj_em`（东财预约披露时间表）。
  实测 20260930 期次 5225 行 / 4.5s / 11 页；更远期次（如当年年报 1231）
  东财还没挂出 → 单期失败不是致命错误，滚动重扫补上。
- 美股财报日历：Nasdaq 官方 `api.nasdaq.com/api/calendar/earnings?date=`。
  实测返回当日全市场排期（symbol / time-pre-market / time-after-hours /
  fiscalQuarterEnding），前瞻窗口约 7~8 周（10-17 能看到 12-10）。
  ⚠️ Yahoo quoteSummary 403、BLS 页 403（Akamai），都别再试。

板块 → 龙头映射（唯一的语义环节，走 LLM + 严格校验）：
- 持仓里 A/H/ETF 三种市场都可能出现，数据源无法把「港股/ETF」映射到
  「A股龙头 + 美股龙头」，这层只能让 LLM 判断（task=standard），结果落
  data/earnings_map.json；持仓代码集合变化 / 超过 30 天 / 手动 force 才重建
  （增量：只补缺的代码，已有条目不动）。
- 防幻觉校验（宁缺毋滥）：A股代码必须 6 位数字；若名册里有该代码但名字
  对不上 → 整条丢弃（实测模型真会写「600502 上上电缆」——代码-名字错配，
  600502 实际是安徽建工）；美股 ticker 必须能被腾讯行情 `usXXX` 取到，
  取不到丢弃。
- ⚠️ 名册 sa_stock_roster 不完整（实测 000333 美的集团缺失），所以
  「不在名册」**不算**无效；真正的闸门在同步期：A股代码不在东财预约披露
  表里就不会产生事件，并记入 unmatched 供排查。

事件口径：
- 只落未来日期（已披露完的自动消失）。A股取最新约定：实际披露时间 >
  三次变更 > 二次变更 > 一次变更 > 首次预约。
- 美股日期按美东日历日（与全球财经网站口径一致），note 写清北京时间含义
  （盘后公布 ≈ 北京时间次日凌晨），日期本身不换算。
- level=mid：今明提醒会合并列出；**不参与「提前 3 天」预告** —— 财报季
  龙头事件可能几十条，逐个提前 3 天推送会把提醒淹掉（噪音等于没报警）。
"""

import json
import re
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
MAP_FILE = BASE_DIR / "data" / "earnings_map.json"
SYNC_FILE = BASE_DIR / "data" / "earnings_sync.json"

SYNC_HORIZON_DAYS = 60          # 美股财报日历扫描窗口（天）
SYNC_REQUEST_DELAY = 0.15       # 请求间隔（秒）
MAP_TTL_DAYS = 30               # 板块映射缓存有效期（天）
_SYNC_LOCK = threading.Lock()

_PERIOD_LABEL = {(3, 31): "一季报", (6, 30): "半年报",
                 (9, 30): "三季报", (12, 31): "年报"}

_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

# Nasdaq 财报日历
_NASDAQ_URL = "https://api.nasdaq.com/api/calendar/earnings"
_NASDAQ_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
}

# 美股公布时段 → 北京时间提示（日期本身不换算，与全球财经网站口径一致）
_US_TIME_NOTE = {
    "time-pre-market": "美股盘前公布（北京时间当日晚间）",
    "time-after-hours": "美股盘后公布（北京时间次日凌晨~早晨）",
    "time-not-supplied": "公布时段未定（通常为盘后）",
}


# ---------------- 基础小工具 ----------------

def _clean(v) -> str:
    """pandas 的 NaN/NaT/None 等脏值统一成空串。"""
    s = str(v if v is not None else "").strip()
    return "" if s in ("nan", "NaT", "None") else s


def _parse_date(v) -> str:
    """把 NaT/None/Timestamp/'2026-10-28' 统一成 'YYYY-MM-DD'，失败返回空串。"""
    s = str(v if v is not None else "").strip()
    if not s or s in ("nan", "NaT", "None"):
        return ""
    m = _DATE_RE.search(s)
    if not m:
        return ""
    out = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    try:
        date.fromisoformat(out)
    except ValueError:
        return ""
    return out


def _norm_name(s: str) -> str:
    """公司名归一化：去空格/星号、去双上市字母后缀与常见后缀，用于一致性校验。"""
    s = re.sub(r"[\s\u3000*]", "", str(s or ""))
    s = re.sub(r"[A-Za-z]{1,2}$", "", s)
    s = re.sub(r"(股份|集团|控股)$", "", s)
    return s


def _name_ok(a: str, b: str) -> bool:
    """两个公司名是否指同一家（归一化后相等或互相包含）。"""
    na, nb = _norm_name(a), _norm_name(b)
    if not na or not nb:
        return True
    return na == nb or na in nb or nb in na


def _kind_label(code: str) -> str:
    """市场标签（与 app._market_of 的口径一致，只用于展示/路由）。"""
    if len(code) == 5 and code.isdigit():
        return "港股"
    if len(code) == 6 and code.startswith(("5", "1")):
        return "ETF"
    return "A股"


def _is_cn_stock(code: str) -> bool:
    """是否按 A 股定期报告口径跟踪（6 位且非基金/债券代码段）。"""
    return len(code) == 6 and code.isdigit() and not code.startswith(("5", "1"))


# ---------------- 板块映射（LLM + 校验） ----------------

_MAPPING_SYSTEM = """你是同时跟踪 A 股、港股、美股的分析师。任务：为一批持仓证券识别其所属「板块/主题」，并给出该板块在 A 股与美股各自最具代表性的龙头公司（各最多 1 家）。

规则：
1. sector：简短中文板块名，必须是**公司真实主营业务所属板块**，不要望文生义（示例：「蜂助手」是数字化服务/虚拟运营商，不是「蜂业」）。同一板块的多个证券必须写完全相同的板块名。
2. cn_leader：该板块在 A 股（沪深北）最领先的公司，代码必须 6 位数字、名称与代码真实对应；没有合适对象就输出 null，**严禁编造代码**。
3. us_leader：该板块在美国市场（NYSE/NASDAQ）的全球龙头，**用 ticker 键**写交易所代码（如 NVDA）；没有合适对象就输出 null。
4. ETF：按其跟踪指数/主题的板块处理，不要当成基金公司。
5. 港股：按公司实际业务板块处理。
6. 无法判断的证券：sector 写「未知」、两个 leader 都输出 null。
7. 只输出一个 JSON 数组（不要 markdown 代码块、不要解释文字），每个输入代码一条，条数与输入一致。

输出示例：
[{"code":"588000","sector":"半导体","cn_leader":{"code":"688981","name":"中芯国际"},"us_leader":{"ticker":"NVDA","name":"英伟达"}}]"""


def _mapping_prompt(universe: list[dict]) -> tuple[str, str]:
    lines = [f"{u['code']} {u['name']}（{_kind_label(u['code'])}）" for u in universe]
    return _MAPPING_SYSTEM, "持仓列表：\n" + "\n".join(lines)


def _llm_mapping_call(system: str, user: str) -> str:
    """默认 LLM 出口（可注入替换）。网关 503 是常态，退避重试。"""
    import llm_advisor
    last: Exception | None = None
    for attempt in range(4):
        try:
            return llm_advisor.ask(system, user, task="standard")
        except Exception as exc:                     # noqa: BLE001
            last = exc
            print(f"[earnings] 板块映射 LLM 第 {attempt + 1} 次失败: "
                  f"{str(exc)[:160]}", flush=True)
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"LLM 映射连续失败: {last}")


def _json_candidates(text: str):
    s = str(text or "").strip()
    yield s
    for m in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", s):
        yield m.group(1).strip()
    a, b = s.find("["), s.rfind("]")
    if a >= 0 and b > a:
        yield s[a:b + 1]


def _parse_json_array(text: str) -> list:
    """从模型输出里稳健地抠出 JSON 数组（容忍围栏/前后缀）。"""
    for cand in _json_candidates(text):
        if not cand:
            continue
        try:
            d = json.loads(cand)
        except ValueError:
            continue
        if isinstance(d, list):
            return d
        if isinstance(d, dict) and isinstance(d.get("items"), list):
            return d["items"]
    raise ValueError("模型输出里找不到 JSON 数组")


def _validate_items(raw_items: list, universe: list[dict],
                    roster: dict | None, warnings: list[str]) -> dict[str, dict]:
    """校验模型输出 → {code: entry}。宁缺毋滥，坏条目降级 null 并记 warning。"""
    by_code = {u["code"]: u for u in universe}
    roster = roster or {}
    out: dict[str, dict] = {}
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        code = str(it.get("code") or "").strip()
        if code not in by_code:
            if code:
                warnings.append(f"映射条目 {code} 不在持仓列表（跳过）")
            continue
        entry = {"name": by_code[code]["name"],
                 "sector": (str(it.get("sector") or "").strip() or "未知")[:16],
                 "cn_leader": None, "us_leader": None}
        cn = it.get("cn_leader")
        if isinstance(cn, str):                       # 容忍只写代码串
            cn = {"code": cn}
        if isinstance(cn, dict) and _clean(cn.get("code")):
            c = _clean(cn.get("code"))
            nm = _clean(cn.get("name"))
            if not re.fullmatch(r"\d{6}", c):
                warnings.append(f"{code}: A股龙头代码格式不对（{c}，已丢弃）")
            else:
                rn = roster.get(c) or ""
                if rn and nm and not _name_ok(rn, nm):
                    warnings.append(f"{code}: A股龙头 {c} 名字对不上"
                                    f"（模型写「{nm}」，名册是「{rn}」，已丢弃）")
                else:
                    entry["cn_leader"] = {"code": c, "name": rn or nm or c}
        us = it.get("us_leader")
        if isinstance(us, str):                       # 容忍只写 ticker 串
            us = {"ticker": us}
        if isinstance(us, dict):
            tk = _clean(us.get("ticker") or us.get("code")).upper()
            nm = _clean(us.get("name"))
            if not tk:
                pass
            elif not re.fullmatch(r"[A-Z][A-Z0-9]{0,5}", tk):
                warnings.append(f"{code}: 美股龙头代码格式不对（{tk}，已丢弃）")
            else:
                entry["us_leader"] = {"ticker": tk, "name": nm or tk}
        out[code] = entry
    return out


def _quote_us(tickers: list[str]) -> dict[str, str]:
    """腾讯行情批量取美股名称（兼当 ticker 真实性校验）。GBK 响应。

    返回 {ticker: 中文名}；取不到的 ticker 不在返回里。
    """
    import requests
    out: dict[str, str] = {}
    tks = [t for t in tickers if t]
    for i in range(0, len(tks), 40):
        chunk = tks[i:i + 40]
        resp = requests.get("https://qt.gtimg.cn/q="
                            + ",".join("us" + t for t in chunk), timeout=20)
        resp.encoding = "gbk"
        for seg in (resp.text or "").split(";"):
            m = re.match(r'v_us([A-Z0-9.\-]+)="([^"]*)"', seg.strip())
            if not m:
                continue
            parts = m.group(2).split("~")
            if len(parts) > 2 and parts[1].strip():
                out[m.group(1)] = parts[1].strip()
    return out


def build_mapping(universe: list[dict], *, roster: dict | None = None,
                  llm_fn=None, quote_fn=None) -> dict:
    """对给定证券跑一轮 LLM 映射 + 校验，返回 mapping payload（不落盘）。

    llm_fn(system, user) / quote_fn(tickers) 可注入（离线测试用）。
    """
    llm = llm_fn or _llm_mapping_call
    system, user = _mapping_prompt(universe)
    text = llm(system, user)
    warnings: list[str] = []
    raw = _parse_json_array(text)
    items = _validate_items(raw, universe, roster, warnings)

    # LLM 漏掉的代码也要落「未知」占位 —— 否则每次都因为缺条目触发重建
    for u in universe:
        if u["code"] not in items:
            items[u["code"]] = {"name": u["name"], "sector": "未知",
                                "cn_leader": None, "us_leader": None}
            warnings.append(f"{u['code']} {u['name']}: 模型未返回该条（板块未知）")

    # 美股 ticker 存在性批量校验（腾讯取不到的丢弃）
    tickers = sorted({v["us_leader"]["ticker"] for v in items.values()
                      if v.get("us_leader")})
    if tickers:
        try:
            names = (quote_fn or _quote_us)(tickers)
        except Exception as exc:                      # noqa: BLE001
            warnings.append(f"美股 ticker 校验失败（本轮跳过校验）: "
                            f"{type(exc).__name__}: {str(exc)[:120]}")
            names = None
        if names is not None:
            for c, v in items.items():
                ul = v.get("us_leader")
                if not ul:
                    continue
                if ul["ticker"] not in names:
                    warnings.append(f"{c}: 美股龙头 {ul['ticker']} 行情取不到（已丢弃）")
                    v["us_leader"] = None
                elif names.get(ul["ticker"]):
                    ul["name"] = names[ul["ticker"]]   # 腾讯中文名（英伟达）

    cap = warnings[:12]
    return {"built_at": datetime.now().isoformat(timespec="seconds"),
            "items": items, "warnings": cap,
            "warn": "；".join(cap[:3])[:300]}


def _load_map() -> dict:
    try:
        d = json.loads(MAP_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_map(payload: dict) -> None:
    MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = MAP_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(MAP_FILE)


def mapping_meta() -> dict:
    """板块映射现状（页面显示用）。"""
    m = _load_map()
    items = m.get("items") or {}
    n_cn = sum(1 for v in items.values() if isinstance(v, dict) and v.get("cn_leader"))
    n_us = sum(1 for v in items.values() if isinstance(v, dict) and v.get("us_leader"))
    return {"built_at": m.get("built_at") or "", "items": len(items),
            "leaders_cn": n_cn, "leaders_us": n_us, "warn": m.get("warn") or ""}


# ---------------- 数据源抓取 ----------------

def _fetch_cn_period(period_end: date) -> list[dict]:
    """东财预约披露时间表（某一报告期）。返回行 dict 列表；失败抛异常。"""
    import akshare as ak
    df = ak.stock_yysj_em(symbol="沪深A股", date=period_end.strftime("%Y%m%d"))
    if df is None or len(df) == 0:
        raise RuntimeError("空表（该期次尚未排期）")
    return [dict(r) for r in df.to_dict("records")]


def _cn_rows_to_map(rows: list[dict]) -> dict[str, dict]:
    """预约披露行 → {code: {name, first, actual, eff}}；eff=最新约定日期。"""
    out: dict[str, dict] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        code = _clean(r.get("股票代码"))
        if not code:
            continue
        first = _parse_date(r.get("首次预约时间"))
        chg = [_parse_date(r.get(c)) for c in ("一次变更日期", "二次变更日期", "三次变更日期")]
        actual = _parse_date(r.get("实际披露时间"))
        eff = actual or next((d for d in reversed(chg) if d), "") or first
        out[code] = {"name": _clean(r.get("股票简称")), "first": first,
                     "actual": actual, "eff": eff}
    return out


def _fetch_us_day(day: date, session=None) -> list[dict]:
    """Nasdaq 官方财报日历（某一天的全市场排期）。失败抛异常，无数据返回 []。"""
    import requests
    sess = session if session is not None else requests
    resp = sess.get(_NASDAQ_URL, params={"date": day.isoformat()},
                    headers=_NASDAQ_HEADERS, timeout=25)
    resp.raise_for_status()
    js = resp.json()
    data = js.get("data") if isinstance(js, dict) else None
    rows = data.get("rows") if isinstance(data, dict) else None
    return rows if isinstance(rows, list) else []


def _quarter_ends(today: date) -> list[date]:
    """最近两个已过的季末 + 下一个季末（年报/一季报排期会提前挂出）。"""
    ends: list[date] = []
    for y in (today.year - 1, today.year, today.year + 1):
        for m, d in ((3, 31), (6, 30), (9, 30), (12, 31)):
            ends.append(date(y, m, d))
    past = [e for e in ends if e <= today]
    future = [e for e in ends if e > today]
    return past[-2:] + future[:1]


# ---------------- 同步主流程 ----------------

def _load_sync() -> dict:
    try:
        d = json.loads(SYNC_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_sync(payload: dict) -> None:
    SYNC_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SYNC_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(SYNC_FILE)


def load_events(from_iso: str | None = None) -> list[dict]:
    """缓存里 from_iso（含）之后的事件（只读，不联网）。"""
    since = from_iso or date.today().isoformat()
    return [e for e in (_load_sync().get("events") or [])
            if isinstance(e, dict) and str(e.get("date") or "") >= since]


def sync_meta() -> dict:
    """同步状态（页面显示用）。"""
    d = _load_sync()
    window = d.get("window") or ["", ""]
    return {"last_synced_at": d.get("synced_at") or "",
            "event_count": len(d.get("events") or []),
            "window_end": window[1] if len(window) > 1 else "",
            "warn": d.get("warn") or "",
            "mapping": mapping_meta(),
            "running": _SYNC_LOCK.locked()}


def sync_due(interval_hours: float) -> bool:
    """距上次成功同步是否已超过 interval_hours（没同步过 = 到期）。"""
    ts = _load_sync().get("synced_at")
    if not ts:
        return True
    try:
        last = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return True
    return (datetime.now() - last).total_seconds() >= float(interval_hours) * 3600


def _event(kind: str, code: str, name: str, day: str, title: str,
           note: str, market: str, period: str = "") -> dict:
    e = {"date": day, "code": f"earn:{kind}:{code}", "title": title,
         "time": "", "market": market, "level": "mid", "note": note,
         "source": "earnings_sync"}
    if period:
        e["period"] = period                    # 期次（A股）用于失败期次沿用旧缓存
    return e


def _sync_impl(*, positions: list[dict] | None = None, days: int | None = None,
               roster: dict | None = None, llm_fn=None, quote_fn=None,
               cn_fetch_fn=None, us_fetch_fn=None, us_session=None,
               today: date | None = None, force_mapping: bool = False) -> dict:
    """主流程：重建映射（如需）→ A股预约披露 → 美股财报日历 → 写缓存。"""
    today = today or date.today()
    days = int(days or SYNC_HORIZON_DAYS)
    fetch_cn = cn_fetch_fn or _fetch_cn_period
    fetch_us = us_fetch_fn or _fetch_us_day

    old = _load_sync()
    old_events = [e for e in (old.get("events") or []) if isinstance(e, dict)]

    # ---- 1) 板块映射（增量重建） ----
    m = _load_map()
    items = m.get("items") or {}
    warnings: list[str] = []
    if positions:
        missing = [p for p in positions if p["code"] not in items]
        stale = False
        if items:
            try:
                stale = (datetime.now() - datetime.fromisoformat(
                    m.get("built_at") or "")).days >= MAP_TTL_DAYS
            except (TypeError, ValueError):
                stale = True
        if force_mapping or missing or stale or not items:
            scope = positions if (force_mapping or stale or not items) else missing
            if scope:
                try:
                    built = build_mapping(scope, roster=roster,
                                          llm_fn=llm_fn, quote_fn=quote_fn)
                    merged = dict(items)
                    merged.update(built["items"])
                    m = {"built_at": built["built_at"], "items": merged,
                         "warnings": built.get("warnings") or [],
                         "warn": built.get("warn") or ""}
                    _write_map(m)
                    items = merged
                except Exception as exc:              # noqa: BLE001
                    warnings.append(f"板块映射重建失败（沿用旧映射）: "
                                    f"{type(exc).__name__}: {str(exc)[:160]}")
                    print(f"[earnings] {warnings[-1]}", flush=True)

    # 若持仓没传（异常兜底），退回用映射里的代码当宇宙
    if not positions:
        positions = [{"code": c, "name": (v or {}).get("name") or c}
                     for c, v in items.items() if isinstance(v, dict)]

    own_cn = {p["code"]: p["name"] for p in positions if _is_cn_stock(p["code"])}
    lead_cn: dict[str, set] = {}
    lead_us: dict[str, set] = {}
    for p in positions:
        v = items.get(p["code"]) or {}
        sec = (v.get("sector") or "").strip() or "未知"
        cn = v.get("cn_leader")
        if isinstance(cn, dict) and cn.get("code"):
            lead_cn.setdefault(cn["code"], set()).add(sec)
        us = v.get("us_leader")
        if isinstance(us, dict) and us.get("ticker"):
            lead_us.setdefault(us["ticker"], set()).add(sec)

    if not own_cn and not lead_cn and not lead_us:
        if old_events:
            return {"ok": False, "error": "未读取到持仓/映射，缓存未更新（保留旧数据）"}
        _write_sync({"synced_at": datetime.now().isoformat(timespec="seconds"),
                     "window": [today.isoformat(), (today + timedelta(days=days)).isoformat()],
                     "events": [], "warn": "未读取到持仓，无事可同步", "warnings": []})
        return {"ok": True, "events": 0, "warn": "未读取到持仓"}

    events: list[dict] = []
    unmatched: list[str] = []
    cn_fails: list[str] = []
    cn_recent_ok = False

    # ---- 2) A股预约披露（最近两期 + 下一期，失败期次沿用旧缓存） ----
    sched_codes = set(own_cn) | set(lead_cn)
    found_codes: set[str] = set()
    for qe in _quarter_ends(today):
        label = _PERIOD_LABEL.get((qe.month, qe.day), qe.isoformat())
        try:
            rows = fetch_cn(qe)
        except Exception as exc:                      # noqa: BLE001
            cn_fails.append(qe.isoformat())
            events += [e for e in old_events if e.get("period") == qe.isoformat()]
            print(f"[earnings] A股 {qe} 期预约披露抓取失败: "
                  f"{type(exc).__name__}: {str(exc)[:120]}", flush=True)
            continue
        if qe <= today:
            cn_recent_ok = True
        table = _cn_rows_to_map(rows)
        for code in sched_codes & set(table):
            ent = table[code]
            found_codes.add(code)
            if not ent["eff"] or ent["eff"] < today.isoformat():
                continue                              # 已披露完 / 无日期
            nm = ent["name"] or own_cn.get(code) or code
            if code in own_cn:
                changed = (ent["eff"] != ent["first"] and not ent["actual"])
                title = f"{nm} {label}预约披露"
                note = ("A股定期报告预约披露（可提前或变更）"
                        + (f"，原定 {ent['first']}" if changed else "")
                        + "；来源：东财预约披露时间表")
                events.append(_event("own", code, nm, ent["eff"], title, note,
                                     "cn", qe.isoformat()))
            else:
                secs = "、".join(sorted(lead_cn.get(code) or [])[:3])
                title = f"{nm} {label}预约披露（{secs}龙头）"
                note = (f"{secs}板块 A股龙头（与你的持仓同板块）；A股定期报告"
                        "预约披露（可提前或变更）；来源：东财预约披露时间表")
                events.append(_event("cn", code, nm, ent["eff"], title, note,
                                     "cn", qe.isoformat()))
    unmatched += sorted(sched_codes - found_codes)

    if not cn_recent_ok:
        # 两个「最近期次」全失败 = 真故障（不是「还没排期」）→ 沿用全部旧 A 股事件
        events += [e for e in old_events if e.get("code", "").startswith(("earn:own:", "earn:cn:"))]
        warnings.append("A股预约披露全部抓取失败（沿用旧缓存）")

    # ---- 3) 美股财报日历（逐日扫描，按 ticker 过滤） ----
    us_new: list[dict] = []
    us_fails: list[str] = []
    us_requests = 0
    if lead_us:
        import requests as _rq
        session = us_session if us_session is not None else _rq.Session()
        for i in range(days + 1):
            day = today + timedelta(days=i)
            if day.weekday() >= 5:                    # 美股财报只在工作日
                continue
            us_requests += 1
            try:
                rows = fetch_us(day, session)
            except Exception as exc:                  # noqa: BLE001
                us_fails.append(day.isoformat())
                us_new += [e for e in old_events
                           if e.get("date") == day.isoformat()
                           and e.get("code", "").startswith("earn:us:")]
                print(f"[earnings] 美股 {day} 财报日历抓取失败: "
                      f"{type(exc).__name__}: {str(exc)[:120]}", flush=True)
            else:
                for r in rows or []:
                    if not isinstance(r, dict):
                        continue
                    tk = _clean(r.get("symbol")).upper()
                    if tk not in lead_us:
                        continue
                    us_new.append(_event(
                        "us", tk, tk, day.isoformat(),
                        f"{_us_display_name(items, tk) or tk} 财报（{_sectors_of(lead_us, tk)}龙头）",
                        _us_note(lead_us, tk, r), "us"))
            if SYNC_REQUEST_DELAY:
                time.sleep(SYNC_REQUEST_DELAY)
        if us_fails and us_requests and len(us_fails) >= us_requests:
            us_new = [e for e in old_events
                      if e.get("code", "").startswith("earn:us:")]
            warnings.append("美股财报日历全部抓取失败（沿用旧缓存）")
    events += us_new

    # ---- 4) 汇总落盘（防呆同 CPI：全失败不写、0 条不清空旧数据） ----
    cn_all_fail = bool(cn_fails) and not cn_recent_ok
    us_all_fail = bool(lead_us) and us_requests > 0 and len(us_fails) >= us_requests
    if cn_all_fail and (us_all_fail or not lead_us) and old_events:
        return {"ok": False,
                "error": "A股/美股源全部抓取失败，缓存未更新",
                "failed": {"cn": cn_fails, "us": us_fails}}
    if not events and old_events:
        events = old_events
        warnings.append("本次未扫到任何财报排期（保留旧缓存）")

    dedup: dict[tuple, dict] = {}
    for e in events:
        dedup[(e.get("date"), e.get("code"))] = e
    events = [dedup[k] for k in sorted(dedup)]

    kinds = {"own": 0, "cn": 0, "us": 0}
    for e in events:
        c = e.get("code") or ""
        for k in kinds:
            if c.startswith(f"earn:{k}:"):
                kinds[k] += 1

    payload = {"synced_at": datetime.now().isoformat(timespec="seconds"),
               "window": [today.isoformat(), (today + timedelta(days=days)).isoformat()],
               "events": events,
               "stats": {"own": kinds["own"], "cn": kinds["cn"], "us": kinds["us"],
                         "us_days_scanned": us_requests, "us_failed_days": len(us_fails),
                         "cn_failed_periods": len(cn_fails)},
               "warn": "；".join(warnings[:3])[:300],
               "warnings": warnings[:12],
               "unmatched": unmatched[:20]}
    _write_sync(payload)
    return {"ok": True, "events": len(events), "stats": payload["stats"],
            "unmatched": len(unmatched), "warn": payload["warn"]}


def _us_display_name(items: dict, ticker: str) -> str:
    """从映射里找该 ticker 的展示名（腾讯中文名优先）。"""
    for v in items.values():
        if not isinstance(v, dict):
            continue
        ul = v.get("us_leader")
        if isinstance(ul, dict) and ul.get("ticker") == ticker:
            return str(ul.get("name") or ticker)
    return ticker


def _sectors_of(lead_us: dict, ticker: str) -> str:
    return "、".join(sorted(lead_us.get(ticker) or [])[:3])


def _us_note(lead_us: dict, ticker: str, row: dict) -> str:
    t = _clean(row.get("time"))
    fq = _clean(row.get("fiscalQuarterEnding"))
    tip = _US_TIME_NOTE.get(t, "公布时段未定")
    parts = [tip]
    if fq:
        parts.append(f"财季截至 {fq}")
    parts.append("来源：Nasdaq 财报日历")
    return "；".join(parts)


def sync_earnings(**kw) -> dict:
    """阻塞式同步（守护线程用）。拿不到锁就跳过。"""
    if not _SYNC_LOCK.acquire(blocking=False):
        return {"ok": False, "busy": True, "error": "已有财报同步在跑"}
    try:
        return _sync_impl(**kw)
    finally:
        _SYNC_LOCK.release()


def sync_async(**kw) -> dict:
    """手动触发：后台线程跑（别占住 HTTP 请求线程），立刻返回。

    锁在本函数先拿、由工作线程释放（与 econ_calendar.sync_async 同款防重入）。
    """
    if not _SYNC_LOCK.acquire(blocking=False):
        return {"started": False, "busy": True}

    def _run():
        try:
            res = _sync_impl(**kw)
            print(f"[earnings] 手动同步完成: {res}", flush=True)
        except Exception as exc:                      # noqa: BLE001
            print(f"[earnings] 手动同步失败: {type(exc).__name__}: {exc}", flush=True)
        finally:
            _SYNC_LOCK.release()

    threading.Thread(target=_run, daemon=True, name="earn_sync_manual").start()
    return {"started": True}
