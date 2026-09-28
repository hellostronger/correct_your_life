# -*- coding: utf-8 -*-
"""一次性脚本：给历史模拟成交补算交易手续费，并同步修正现金。

背景
----
2026-09-28 之前，模拟盘成交**不扣手续费**（买入只减「成交额」，卖出只加
「成交额」）。当天给 `_execute_decision` 接上了按真实市场规则算费
（A股佣金+印花税+过户费、ETF 免印花税、港股双向印花税等），但**历史行补不回来**，
于是历史胜率/收益率系统性偏高，且没有改历史行的手段。

本脚本按每行的 (code, name, side, value) 重算费用：
  1. 备份受影响的行到 reports/paper_fee_backfill_<ts>.json
  2. UPDATE sa_paper_trades 的 fee_total / fee_detail
  3. 把费用总额从 sa_paper_account.cash 里扣掉
这样 cash 与流水重新自洽：cash == 初始 - Σ(买入+费) + Σ(卖出-费)。

安全性
------
- **默认 dry-run**，只打印将要发生什么，不写库。要真执行加 --apply
- 幂等：只处理 fee_total = 0 且 shares > 0 的行，重复跑不会重复扣钱
- 全程一个事务：中途报错整体回滚
- 备份文件可用来手工回滚（见文末的 --rollback 用法）

用法
----
    python scripts/backfill_paper_fees.py                # 预览
    python scripts/backfill_paper_fees.py --apply        # 执行
    python scripts/scripts/backfill_paper_fees.py --rollback reports/xxx.json
"""
import io
import json
import os
import sys
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import yaml
import psycopg2
import paper_trading as PT

CST = timezone(timedelta(hours=8))
BACKUP_DIR = BASE / "reports"


# ---------------- DB 配置（复刻 app._load_db_conf，不依赖 dotenv） ----------------

def load_db_conf() -> dict:
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


def load_fee_conf() -> dict:
    try:
        raw = (BASE / "config.yaml").read_text(encoding="utf-8")
        return (yaml.safe_load(raw) or {}).get("paper", {}).get("fees") or {}
    except Exception as exc:
        print(f"  ! 读 config.yaml 失败，用内置默认费率：{exc}")
        return {}


# ---------------- 主流程 ----------------

def collect_targets(cur) -> list[dict]:
    """待补算的行：只有真正成交过的才该有费用。

    排除：
      status='skipped'  —— 没成交，不该收费（买入是「钱不够」，卖出是「被 T+1 拦」）
      shares = 0        —— hold 行 / skipped 行
      fee_total > 0     —— 已经算过了（幂等）
    """
    cur.execute("""
        SELECT id, trade_date, slot, code, name, side, shares, price, value
        FROM sa_paper_trades
        WHERE side IN ('buy','sell')
          AND status <> 'skipped'
          AND shares > 0
          AND COALESCE(fee_total, 0) = 0
        ORDER BY id""")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def plan(rows: list[dict], fee_conf: dict) -> list[dict]:
    """算出每行应收的费用。不改库。"""
    out = []
    for r in rows:
        value = float(r["value"] or 0) or (int(r["shares"]) * float(r["price"] or 0))
        fee = PT.trade_fees(r["code"], r["name"] or "", r["side"], value, fee_conf)
        out.append({**r, "value_calc": round(value, 4), "fee": fee})
    return out


def print_plan(planned: list[dict]) -> dict:
    by_kind: dict = {}
    for p in planned:
        key = ("港股" if PT.market_of(p["code"]) == "hk"
               else ("场内基金" if PT.is_fund(p["code"], p["name"] or "") else "A股股票"))
        by_kind.setdefault(key, []).append(p)

    total_fee = 0.0
    print("=" * 100)
    print("将被补算的成交行（共 %d 笔）" % len(planned))
    print("=" * 100)
    for kind in ("A股股票", "场内基金", "港股"):
        items = by_kind.get(kind) or []
        if not items:
            continue
        sub = sum(i["fee"]["total"] for i in items)
        print("\n【%s】%d 笔，费用合计 %.2f 元" % (kind, len(items), sub))
        print("  %-6s %-11s %-9s %-6s %-10s %10s %8s %8s %8s %9s"
              % ("id", "日期", "slot", "方向", "代码", "名称", "成交额", "佣金",
                 "印花税", "合计"))
        for i in items:
            f = i["fee"]
            print("  %-6s %-11s %-9s %-6s %-10s %10.2f %8.2f %8.2f %8.2f %9.2f"
                  % (i["id"], str(i["trade_date"])[:10], i["slot"] or "''",
                     i["side"], "%s %s" % (i["code"], (i["name"] or "")[:8]),
                     i["value_calc"], f["commission"], f["stamp_duty"],
                     f["transfer"] + f["levy"], f["total"]))
        total_fee += sub

    print("\n" + "=" * 100)
    print("费用总计 %.2f 元" % total_fee)
    if planned:
        vals = [p["value_calc"] for p in planned]
        print("对应成交额合计 %.2f 元，综合费率 %.4f%%"
              % (sum(vals), total_fee / sum(vals) * 100 if sum(vals) else 0))
    return {"total_fee": round(total_fee, 2), "count": len(planned)}


