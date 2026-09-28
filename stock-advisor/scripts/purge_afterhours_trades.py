# -*- coding: utf-8 -*-
"""一次性脚本：清除「收市后成交」的历史行，并把现金重算回自洽。

背景
----
2026-09-28 修好之前，模拟盘有两处能绕过交易时段：
  1) 兜底补跑只判 `>= decide_time(15:35)`，没有上界 → 17:24 / 17:48 / 18:03
     这几轮在**两市都收市后**照样跑，还照常成交
  2) 行情接口收市后仍返回当日收盘价，且不带「已收市」标记，交易代码直接
     拿那个价格成交 —— 真实盘接不到这种单
于是流水里混进了若干笔「用收盘价成交」的成交，胜率/收益率统计失真。

本脚本按**每笔自己的交易时段**判定，而不是按 slot 整轮删 —— 因为同一轮里
可能既有合法的（港股 16:00 前）又有非法的（A 股 15:00 后）。实测 15:35 那轮
就属于这种情况：两笔港股合法，1 笔 A 股（600186）非法。

安全设计
--------
- **默认 dry-run**，只打印将要删什么、为什么删；--apply 才写库
- 逐笔给出判定依据（哪个市场、当时开没开市、现在还开吗），人工可核对
- 幂等：只删「当前代码判定为收市后成交」的行；跑第二遍会是空的
- 全程单事务，中途报错整体回滚
- 备份全部被删行到 reports/paper_afterhours_purge_<ts>.json，可 --rollback
- 只动 status<>'skipped' 的成交行；hold 行（0 股）连带删掉没有账务影响，
  但为保持「轮次台账」一致，这些轮次的 hold 行也一并清理
- 默认只清理 today 之前的、不晚于 --before 时刻的轮次，避免误伤未来数据

用法
----
    python scripts/purge_afterhours_trades.py                 # 预览
    python scripts/purge_afterhours_trades.py --apply         # 执行
    python scripts/purge_afterhours_trades.py --before 16:00   # 连 15:35 那轮一起查
    python scripts/purge_afterhours_trades.py --rollback <备份.json>
"""
import io
import json
import sys
import argparse
from datetime import date as _date, datetime, timedelta, timezone
from decimal import Decimal as _Decimal
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import psycopg2
import paper_trading as PT

CST = timezone(timedelta(hours=8))
BACKUP_DIR = BASE / "reports"


def load_db_conf() -> dict:
    import os
    env_file = BASE.parent / ".env"
    conf = {
        "host": os.environ.get("DB_HOST", "127.0.0.1"),
        "port": int(os.environ.get("DB_PORT", "5432")),
        "user": os.environ.get("DB_USERNAME", "postgres"),
        "password": os.environ.get("DB_PASSWORD", ""),
        "dbname": os.environ.get("DB_DATABASE", "postgres"),
    }
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" not in line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            mapping = {"DB_HOST": "host", "DB_PORT": "port", "DB_USERNAME": "user",
                       "DB_PASSWORD": "password", "DB_DATABASE": "dbname"}
            if key in mapping:
                conf[mapping[key]] = int(value) if key == "DB_PORT" else value
    return conf


def slot_time(slot: str, trade_date) -> datetime | None:
    """把 slot 标签（'17:24' / '17:24-m'）还原成那一刻的 datetime。"""
    s = (slot or "").strip()
    if not s:
        return None
    hhmm = s.split("-")[0]
    try:
        h, m = [int(x) for x in hhmm.split(":")]
    except (ValueError, AttributeError):
        return None
    d = trade_date if isinstance(trade_date, datetime) else datetime.combine(
        trade_date, datetime.min.time())
    return d.replace(hour=h, minute=m, second=0, microsecond=0)


