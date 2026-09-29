"""自选股事件告警：限售解禁 + 增发新股上市 + 股东/高管减持（东财公开接口，零成本、无需 key）。

这些事件都会带来抛压/摊薄，值得提前知道：
    - 解禁    定向增发机构配售股份、股权激励限售股等到期上市流通，供给突增
    - 增发    定增/公开增发的新增股份上市流通，股本摊薄（价格通常低于市价）
    - 减持    董监高/重要股东卖出。2024《上市公司股东减持股份管理暂行办法》要求
              预披露（一般提前 15 个交易日），所以**提前预警是做得到的**——
              靠「减持计划公告」在发布当天就推，而不是等真减了才知道。

⚠️ 两类事件的**时间方向相反**，别混：
    - 解禁/增发 查的是**未来**窗口（今天 <= 事件日 <= 今天+days）
    - 减持      查的是**刚过去**的 lookback（公告/明细发布即可推，天然就是"提前"）

数据源（2026-09-29 本机逐个实测，能通的列在下面，报不通的**不要**再写进来）：
    - RPT_LIFT_STAGE   限售解禁表：FREE_DATE 解禁日、CURRENT_FREE_SHARES 解禁股数(万股)、
      LIFT_MARKET_CAP 解禁市值(万元)、FREE_SHARES_TYPE 限售类型
    - RPT_SEO_DETAIL   增发明细表：ISSUE_LISTING_DATE 新增股份上市日、ISSUE_NUM 发行股数(股)、
      ISSUE_PRICE 发行价、ISSUE_WAY 发行方式、LOCKIN_PERIOD 锁定期、SEO_TYPE 1=定向 2=公开
    - np-anotice-stock 个股公告流：减持**计划**公告（预披露在这里，不在任何报表里）
    - RPT_EXECUTIVE_HOLD_DETAILS 董监高持股变动**明细**：CHANGE_DATE/PERSON_NAME/
      POSITION_NAME/CHANGE_SHARES/AVERAGE_PRICE/CHANGE_REASON/CHANGE_RATIO(占总股本%)/GGEID
    filter 语法：SECURITY_CODE in ("600519",...)；日期比较必须用单引号
    （FREE_DATE>='2026-09-14'，双引号会被判成格式错误）。
    ❌ RPT_SHARE_HOLDER_REDUCE / RPT_SHAREHOLDER_REDUCE 等减持计划报表名均不存在
       （接口回 success=false:「报表名不存在」），预披露只能走公告流。

提醒策略：首轮发现即推一条汇总（每事件只推一次，data/alerts_state.json 记键防轰炸）；
页面「自选行情」卡每次现拉，不受已推状态影响。
"""

import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "data" / "alerts_state.json"

DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
ANN_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://data.eastmoney.com/",
}

DEFAULT_DAYS = 14           # 解禁/增发的未来关注窗口（自然日）
DEFAULT_REDUCE_LOOKBACK = 5  # 减持公告/明细回看天数
PAGE_SIZE = 200
ANN_PAGE_SIZE = 100
ANN_MAX_PAGES = 4          # 公告量大，翻到不满一页就停
ANN_REDUCE_NODE = 7        # 东财公告分类：股东减持/高管持股变动（穷举试出来的，别乱改）
ANN_CODE_CHUNK = 40        # stock_list 逗号拼接的代码上限，防 URL 过长
CODES_PER_FILTER = 60      # filter 里 in (...) 的代码上限，参照 _in_filter

# 事件类型 -> (图标, 中文标签)。前端和 app.py 都从这里取，别再各自 if/else 写死
# ——加新类型时漏改一处，LLM 上下文里就会出现「减持…增发上市」这种错标签。
KIND_META = {
    "lift":         ("🔓", "解禁"),
    "placement":    ("📤", "增发上市"),
    "reduce_plan":  ("📉", "减持计划"),
    "reduce_done":  ("🔻", "已减持"),
}


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


def _in_filters(codes: list[str]) -> list[str]:
    """把代码分批拼成多个 filter。自选股超过 CODES_PER_FILTER 个时必须分批，
    否则 filter 串过长会被接口判成格式错误、**静默返回空**（看起来像"没有事件"）。"""
    return [_in_filter(codes[i:i + CODES_PER_FILTER])
            for i in range(0, len(codes), CODES_PER_FILTER)]


