"""机构信号：券商评级 / 机构与社保持仓变化 / 指数成分调整（2026-10-12 新增）。

## 为什么要它 —— 先看实测（本模块存在的唯一理由）

用户诉求：「机构看多看空、机构持仓变化、社保基金持仓变化、被挪出重要指数，
这些也很重要，要参与决策」。开工前逐条实测了数据可得性（**真请求，不是查文档**）：

| 想要的信号 | 能用的接口（akshare 1.18.88） | 实测 | 硬限制 |
|---|---|---|---|
| 机构看多看空 | `stock_research_report_em` | 000858 **460 条**、600519 **772 条**、600036 **232 条**；评级 买入/增持/中性/减持 + 研究机构名 | 无，最新到上月末 |
| 机构持仓变化 | `stock_main_stock_holder` | 600036 **1342 行**、000858 961 行，含全部报告期 | **最新报告期 2026-06-30、公告 08-29 → 滞后约 2 个月** |
| 社保基金持仓 | 同上（股东名含「社保」） | 000858 机构类股东 **655/961 行** | 同上，季度频率 |
| 被挪出重要指数 | `index_stock_cons_csindex` | 000300 = 300 行、日期=当天 | **只有当前成分、没有调整历史** |

两个坑（省得以后再踩）：

1. akshare 的**机构持股汇总接口在这个版本全是坏的**：
   `stock_institute_rating_rank` 不存在、`stock_gdfx_free_top_10_em` 报
   `KeyError: 'sdltgd'`、`stock_report_fund_hold_detail` 报 `TypeError: NoneType`、
   `stock_profit_forecast_em` 同样报错。所以**不能指望现成聚合**，
   只能从「十大股东明细」自己识别机构类型、自己算环比。
2. `index_stock_cons_csindex` 很慢（000300 单只实测 11 秒），批量指数要串行 + 单独计时。

## 「被调出」只能自己 diff

成分调整公告本身抓不到（东财/中证都没有稳定的结构化接口），但**成分快照**有。
所以本模块每天存一份快照，与上一份 diff 得到 `in`/`out`——第一次快照只是基线，
不产生任何变更事件（否则上线第一天会误报「全部调入」）。

## 时滞必须写在注入文本里（不是写在文档里）

持仓数据滞后 2 个月、评级滞后 0~1 个月、成分变更 T+1。**这三者时效完全不同，
混在一起给 LLM 会让它把 6 月的社保持仓当成今天的信号** —— 这就是 AGENTS.md
反复记的「抓取时间冒充事实时间」的同类坑。所以每一条都带数据截止日与滞后天数。

## 不做什么

- 不 import app（同 alerts.py / paper_memory.py 惯例，避免循环依赖）。
- 不改任何交易逻辑：只读行情/公告数据、只写自己的表，注入由开关控制（默认关）。
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta

DEFAULTS = {
    # 默认关的理由是**注入决策 = 改决策口径**（AGENTS.md §0 的授权边界），
    # 不是省钱 —— 见 AGENTS.md「别为省 LLM 调用而牺牲效果」。
    "enabled": False,
    "interval_minutes": 0,     # 守护线程轮询间隔，0 = 线程空转
    "rating_days": 90,         # 评级统计窗口
    "rating_top": 3,           # 展示最近 N 篇研报
    "holder_top": 3,           # 展示增减各 N 条
    "indices": ["000300", "000016", "000905", "000852", "000688"],
    "hold_season": True,       # True = 只在披露季窗口拉持仓
    "hold_season_windows": [(4, 20, 5, 10), (8, 20, 9, 10), (10, 20, 11, 10)],
}

# ---------------- 机构类型识别 ----------------
# ⚠️ 顺序是**规则的一部分**，不是风格问题（离线测试实测出来的一条真 bug）：
# 先判券商资管会让「XX证券投资基金」全部被归成券商 —— 公募基金名字里几乎
# 都带「证券」两字，结果最常见的一类被错分。改成基金先判，券商资管只认
# 「资产管理/资管/证券公司」这三个**不会与基金重名**的特征。
# 社保同理：必须早于基金，否则「社保基金」被归成普通基金。
_HOLDER_PATTERNS = [
    ("社保", ("社保",)),
    ("港股通", ("香港中央结算",)),
    ("证金", ("证券金融", "中证金融")),
    ("保险", ("保险", "人寿", "财险", "人保", "太保")),
    ("基金", ("基金", "ETF")),
    ("券商资管", ("资产管理", "资管", "证券公司")),
    ("信托", ("信托",)),
    ("QFII", ("QFII", "合格境外", "摩根大通", "高盛")),
]


def classify_holder(name: str) -> str:
    """按股东名识别机构类型。返回 '' 表示不是机构（自然人/未匹配）。"""
    s = str(name or "")
    if not s:
        return ""
    for label, pats in _HOLDER_PATTERNS:
        if any(p in s for p in pats):
            return label
    return ""


# ---------------- DDL ----------------

DDL = """
-- ① 券商研报评级（看多看空）
CREATE TABLE IF NOT EXISTS sa_analyst_rating (
    code        VARCHAR(8)  NOT NULL,
    report_date date        NOT NULL,
    org         text        NOT NULL DEFAULT '',
    rating      text        NOT NULL DEFAULT '',
    title       text        NOT NULL DEFAULT '',
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (code, report_date, org, title)
);
CREATE INDEX IF NOT EXISTS idx_sa_analyst_rating_code
    ON sa_analyst_rating (code, report_date DESC);

