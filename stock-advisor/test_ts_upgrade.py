# -*- coding: utf-8 -*-
"""2026-11-20 全系统时间戳升级：离线测试（不联网、不连库、不烧 LLM）。

覆盖四层：
1. 归一函数 `broker_cash.norm_ts`（app 交易时间 / 台账时间共用）
2. 模拟盘 `slot_ts` / `_ts_str` / `_weighted_date` 对 datetime 对象的兼容
   （迁移后 psycopg2 读出来是 datetime，逐处 [:10] 截断必须仍然成立）
3. **源码级断言**：所有「日相等/日边界」比较都改成 ::date（FakeCur 测不出
   psycopg2/PG 的类型转换层 —— AGENTS.md 已有先例，[做T]%% 同款教训）
4. 前端静态接线：fmtTs 统一入口、时刻输入控件、旧 UTC 截断模式清零

跑法：python test_ts_upgrade.py
"""
from __future__ import annotations

import re
import sys
from datetime import date, datetime
from pathlib import Path

import broker_cash as BC
import paper_trading as PT

BASE = Path(__file__).resolve().parent
N = 0


def ck(cond, label):
    global N
    N += 1
    if not cond:
        raise AssertionError(f"#{N} {label}")


# ============ 1) 归一函数 ============
ck(BC.norm_ts("2026-01-12") == "2026-01-12 00:00:00", "纯日期 → 00:00:00 全串")
ck(BC.norm_ts("2026-01-12 14:22") == "2026-01-12 14:22:00", "HH:MM 补 :00")
ck(BC.norm_ts("2026-01-12 14:22:55") == "2026-01-12 14:22:55", "全串原样")
ck(BC.norm_ts("2026-01-12T14:22:55") == "2026-01-12 14:22:55", "datetime-local 的 T 形态")
for bad in ("2026/01/12", "01-12", "2026-01-12 24:00:00", "2026-01-12 10:60:00",
            "2026-01-12 10:00:60", "abc", "2026-1-12 9:5:3"):
    try:
        BC.norm_ts(bad)
        raise AssertionError("norm_ts 应当拒绝: " + bad)
    except ValueError:
        pass
ck(True, "norm_ts 拒绝 7 种非法形态")

# ============ 2) 模拟盘纯函数 ============
ck(PT.slot_ts("2026-11-20", "10:30") == "2026-11-20 10:30:00", "slot_ts HH:MM")
ck(PT.slot_ts("2026-11-20", "10:30#2") == "2026-11-20 10:30:00", "slot_ts 做T子序号")
ck(PT.slot_ts("2026-11-20", "14:05-m") == "2026-11-20 14:05:00", "slot_ts 手动轮 -m")
ck(PT.slot_ts("2026-11-20", "") == "2026-11-20 00:00:00", "slot_ts 空 slot 落 00:00:00")
ck(PT.slot_ts("2026-11-20", "25:00") == "2026-11-20 00:00:00", "slot_ts 非法小时兜底")
ck(PT.slot_ts(None, "10:30") == "", "slot_ts 无日期返回空串（不编造）")
ck(PT.slot_ts("2026-11-20", "10:30") == PT.slot_ts("2026-11-20", "10:30"),
   "slot_ts 确定性（ON CONFLICT 幂等的前提）")

