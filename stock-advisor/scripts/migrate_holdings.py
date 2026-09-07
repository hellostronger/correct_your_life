"""一次性迁移：把旧 sa_holdings 表（一股一行）转成 sa_trades 买入流水。

用法（手动执行一次）：
    python scripts/migrate_holdings.py           # 预览，不写库
    python scripts/migrate_holdings.py --apply   # 真正写入

迁移后 sa_holdings 不再使用（持仓全部由 sa_trades 推导），表保留不删。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import get_conn

APPLY = "--apply" in sys.argv

with get_conn() as conn, conn.cursor() as cur:
    cur.execute("SELECT code, name, shares, cost, COALESCE(buy_date, CURRENT_DATE) "
                "FROM sa_holdings ORDER BY code")
    rows = cur.fetchall()

if not rows:
    print("sa_holdings 无数据，无需迁移")
    sys.exit(0)

print(f"待迁移 {len(rows)} 条持仓 -> sa_trades 买入流水：")
for code, name, shares, cost, buy_date in rows:
    print(f"  {code} {name}: {shares} 股 @ {cost}（{buy_date}）")

# 已有流水的股票跳过，避免重复导入
cur_codes = set()
with get_conn() as conn, conn.cursor() as cur:
    cur.execute("SELECT DISTINCT code FROM sa_trades")
    cur_codes = {r[0] for r in cur.fetchall()}

todo = [r for r in rows if r[0] not in cur_codes]
skip = [r for r in rows if r[0] in cur_codes]
for code, *_ in skip:
    print(f"  跳过 {code}：sa_trades 已有该股流水")

if not todo:
    print("没有需要迁移的记录")
    sys.exit(0)

if not APPLY:
    print("\n预览模式（加 --apply 真正写入）")
    sys.exit(0)

with get_conn() as conn, conn.cursor() as cur:
    for code, name, shares, cost, buy_date in todo:
        cur.execute(
            "INSERT INTO sa_trades (code, trade_date, side, shares, price, note) "
            "VALUES (%s,%s,'buy',%s,%s,%s)",
            (code, buy_date, shares, cost, f"迁移自旧持仓（{name}）"))
print(f"\n已写入 {len(todo)} 条买入流水 ✔")
