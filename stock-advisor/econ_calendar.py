"""财经日历：固定规则宏观/交割事件自动生成 + 手动事件 + 盘前微信提醒。

为什么用规则生成而不是接口：东财/金十的经济日历接口未验证且随时可能变，
而这类事件里最影响盘面的几个本身就是日历规则，可以纯本地算——

| 事件 | 规则 | 影响 |
|---|---|---|
| 美国非农就业 | 每月第一个周五（北京 20:30 夏/21:30 冬） | 当晚美股剧烈波动，隔天 A/港情绪 |
| 四巫日 Quad Witching | 季月（3/6/9/12）第三个周五 | 美股期货期权到期 + 富时罗素/标普季调生效，被动资金调仓砸盘/拉尾盘高发 |
| 股指期货交割日 | 每月第三个周五（非季月单列） | A股期现收敛，尾盘易波动 |
| ETF期权到期 | 每月第四个周三 | 标的（50/300/500ETF 等）临近行权价磁吸/甩离 |
| LPR 报价 | 每月 20 日（遇周末顺延下一工作日）9:15 | 利率敏感板块（银行/地产）异动 |

FOMC 议息、美国 CPI 这类「不精确到规则」的日期没有可靠推算方式，
走 sa_calendar_events 手动/脚本添加（UI 上有入口，也可让 Claude 定时查好写库），
同样参与每日提醒。

提醒策略（_calendar_loop，交易日早 8:47 盘前）：
- 今天/明天发生的事件合并一条推微信
- level=high（非农/四巫）额外提前 3 天预告一次
- 提醒键记在 data/calendar_state.json，同键只推一次
"""

import json
from datetime import date, datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "data" / "calendar_state.json"