-- ② 机构/社保持仓明细（只存机构，自然人不存）
CREATE TABLE IF NOT EXISTS sa_holder_inst (
    code       VARCHAR(8)  NOT NULL,
    period     date        NOT NULL,          -- 报告期（季末），不是公告日
    holder     text        NOT NULL,
    htype      text        NOT NULL DEFAULT '',
    shares     numeric(20,4),
    ratio      numeric(8,4),
    fetched_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (code, period, holder)
);
CREATE INDEX IF NOT EXISTS idx_sa_holder_inst_code
    ON sa_holder_inst (code, period DESC);

-- ③ 指数成分快照（「被调出」靠它 diff 出来）
CREATE TABLE IF NOT EXISTS sa_index_cons (
    index_code VARCHAR(12) NOT NULL,
    snap_date  date        NOT NULL,
    code       VARCHAR(8)  NOT NULL,
    cons_name  text        NOT NULL DEFAULT '',
    PRIMARY KEY (index_code, snap_date, code)
);
CREATE INDEX IF NOT EXISTS idx_sa_index_cons_code
    ON sa_index_cons (code, index_code, snap_date DESC);

-- ④ 成分变更事件（in/out）
CREATE TABLE IF NOT EXISTS sa_index_cons_change (
    index_code VARCHAR(12) NOT NULL,
    code       VARCHAR(8)  NOT NULL,
    direction  text        NOT NULL,          -- 'in' | 'out'
    prev_date  date,
    new_date   date        NOT NULL,
    prev_count int,
    new_count  int,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (index_code, code, new_date, direction)
);
CREATE INDEX IF NOT EXISTS idx_sa_index_cons_change_code
    ON sa_index_cons_change (code, new_date DESC);