def judge(row: dict, before_hhmm: str | None) -> tuple[bool, str]:
    """这一笔该不该删。返回 (该删, 理由)。

    「收市后」严格定义为：**晚于该市场当日最后一段交易时段的结束时刻**
    （A 股 15:00、港股 16:00）。这样：
      - 午休（11:30-13:00）**不算**收市后 —— 那是另一个问题（午休行情不更新，
        但价格仍是当日真实的午盘价，不算「用收盘价成交」），不在本次范围
      - 15:35 那轮里港股（16:00 前）算合法、A 股（15:00 后）算收市后，天然分开
    """
    t = slot_time(row["slot"], row["trade_date"])
    if t is None:
        return False, "slot 无法解析成时刻，跳过（不冒险）"
    mk = PT.market_of(row["code"])
    name = {"sh": "A股", "sz": "A股", "hk": "港股"}.get(mk, mk)
    sessions = PT.TRADING_SESSIONS.get(mk) or ()
    last_end = max(h * 60 + m for (_h, _m, h, m) in sessions) if sessions else 15 * 60
    close_hhmm = "%02d:%02d" % divmod(last_end, 60)
    cur_min = t.hour * 60 + t.minute
    if cur_min < last_end:
        if PT.market_session_state(row["code"], t)["open"]:
            return False, f"{name} {t:%H:%M} 在交易时段内 → 合法成交，保留"
        return False, (f"{name} {t:%H:%M} 是午休/非交易时段但**未收市**"
                       f"（{close_hhmm} 前），价格仍是当日真实价 → 本次不动")
    if before_hhmm:
        bh, bm = [int(x) for x in before_hhmm.split(":")]
        if cur_min > bh * 60 + bm:
            return False, f"{t:%H:%M} 晚于 --before {before_hhmm}，不在本次范围"
    return True, (f"{name} {t:%H:%M} 已过 {close_hhmm} 收市"
                  f" → 用当日收盘价成交，真实盘做不到，剔除")


def collect(cur, before_hhmm, only_slots=None) -> list[dict]:
    # 只看**真实成交**的行：hold 行 shares=0、不动账，单独按轮次清理
    sql = ("SELECT id, trade_date, slot, code, name, side, shares, price, value, "
           "fee_total, status, decision_raw, reasoning, report, confidence, "
           "stop_loss_pct, created_at, auto_closed "
           "FROM sa_paper_trades WHERE status <> 'skipped' "
           "AND side IN ('buy','sell') ORDER BY id")
    args = ()
    if only_slots:
        sql += " AND slot = ANY(%s)"
        args = (list(only_slots),)
    cur.execute(sql, args)
    cols = [d[0] for d in cur.description]
    out = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        drop, why = judge(d, before_hhmm)
        if drop:
            out.append({**d, "_why": why})
    return out


def purge_hold_rows(cur, slots) -> int:
    """同轮次的 hold 行（shares=0，无账务影响）一并清掉，保持台账一致。"""
    if not slots:
        return 0
    cur.execute("SELECT count(*) FROM sa_paper_trades WHERE side='hold' "
                "AND status <> 'skipped' AND slot = ANY(%s)", (list(slots),))
    n = cur.fetchone()[0]
    cur.execute("DELETE FROM sa_paper_trades WHERE side='hold' "
                "AND status <> 'skipped' AND slot = ANY(%s)", (list(slots),))
    return n


def show(planned: list[dict]) -> float:
    print("=" * 104)
    print("将被剔除的「收市后成交」共 %d 笔" % len(planned))
    print("=" * 104)
    print("  %-5s %-11s %-9s %-8s %-14s %-5s %8s %9s %8s  %s"
          % ("id", "日期", "slot", "代码", "名称", "方向", "股数", "成交额", "费用", "判定依据"))
    cash_delta = 0.0
    for p in planned:
        v = float(p["value"] or 0)
        f = float(p["fee_total"] or 0)
        cash_delta += (-(v + f) if p["side"] == "buy" else (v - f))
        print("  %-5s %-11s %-9s %-8s %-14s %-5s %8d %9.2f %8.2f  %s"
              % (p["id"], str(p["trade_date"])[:10], p["slot"], p["code"],
                 (p["name"] or "")[:12], p["side"], p["shares"], v, f, p["_why"]))
    print("-" * 104)
    print("  现金净影响 = %+.2f" % cash_delta)
    return cash_delta