EVENT_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS sa_calendar_events (
    id          BIGSERIAL PRIMARY KEY,
    event_date  DATE NOT NULL,
    time_hint   VARCHAR(16) NOT NULL DEFAULT '',
    title       VARCHAR(128) NOT NULL,
    note        VARCHAR(255) NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sa_calendar_date ON sa_calendar_events (event_date);
"""


def ensure_tables(conn):
    with conn.cursor() as cur:
        for stmt in [s.strip() for s in EVENT_TABLE_DDL.split(";") if s.strip()]:
            cur.execute(stmt)


# ---------------- 日期规则工具 ----------------

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """当月第 n 个星期 weekday（0=周一 … 4=周五 5=周六 6=周日）。"""
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7, weeks=n - 1)
    return d


def _next_weekday(d: date) -> date:
    while d.weekday() >= 5:  # 周六日顺延
        d += timedelta(days=1)
    return d


# ---------------- 自动生成事件 ----------------

AUTO_EVENT_TYPES = {
    "nfp":   {"title": "美国非农就业数据", "market": "us", "level": "high"},
    "quad":  {"title": "四巫日：美股期货期权到期 + 富时罗素/标普季调生效", "market": "us", "level": "high"},
    "future": {"title": "股指期货交割日（A股期现收敛，尾盘易波动）", "market": "cn", "level": "mid"},
    "option": {"title": "ETF期权到期日（第四个周五，行权价磁吸效应）", "market": "cn", "level": "low"},
    "lpr":   {"title": "LPR贷款市场报价利率公布", "market": "cn", "level": "mid"},
}


def generate_events(from_date: date | None = None, months: int = 3) -> list[dict]:
    """生成 from_date 起 months 个月内的规则事件（按日期升序）。"""
    today = from_date or date.today()
    y, m = today.year, today.month
    events: list[dict] = []

    def add(d: date, code: str, time_hint: str, note: str = ""):
        meta = AUTO_EVENT_TYPES[code]
        events.append({"date": d.isoformat(), "code": code,
                       "title": meta["title"], "time": time_hint,
                       "market": meta["market"], "level": meta["level"],
                       "note": note, "source": "auto"})

    for _ in range(months + 1):
        # 非农：第一个周五（数据北京时间周六凌晨发布，周五晚行情先抢跑）
        nfp = _nth_weekday(y, m, 4, 1)
        add(nfp, "nfp", "北京周六凌晨公布",
            "当晚美股剧烈波动，关注对下周一 A/港情绪传导")
        # 交割：每月第三个周五；季月升级为四巫日
        third_fri = _nth_weekday(y, m, 4, 3)
        if m in (3, 6, 9, 12):
            add(third_fri, "quad", "全天（美收盘为调仓生效点）",
                "被动基金集中换仓，尾盘波动放大；A股同期指期货交割")
        else:
            add(third_fri, "future", "尾盘")
        # ETF期权：第四个周五（2024-12 起由周三改到周五）
        add(_nth_weekday(y, m, 4, 4), "option", "收盘")
        # LPR：20 日遇周末顺延，9:15
        lpr = _next_weekday(date(y, m, 20))
        add(lpr, "lpr", "09:15", "1 年期/5 年期品种价，银行地产敏感")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    out = [e for e in events if e["date"] >= today.isoformat()]
    out.sort(key=lambda e: (e["date"], e["code"]))
    return out


# ---------------- 手动事件（FOMC/CPI 等非规则日期） ----------------

def load_manual(conn, from_iso: str) -> list[dict]:
    """库里的手动事件（from_iso 起）。"""
    import psycopg2.extras
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT id, event_date, time_hint, title, note "
                    "FROM sa_calendar_events WHERE event_date >= %s "
                    "ORDER BY event_date, id", (from_iso,))
        return [{"id": r["id"], "date": r["event_date"].isoformat(),
                 "code": "manual", "title": r["title"], "time": r["time_hint"],
                 "market": "cn", "level": "mid", "note": r["note"],
                 "source": "manual"} for r in cur.fetchall()]


# ---------------- 每日盘前提醒 ----------------

def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"sent": {}}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    # 只保留最近 60 天的键，防文件无限膨胀
    cutoff = (date.today() - timedelta(days=60)).isoformat()
    state["sent"] = {k: v for k, v in state.get("sent", {}).items() if k >= cutoff}
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def upcoming_events(conn, months: int = 3) -> list[dict]:
    """规则事件 + 手动事件合并视图（按日期排序），同事件日多来源都保留。"""
    auto = generate_events(months=months)
    end = (date.today() + timedelta(days=months * 31)).isoformat()
    manual = [e for e in load_manual(conn, date.today().isoformat()) if e["date"] <= end]
    return sorted(auto + manual, key=lambda e: (e["date"], e["source"], e["code"]))


def check_calendar_once(conn, notify_fn=None) -> list[dict]:
    """盘前提醒：今天/明天有事件合并推一条；high 级再提前 3 天预告一次。

    notify_fn(title, content) 注入（app 传 notifier.notify），None 时只返回
    将推内容（测试用）。返回本次触发的提醒列表；同键只推一次（state 文件记账）。
    """
    today = date.today()
    events = [e for e in upcoming_events(conn, months=1)
              if e["date"] <= (today + timedelta(days=31)).isoformat()]
    state = _load_state()
    sent = state["sent"]
    alerts: list[dict] = []

    def fmt(e: dict) -> str:
        mark = "⚡" if e["level"] == "high" else "•"
        t = f" {e['time']}" if e.get("time") else ""
        return f"{mark} {e['date']}{t} {e['title']}"

    # 今天/明天
    near = [e for e in events if e["date"] in (today.isoformat(),
                                               (today + timedelta(days=1)).isoformat())]
    key = f"near:{today.isoformat()}"
    if near and key not in sent:
        content = "今明两日财经日历：\n\n" + "\n".join(fmt(e) for e in near) + \
                  "\n\n（交割/调仓日尾盘易波动，注意仓位）"
        alerts.append({"key": key, "title": "📅 财经日历提醒", "content": content})
        sent[key] = today.isoformat()
    # high 级提前 3 天
    for e in events:
        if e["level"] != "high":
            continue
        d0 = date.fromisoformat(e["date"])
        if (d0 - today).days == 3:
            k3 = f"{e['date']}:pre3"
            if k3 not in sent:
                alerts.append({"key": k3, "title": "📅 重要事件预告",
                               "content": f"3 天后（{e['date']}）：{e['title']}"
                                          + (f"\n{e['note']}" if e.get("note") else "")})
                sent[k3] = today.isoformat()
    _save_state(state)
    if notify_fn:
        for a in alerts:
            try:
                notify_fn(a["title"], a["content"])
            except Exception as exc:
                print(f"[calendar] 通知失败: {exc}", flush=True)
    return alerts