"""


def ensure_ddl(conn) -> None:
    """建表。幂等，可重复跑。"""
    with conn.cursor() as cur:
        cur.execute(DDL)
    conn.commit()


# ---------------- 小工具 ----------------

def _pick(cols, *words):
    """按关键字挑列名（东财改过列名，写死下标会静默取错列）。"""
    for w in words:
        for c in cols:
            if w in str(c):
                return c
    return None


def _with_timeout(fn, timeout: float = 30.0, **kw):
    """给 akshare 的单次调用套一个**硬超时**。

    为什么必须：akshare 底层是 requests/urllib，遇到东财限流时会**无限期挂着**
    （不是抛异常，是不返回）。放进守护线程就意味着整条机构信号回路停摆 ——
    2026-10-12 E2E 实测：同一接口短时间打了几轮之后，采集跑到一半就再也
    不返回，整脚本 420 秒超时。所以每个调用都必须有上限，超了就当这只票没数据。

    代价：超时后那个线程仍在后台跑（Python 杀不掉阻塞线程），但**主流程继续**，
    且并发数被 executor 限住，不会无限堆积。
    """
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FTimeout
    ex = ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(fn, **kw)
        return fut.result(timeout=timeout)
    except FTimeout:
        print("[inst] 调用超时 %.0fs（%s），跳过" % (timeout, getattr(fn, "__name__", fn)),
              flush=True)
        return None
    except Exception as exc:                      # noqa: BLE001
        print("[inst] 调用失败 %s: %s" % (getattr(fn, "__name__", fn),
                                        str(exc)[:100]), flush=True)
        return None
    finally:
        ex.shutdown(wait=False)


def _num(v):
    """转数字；**NaN/Inf 一律当没有值**。

    ⚠️ akshare 的持股数在部分股东行（港股通/证金）上是字符串 'nan'，
    `float('nan')` 不抛异常、直接返回 nan —— 存进 numeric 列就成了 NaN，
    后面「本期 - 上期」算出 nan，排序与展示全被污染，而且**不报错**。
    （与 AGENTS.md 记的 `fnum` 拦不住 NaN 是同一类坑。）
    """
    import math
    try:
        f = float(str(v).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _date(v):
    s = str(v)[:10].replace("/", "-")
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def in_hold_season(d: date | None = None) -> bool:
    """披露季窗口（默认 4/8/10 月 20 日 ~ 各自次月 10 日）。

    持仓是季报数据，平时再跑多少次都不会变 —— 与其每天白拉几十次，
    不如只在它真的会变的那个窗口跑。
    """
    d = d or date.today()
    md = (d.month, d.day)
    for m0, d0, m1, d1 in DEFAULTS["hold_season_windows"]:
        if (m0, d0) <= (m1, d1):
            if (m0, d0) <= md <= (m1, d1):
                return True
        else:                                     # 跨年窗口（如 12 月->1 月）
            if md >= (m0, d0) or md <= (m1, d1):
                return True
    return False


def _ratings_supported(code: str, name: str) -> bool:
    """研报只覆盖 A 股个股。

    实测：43 只自选里 17 只被东财拒（9 只港股 + 5 只 ETF + 3 只无研报的小盘股），
    报错是 `KeyError: 'infoCode'` —— akshare 拿到的响应里没有预期字段。
    港股/ETF 是**结构性**拿不到（不是「暂时没有」），每轮白跑 14 次纯属浪费，
    还把日志刷成一片报错。规则是通用的（按代码位数与名字含 ETF），没有逐票映射。
    """
    if len(str(code)) == 5:                      # 港股 5 位
        return False
    if "ETF" in str(name or "").upper():
        return False
    return True


def universe(cur, limit: int = 120) -> list[tuple[str, str]]:
    """采集范围 = 自选 ∪ 持仓（决策只关心这两类，别把 5000 只全市场拉一遍）。"""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    try:
        cur.execute("SELECT code, name FROM sa_watchlist ORDER BY code")
        for code, name in cur.fetchall():
            if code and code not in seen:
                seen.add(code)
                out.append((code, (name or "").strip()))
    except Exception as exc:                     # noqa: BLE001
        print("[inst] 读自选失败: %s" % str(exc)[:100], flush=True)
    try:
        cur.execute("SELECT code, sum(CASE WHEN side='buy' THEN shares "
                    "ELSE -shares END) s FROM sa_paper_trades "
                    "WHERE status <> 'skipped' GROUP BY code")
        for code, s in cur.fetchall():
            if s and s > 0 and code not in seen:
                seen.add(code)
                out.append((code, ""))
    except Exception as exc:                     # noqa: BLE001
        print("[inst] 读持仓失败: %s" % str(exc)[:100], flush=True)
    return out[:limit]


# ---------------- 采集 ①：券商评级 ----------------

def collect_ratings(conn, codes) -> dict:
    """逐只拉研报评级，**只插新行**（ON CONFLICT DO NOTHING），可每天跑。

    `codes` 可以是代码列表，也可以是 (code, name) 列表 —— 给 name 是为了跳过
    结构性拿不到的（港股/ETF，见 `_ratings_supported`）。
    """
    import akshare as ak
    stat = {"codes": 0, "fetched": 0, "inserted": 0, "newest": None,
            "skipped_unsupported": 0, "errors": []}
    rows: list[tuple] = []
    for item in codes:
        code, name = item if isinstance(item, (tuple, list)) else (item, "")
        if not _ratings_supported(code, name):
            stat["skipped_unsupported"] += 1
            continue
        stat["codes"] += 1
        try:
            df = _with_timeout(ak.stock_research_report_em, timeout=30, symbol=code)
        except Exception as exc:                 # noqa: BLE001
            stat["errors"].append("%s:%s" % (code, str(exc)[:60]))
            continue
        if df is None:
            stat["errors"].append("%s:timeout/err" % code)
            continue
        if df is None or len(df) == 0:
            continue
        cols = list(df.columns)
        # ⚠️ 列名以**真库实测**为准，不是猜的：
        #   实测 stock_research_report_em 的标题列叫「报告名称」（不是「报告标题」），
        #   日期列叫「日期」。我第一版按「报告标题」挑列 → 挑不到 → 标题全空，
        #   而离线测试用的假 df 里我自己写的是「报告标题」，**测试照样全绿** ——
        #   测试和真实接口各说各话，是 AGENTS.md 反复记的那类假绿。
        d_col = _pick(cols, "日期", "报告日期", "发布时间")
        r_col = _pick(cols, "东财评级", "评级")
        o_col = _pick(cols, "机构", "研究机构", "券商")
        t_col = _pick(cols, "报告名称", "报告标题", "标题")
        if not (d_col and r_col):
            stat["errors"].append("%s:no-cols" % code)
            continue
        if not t_col:
            print("[inst] %s 没找到研报标题列（现有列：%s）" % (code, cols),
                  flush=True)
        stat["fetched"] += 1
        for _, r in df.iterrows():
            d = _date(r.get(d_col))
            if not d:
                continue
            rows.append((code, d, str(r.get(o_col) or ""),
                         str(r.get(r_col) or ""), str(r.get(t_col) or "")[:300]))
            if not stat["newest"] or d > stat["newest"]:
                stat["newest"] = d
    if rows:
        import psycopg2.extras as _ex
        with conn.cursor() as cur:
            # ⚠️ 必须批量。逐行 INSERT 在**远程 PG**（101.43.25.101）上是
            # 每行一次网络往返：实测 249 行插入花了 81.8 秒，而抓取本身只要 2.4 秒
            # —— 整轮守护线程会被它拖死，页面上的「手动刷新」更是直接超时。
            _ex.execute_values(cur,
                               "INSERT INTO sa_analyst_rating (code, report_date, "
                               "org, rating, title) VALUES %s "
                               "ON CONFLICT (code, report_date, org, title) "
                               "DO NOTHING", rows, page_size=500)
        conn.commit()
        stat["inserted"] = len(rows)
    print("[inst] 研报评级：%d 只取到 %d 条，最新 %s，插入 %d 条"
          "（跳过港股/ETF %d 只）"
          % (stat["codes"], len(rows), stat["newest"], stat["inserted"],
             stat["skipped_unsupported"]), flush=True)
    return stat


# ---------------- 采集 ②：机构/社保持仓 ----------------

def collect_holders(conn, codes) -> dict:
    """拉十大股东明细，只留**机构**，存全部历史报告期（环比在渲染时算）。

    `codes` 同 collect_ratings：可以是代码列表或 (code, name) 列表。
    """
    import akshare as ak
    stat = {"codes": 0, "rows": 0, "periods": set(), "errors": []}
    rows: list[tuple] = []
    for item in codes:
        code = item[0] if isinstance(item, (tuple, list)) else item
        stat["codes"] += 1
        try:
            df = _with_timeout(ak.stock_main_stock_holder, timeout=30, stock=code)
        except Exception as exc:                 # noqa: BLE001
            stat["errors"].append("%s:%s" % (code, str(exc)[:60]))
            continue
        if df is None:
            stat["errors"].append("%s:timeout/err" % code)
            continue
        if len(df) == 0:
            continue
        cols = list(df.columns)
        n_col = _pick(cols, "股东名称")
        d_col = _pick(cols, "截止日期", "报告期", "日期")
        s_col = _pick(cols, "持股数", "持股数量")
        r_col = _pick(cols, "持股比例", "比例")
        if not (n_col and d_col):
            stat["errors"].append("%s:no-cols" % code)
            continue
        for _, r in df.iterrows():
            holder = str(r.get(n_col) or "").strip()
            htype = classify_holder(holder)
            if not htype:
                continue
            d = _date(r.get(d_col))
            if not d:
                continue
            rows.append((code, d, holder[:120], htype,
                         _num(r.get(s_col)), _num(r.get(r_col))))
            stat["periods"].add(d.isoformat())
    stat["rows"] = len(rows)
    if rows:
        import psycopg2.extras as _ex
        with conn.cursor() as cur:
            # 同上：批量插入，别逐行往返（81.8 秒 → 秒级）
            _ex.execute_values(cur,
                               "INSERT INTO sa_holder_inst (code, period, holder, "
                               "htype, shares, ratio) VALUES %s "
                               "ON CONFLICT (code, period, holder) DO UPDATE "
                               "SET htype = EXCLUDED.htype, shares = EXCLUDED.shares, "
                               "    ratio = EXCLUDED.ratio", rows, page_size=500)
        conn.commit()
    print("[inst] 机构/社保持仓：%d 只取到 %d 行，报告期 %s"
          % (stat["codes"], stat["rows"],
             sorted(stat["periods"])[-3:] or "-"), flush=True)
    return stat


# ---------------- 采集 ③：指数成分快照 + diff ----------------

def snapshot_index(conn, index_code: str) -> dict:
    """存一份成分快照，并与上一份 diff 出 in/out。

    首次快照 = 基线，**不产生任何变更事件**（否则上线当天会「全部调入」）。
    """
    import akshare as ak
    out = {"index": index_code, "snap_date": None, "rows": 0,
           "added": 0, "removed": 0, "prev_date": None, "error": None}
    try:
        df = _with_timeout(ak.index_stock_cons_csindex, timeout=60,
                           symbol=index_code)
    except Exception as exc:                     # noqa: BLE001
        out["error"] = str(exc)[:140]
        print("[inst] 指数 %s 成分抓取失败：%s" % (index_code, out["error"]),
              flush=True)
        return out
    if df is None:
        out["error"] = "timeout_or_error"        # 超时/异常，别写成 "empty"
        print("[inst] 指数 %s 成分没拿到（超时或异常）" % index_code, flush=True)
        return out
    if len(df) == 0:
        out["error"] = "empty"
        return out
    cols = list(df.columns)
    d_col = _pick(cols, "日期")
    c_col = _pick(cols, "成分券代码", "证券代码", "代码")
    n_col = _pick(cols, "成分券名称", "证券名称", "名称")
    snap = _date(df.iloc[0].get(d_col)) if d_col else date.today()
    codes = []
    for _, r in df.iterrows():
        c = str(r.get(c_col) or "").strip()
        if not c:
            continue
        codes.append((index_code, snap, c, str(r.get(n_col) or "")[:60]))
    out["snap_date"], out["rows"] = snap, len(codes)
    if not codes:
        return out
    with conn.cursor() as cur:
        cur.execute("SELECT max(snap_date) FROM sa_index_cons WHERE index_code=%s",
                    (index_code,))
        prev = (cur.fetchone() or (None,))[0]
        # 同上：300 行逐行插入在远程 PG 上就是 ~90 秒，批量一下
        import psycopg2.extras as _ex
        _ex.execute_values(cur,
                           "INSERT INTO sa_index_cons (index_code, snap_date, "
                           "code, cons_name) VALUES %s "
                           "ON CONFLICT (index_code, snap_date, code) DO UPDATE "
                           "SET cons_name = EXCLUDED.cons_name", codes,
                           page_size=500)
        if prev and prev < snap:
            cur.execute("SELECT code FROM sa_index_cons "
                        "WHERE index_code=%s AND snap_date=%s", (index_code, prev))
            old = {r[0] for r in cur.fetchall()}
            new = {c for _, _, c, _ in codes}
            added, removed = sorted(new - old), sorted(old - new)
            out["prev_date"], out["added"], out["removed"] = prev, len(added), len(removed)
            for c in added:
                cur.execute("INSERT INTO sa_index_cons_change (index_code, code, "
                            "direction, prev_date, new_date, prev_count, new_count) "
                            "VALUES (%s,%s,'in',%s,%s,%s,%s) "
                            "ON CONFLICT (index_code, code, new_date, direction) "
                            "DO NOTHING",
                            (index_code, c, prev, snap, len(old), len(new)))
            for c in removed:
                cur.execute("INSERT INTO sa_index_cons_change (index_code, code, "
                            "direction, prev_date, new_date, prev_count, new_count) "
                            "VALUES (%s,%s,'out',%s,%s,%s,%s) "
                            "ON CONFLICT (index_code, code, new_date, direction) "
                            "DO NOTHING",
                            (index_code, c, prev, snap, len(old), len(new)))
    conn.commit()
    print("[inst] 指数 %s 快照 %s：%d 只（基线 %s）调入 %d / 调出 %d"
          % (index_code, snap, len(codes), prev or "-", out["added"],
             out["removed"]), flush=True)
    return out


# ---------------- 渲染：给 LLM 的注入块 ----------------

def _lag_text(d: date | None, as_of: date) -> str:
    if not d:
        return "数据截止日未知"
    n = (as_of - d).days
    return "数据截至 %s（滞后 %d 天）" % (d.isoformat(), n)


def _is_nan(v) -> bool:
    """Postgres 的 numeric 能存 NaN，而 psycopg2 会把它读成 `Decimal('NaN')`。

    实测踩到：`Decimal('NaN') - Decimal(3200)` 在默认 context 下抛
    `decimal.InvalidOperation`，于是整个②段变成「机构持仓读取失败」——
    一只股票的机构持仓整段消失，而且错误信息完全指不到「有个 NaN」。
    （集合里已经有 `_num()` 的 isfinite 闸门，但**修复前**写进去的老行还在。）
    """
    if v is None:
        return False
    if hasattr(v, "is_nan"):
        return bool(v.is_nan())
    return v != v                      # 浮点 NaN 的通用判据


def _row(row, key: str, idx: int = 0):
    """取一行里的第 key 列，**dict 游标与 tuple 游标都能用**。

    ⚠️ 为什么必须：调用方给的是哪种游标取决于上下文 ——
    `paper_trading._decide_one` 用 RealDictCursor、`/api/inst/signals` 也用，
    而我自己写 `(cur.fetchone() or (None,))[0]` 在 dict 行上会抛 `KeyError: 0`，
    现象是「②③ 两段静默变成『读取失败』」，第一段却正常 —— 很难一眼看出是游标问题。
    """
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[idx]
    except (TypeError, IndexError):
        return None


def build_injection(cur, code: str, as_of: str | None = None,
                    conf: dict | None = None) -> str:
    """拼该股的机构信号块。**读不出来就少写一段，绝不抛异常**。"""
    c = {**DEFAULTS, **{k: v for k, v in (conf or {}).items() if v is not None}}
    try:
        d = datetime.strptime(str(as_of)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        d = date.today()
    lines = ["【机构信号（研报评级 / 机构与社保持仓 / 指数成分）】"]
    # ---- ① 券商评级 ----
    # ⚠️ 两处 `title <> ''`：空标题行是 2026-10-12 第一次跑 collector 时的
    # 事故产物（当时按「报告标题」挑列，真列名是「报告名称」→ 标题全空）。
    # 这些行会让评级分布**重复计数**（同一家机构的同一篇研报出现两次）。
    # 过滤掉让它们不影响决策；物理删除要用户点头。
    try:
        cur.execute("SELECT rating, count(*) n, max(report_date) d "
                    "FROM sa_analyst_rating WHERE code=%s AND report_date >= %s "
                    "   AND title <> '' "
                    "GROUP BY rating", (code, d - timedelta(days=int(c["rating_days"]))))
        rows = cur.fetchall()
        if rows:
            dist = {}
            for r in rows:
                key = str(_row(r, "rating", 0) or "未评级").strip()
                dist[key] = int(_row(r, "n", 1) or 0)
            total = sum(dist.values())
            dist_txt = "、".join("%s %d" % (k, v) for k, v in
                                 sorted(dist.items(), key=lambda kv: -kv[1]))
            lines.append("① 券商评级（近 %d 天共 %d 篇：%s）："
                     % (int(c["rating_days"]), total, dist_txt))
            cur.execute("SELECT report_date, org, rating, title "
                        "FROM sa_analyst_rating WHERE code=%s "
                        "  AND report_date >= %s AND title <> '' "
                        " ORDER BY report_date DESC LIMIT %s",
                        (code, d - timedelta(days=int(c["rating_days"])),
                         int(c["rating_top"])))
            for r in cur.fetchall():
                rd = _row(r, "report_date", 0)
                org = _row(r, "org", 1)
                rt = _row(r, "rating", 2)
                ti = _row(r, "title", 3)
                lines.append("   - %s %s「%s」%s"
                             % (rd, org or "未知机构", (ti or "（无标题）")[:40],
                                ("评级：" + str(rt)) if rt else ""))
            lines.append("   ⚠ 研报是**卖方观点**、有滞后与利益倾向，只能当"
                         "「市场共识温度计」，不得当作事实依据。")
        else:
            lines.append("① 近 %d 天无券商研报评级。" % int(c["rating_days"]))
    except Exception as exc:                     # noqa: BLE001
        lines.append("① 券商评级读取失败（跳过）：%s" % str(exc)[:60])

    # ---- ② 机构与社保持仓 ----
    try:
        cur.execute("SELECT max(period) FROM sa_holder_inst WHERE code=%s", (code,))
        period = _row(cur.fetchone(), "max")
        if period:
            lines.append("② 机构与社保持仓：%s"
                         % _lag_text(period if isinstance(period, date) else None, d))
            cur.execute(
                # ⚠️ 上一期必须**限定季末**（3/6/9/12 月）。akshare 返回的
                # 「截至日期」里混着公告日 —— 实测 000858 的期次是
                # [2026-06-30, 2026-05-11, 2026-04-29, 2026-03-31]，
                # 直接取 max(period < 当期) 会选中 05-11（公告日），
                # 交集只剩 2 家 → 明明有 2026-03-31 的完整名单却比不出来。
                "WITH cur_p AS (SELECT holder, htype, shares FROM sa_holder_inst "
                "  WHERE code=%s AND period=%s), "
                "  prev_p AS (SELECT holder, shares FROM sa_holder_inst "
                "  WHERE code=%s AND period=(SELECT max(period) FROM sa_holder_inst "
                "   WHERE code=%s AND period < %s "
                "     AND extract(month FROM period) IN (3,6,9,12))) "
                "SELECT c.htype, c.holder, c.shares, p.shares AS prev_shares "
                "  FROM cur_p c LEFT JOIN prev_p p ON p.holder = c.holder "
                " ORDER BY coalesce(c.shares,0) DESC", (code, period, code, code, period))
            rows_cur = cur.fetchall()
            changed = []
            for r in rows_cur:
                h = (_row(r, "htype", 0), _row(r, "holder", 1),
                     _row(r, "shares", 2), _row(r, "prev_shares", 3))
                if _is_nan(h[2]) or _is_nan(h[3]):
                    h = (h[0], h[1], None, None)      # NaN 当「没有数据」
                if h[2] is not None and h[3] is not None:
                    changed.append((h[0], h[1], h[2] - h[3]))
                elif h[2] is not None:
                    changed.append((h[0], h[1], None))
            n_cur = len(rows_cur)
            n_cmp = sum(1 for x in changed if x[2] is not None)
            lines.append("   - 本期机构股东 %d 家（十大股东口径），能与上期同名的 %d 家"
                         % (n_cur, n_cmp))
            top = int(c["holder_top"])
            ups = sorted([x for x in changed if x[2] is not None and x[2] > 0],
                         key=lambda x: -x[2])[:top]
            downs = sorted([x for x in changed if x[2] is not None and x[2] < 0],
                           key=lambda x: x[2])[:top]
            for lbl, arr in (("增持", ups), ("减持", downs)):
                for ht, holder, delta in arr:
                    lines.append("   - %s %s%s：%s万股"
                                 % (lbl, ht, "" if holder else "", f"{delta/10000:.0f}"))
            if not ups and not downs:
                lines.append("   - 与上一报告期相比无机构增减（或无对比期数据）。")
            cur.execute("SELECT htype, sum(shares) FROM sa_holder_inst "
                        "WHERE code=%s AND period=%s "
                        "  AND shares IS NOT NULL AND shares <> 'NaN'::numeric "
                        "GROUP BY htype ORDER BY 2 DESC",
                        (code, period))
            tot = [(_row(r, "htype", 0), _row(r, "sum", 1)) for r in cur.fetchall()]
            if tot:
                lines.append("   - 各类机构合计持股（万股）："
                             + "、".join("%s %s" % (t, f"{(v or 0)/10000:.0f}")
                                         for t, v in tot))
            lines.append("   ⚠ 季报数据，滞后 1~2 个月，**反映的是过去**；"
                         "不能当作当前持仓。")
        else:
            lines.append("② 无机构/社保持仓数据。")
    except Exception as exc:                     # noqa: BLE001
        lines.append("② 机构持仓读取失败（跳过）：%s" % str(exc)[:60])

    # ---- ③ 指数成分 ----
    try:
        cur.execute("SELECT DISTINCT ON (index_code) index_code, snap_date "
                    "  FROM sa_index_cons WHERE code=%s "
                    " ORDER BY index_code, snap_date DESC", (code,))
        members = cur.fetchall()
        if members:
            names = []
            for r in members:
                ic, sd_ = _row(r, "index_code", 0), _row(r, "snap_date", 1)
                cur.execute("SELECT count(*) FROM sa_index_cons "
                            "WHERE index_code=%s AND snap_date=%s", (ic, sd_))
                n = _row(cur.fetchone(), "count")
                names.append("%s(%d只)" % (ic, n or 0))
            lines.append("③ 指数成分：本股属于 %s（最新快照 %s）"
                         % ("、".join(names), _row(members[0], "snap_date", 1)))
        else:
            lines.append("③ 本股不在任何被跟踪的指数里。")
        cur.execute("SELECT index_code, direction, new_date FROM "
                    "  sa_index_cons_change WHERE code=%s "
                    " ORDER BY new_date DESC LIMIT 3", (code,))
        chg = cur.fetchall()
        if chg:
            txt = []
            for r in chg:
                ic = _row(r, "index_code", 0)
                dr = _row(r, "direction", 1)
                nd = _row(r, "new_date", 2)
                txt.append("%s 于 %s 被%s" % (
                    ic, nd, ("调入" if dr == "in" else "调出")))
            lines.append("   ⚠ 近期成分变更：" + "；".join(txt))
        else:
            lines.append("   近期无成分调入/调出记录。")
    except Exception as exc:                     # noqa: BLE001
        lines.append("③ 指数成分读取失败（跳过）：%s" % str(exc)[:60])
    return "\n".join(lines)


# ---------------- 一轮采集（守护线程用） ----------------

def run_once(deps, conf: dict | None = None) -> dict:
    """一轮采集。任一子步骤失败都不阻断其它步骤（部分可用好过全挂）。"""
    c = {**DEFAULTS, **{k: v for k, v in (conf or {}).items() if v is not None}}
    out = {"ts": datetime.now().isoformat(timespec="seconds"),
           "ratings": None, "holders": None, "indices": []}
    conn = deps["get_conn"]()
    try:
        ensure_ddl(conn)
        codes = universe(conn.cursor())
        out["codes"] = len(codes)
        if not codes:
            out["reason"] = "自选与持仓都为空"
            return out
        try:
            out["ratings"] = collect_ratings(conn, codes)
        except Exception as exc:                 # noqa: BLE001
            out["ratings"] = {"error": str(exc)[:200]}
            print("[inst] 评级采集异常: %s" % str(exc)[:150], flush=True)
        if c.get("hold_season", True) and not in_hold_season():
            out["holders"] = {"skipped": "非披露季窗口"}
        else:
            try:
                out["holders"] = collect_holders(conn, codes)
            except Exception as exc:             # noqa: BLE001
                out["holders"] = {"error": str(exc)[:200]}
                print("[inst] 持仓采集异常: %s" % str(exc)[:150], flush=True)
        for idx in (c.get("indices") or []):
            try:
                out["indices"].append(snapshot_index(conn, idx))
            except Exception as exc:             # noqa: BLE001
                out["indices"].append({"index": idx, "error": str(exc)[:200]})
                print("[inst] 指数 %s 异常: %s" % (idx, str(exc)[:150]), flush=True)
    finally:
        conn.close()
    return out


__all__ = ["DEFAULTS", "DDL", "ensure_ddl", "classify_holder", "in_hold_season",
           "universe", "collect_ratings", "collect_holders", "snapshot_index",
           "build_injection", "run_once"]