def _dc_query_codes(report: str, fields: str, codes: list[str],
                    extra_flt: str = "", sort_col: str = "") -> list[dict]:
    """按 _in_filters 分批查、结果拼接。任何一批失败只丢那一批（已打印原因）。"""
    out = []
    for flt in _in_filters(codes):
        out.extend(_dc_query(report, fields, flt + extra_flt, sort_col))
    return out


def fetch_lift_events(codes: list[str], end_iso: str) -> list[dict]:
    """未来解禁：今天 <= FREE_DATE <= end。"""
    extra = (f"(FREE_DATE>='{date.today().isoformat()}')(FREE_DATE<='{end_iso}')")
    out = []
    for r in _dc_query_codes("RPT_LIFT_STAGE",
                             "SECURITY_CODE,SECURITY_NAME_ABBR,FREE_DATE,"
                             "CURRENT_FREE_SHARES,LIFT_MARKET_CAP,FREE_SHARES_TYPE",
                             codes, extra, "FREE_DATE"):
        # 万股 -> 亿元；接口口径：CURRENT_FREE_SHARES 万股、LIFT_MARKET_CAP 万元
        shares_yi = round((r.get("CURRENT_FREE_SHARES") or 0) / 1e4, 2)
        cap_yi = round((r.get("LIFT_MARKET_CAP") or 0) / 1e4, 2)
        out.append({
            "type": "lift",
            "code": r["SECURITY_CODE"],
            "name": r.get("SECURITY_NAME_ABBR", ""),
            "event_date": (r.get("FREE_DATE") or "")[:10],
            "shares_yi": shares_yi,
            "cap_yi": cap_yi,
            "shares_txt": f"{shares_yi}亿股",
            "detail": r.get("FREE_SHARES_TYPE", ""),
        })
    return out


def fetch_placement_events(codes: list[str], end_iso: str) -> list[dict]:
    """未来增发新增股上市：今天 <= ISSUE_LISTING_DATE <= end。"""
    extra = (f"(ISSUE_LISTING_DATE>='{date.today().isoformat()}')"
             f"(ISSUE_LISTING_DATE<='{end_iso}')")
    out = []
    for r in _dc_query_codes("RPT_SEO_DETAIL",
                             "SECURITY_CODE,SECURITY_NAME_ABBR,ISSUE_LISTING_DATE,"
                             "ISSUE_NUM,ISSUE_PRICE,ISSUE_WAY,LOCKIN_PERIOD,SEO_TYPE",
                             codes, extra, "ISSUE_LISTING_DATE"):
        kind = "定向增发" if str(r.get("SEO_TYPE")) == "1" else "公开增发"
        num = r.get("ISSUE_NUM") or 0
        shares_yi = round(num / 1e8, 2)
        cap_yi = round(num * (r.get("ISSUE_PRICE") or 0) / 1e8, 2)
        out.append({
            "type": "placement",
            "code": r["SECURITY_CODE"],
            "name": r.get("SECURITY_NAME_ABBR", ""),
            "event_date": (r.get("ISSUE_LISTING_DATE") or "")[:10],
            "shares_yi": shares_yi,
            "cap_yi": cap_yi,
            "shares_txt": f"{shares_yi}亿股",
            "detail": f"{kind}·{r.get('ISSUE_WAY', '')}·{r.get('LOCKIN_PERIOD', '')}",
        })
    return out


# ---------------- 减持：预披露公告 + 董监高实际减持 ----------------

# 「已经发生/进行中」类措辞。必须**先于**计划判定：江波龙 2026-09-29 的
# 「关于高级管理人员股份减持计划期限届满暨减持计划实施完毕的公告」同时含
# 「减持」「计划」「届满」「实施完毕」——按「计划」在前判定会把它当成新计划再推一次，
# 变成噪音。真实存在这种标题（2026-09-29 本机实测），所以顺序不能反。
_DONE_WORDS = ("实施完毕", "减持完毕", "减持完成", "实施结果", "实施进展",
               "减持进展", "进展", "结果", "届满", "实施情况", "变动")
# 「预披露」必须单列：法定预披露公告的常见标题是「…减持股份**预披露**公告」，
# 不含「减持计划」字样（江波龙 2026-06-05、2026-03-19 等多条都是这个句式），
# 不加就会被下面的兜底判成"已减持"——提前预警直接失效。
_PLAN_WORDS = ("减持计划", "拟减持", "减持股份计划", "计划减持", "预披露")