def verify(cur) -> bool:
    cur.execute("SELECT initial_cash, cash FROM sa_paper_account WHERE id = 1")
    r = cur.fetchone()
    initial, cash = float(r[0]), float(r[1])
    cur.execute("""SELECT
          COALESCE(SUM(CASE WHEN side='buy'  THEN shares*price + fee_total END),0),
          COALESCE(SUM(CASE WHEN side='sell' THEN shares*price - fee_total END),0)
        FROM sa_paper_trades
        WHERE status <> 'skipped' AND side IN ('buy','sell')""")
    r2 = cur.fetchone()
    buy_sum, sell_sum = float(r2[0]), float(r2[1])
    calc = round(initial - buy_sum + sell_sum, 2)
    drift = round(cash - calc, 2)
    print("  初始 %.2f  Σ买入(含费) %.2f  Σ卖出(净) %.2f" % (initial, buy_sum, sell_sum))
    print("  推算 cash %.2f  账面 cash %.2f  差额 %.2f  %s"
          % (calc, cash, drift, "✓ 一致" if abs(drift) < 0.01 else "✗"))

    # 复核必须用**和 judge 一样的口径**（晚于该市场最后一段时段 = 收市后）。
    # 早先用 market_session_state 判，把午休也算成「收市后」，口径比清理范围更宽，
    # 于是清理干净了却报自检失败（2026-09-28 实测）。
    bad, lunch = [], []
    cur.execute("SELECT id, trade_date, slot, code FROM sa_paper_trades "
                "WHERE status <> 'skipped' AND side IN ('buy','sell')")
    for rid, d, slot, code in cur.fetchall():
        t = slot_time(slot, d)
        if not t:
            continue
        sess = PT.TRADING_SESSIONS.get(PT.market_of(code)) or ()
        last_end = max(h * 60 + m for (_h1, _m1, h, m) in sess) if sess else 900
        cur_min = t.hour * 60 + t.minute
        if cur_min >= last_end:
            bad.append((rid, slot, code))
        elif not PT.market_session_state(code, t)["open"]:
            lunch.append((rid, slot, code))
    print("  残留的**收市后**成交：%s" % (bad if bad else "无 ✓"))
    if lunch:
        print("  另有 %d 笔**午休时段**成交（本次范围外，见脚本说明）：%s"
              % (len(lunch), lunch))
    return abs(drift) < 0.01 and not bad


def do_apply(conn, cur, planned: list[dict], cash_delta: float) -> str:
    ts = datetime.now(CST).strftime("%Y%m%d-%H%M%S")
    BACKUP_DIR.mkdir(exist_ok=True)
    path = BACKUP_DIR / ("paper_afterhours_purge_%s.json" % ts)
    cur.execute("SELECT cash FROM sa_paper_account WHERE id = 1")
    cash_before = float(cur.fetchone()[0])
    slots = sorted({p["slot"] for p in planned})
    cur.execute("SELECT count(*) FROM sa_paper_trades WHERE side='hold' "
                "AND status <> 'skipped' AND slot = ANY(%s)", (slots,))
    hold_n = cur.fetchone()[0]
    payload = {
        "generated_at": datetime.now(CST).isoformat(),
        "cash_before": cash_before,
        "hold_slots": slots,
        "hold_rows_deleted": hold_n,
        "rows": planned,
    }

    def _jsonable(o):
        # date / datetime / Decimal 都不是 JSON 原生类型，统一转字符串。
        # （漏了 date 会在这里直接炸，且炸在写备份那一刻 —— 好处是还没删任何行）
        if isinstance(o, (datetime,)):
            return o.isoformat()
        if isinstance(o, _date):
            return o.isoformat()
        if isinstance(o, _Decimal):
            return float(o)
        if isinstance(o, (dict, list)):
            raise TypeError("嵌套容器应先逐项转换")
        return str(o)

    io.open(path, "w", encoding="utf-8").write(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_jsonable))
    print("  备份已写：%s（含 %d 笔成交行 + cash_before=%.2f）"
          % (path, len(planned), cash_before))

    ids = [p["id"] for p in planned]
    cur.execute("DELETE FROM sa_paper_trades WHERE id = ANY(%s)", (ids,))
    print("  已删除成交行 %d 行" % cur.rowcount)
    print("  已删除同轮次 hold 行 %d 行（0 股，无账务影响）" % hold_n)

    # 现金按剩余真实流水重算
    cur.execute("SELECT initial_cash FROM sa_paper_account WHERE id = 1")
    initial = float(cur.fetchone()[0])
    cur.execute("""SELECT
          COALESCE(SUM(CASE WHEN side='buy'  THEN shares*price + fee_total END),0),
          COALESCE(SUM(CASE WHEN side='sell' THEN shares*price - fee_total END),0)
        FROM sa_paper_trades
        WHERE status <> 'skipped' AND side IN ('buy','sell')""")
    r = cur.fetchone()
    cash_new = round(initial - float(r[0]) + float(r[1]), 2)
    cur.execute("UPDATE sa_paper_account SET cash = %s, updated_at = now() WHERE id = 1",
                (cash_new,))
    conn.commit()
    print("  现金 %.2f -> %.2f" % (cash_before, cash_new))
    return str(path)