ck(PT._ts_str(datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02 03:04:05", "_ts_str datetime 带秒")
ck(PT._ts_str(date(2026, 1, 2)) == "2026-01-02", "_ts_str date 原样")
ck(PT._ts_str("2026-01-02 03:04:05") == "2026-01-02 03:04:05", "_ts_str 字符串透传")
ck(PT._ts_str(None) == "", "_ts_str None → 空串")

d = PT._weighted_date(datetime(2026, 1, 10, 9, 0, 0), 100,
                      datetime(2026, 1, 20, 10, 0, 0), 100)
ck(isinstance(d, date) and d == date(2026, 1, 15), "_weighted_date 接受 datetime（迁移后形态）")
d2 = PT._weighted_date(date(2026, 1, 10), 100, datetime(2026, 1, 20, 10, 0, 0), 300)
ck(isinstance(d2, date), "_weighted_date 混合 date/datetime")


class _Cur:
    """sellable_shares 的最小游标桩：行是 tuple，trade_date 为 datetime（迁移后）。"""

    def __init__(self, rows):
        self._rows = rows

    def execute(self, *a, **k):
        pass

    def fetchall(self):
        return self._rows


cur = _Cur([(datetime(2026, 11, 20, 9, 31, 0), "buy", 500),
            (datetime(2026, 11, 19, 10, 0, 0), "buy", 1000)])
free, held, note = PT.sellable_shares(cur, "600000", "测试股", "2026-11-20")
ck(free == 1000 and held == 1500 and "T+1" in note,
   "T+1 对 datetime 行仍成立：今日买入 500 锁定（str()[:10] 兼容）")

# ============ 3) 源码级断言：::date 修复点全都在 ============
app = (BASE / "app.py").read_text(encoding="utf-8")
pt = (BASE / "paper_trading.py").read_text(encoding="utf-8")
tt = (BASE / "t_trading.py").read_text(encoding="utf-8")
ps = (BASE / "paper_strategy.py").read_text(encoding="utf-8")
se = (BASE / "self_evolution.py").read_text(encoding="utf-8")
html = (BASE / "static" / "index.html").read_text(encoding="utf-8")

for s in ("t.trade_date::date >= %s::date", "t.trade_date::date <= %s::date",
          "trade_date::date > %s::date",            # _net_trade_flow_cny 锚点口径
          "trade_date::date > current_date - %s",   # advice_followup 窗口
          "trade_date::date::text",                 # 每日净变现曲线按日分桶
          "trade_date::date = %s::date",            # 当日反向 + 盘后决策日报
          "_migrate_ts_columns", "_TS_MIGRATIONS",
          "AT TIME ZONE 'Asia/Shanghai'",           # created_at 回填不偏 8 小时
          "CREATE TABLE {bak} AS SELECT * FROM {table}"):
    ck(s in app, "app.py 含 " + s)
ck(app.count('("sa_trades", "trade_date"') == 1
   and app.count('("sa_paper_trades", "trade_date"') == 1
   and app.count('("sa_paper_trades", "settle_date"') == 1
   and app.count('("sa_paper_cycles", "trade_date"') == 1
   and app.count('("sa_paper_strategy_exits", "exit_date"') == 1,
   "_TS_MIGRATIONS 覆盖 5 列")

ck("trade_date::date=%s::date GROUP BY side" in tt, "t_trading 做T当日实盘汇总 ::date")
ck("trade_date::date=%s::date AND side IN ('buy','sell')" in tt, "t_trading 模拟盘当日 ::date")
ck("trade_date::date=%s::date" in tt and tt.count("trade_date::date=%s::date") == 3,
   "t_trading 三处日相等全部 ::date")

ck(pt.count("trade_date::date = %s::date AND slot = %s") == 2, "两处 slot 幂等守卫都 ::date")
ck("trade_date::date = %s::date AND code = %s" in pt, "辩论上一轮查询 ::date")
ck("trade_date::date = %s::date AND side IN" in pt, "同日反向闸门 ::date")
ck("trade_date::date < %s::date" in pt, "结算队列 ::date（严格小于基准日）")
ck(pt.count('(code, stock["name"], now_ts, slot,') == 9, "9 处 INSERT 写入成交时刻 now_ts")
ck('(code, stock["name"], today, slot,' not in pt, "INSERT 不再用日粒度 today")
ck("slot_ts(cycle_date, slot)" in pt, "record_cycle 用确定性 slot_ts（保 ON CONFLICT 幂等）")
ck("trade_date::date=%s::date" in pt and " AND slot=%s AND side='sell'" in pt,
   "卖出回填子查询 ::date")

ck("interval '7 days'" in ps, "scorecard LATERAL 窗口 timestamp+interval（+7 会 SQL 报错）")
ck("PT.slot_ts(today" in ps, "影子卖出 exit_date 用确定性 slot_ts")
ck("trade_date::date < %s::date" in se, "self_evolution 到期扫描 ::date")

# DDL 口径
ck("trade_date  TIMESTAMP NOT NULL," in app, "sa_trades DDL timestamp")
ck("trade_date    TIMESTAMP NOT NULL," in app, "sa_paper_trades DDL timestamp")
ck("trade_date  TIMESTAMP NOT NULL," in app, "sa_paper_cycles DDL timestamp")
ck("exit_date    TIMESTAMP   NOT NULL," in app, "sa_paper_strategy_exits DDL timestamp")
ck("settle_date   TIMESTAMP," in app, "sa_paper_trades.settle_date DDL timestamp")
# 基准日/日频数据保持 DATE（口径总表里「不改」的部分）
ck("asof_date" in (BASE / "account_anchors.py").read_text(encoding="utf-8"),
   "锚点基准日保持日粒度")

# ============ 4) 前端静态接线 ============
ck("function fmtTs(" in html, "fmtTs 统一入口存在")
ck(html.count("fmtTs(") >= 40, f"显示点已收编（实际 {html.count('fmtTs(')} 处 fmtTs 调用）")
ck('id="t-time"' in html, "实盘记一笔带时刻输入")
ck("{ key: 'time', label: '时刻', type: 'time', step: '1'," in html,
   "做T记录 + 银证转账表单带时刻输入")
# 旧 UTC 截断模式清零（时区 8 小时坑）
for gone in ("n.publish_time || n.fetched_at || '').replace",
             "(d.pub_ts || '').replace",
             "last_run.replace('T', ' ').slice",
             "created_at || '').slice(5, 16)",
             "created_at).slice(5, 16).replace",
             "fmtLocalTs(i.ts)", "fmtLocalTs(d.updated_at)", "fmtLocalTs(st.started_at)"):
    ck(gone not in html, "旧截断模式已清零: " + gone[:44])
# fmtTs 对带时区串走 Date 换算（UTC 坑修复）
ck("/(?:Z|z|[+-]\\d{2}:?\\d{2})$/.test(raw)" in html, "fmtTs 识别时区尾巴走 Date 换算")

print(f"test_ts_upgrade: {N}/{N} 全过")