def classify_reduce_title(title: str) -> str | None:
    """公告标题 -> 'reduce_plan' / 'reduce_done' / None（不是减持事件）。"""
    if "减持" not in title:
        return None
    if any(w in title for w in _DONE_WORDS):
        return "reduce_done"
    if any(w in title for w in _PLAN_WORDS):
        return "reduce_plan"
    # 说不清是计划还是进展的，保守当「已发生」——计划类已由 _PLAN_WORDS 覆盖
    return "reduce_done"


def _ann_query(codes: list[str], cutoff_iso: str) -> list[dict]:
    """按代码批量拉**减持分类**的个股公告（f_node=7），返回 notice_date >= cutoff 的。

    为什么必须带 f_node=7（2026-09-29 实测，别去掉）：
      - 不带分类时接口按「更新时间」倒序返回全类别公告，H股「翌日披露报表」这种
        每天重复更新的条目会把 page_size 填满，真正的减持公告被挤出第一页
        —— 表现为"明明有公告却查不到"，且**静默无报错**。
      - f_node=7 是东财的「股东减持/高管持股变动」专属分类。江波龙在该分类下
        只有 26 条（2023-08 至今），逐条都是减持事件，语义干净、量级小。
      - 分类取值是穷举试出来的：f_node=0/1..6/8..10 都不对（1=定期报告、4=只有
        权益变动类，8 起返回空）。改这个值前请重新验证。

    ⚠️ 源的一致性：同一 URL 重复请求结果稳定（md5 验证过），但历史上观察到过
    不带分类的查询在两次调用间返回**不同的同日条目**（副本索引不一致）。限定
    f_node=7 + 日期区间后未再复现；仍保留"宁可漏报也不误报"的取向——漏了下轮
    补上，误报会消耗用户对推送的信任。

    stock_list 支持逗号分隔多代码，所以 36 只自选是几次请求而不是 36 次。
    """
    out = []
    end_iso = date.today().isoformat()
    for i in range(0, len(codes), ANN_CODE_CHUNK):
        chunk = codes[i:i + ANN_CODE_CHUNK]
        for page in range(1, ANN_MAX_PAGES + 1):
            try:
                r = requests.get(ANN_URL, params={
                    "sr": -1, "page_size": ANN_PAGE_SIZE, "page_index": page,
                    "ann_type": "A", "client_source": "web",
                    "stock_list": ",".join(chunk),
                    "f_node": ANN_REDUCE_NODE, "s_node": 0,
                    "begin_time": cutoff_iso, "end_time": end_iso,
                }, headers=HEADERS, timeout=20)
                r.raise_for_status()
                lst = ((r.json().get("data") or {}).get("list")) or []
            except Exception as exc:
                print(f"[alerts] announcement query failed: {exc}", flush=True)
                break
            if not lst:
                break
            out.extend(a for a in lst
                       if (a.get("notice_date") or "")[:10] >= cutoff_iso)
            if len(lst) < ANN_PAGE_SIZE:   # 最后一页
                break
    return out


def _ann_codes(a: dict) -> tuple[str, str]:
    """公告条目 -> (6 位 A股代码, 简称)。港股 5 位/其它一律跳过。"""
    cs = a.get("codes") or [{}]
    c0 = cs[0]
    code = str(c0.get("stock_code") or "")
    if len(code) != 6 or not code.isdigit():
        return "", ""
    return code, c0.get("short_name") or ""


def _ann_title(a: dict, name: str) -> str:
    """公告标题去掉「江波龙:」这类前缀——简称已经单独渲染了，留着会重复。"""
    title = (a.get("title") or "").strip()
    for pre in (f"{name}:", f"{name}："):
        if name and title.startswith(pre):
            return title[len(pre):].strip()
    return title


def _num(v) -> str:
    """人读数字：去尾零、保留前导零、不出科学计数。

    Python 的 :g 两条都做不到——2_735_000 会变成 '2.735e+06'，
    0.0261 会变成 '.0261'（前导零没了，读起来像少了位数）。
    """
    if v is None:
        return ""
    s = f"{float(v):.4f}".rstrip("0").rstrip(".")
    return s or "0"


