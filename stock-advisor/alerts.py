"""自选股事件告警：限售解禁 + 增发新股上市（东财公开接口，零成本、无需 key）。

两个事件都会带来抛压/摊薄，值得提前知道：
    - 解禁  定向增发机构配售股份、股权激励限售股等到期上市流通，供给突增
    - 增发  定增/公开增发的新增股份上市流通，股本摊薄（价格通常低于市价）

数据源（datacenter-web 通用报表接口，2026-09-14 实测）：
    - RPT_LIFT_STAGE   限售解禁表：FREE_DATE 解禁日、CURRENT_FREE_SHARES 解禁股数(万股)、
      LIFT_MARKET_CAP 解禁市值(万元)、FREE_SHARES_TYPE 限售类型
    - RPT_SEO_DETAIL   增发明细表：ISSUE_LISTING_DATE 新增股份上市日、ISSUE_NUM 发行股数(股)、
      ISSUE_PRICE 发行价、ISSUE_WAY 发行方式、LOCKIN_PERIOD 锁定期、SEO_TYPE 1=定向 2=公开
    filter 语法：SECURITY_CODE in ("600519",...)；日期比较必须用单引号
    （FREE_DATE>='2026-09-14'，双引号会被判成格式错误）。

提醒策略：窗口（默认 14 天）内的事件，首轮发现即推一条汇总（每事件只推一次，
data/alerts_state.json 记键防轰炸）；页面「自选行情」卡每次现拉，不受已推状态影响。
"""

import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "data" / "alerts_state.json"

DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

DEFAULT_DAYS = 14       # 提前关注窗口（自然日）
PAGE_SIZE = 200


# ---------------- 东财接口 ----------------

def _dc_query(report: str, fields: str, flt: str, sort_col: str) -> list[dict]:
    """datacenter 通用查询：单页拉全（自选股窗口内事件量级很小），失败返回空。"""
    try:
        r = requests.get(DC_URL, params={
            "reportName": report, "columns": fields, "filter": flt,
            "pageNumber": 1, "pageSize": PAGE_SIZE,
            "sortTypes": 1, "sortColumns": sort_col,
            "source": "WEB", "client": "WEB",
        }, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return ((r.json().get("result") or {}).get("data")) or []
    except Exception as exc:
        print(f"[alerts] {report} query failed: {exc}", flush=True)
        return []


def _in_filter(codes: list[str]) -> str:
    return "(" + "SECURITY_CODE in (" + ",".join(f'"{c}"' for c in codes) + "))"


def fetch_lift_events(codes: list[str], end_iso: str) -> list[dict]:
    """未来解禁：今天 <= FREE_DATE <= end。"""
    flt = _in_filter(codes) + f"(FREE_DATE>='{date.today().isoformat()}')(FREE_DATE<='{end_iso}')"
    out = []
    for r in _dc_query("RPT_LIFT_STAGE",
                       "SECURITY_CODE,SECURITY_NAME_ABBR,FREE_DATE,CURRENT_FREE_SHARES,"
                       "LIFT_MARKET_CAP,FREE_SHARES_TYPE", flt, "FREE_DATE"):
        out.append({
            "type": "lift",
            "code": r["SECURITY_CODE"],
            "name": r.get("SECURITY_NAME_ABBR", ""),
            "event_date": (r.get("FREE_DATE") or "")[:10],
            # 万股 -> 亿元；接口口径：CURRENT_FREE_SHARES 万股、LIFT_MARKET_CAP 万元
            "shares_yi": round((r.get("CURRENT_FREE_SHARES") or 0) / 1e4, 2),
            "cap_yi": round((r.get("LIFT_MARKET_CAP") or 0) / 1e4, 2),
            "detail": r.get("FREE_SHARES_TYPE", ""),
        })
    return out


def fetch_placement_events(codes: list[str], end_iso: str) -> list[dict]:
    """未来增发新增股上市：今天 <= ISSUE_LISTING_DATE <= end。"""
    flt = (_in_filter(codes)
           + f"(ISSUE_LISTING_DATE>='{date.today().isoformat()}')"
             f"(ISSUE_LISTING_DATE<='{end_iso}')")
    out = []
    for r in _dc_query("RPT_SEO_DETAIL",
                       "SECURITY_CODE,SECURITY_NAME_ABBR,ISSUE_LISTING_DATE,ISSUE_NUM,"
                       "ISSUE_PRICE,ISSUE_WAY,LOCKIN_PERIOD,SEO_TYPE", flt,
                       "ISSUE_LISTING_DATE"):
        kind = "定向增发" if str(r.get("SEO_TYPE")) == "1" else "公开增发"
        num = r.get("ISSUE_NUM") or 0
        out.append({
            "type": "placement",
            "code": r["SECURITY_CODE"],
            "name": r.get("SECURITY_NAME_ABBR", ""),
            "event_date": (r.get("ISSUE_LISTING_DATE") or "")[:10],
            "shares_yi": round(num / 1e8, 2),
            "cap_yi": round(num * (r.get("ISSUE_PRICE") or 0) / 1e8, 2),
            "detail": f"{kind}·{r.get('ISSUE_WAY', '')}·{r.get('LOCKIN_PERIOD', '')}",
        })
    return out


def upcoming_events(codes: list[str], days: int = DEFAULT_DAYS) -> list[dict]:
    """自选 A股窗口内解禁/增发事件（港股 6 位以下代码不适用，按 6 位过滤）。"""
    a_codes = [c for c in codes if len(c) == 6 and c.isdigit()]
    if not a_codes:
        return []
    end_iso = (date.today() + timedelta(days=days)).isoformat()
    events = fetch_lift_events(a_codes, end_iso) + fetch_placement_events(a_codes, end_iso)
    events.sort(key=lambda e: (e["event_date"], -e["cap_yi"]))
    return events


# ---------------- 推送去重状态 ----------------

def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"sent": {}}