def do_apply(conn, cur, planned: list[dict], fee_total: float) -> dict:
    """写库 + 扣现金。整个过程在一个事务里。"""
    ts = datetime.now(CST).strftime("%Y%m%d-%H%M%S")
    BACKUP_DIR.mkdir(exist_ok=True)
    backup_path = BACKUP_DIR / ("paper_fee_backfill_%s.json" % ts)

    # 1) 备份（回滚用）
    backup = {
        "generated_at": datetime.now(CST).isoformat(),
        "rows": [{"id": p["id"], "fee_total": 0, "fee_detail": {},
                  "cash_before": None} for p in planned],
    }
    cur.execute("SELECT cash FROM sa_paper_account WHERE id = 1")
    row = cur.fetchone()
    cash_before = float(row[0]) if row else 0.0
    for b in backup["rows"]:
        b["cash_before"] = cash_before
    io.open(backup_path, "w", encoding="utf-8").write(
        json.dumps(backup, ensure_ascii=False, indent=2))
    print("  备份已写：%s" % backup_path)

    # 2) 逐行更新费用
    for p in planned:
        cur.execute(
            "UPDATE sa_paper_trades SET fee_total = %s, fee_detail = %s WHERE id = %s",
            (p["fee"]["total"], json.dumps(p["fee"], ensure_ascii=False), p["id"]))

    # 3) 现金扣掉费用总额（买入时已多付、卖出时少收，净额都是减少现金）
    cur.execute("UPDATE sa_paper_account SET cash = cash - %s, updated_at = now() "
                "WHERE id = 1", (fee_total,))
    conn.commit()
    cur.execute("SELECT cash FROM sa_paper_account WHERE id = 1")
    cash_after = float(cur.fetchone()[0])
    print("  已更新 %d 行费用；现金 %.2f -> %.2f（扣 %.2f）"
          % (len(planned), cash_before, cash_after, fee_total))
    return {"backup": str(backup_path), "cash_before": cash_before,
            "cash_after": cash_after}


def verify(cur) -> bool:
    """执行后自检：cash 必须等于流水推算值。"""
    cur.execute("SELECT initial_cash, cash FROM sa_paper_account WHERE id = 1")
    r = cur.fetchone()          # 只能取一次：第二次 fetchone() 是 None
    initial, cash = float(r[0]), float(r[1])
    cur.execute("""SELECT
          COALESCE(SUM(CASE WHEN side='buy'  THEN shares*price + fee_total END),0),
          COALESCE(SUM(CASE WHEN side='sell' THEN shares*price - fee_total END),0)
        FROM sa_paper_trades
        WHERE status <> 'skipped' AND side IN ('buy','sell')""")
    r = cur.fetchone()
    buy_sum, sell_sum = float(r[0]), float(r[1])
    calc = round(initial - buy_sum + sell_sum, 2)
    drift = round(cash - calc, 2)
    print("  自检：初始 %.2f  Σ买入(含费) %.2f  Σ卖出(净) %.2f" % (initial, buy_sum, sell_sum))
    print("        推算 cash %.2f  账面 cash %.2f  差额 %.2f  %s"
          % (calc, cash, drift, "✓ 一致" if abs(drift) < 0.01 else "✗ 仍不一致"))
    cur.execute("""SELECT count(*), COALESCE(sum(fee_total),0) FROM sa_paper_trades
                   WHERE status <> 'skipped' AND side IN ('buy','sell') AND shares > 0""")
    n, s = cur.fetchone()
    print("        已计费成交行 %d 笔，费用合计 %.2f" % (n, float(s)))
    return abs(drift) < 0.01


def do_rollback(backup_file: str) -> None:
    """按备份把 fee 清零、现金还原。"""
    data = json.loads(Path(backup_file).read_text(encoding="utf-8"))
    rows = data["rows"]
    if not rows:
        print("备份里没有行，无需回滚")
        return
    conn = psycopg2.connect(connect_timeout=45, **load_db_conf())
    with conn:
        cur = conn.cursor()
        for r in rows:
            cur.execute("UPDATE sa_paper_trades SET fee_total = 0, fee_detail = '{}' "
                        "WHERE id = %s", (r["id"],))
        cash_before = rows[0].get("cash_before")
        if cash_before is not None:
            cur.execute("UPDATE sa_paper_account SET cash = %s, updated_at = now() "
                        "WHERE id = 1", (cash_before,))
    print("  已回滚 %d 行，现金还原为 %s" % (len(rows), cash_before))
    conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="给历史模拟成交补算交易手续费")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只预览）")
    ap.add_argument("--rollback", metavar="FILE", help="按备份文件回滚")
    args = ap.parse_args()

    if args.rollback:
        do_rollback(args.rollback)
        return 0

    fee_conf = load_fee_conf()
    print("费率来源：config.yaml paper.fees" if fee_conf else "费率来源：paper_trading.DEFAULT_FEES")
    if fee_conf:
        print("  佣金 %s（最低 %s）  印花税 %s  过户费 %s  港股印花税 %s"
              % (fee_conf.get("a_commission_rate"), fee_conf.get("a_commission_min"),
                 fee_conf.get("a_stamp_duty"), fee_conf.get("transfer_fee"),
                 fee_conf.get("hk_stamp_duty")))

    conn = psycopg2.connect(connect_timeout=45, **load_db_conf())
    try:
        with conn:
            cur = conn.cursor()
            rows = collect_targets(cur)
            if not rows:
                print("\n没有需要补算的行（都已计过费，或没有真实成交）。")
                return 0
            planned = plan(rows, fee_conf)
            info = print_plan(planned)

            if not args.apply:
                print("\n" + "=" * 100)
                print("这是预览，未写库。确认无误后执行：")
                print("    python scripts/backfill_paper_fees.py --apply")
                print("=" * 100)
                return 0

            print("\n" + "=" * 100)
            print("开始写入…")
            res = do_apply(conn, cur, planned, info["total_fee"])
            ok = verify(cur)
            print("\n完成。备份：%s" % res["backup"])
            if not ok:
                print("!! 自检未通过，请检查（可用 --rollback %s 回滚）" % res["backup"])
                return 1
            print("提示：持仓成本与 pnl_pct 会随之变化（成本已含费用），这是正确的。")
            return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