def do_rollback(backup_file: str) -> None:
    data = json.loads(Path(backup_file).read_text(encoding="utf-8"))
    conn = psycopg2.connect(connect_timeout=45, **load_db_conf())
    with conn:
        cur = conn.cursor()
        cols = ["id", "trade_date", "slot", "code", "name", "side", "shares", "price",
                "value", "fee_total", "status", "decision_raw", "reasoning", "report",
                "confidence", "stop_loss_pct", "created_at", "auto_closed"]
        for r in data["rows"]:
            cur.execute(
                "INSERT INTO sa_paper_trades (%s) VALUES (%s)" % (
                    ",".join(cols), ",".join(["%s"] * len(cols))),
                tuple(r.get(c) for c in cols))
        slots = data.get("hold_slots") or []
        if slots:
            # hold 行备份里只存了被删的成交行，hold 行需要靠轮次重建，无法自动还原
            print("  提示：%d 笔成交行已还原；同轮次的 %d 行 hold 决策记录"
                  "（仅供复盘用，无账务影响）无法自动还原" % (len(data["rows"]), data.get("hold_rows_deleted", 0)))
        if data.get("cash_before") is not None:
            cur.execute("UPDATE sa_paper_account SET cash = %s, updated_at = now() "
                        "WHERE id = 1", (data["cash_before"],))
    print("  已回滚 %d 笔成交行，现金还原为 %s" % (len(data["rows"]), data.get("cash_before")))
    conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="清除模拟盘「收市后成交」的历史行")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只预览）")
    ap.add_argument("--before", metavar="HH:MM",
                    help="只处理这一刻**之前**的收市后轮次（默认不限制）")
    ap.add_argument("--slots", metavar="A,B",
                    help="只检查这几个 slot（默认检查全部）")
    ap.add_argument("--rollback", metavar="FILE", help="按备份文件回滚")
    args = ap.parse_args()

    if args.rollback:
        do_rollback(args.rollback)
        return 0

    only = [s.strip() for s in args.slots.split(",")] if args.slots else None
    before = args.before

    conn = psycopg2.connect(connect_timeout=45, **load_db_conf())
    try:
        with conn:
            cur = conn.cursor()
            planned = collect(cur, before, only)
            if not planned:
                print("没有「收市后成交」的行需要清理（干净）。")
                return 0
            cash_delta = show(planned)
            if not args.apply:
                print("\n" + "=" * 104)
                print("这是预览，未写库。确认无误后执行：")
                print("    python scripts/purge_afterhours_trades.py --apply%s"
                      % (" --before %s" % args.before if args.before else ""))
                print("=" * 104)
                return 0
            print("\n开始写入…")
            path = do_apply(conn, cur, planned, cash_delta)
            ok = verify(cur)
            print("\n完成。备份：%s" % path)
            print("提示：持仓数量与成本会随之变化（这是正确的，那几笔本就不该成交）。")
            if not ok:
                print("!! 自检未通过，可用 --rollback %s 回滚" % path)
                return 1
            return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