def fetch_reduce_events(codes: list[str], lookback_days: int = DEFAULT_REDUCE_LOOKBACK
                        ) -> list[dict]:
    """自选股近 lookback_days 的减持：预披露计划公告 + 董监高实际减持明细。

    两个来源互补、都要：
      - 公告流  是**提前预警**的唯一来源（预披露只存在于公告正文/标题里）
      - 高管明细 是**事后**的事实（谁、职务、均价、占总股本比例），公告标题里没有
    """
    a_codes = [c for c in codes if len(c) == 6 and c.isdigit()]
    if not a_codes:
        return []
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    events = []

    # --- 1) 公告流：计划 / 进展 ---
    for a in _ann_query(a_codes, cutoff):
        title = a.get("title") or ""
        kind = classify_reduce_title(title)
        if not kind:
            continue
        code, name = _ann_codes(a)
        if code not in a_codes:
            continue
        art = a.get("art_code") or ""
        events.append({
            "type": kind,
            "code": code,
            "name": name,
            # 计划类没有可靠的"事件日"（区间在公告正文里，不解析）——用公告日，
            # 语义是「这一天该知道了」，这正是预披露的意义
            "event_date": (a.get("notice_date") or "")[:10],
            "shares_yi": None,
            "cap_yi": None,
            "shares_txt": "",
            "detail": _ann_title(a, name),
            "key": f"{kind}:{code}:{art}",
        })

    # --- 2) 董监高持股变动明细 ---
    extra = f"(CHANGE_DATE>='{cutoff}')"
    for r in _dc_query_codes("RPT_EXECUTIVE_HOLD_DETAILS",
                             "SECURITY_CODE,SECURITY_NAME,CHANGE_DATE,PERSON_NAME,"
                             "POSITION_NAME,CHANGE_SHARES,AVERAGE_PRICE,CHANGE_REASON,"
                             "CHANGE_RATIO,CHANGE_AFTER_HOLDNUM,GGEID",
                             a_codes, extra, "CHANGE_DATE"):
        shares = r.get("CHANGE_SHARES") or 0
        if shares >= 0:      # 只收减持；增持不是抛压
            continue
        avg = r.get("AVERAGE_PRICE") or 0
        # CHANGE_RATIO 已是**百分数**（不是小数）：江波龙 -85000 股 ratio=0.0185
        # 反推总股本 ≈ 4.59 亿股，与实际一致，别再除 100
        ratio = r.get("CHANGE_RATIO")
        bits = [x for x in (r.get("POSITION_NAME"), r.get("CHANGE_REASON")) if x]
        if avg:
            bits.append(f"均价{_num(avg)}")
        if ratio is not None:
            bits.append(f"占总股本{_num(ratio)}%")
        if r.get("CHANGE_AFTER_HOLDNUM") is not None:
            bits.append(f"变动后持股{r['CHANGE_AFTER_HOLDNUM']:,.0f}股")
        ggeid = r.get("GGEID") or ""
        if not ggeid:   # 极少数行没有 GGEID，退化成 日期+人+股数 的组合键
            ggeid = "|".join(str(x) for x in
                             (r.get("CHANGE_DATE"), r.get("PERSON_NAME"), shares))
        events.append({
            "type": "reduce_done",
            "code": r["SECURITY_CODE"],
            "name": r.get("SECURITY_NAME", ""),
            "event_date": (r.get("CHANGE_DATE") or "")[:10],
            "shares_yi": None,
            "cap_yi": round(abs(shares) * avg / 1e8, 4) if avg else None,
            "shares_txt": f"{abs(shares) / 1e4:g}万股",
            "detail": f"{r.get('PERSON_NAME', '')}·" + "·".join(bits),
            # GGEID 是这次持股变动的稳定事件号
            "key": f"reduce_done:{r['SECURITY_CODE']}:{ggeid}",
        })

    # 同一个 key 只留一条（公告流和明细都可能报同一件事）
    seen, uniq = set(), []
    for e in events:
        if e["key"] in seen:
            continue
        seen.add(e["key"])
        uniq.append(e)
    return uniq


# ---------------- 事件汇总 ----------------

def _a_codes_only(codes: list[str]) -> list[str]:
    return [c for c in codes if len(c) == 6 and c.isdigit()]


def upcoming_events(codes: list[str], days: int = DEFAULT_DAYS) -> list[dict]:
    """自选 A股**未来**窗口内解禁/增发事件（港股 6 位以下代码不适用，按 6 位过滤）。

    只含前瞻事件——要连减持一起看用 all_events()。
    """
    a_codes = _a_codes_only(codes)
    if not a_codes:
        return []
    end_iso = (date.today() + timedelta(days=days)).isoformat()
    events = fetch_lift_events(a_codes, end_iso) + fetch_placement_events(a_codes, end_iso)
    events.sort(key=lambda e: (e["event_date"], -(e.get("cap_yi") or 0)))
    return events


