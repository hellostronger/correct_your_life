"""全市场供给冲击日历：限售解禁 / 增发上市 / 股东减持。

与 `alerts.py` 的分工：
- `alerts.py` = **自选股**维度，逐个代码查 `datacenter-web`，只覆盖你盯的票。
- 本模块 = **全市场**维度，同一套接口去掉 `SECURITY_CODE in (...)` 过滤直接拉全市场，
  按日聚合成「供给冲击强度」，回答「这几天整个市场的解禁/增发/减持压力大不小」。

「潮」的判定用**历史分位数**，不拍绝对值（AGENTS.md 第 1 条：不要自己造轮子/拍脑袋）。
2026-10-05 实测基线（datacenter-web 0.1~0.3 秒/请求）：
- 解禁全表 31,617 行；未来 60 天 293 行；`pageSize=300` 一次拿完
- 历史 120 天 572 行 2 次请求；每日解禁市值 p50=45.7 p80=155.6 p90=328.1 max=645.6 亿元
- 增发全表 5,900 行；未来 180 天只有 3 条 → **天然稀疏，不能用分位数判潮**
- 减持全表 146,773 行；近 60 天 753 行、23 个有公告的天数，
  日减持市值 p50=6.67 p80=20.27 p90=25.75 亿元

口径要点（全部实测踩出来的，改代码前先读）：
1. `LIFT_MARKET_CAP` 单位是**万元**（不是元），/1e4 才是亿元；
   `CURRENT_FREE_SHARES` 是**万股**，/1e4 才是亿股。
2. `LIFT_MARKET_CAP=0` 的行占 0.9%（都是「定向增发机构配售股份」），
   求和不会失真，但单独计数以便报告里说明覆盖度。
3. `RPT_SHARE_HOLDER_INCREASE` 的 filter 里**中文枚举值必须双引号**
   `(DIRECTION="减持")`。单引号 `(DIRECTION='减持')` 实测**静默返回空**
   （看起来像「最近没人减持」）——这是本模块最容易复发的坑。
4. 聚类字段（`COUNT`/`SUM_MARKET_CAP`）在这套接口**不存在**，会报
   `9501 COUNT返回字段不存在` → 按日聚合必须在客户端做。
5. `pageSize` 实测可到 300；翻页一律按返回的 `pages` 走，不猜。
6. 这套接口对「不存在的 reportName / columns」返回
   `{"success": false, "code": 9501, "message": ...}` 而**不是 HTTP 错误**。
   所以「拿到空」必须区分「真的没有」和「参数写错」，见 `_dc_query` 的注释。

只读外部数据，不写数据库。历史快照落 `data/supply_events_<年>.jsonl`
（JSONL 追加安全、不依赖事务）。**不新增数据库表** —— schema 变更超出「修 bug」范围。
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta

import requests

DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Referer": "https://data.eastmoney.com/",
}

LIFT_COLS = ("SECURITY_CODE,SECURITY_NAME_ABBR,FREE_DATE,CURRENT_FREE_SHARES,"
             "LIFT_MARKET_CAP,FREE_SHARES_TYPE")
SEO_COLS = ("SECURITY_CODE,SECURITY_NAME_ABBR,ISSUE_LISTING_DATE,ISSUE_NUM,"
            "ISSUE_PRICE,ISSUE_WAY,LOCKIN_PERIOD,SEO_TYPE")
HOLDER_COLS = ("SECURITY_CODE,SECURITY_NAME_ABBR,NOTICE_DATE,DIRECTION,CHANGE_NUM,"
               "CHANGE_RATE,HOLD_RATIO,CLOSE_PRICE")

PAGE_SIZE = 300        # 实测 300 一次拿完 293 行
MAX_PAGES = 20         # 单窗口最多 20 页（6000 行）保护
HISTORY_DAYS = 120     # 解禁/增发的分位数回看窗口
REDUCE_HISTORY_DAYS = 60   # 减持近 60 天只有 23 个有公告的天数，回看再长也没样本
DEFAULT_DAYS = 30      # 默认展示的未来天数
TIDE_P80 = 80          # 分位阈值：中潮
TIDE_P90 = 90          # 分位阈值：强潮
# 每类事件**各自**的请求预算。共用一个池子会被先执行的解禁吃光
# （实测：解禁未来 1 页 + 历史 2 页 + 增发 2 页 = 5 页，减持 0 页就被截断，
#  表现为「减持窗口无数据」——看起来像最近没人减持，其实是没采到）。
# 减持行数最多（近 60 天 753 行 = 3 页 + 未来窗口），预算给到 8。
BUDGET_PER_KIND = {"lift": 4, "seo": 3, "reduce": 8}

KIND_LABEL = {"lift": "解禁", "seo": "增发", "reduce": "减持"}
# 影响性质：解禁/增发是**确定的供给增加**（日期已定），减持是**公告**（可能不执行）
KIND_NATURE = {"lift": "确定", "seo": "确定", "reduce": "公告"}


# ---------------- 底层查询 ----------------

def _dc_query(report: str, columns: str, flt: str = "", sort_col: str = "",
               page_size: int = PAGE_SIZE, page_no: int = 1):
    """datacenter-web 单页查询，返回 (rows, pages, meta)。

    `meta` 里带 `success` / `code` / `message` / `count`，因为**「空」有两种完全不同
    的原因**（AGENTS.md 第 2 条的老坑，这里必须区分开）：

    | 现象 | 含义 | 处置 |
    |---|---|---|
    | `success=true` 且 `result=null` | **真的 0 行**（该窗口没事件） | 正常，不报错 |
    | `success=false` + `code=9501` | **参数写错**（reportName/columns 不存在） | 必须报错 |
    | `code=0` / 其他 | 传输层异常 | 报错 |

    2026-10-05 实测踩过：`PREDICT_DATE` 这个列不存在时报
    `9501 PREDICT_DATE返回字段不存在`，而 HTTP 仍是 200 —— 不看 meta 就会
    误判成「未来没有增发」。
    """
    try:
        r = requests.get(DC_URL, params={
            "reportName": report, "columns": columns, "filter": flt,
            "pageNumber": page_no, "pageSize": page_size,
            "sortTypes": 1, "sortColumns": sort_col,
            "source": "WEB", "client": "WEB",
        }, headers=HEADERS, timeout=20)
        r.raise_for_status()
        j = r.json()
        res = j.get("result") or {}
        meta = {
            "success": bool(j.get("success", True)),
            "code": j.get("code"),
            "message": str(j.get("message") or "")[:120],
            "count": res.get("count"),
        }
        return (res.get("data") or []), int(res.get("pages") or 1), meta
    except Exception as exc:
        print(f"[supply_events] {report} 查询失败: {exc}", flush=True)
        return [], 1, {"success": False, "code": "exc", "message": str(exc)[:120],
                       "count": None}


def _dc_paged(report: str, columns: str, flt: str, sort_col: str, budget: list,
              meta_out: list | None = None) -> list:
    """按 `pages` 翻页取全。`budget` 是长度 1 的列表当计数器（不引入 nonlocal）。

    预算用完立即停 —— 一次采集把东财打爆会招来 WAF 封禁（AGENTS.md 有前车之鉴）。
    """
    out = []
    page = 1
    while page <= MAX_PAGES:
        if budget[0] <= 0:
            print(f"[supply_events] {report} 预算用尽，取到 {len(out)} 行", flush=True)
            break
        budget[0] -= 1
        rows, pages, meta = _dc_query(report, columns, flt, sort_col, PAGE_SIZE, page)
        if meta_out is not None and (not meta.get("success") or meta.get("code")):
            meta_out.append({"report": report, "page": page, **meta})
        out.extend(rows)
        if not rows or page >= pages:
            break
        page += 1
    return out


# ---------------- 三类事件 ----------------

def fetch_lift(start_iso: str, end_iso: str, budget: list) -> list:
    """限售解禁，全市场，`FREE_DATE` 落在 [start, end]。"""
    flt = f"(FREE_DATE>='{start_iso}')(FREE_DATE<='{end_iso}')"
    rows = _dc_paged("RPT_LIFT_STAGE", LIFT_COLS, flt, "FREE_DATE", budget)
    out = []
    for r in rows:
        d = (r.get("FREE_DATE") or "")[:10]
        if not d:
            continue
        cap = float(r.get("LIFT_MARKET_CAP") or 0) / 1e4        # 万元 -> 亿元
        sh = float(r.get("CURRENT_FREE_SHARES") or 0) / 1e4     # 万股 -> 亿股
        out.append({
            "kind": "lift", "code": r.get("SECURITY_CODE", ""),
            "name": r.get("SECURITY_NAME_ABBR", ""), "date": d,
            "cap_yi": round(cap, 2), "shares_yi": round(sh, 2),
            "detail": str(r.get("FREE_SHARES_TYPE") or "")[:40],
            "zero_cap": cap <= 0,
        })
    return out


def fetch_seo(start_iso: str, end_iso: str, budget: list) -> list:
    """增发新股上市，全市场，`ISSUE_LISTING_DATE` 落在 [start, end]。"""
    flt = f"(ISSUE_LISTING_DATE>='{start_iso}')(ISSUE_LISTING_DATE<='{end_iso}')"
    rows = _dc_paged("RPT_SEO_DETAIL", SEO_COLS, flt, "ISSUE_LISTING_DATE", budget)
    out = []
    for r in rows:
        d = (r.get("ISSUE_LISTING_DATE") or "")[:10]
        if not d:
            continue
        num = float(r.get("ISSUE_NUM") or 0)
        price = float(r.get("ISSUE_PRICE") or 0)
        kind = "定向增发" if str(r.get("SEO_TYPE")) == "1" else "公开增发"
        out.append({
            "kind": "seo", "name_type": kind,
            "code": r.get("SECURITY_CODE", ""),
            "name": r.get("SECURITY_NAME_ABBR", ""), "date": d,
            "cap_yi": round(num * price / 1e8, 2),      # ISSUE_NUM 是股
            "shares_yi": round(num / 1e8, 4),
            "detail": f"{kind}·{r.get('ISSUE_WAY') or ''}",
            "zero_cap": False,
        })
    return out


def fetch_reduce(start_iso: str, end_iso: str, budget: list) -> list:
    """股东/高管减持公告，全市场，`NOTICE_DATE` 落在 [start, end]。

    ⚠️ 中文枚举值必须双引号：`(DIRECTION="减持")`。
    单引号实测**静默返回空**（像「最近没人减持」），这是最容易复发的坑。
    """
    flt = (f'(DIRECTION="减持")(NOTICE_DATE>=\'{start_iso}\')'
           f'(NOTICE_DATE<=\'{end_iso}\')')
    rows = _dc_paged("RPT_SHARE_HOLDER_INCREASE", HOLDER_COLS, flt, "NOTICE_DATE", budget)
    out = []
    for r in rows:
        d = (r.get("NOTICE_DATE") or "")[:10]
        if not d:
            continue
        num = float(r.get("CHANGE_NUM") or 0)      # 万股
        close = float(r.get("CLOSE_PRICE") or 0)
        out.append({
            "kind": "reduce", "code": r.get("SECURITY_CODE", ""),
            "name": r.get("SECURITY_NAME_ABBR", ""), "date": d,
            "cap_yi": round(num * close / 1e4, 2),
            "shares_yi": round(num / 1e4, 2),
            "detail": f"减持{num:,.0f}万股({r.get('CHANGE_RATE') or 0}%)",
            "zero_cap": False,
        })
    return out


# ---------------- 聚合与「潮」判定 ----------------

def aggregate_by_day(events: list) -> dict:
    """按日聚合：家数 / 市值(亿) / 股数(亿股) / 市值为 0 的家数。"""
    out = {}
    for e in events:
        b = out.setdefault(e["date"], {"n": 0, "cap_yi": 0.0, "shares_yi": 0.0,
                                       "zero_cap": 0})
        b["n"] += 1
        b["cap_yi"] += e["cap_yi"]
        b["shares_yi"] += e["shares_yi"]
        if e.get("zero_cap"):
            b["zero_cap"] += 1
    for b in out.values():
        b["cap_yi"] = round(b["cap_yi"], 2)
        b["shares_yi"] = round(b["shares_yi"], 2)
    return out


def _pct(sorted_vals: list, p: float) -> float:
    """nearest-rank 分位数：取 ``sorted[int(n*p)]``。

    **不做插值**，这是有意的：分位数只用于给「潮」分级，不需要统计精度，
    而 nearest-rank 的行为可预测、易复算（n=20 时 p50 取 index10=110，
    而不是统计学「中间两项平均」的 105）。改这里会改变历史告警的口径。
    """
    if not sorted_vals:
        return 0.0
    return sorted_vals[min(len(sorted_vals) - 1, int(len(sorted_vals) * p))]


def build_baseline(history_by_day: dict, kind: str = "lift") -> dict:
    """历史每日分位数基线。

    只用**有事件的天**做分布 —— 那天没解禁不是「压力小」，而是「那天没这回事」，
    混进来会把基线整体拉低、让每天都看起来像「高潮」。
    """
    caps = sorted(v["cap_yi"] for v in history_by_day.values() if v["cap_yi"] > 0)
    return {
        "kind": kind,
        "days": len(caps),
        "p50": round(_pct(caps, 0.50), 2),
        "p80": round(_pct(caps, 0.80), 2),
        "p90": round(_pct(caps, 0.90), 2),
        "max": round(caps[-1], 2) if caps else 0.0,
        "mean": round(sum(caps) / len(caps), 2) if caps else 0.0,
    }


def classify(cap_yi: float, baseline: dict) -> str:
    """给某日/某窗口的供给市值定级：none / normal / tide / surge。

    规则（全部相对历史分位，不含绝对数）：
    - 无基线可比（days=0）→ normal（**不硬编一个「安全」结论**）
    - >= p90 或 > 历史最大值 → surge（强潮）
    - >= p80 → tide（中潮）
    - 其余 → normal
    """
    days = baseline.get("days", 0)
    if days < 10:
        return "normal"
    if cap_yi >= baseline["p90"] or cap_yi > baseline["max"]:
        return "surge"
    if cap_yi >= baseline["p80"]:
        return "tide"
    return "normal"


LEVEL_TEXT = {"normal": "正常", "tide": "中潮", "surge": "强潮"}
LEVEL_ICON = {"normal": "·", "tide": "⚠", "surge": "🔥"}


def rolling_window_totals(by_day: dict, length: int) -> list:
    """历史**滚动窗口合计**的分布（每天一个窗口，窗口=length 个自然日）。

    为什么必须要它：拿「30 天合计」去比「单日 p90」是量纲错误 ——
    实测未来 30 天解禁合计 3213 亿，而单日 p90 只有 328 亿，
    直接比会**永远判成「强潮」**，等于永远报警 = 没有报警（AGENTS.md 第 1 条）。
    正确的对照是「历史上同样长度窗口的合计分布」。
    """
    if not by_day or length <= 0:
        return []
    dates = sorted(by_day)
    start, end = datetime.strptime(dates[0], "%Y-%m-%d").date(), \
        datetime.strptime(dates[-1], "%Y-%m-%d").date()
    span = (end - start).days + 1
    if span < length:
        return []
    vals = [0.0] * span
    for d, v in by_day.items():
        i = (datetime.strptime(d, "%Y-%m-%d").date() - start).days
        vals[i] = v["cap_yi"]
    out, run = [], 0.0
    for i in range(span):
        run += vals[i]
        if i >= length:
            run -= vals[i - length]
        if i >= length - 1:
            out.append(round(run, 2))
    return out


def classify_window(window_cap: float, win_dist: list) -> str:
    """窗口合计的定级，对照历史同长度窗口的分布。

    与 `classify`（单日）严格分开：两种量纲两种基线，混用会得到恒真的告警。
    """
    if len(win_dist) < 5:
        return "normal"
    s = sorted(win_dist)
    if window_cap >= _pct(s, 0.90) or window_cap > s[-1]:
        return "surge"
    if window_cap >= _pct(s, 0.80):
        return "tide"
    return "normal"


# ---------------- 主流程 ----------------

MODE = {"lift": "forward", "seo": "forward", "reduce": "backward"}
"""每类事件的日历方向。