def _prune_state(sent: dict) -> dict:
    """只留近 90 天的键：事件都是前瞻性的，旧键不再有意义，防文件无限膨胀。"""
    cutoff = (date.today() - timedelta(days=90)).isoformat()
    return {k: v for k, v in sent.items() if str(v)[:10] >= cutoff}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        print(f"[alerts] state save failed: {exc}", flush=True)


def _fmt_event(e: dict) -> str:
    label = "🔓解禁" if e["type"] == "lift" else "📤增发"
    return (f"{label} {e['event_date']} {e['name']}（{e['code']}）"
            f" {e['shares_yi']}亿股/约{e['cap_yi']}亿元 · {e['detail']}")


def check_alerts_once(conn, notify_fn=None, days: int = DEFAULT_DAYS) -> list[dict]:
    """扫一遍自选股，首轮发现的新事件合并推一条；返回本次推送的事件列表。

    notify_fn(title, content) 注入（app 传 notifier.notify），None 时只组装不推送。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT code FROM sa_watchlist")
        codes = [r[0] for r in cur.fetchall()]
    events = upcoming_events(codes, days)
    state = _load_state()
    sent = _prune_state(state["sent"])
    state["sent"] = sent
    today = date.today().isoformat()
    fresh = []
    for e in events:
        key = f"{e['type']}:{e['code']}:{e['event_date']}"
        if key in sent:
            continue
        sent[key] = today
        fresh.append(e)
    _save_state(state)
    if fresh and notify_fn:
        lift = [e for e in fresh if e["type"] == "lift"]
        plc = [e for e in fresh if e["type"] == "placement"]
        parts = []
        if lift:
            parts.append("【限售解禁】\n" + "\n".join(_fmt_event(e) for e in lift))
        if plc:
            parts.append("【增发新股上市】\n" + "\n".join(_fmt_event(e) for e in plc))
        notify_fn(
            "⚠️ 自选股解禁/增发提醒",
            f"未来 {days} 天内有 {len(fresh)} 个新事件：\n\n" + "\n\n".join(parts) +
            f"\n\n（解禁=供给冲击，增发上市=股本摊薄；来自 stock-advisor 事件监控，"
            f"{datetime.now().strftime('%Y-%m-%d %H:%M')}）")
    return fresh