def all_events(codes: list[str], days: int = DEFAULT_DAYS,
               reduce_lookback_days: int = DEFAULT_REDUCE_LOOKBACK,
               include_reduce: bool = True) -> list[dict]:
    """解禁/增发（前瞻）+ 减持（回看），按日期倒序——最新的、最该先看的排最前。

    倒序而非像 upcoming_events 那样升序：减持/公告类事件日期在过去甚至今天，
    升序会把它们全推到列表末尾，网页上根本看不见。列表头部另有"未读"高亮语义。
    """
    events = upcoming_events(codes, days=days)
    if include_reduce:
        events += fetch_reduce_events(codes, lookback_days=reduce_lookback_days)
    # 未推过的新事件优先（event_date 空/无效的排最后）
    events.sort(key=lambda e: (e.get("event_date") or "", -(e.get("cap_yi") or 0)),
                reverse=True)
    return events


# ---------------- 推送去重状态 ----------------

def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"sent": {}}


def _prune_state(sent: dict) -> dict:
    """只留近 90 天推过的键：旧键不再有意义，防文件无限膨胀。"""
    cutoff = (date.today() - timedelta(days=90)).isoformat()
    return {k: v for k, v in sent.items() if str(v)[:10] >= cutoff}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        print(f"[alerts] state save failed: {exc}", flush=True)


def event_key(e: dict) -> str:
    """事件去重键。减持类用数据源自带的稳定 id（art_code / GGEID），
    其余用 类型:代码:日期。别自己编日期串——同一事项在公告和明细里是两个 key，
    会推两遍。"""
    if e.get("key"):
        return e["key"]
    return f"{e['type']}:{e['code']}:{e['event_date']}"


def _fmt_event(e: dict) -> str:
    icon, label = KIND_META.get(e["type"], ("•", e["type"]))
    amt = " ".join(x for x in (e.get("shares_txt") or "",
                               f"约{_num(e['cap_yi'])}亿元" if e.get("cap_yi") else "") if x)
    return (f"{icon}{label} {e['event_date']} {e['name']}（{e['code']}）"
            + (f" {amt} · " if amt else "")
            + f"{e.get('detail') or ''}")


_GROUP_TITLES = [
    ("lift",        "【限售解禁】"),
    ("placement",   "【增发新股上市】"),
    ("reduce_plan", "【减持计划·预披露】"),
    ("reduce_done", "【已发生减持】"),
]


def check_alerts_once(conn, notify_fn=None, days: int = DEFAULT_DAYS,
                      reduce_lookback_days: int = DEFAULT_REDUCE_LOOKBACK,
                      include_reduce: bool = True) -> list[dict]:
    """扫一遍自选股，首轮发现的新事件合并推一条；返回本次推送的事件列表。

    notify_fn(title, content) 注入（app 传 notifier.notify），None 时只组装不推送。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT code FROM sa_watchlist")
        codes = [r[0] for r in cur.fetchall()]
    events = all_events(codes, days=days, reduce_lookback_days=reduce_lookback_days,
                        include_reduce=include_reduce)
    state = _load_state()
    sent = _prune_state(state["sent"])
    state["sent"] = sent
    today = date.today().isoformat()
    fresh = []
    for e in events:
        key = event_key(e)
        if key in sent:
            continue
        sent[key] = today
        fresh.append(e)
    _save_state(state)
    if fresh and notify_fn:
        parts = []
        for kind, title in _GROUP_TITLES:
            group = [e for e in fresh if e["type"] == kind]
            if group:
                parts.append(title + "\n" + "\n".join(_fmt_event(e) for e in group))
        notify_fn(
            "⚠️ 自选股解禁/增发/减持提醒",
            f"有 {len(fresh)} 个新事件（解禁/增发看未来 {days} 天，"
            f"减持看最近 {reduce_lookback_days} 天）：\n\n" + "\n\n".join(parts) +
            f"\n\n（解禁=供给冲击，增发上市=股本摊薄，减持=已公告的抛压；"
            f"来自 stock-advisor 事件监控，"
            f"{datetime.now().strftime('%Y-%m-%d %H:%M')}）")
    return fresh