`reduce` 是 **backward**（回顾），不是 forward：2026-10-06 实测
`RPT_SHARE_HOLDER_INCREASE` 的 `NOTICE_DATE` / `START_DATE` / `END_DATE` /
`TRADE_DATE` **四个字段在未来窗口全部 0 行** —— 这张表只收录已过公告日的减持。
所以「未来减持日历」从本源拿不到，硬做成前瞻日历会永远显示空。
（要前瞻只能走逐代码的公告检索，即 alerts.py 的 `f_node=7`，那是自选股维度。）
"""


def collect(days: int = DEFAULT_DAYS, history_days: int = HISTORY_DAYS,
            kinds=("reduce", "lift", "seo")) -> dict:
    """采集供给事件 + 历史基线，算出每日与整窗口的强度分级。

    两套基线，别混用：
    - **单日**分级 vs 历史「每日」分布 → `day_levels[date]`
    - **窗口合计**分级 vs 历史「同长度滚动窗口合计」分布 → `level`
      （拿 30 天合计比单日 p90 会永远判强潮，等于永远报警）
    """
    today = date.today()
    now_iso = today.isoformat()
    fetcher = {"lift": fetch_lift, "seo": fetch_seo, "reduce": fetch_reduce}
    out = {"ts": datetime.now().isoformat(timespec="seconds"),
           "days": days, "kinds": {}, "errors": []}

    for kind in kinds:
        fn = fetcher.get(kind)
        if not fn:
            continue
        mode = MODE.get(kind, "forward")
        h_days = REDUCE_HISTORY_DAYS if kind == "reduce" else history_days
        if mode == "forward":
            win_start, win_end = now_iso, (today + timedelta(days=days)).isoformat()
        else:
            win_start, win_end = (today - timedelta(days=days)).isoformat(), now_iso
        budget = [BUDGET_PER_KIND.get(kind, 3)]
        try:
            win = fn(win_start, win_end, budget)
            if mode == "forward":
                h_start = (today - timedelta(days=h_days)).isoformat()
            else:
                h_start = (today - timedelta(days=h_days + days)).isoformat()
            hist = fn(h_start, win_start, budget)
        except Exception as exc:
            out["errors"].append(f"{kind}: {exc}")
            continue

        hist_by_day = {k: v for k, v in aggregate_by_day(hist).items() if k < win_start}
        by_day = aggregate_by_day(win)
        base = build_baseline(hist_by_day, kind)
        win_cap = round(sum(v["cap_yi"] for v in by_day.values()), 2)
        win_dist = rolling_window_totals(hist_by_day, days)
        day_levels = {d: classify(v["cap_yi"], base) for d, v in by_day.items()}

        out["kinds"][kind] = {
            "mode": mode,
            "window": [win_start, win_end],
            "baseline": base,
            "by_day": by_day,
            "day_levels": day_levels,
            "future": win,
            "level": classify_window(win_cap, win_dist),
            "window_cap_yi": win_cap,
            "window_n": len(win),
            "event_days": len(by_day),
            "window_p90": round(_pct(sorted(win_dist), 0.90), 2) if win_dist else 0.0,
            "window_p50": round(_pct(sorted(win_dist), 0.50), 2) if win_dist else 0.0,
            "history_days_scanned": h_days,
            "budget_left": budget[0],
        }
        if by_day:
            worst = sorted(by_day.items(), key=lambda kv: -kv[1]["cap_yi"])[:5]
            out["kinds"][kind]["top_days"] = [
                {"date": d, "cap_yi": v["cap_yi"], "n": v["n"],
                 "level": day_levels.get(d, "normal")}
                for d, v in worst
            ]
        if budget[0] <= 0:
            out["errors"].append(
                f"{kind}: 请求预算用尽（历史窗口可能不完整，基线偏保守）")
    return out


def append_snapshot(snapshot: dict, data_dir: str = "") -> str:
    """把一次采集结果追加到 `data/supply_events_<年>.jsonl`（失败只打印）。"""
    try:
        base = data_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        os.makedirs(base, exist_ok=True)
        path = os.path.join(base, f"supply_events_{datetime.now():%Y}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(snapshot, ensure_ascii=False, default=str) + "\n")
        return path
    except Exception as exc:
        print(f"[supply_events] 快照写入失败: {exc}", flush=True)
        return ""


def read_snapshots(year: int = 0, data_dir: str = "") -> list:
    """读回历史快照（给复盘/回测用）。"""
    base = data_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    y = year or datetime.now().year
    path = os.path.join(base, f"supply_events_{y}.jsonl")
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    return out


# ---------------- 文本渲染 ----------------

def render(snapshot: dict, top_n: int = 6) -> str:
    """渲染成推送/日报用的文本。数字缺失一律显式说明，不静默填 0。"""
    L = [f"【全市场供给冲击日历】（未来 {snapshot.get('days')} 天，"
         f"截至 {snapshot.get('ts', '')[:16]}）"]
    kinds = snapshot.get("kinds") or {}

    lift = kinds.get("lift")
    if lift:
        b = lift["baseline"]
        L.append("")
        L.append(f"🔓 解禁：未来 {snapshot.get('days')} 天内 {lift['window_n']} 家 / "
                 f"合计 {lift['window_cap_yi']:.0f} 亿元（{lift['event_days']} 个交易日）")
        if b["days"]:
            L.append(f"   单日基线（{b['days']} 天有解禁）：p50={b['p50']:.0f} "
                     f"p80={b['p80']:.0f} p90={b['p90']:.0f} 最高={b['max']:.0f} 亿")
        else:
            L.append("   ⚠ 单日基线样本不足，不给单日评级")
        if lift.get("window_p50"):
            L.append(f"   窗口合计对照（历史同 {snapshot.get('days')} 天窗口）："
                     f"p50={lift['window_p50']:.0f} p90={lift['window_p90']:.0f} 亿 "
                     f"→ 整体 {LEVEL_ICON[lift['level']]} {LEVEL_TEXT[lift['level']]}")
        for d in (lift.get("top_days") or [])[:top_n]:
            L.append(f"     {d['date']}  {d['cap_yi']:>7.0f}亿  {d['n']:>3}家  "
                     f"{LEVEL_ICON[d['level']]}{LEVEL_TEXT[d['level']]}")

    seo = kinds.get("seo")
    if seo and seo["window_n"]:
        L.append("")
        L.append(f"📤 增发上市：窗口内 {seo['window_n']} 家 / {seo['window_cap_yi']:.1f} 亿")
        for e in sorted(seo["future"], key=lambda x: x["date"])[:top_n]:
            L.append(f"     {e['date']}  {e['name']}({e['code']})  "
                     f"{e['cap_yi']:.2f}亿  {e['detail']}")
        if seo["window_n"] <= 3:
            L.append("     （增发天然稀疏，未来 180 天全市场仅个位数，不做分位数评级）")

    red = kinds.get("reduce")
    if red and red["window_n"]:
        b = red["baseline"]
        L.append("")
        L.append(f"📉 减持公告（**回顾**近 {snapshot.get('days')} 天）："
                 f"{red['window_n']} 家次 / 合计 {red['window_cap_yi']:.0f} 亿元 "
                 f"→ {LEVEL_ICON[red['level']]} {LEVEL_TEXT[red['level']]}")
        if b["days"]:
            L.append(f"   单日基线（{b['days']} 天有公告）：p50={b['p50']:.1f} "
                     f"p80={b['p80']:.1f} p90={b['p90']:.1f} 亿")
        for e in sorted(red["future"], key=lambda x: -x["cap_yi"])[:top_n]:
            L.append(f"     {e['date']}  {e['name']}({e['code']})  {e['cap_yi']:.1f}亿")
        L.append("     ⚠ 无法做**前瞻**减持日历：该数据源只收录已过公告日的记录")
        L.append("     （未来窗口四个日期字段实测全 0 行）；家次口径同一股可多次")

    if snapshot.get("errors"):
        L.append("")
        for e in snapshot["errors"]:
            L.append(f"⚠ 采集失败：{e}")
    if len(L) == 1:
        L.append("（窗口内无供给事件，或数据源异常）")
    return "\n".join(L)


if __name__ == "__main__":
    import sys
    d = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DAYS
    t0 = time.time()
    snap = collect(days=d)
    print(render(snap))
    print(f"\n采集耗时 {time.time()-t0:.1f}s")
    p = append_snapshot(snap)
    print(f"快照: {p}")
