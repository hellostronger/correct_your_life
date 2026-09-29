"""Stock Advisor - 本地股票跟踪分析服务

零成本方案：
- 行情：腾讯公开行情接口（免费，无需 key）
- 存储：云上 PostgreSQL（复用 D:\\correct_your_life\\.env 中的 DB_* 配置）
- 报告：reports/ 目录下的 Markdown，由 Claude 定时任务生成
- 分析：Claude 会话定时任务搜新闻 + 综合研判

启动：python app.py  （或 uvicorn app:app --port 8686）
页面：http://127.0.0.1:8686/
"""

import json
import os
import re
import threading
import time
import traceback          # 守护线程/后台线程的 except 里要打栈（2026-09-25 顺手补：原来漏 import）
from datetime import datetime, timedelta, timezone
import datetime as _dt_mod

# 需要序列化成 ISO 字符串的数据库类型（别在函数签名里用 date 当参数名，
# 那会遮蔽同名类型，isinstance 会直接 TypeError）
_DT_TYPES = (_dt_mod.datetime, _dt_mod.date)
from pathlib import Path

# 禁用 requests 对系统代理的读取：urllib.getproxies() 在 Windows 上会读到注册表
# 的 WinINET 代理（如 127.0.0.1:6478），那是给浏览器的——挂掉/半挂时脚本内全部
# 行情/东财请求跟着 ProxyError（2026-09-25 实测）。NO_PROXY=* 等效全部直连。
os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

import psycopg2
import psycopg2.extras
import requests
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import conf_util
import config_schema
import crypto_watch
import wechat_mp
import x_monitor

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"          # 兼容保留：迁移前旧 JSON 数据所在目录
REPORTS_DIR = BASE_DIR / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

ENV_FILE = BASE_DIR.parent / ".env"

# 调度默认值：config.yaml 的 schedule 段缺失/坏值时的回落（网页「⚙️ 调度设置」可改）
DEFAULT_SCHEDULE = {
    "premarket_report": {"enabled": True, "time": "08:23"},
    "postmarket_report": {"enabled": True, "time": "15:57"},
    "withdrawal": {"check_time": "15:10"},
    "paper": {"decide_time": "15:35", "settle_time": "16:10"},
    "sector": {"snapshot_time": "15:10"},
    "alerts": {"check_time": "08:30"},
    "calendar": {"start_time": "08:00", "end_time": "12:00"},
}
# 抓取周期在各模块自己的段里（news/bili/wb/sector/volume），调度页只做单键手术式改写，
# 不复制一份真源。这里的默认值仅用于 GET 回显。
DEFAULT_INTERVALS = {
    "news": {"enabled": True, "interval_minutes": 30},
    "bili": {"enabled": True, "interval_minutes": 30},
    "wb": {"enabled": True, "interval_minutes": 60},
    "sector": {"enabled": True, "interval_minutes": 5},
    "volume": {"enabled": True, "interval_minutes": 5},
}
# ⚠️ news 的周期真身是 `news.fetch_interval_minutes`（_auto_fetch_loop 和
# news_fetcher 都读它），而 `news.interval_minutes` 是历史遗留的**幽灵键**：
# 调度页一直写的是它，于是「在调度页改新闻抓取周期」从来没生效过
# （2026-09-26 核对 _auto_fetch_loop 源码时发现）。这里把读写都指到真身上，
# 幽灵键已从 config.yaml 删除。
INTERVAL_KEY = {"news": "fetch_interval_minutes"}
# news 没有独立的 enabled 开关（0 分钟即关闭），UI 上不给它画复选框
INTERVAL_NO_ENABLED = {"news"}


def _interval_key(name: str) -> str:
    return INTERVAL_KEY.get(name, "interval_minutes")


def _load_db_conf() -> dict:
    """从仓库根目录 .env 读取 DB_* 配置（不依赖第三方 dotenv）。"""
    conf = {
        "host": os.environ.get("DB_HOST", "127.0.0.1"),
        "port": int(os.environ.get("DB_PORT", "5432")),
        "user": os.environ.get("DB_USERNAME", "postgres"),
        "password": os.environ.get("DB_PASSWORD", ""),
        "dbname": os.environ.get("DB_DATABASE", "postgres"),
    }
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" not in line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            mapping = {
                "DB_HOST": "host", "DB_PORT": "port",
                "DB_USERNAME": "user", "DB_PASSWORD": "password",
                "DB_DATABASE": "dbname",
            }
            if key in mapping:
                conf[mapping[key]] = int(value) if key == "DB_PORT" else value
    return conf


DB_CONF = _load_db_conf()
# 同一张云库上还有其他业务（dify 的 144 张表），前缀 sa_ 隔离命名空间
SA_TABLES = ("sa_watchlist", "sa_holdings")

LOCK = threading.Lock()  # 序列化写操作，避免多请求并发写坏数据


def get_conn():
    """云库建连偶发超时（同实例还跑着 dify，负载波动），重试 2 次兜底。"""
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            return psycopg2.connect(connect_timeout=45, **DB_CONF)
        except psycopg2.OperationalError as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(1 + attempt)  # 1s、2s 退避
    raise last_exc


def init_db():
    """建表（幂等）。自动把本地旧 JSON 数据迁移进云库。

    云库不稳：建连慢（最长 20s+）、长会话还可能被服务端掐断。之前整块 DDL
    一条 execute 提交，单次 >100s 甚至 hang 死；改成逐条语句 + 独立短连接 +
    每条重试 2 次，单条失败不影响其余（DDL 幂等，重跑安全）。
    """
    ddl = """
    CREATE TABLE IF NOT EXISTS sa_watchlist (
        code      VARCHAR(8) PRIMARY KEY,
        name      VARCHAR(64) NOT NULL DEFAULT '',
        note      VARCHAR(255) NOT NULL DEFAULT '',
        added_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    ALTER TABLE sa_watchlist ADD COLUMN IF NOT EXISTS keywords TEXT NOT NULL DEFAULT '';
    ALTER TABLE sa_watchlist ADD COLUMN IF NOT EXISTS keywords_pos TEXT NOT NULL DEFAULT '';
    ALTER TABLE sa_watchlist ADD COLUMN IF NOT EXISTS keywords_neg TEXT NOT NULL DEFAULT '';
    CREATE TABLE IF NOT EXISTS sa_holdings (
        code      VARCHAR(8) PRIMARY KEY,
        name      VARCHAR(64) NOT NULL DEFAULT '',
        shares    INTEGER NOT NULL CHECK (shares > 0),
        cost      NUMERIC(12,4) NOT NULL CHECK (cost > 0),
        buy_date  DATE,
        added_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS sa_trades (
        id          BIGSERIAL PRIMARY KEY,
        code        VARCHAR(8) NOT NULL,
        trade_date  DATE NOT NULL,
        side        VARCHAR(4) NOT NULL CHECK (side IN ('buy', 'sell')),
        shares      INTEGER NOT NULL CHECK (shares > 0),
        price       NUMERIC(12,4) NOT NULL CHECK (price > 0),
        note        VARCHAR(255) NOT NULL DEFAULT '',
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS sa_news (
        id            BIGSERIAL PRIMARY KEY,
        code          VARCHAR(8) NOT NULL,
        title         TEXT NOT NULL,
        url           TEXT NOT NULL UNIQUE,
        source        VARCHAR(32) NOT NULL,
        media         VARCHAR(64) NOT NULL DEFAULT '',
        publish_time  TIMESTAMPTZ,
        fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    -- 关键词轮命中的新闻情绪标记：pos=利好词抓到，neg=利空词抓到，空=按股票名抓的
    ALTER TABLE sa_news ADD COLUMN IF NOT EXISTS sentiment VARCHAR(4) NOT NULL DEFAULT '';
    CREATE TABLE IF NOT EXISTS sa_news_related (
        url      TEXT NOT NULL,
        code     VARCHAR(8) NOT NULL,
        PRIMARY KEY (url, code)
    );
    CREATE TABLE IF NOT EXISTS sa_notify_wx (
        uid       VARCHAR(64) PRIMARY KEY,
        note      VARCHAR(255) NOT NULL DEFAULT '',
        bound_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS sa_wx_ilink (
        field       VARCHAR(32) PRIMARY KEY,
        value       TEXT NOT NULL DEFAULT '',
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS sa_wx_users (
        ilink_user_id  VARCHAR(128) PRIMARY KEY,
        note           VARCHAR(255) NOT NULL DEFAULT '',
        context_token  TEXT NOT NULL DEFAULT '',
        bound_at       TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    -- 止盈策略=与股票无关的通用模板（如「+10%」），由买入笔引用；
    -- 触发状态记在交易行上（每笔只触发一次）。旧版按股票建的表直接重建。
    -- kind 全集（内置常见量化退出策略，config 存放各类型专有参数）：
    --   pct        固定止盈（涨幅达 target_pct%）
    --   drawdown   回撤止盈（盈利超 target_pct% 激活，离峰值回撤 drawdown_pct% 触发）
    --   trailing   移动止盈（买入起跟踪峰值，回撤 drawdown_pct% 即触发，无激活线）
    --   stop_loss  止损（现价 <= 成本*(1-target_pct%) 触发，target_pct 存损失%）
    --   ladder     分批止盈（config.steps = [{pct, ratio}...]，到档通知逐档卖出）
    --   time_stop  时间止盈（config.hold_days 个交易日后通知复盘）
    DROP TABLE IF EXISTS sa_strategies;
    CREATE TABLE IF NOT EXISTS sa_strategies (
        id            BIGSERIAL PRIMARY KEY,
        name          VARCHAR(128) NOT NULL DEFAULT '',
        kind          VARCHAR(16) NOT NULL DEFAULT 'pct'
                      CHECK (kind IN ('pct', 'drawdown', 'trailing', 'stop_loss',
                                      'ladder', 'time_stop')),
        target_pct    NUMERIC(8,2) NOT NULL CHECK (target_pct > 0),
        drawdown_pct  NUMERIC(8,2),
        config        JSONB NOT NULL DEFAULT '{}'::jsonb,
        note          VARCHAR(255) NOT NULL DEFAULT '',
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    ALTER TABLE sa_trades ADD COLUMN IF NOT EXISTS strategy_id BIGINT;
    ALTER TABLE sa_trades ADD COLUMN IF NOT EXISTS strategy_triggered_at TIMESTAMPTZ;
    ALTER TABLE sa_trades ADD COLUMN IF NOT EXISTS strategy_peak_price NUMERIC(12,4);
    ALTER TABLE sa_trades ADD COLUMN IF NOT EXISTS strategy_ladder_step INTEGER NOT NULL DEFAULT 0;
    DROP INDEX IF EXISTS idx_sa_strategies_code;
    CREATE INDEX IF NOT EXISTS idx_sa_news_code_time ON sa_news (code, publish_time DESC);
    CREATE INDEX IF NOT EXISTS idx_sa_news_rel_code ON sa_news_related (code);
    CREATE INDEX IF NOT EXISTS idx_sa_trades_code_date ON sa_trades (code, trade_date);
    CREATE TABLE IF NOT EXISTS sa_bili_creators (
        uid       VARCHAR(32) PRIMARY KEY,
        name      VARCHAR(128) NOT NULL DEFAULT '',
        note      VARCHAR(255) NOT NULL DEFAULT '',
        added_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS sa_bili_dynamics (
        dynamic_id  VARCHAR(128) PRIMARY KEY,
        uid         VARCHAR(32) NOT NULL DEFAULT '',
        author_name VARCHAR(128) NOT NULL DEFAULT '',
        dtype       VARCHAR(64) NOT NULL DEFAULT '',
        title       TEXT NOT NULL DEFAULT '',
        text        TEXT NOT NULL DEFAULT '',
        bvid        VARCHAR(64) NOT NULL DEFAULT '',
        aid         VARCHAR(64) NOT NULL DEFAULT '',
        pub_ts      TIMESTAMPTZ,
        stats       JSONB NOT NULL DEFAULT '{}'::jsonb,
        is_read     BOOLEAN NOT NULL DEFAULT FALSE,
        fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS sa_bili_comments (
        comment_id  VARCHAR(128) PRIMARY KEY,
        dynamic_id  VARCHAR(128) NOT NULL DEFAULT '',
        content     TEXT NOT NULL DEFAULT '',
        author      VARCHAR(128) NOT NULL DEFAULT '',
        pub_ts      TIMESTAMPTZ,
        like_count  INTEGER NOT NULL DEFAULT 0,
        fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS idx_sa_bili_dyn_pub ON sa_bili_dynamics (pub_ts DESC);
    CREATE INDEX IF NOT EXISTS idx_sa_bili_dyn_uid ON sa_bili_dynamics (uid);
    CREATE INDEX IF NOT EXISTS idx_sa_bili_cmt_dyn ON sa_bili_comments (dynamic_id);
    CREATE TABLE IF NOT EXISTS sa_wb_creators (
        uid       VARCHAR(32) PRIMARY KEY,
        name      VARCHAR(128) NOT NULL DEFAULT '',
        note      VARCHAR(255) NOT NULL DEFAULT '',
        added_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS sa_wb_posts (
        note_id     VARCHAR(64) PRIMARY KEY,
        uid         VARCHAR(32) NOT NULL DEFAULT '',
        author_name VARCHAR(128) NOT NULL DEFAULT '',
        text        TEXT NOT NULL DEFAULT '',
        pub_ts      TIMESTAMPTZ,
        stats       JSONB NOT NULL DEFAULT '{}'::jsonb,
        is_read     BOOLEAN NOT NULL DEFAULT FALSE,
        fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS sa_wb_comments (
        comment_id  VARCHAR(128) PRIMARY KEY,
        note_id     VARCHAR(64) NOT NULL DEFAULT '',
        content     TEXT NOT NULL DEFAULT '',
        author      VARCHAR(128) NOT NULL DEFAULT '',
        pub_ts      TIMESTAMPTZ,
        like_count  INTEGER NOT NULL DEFAULT 0,
        fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS idx_sa_wb_post_pub ON sa_wb_posts (pub_ts DESC);
    CREATE INDEX IF NOT EXISTS idx_sa_wb_post_uid ON sa_wb_posts (uid);
    CREATE INDEX IF NOT EXISTS idx_sa_wb_cmt_post ON sa_wb_comments (note_id);
    -- 提款计划：某日期前要从账户提取多少现金；提款流水累计记进度。
    -- notified 记录已推送过的里程碑键（ready/d10/d5/d1/deadline），防重复轰炸
    CREATE TABLE IF NOT EXISTS sa_withdrawal_plans (
        id            BIGSERIAL PRIMARY KEY,
        target_date   DATE NOT NULL,
        target_amount NUMERIC(14,2) NOT NULL CHECK (target_amount > 0),
        note          VARCHAR(255) NOT NULL DEFAULT '',
        status        VARCHAR(8) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'done')),
        notified      JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS sa_withdrawals (
        id          BIGSERIAL PRIMARY KEY,
        plan_id     BIGINT NOT NULL REFERENCES sa_withdrawal_plans(id) ON DELETE CASCADE,
        wd_date     DATE NOT NULL,
        amount      NUMERIC(14,2) NOT NULL CHECK (amount > 0),
        note        VARCHAR(255) NOT NULL DEFAULT '',
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS idx_sa_withdrawals_plan ON sa_withdrawals (plan_id);
    -- 整仓级策略关联：直接给持仓（代码粒度）挂策略，不必逐笔买入引用
    CREATE TABLE IF NOT EXISTS sa_position_strategies (
        id            BIGSERIAL PRIMARY KEY,
        code          VARCHAR(8) NOT NULL,
        strategy_id   BIGINT NOT NULL,
        triggered_at  TIMESTAMPTZ,
        peak_price    NUMERIC(12,4),
        ladder_step   INTEGER NOT NULL DEFAULT 0,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (code, strategy_id)
    );
    -- 财经日历手动事件（FOMC/CPI 等非规则日期；规则事件由 econ_calendar 本地生成）
    CREATE TABLE IF NOT EXISTS sa_calendar_events (
        id          BIGSERIAL PRIMARY KEY,
        event_date  DATE NOT NULL,
        time_hint   VARCHAR(16) NOT NULL DEFAULT '',
        title       VARCHAR(128) NOT NULL,
        note        VARCHAR(255) NOT NULL DEFAULT '',
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS idx_sa_calendar_date ON sa_calendar_events (event_date);
    -- LLM 提款建议：一个计划多条历史（每次生成插一行，页面取最新）
    CREATE TABLE IF NOT EXISTS sa_plan_advice (
        id         BIGSERIAL PRIMARY KEY,
        plan_id    BIGINT NOT NULL REFERENCES sa_withdrawal_plans(id) ON DELETE CASCADE,
        content    TEXT NOT NULL,
        model      VARCHAR(64) NOT NULL DEFAULT '',
        auto       BOOLEAN NOT NULL DEFAULT FALSE,   -- 自动（盘后）/手动生成
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS idx_sa_plan_advice_plan ON sa_plan_advice (plan_id, created_at DESC);
    -- 模拟交易（Paper Trading）：LLM 决策 → 收盘价成交 → 5 交易日结算 → 反思沉淀（paper_trading.py）
    -- 账户单行表（id 恒为 1；reset 即清空三张业务表后重插）
    CREATE TABLE IF NOT EXISTS sa_paper_account (
        id             INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
        initial_cash   NUMERIC(14,2) NOT NULL,
        cash           NUMERIC(14,2) NOT NULL,
        total_value    NUMERIC(14,2),
        updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    -- 决策日志 = 成交流水（每行既是成交也是可复盘决策；持仓由流水推导，不建持仓表）
    CREATE TABLE IF NOT EXISTS sa_paper_trades (
        id            BIGSERIAL PRIMARY KEY,
        code          VARCHAR(8) NOT NULL,
        name          VARCHAR(64) NOT NULL DEFAULT '',
        trade_date    DATE NOT NULL,
        side          VARCHAR(8) NOT NULL CHECK (side IN ('buy','sell','hold')),
        shares        INTEGER NOT NULL DEFAULT 0,
        price         NUMERIC(12,4),
        value         NUMERIC(14,2),
        confidence    SMALLINT,
        stop_loss_pct NUMERIC(6,2),
        reasoning     TEXT NOT NULL DEFAULT '',
        report        TEXT NOT NULL DEFAULT '',
        decision_raw  JSONB NOT NULL DEFAULT '{}'::jsonb,
        status        VARCHAR(10) NOT NULL DEFAULT 'open'
                      CHECK (status IN ('open','resolved','skipped')),
        auto_closed   BOOLEAN NOT NULL DEFAULT FALSE,
        settle_date   DATE,
        settle_price  NUMERIC(12,4),
        raw_return    NUMERIC(10,6),
        alpha_return  NUMERIC(10,6),
        benchmark     VARCHAR(12),
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (trade_date, code, side)
    );
    CREATE INDEX IF NOT EXISTS idx_sa_paper_trades_status ON sa_paper_trades (status);
    -- 经验库（Reflector 产物；code='' 为全局蒸馏规则；resolved_at 是 point-in-time 注入键）
    CREATE TABLE IF NOT EXISTS sa_paper_reflections (
        id              BIGSERIAL PRIMARY KEY,
        trade_id        BIGINT NOT NULL UNIQUE,
        code            VARCHAR(8) NOT NULL DEFAULT '',
        action          VARCHAR(8) NOT NULL,
        decision_digest TEXT NOT NULL DEFAULT '',
        raw_return      NUMERIC(10,6) NOT NULL DEFAULT 0,
        alpha_return    NUMERIC(10,6) NOT NULL DEFAULT 0,
        holding_days    SMALLINT NOT NULL DEFAULT 5,
        benchmark       VARCHAR(12) NOT NULL DEFAULT '',
        lesson          TEXT NOT NULL,
        resolved_at     DATE NOT NULL,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS idx_sa_paper_reflections_code_time
        ON sa_paper_reflections (code, created_at DESC);
    -- status 语义（2026-09-28 修正）：这个字段原本同时承担「交易生命周期」和
    -- 「本轮决策结果」两件事，于是 side='hold'（判了但没动手、shares=0、根本没
    -- 持仓）的行也被写成 'open'，页面显示成「持有中」，和旁边的「观望」自相矛盾
    -- （实测 335 行里有 308 行是这种）。现在拆开：
    --   none     = 无仓位，仅决策记录（hold）
    --   skipped  = 想动但没成交（钱不够 / 被 T+1 拦 / 持仓已满）
    --   open     = 持有中（buy/sell 成交且未平仓）
    --   resolved = 已结算（含到期平仓写入的反向行）
    --   closed   = 已卖出平仓（2026-09-29 补）。此前卖出只插 sell 行，buy 仍挂
    --             'open'，于是止损/T+0 卖出后被平掉的买入会被 settle_and_reflect
    --             当成未平仓再结一次，_close_trade 里重复 cash += value - fee。
    ALTER TABLE sa_paper_trades DROP CONSTRAINT IF EXISTS sa_paper_trades_status_check;
    ALTER TABLE sa_paper_trades ADD CONSTRAINT sa_paper_trades_status_check
        CHECK (status IN ('open','resolved','skipped','none','closed'));
    UPDATE sa_paper_trades SET status = 'none'
        WHERE side = 'hold' AND status = 'open' AND COALESCE(shares,0) = 0;
    -- 每日资产快照（收益曲线数据源）
    CREATE TABLE IF NOT EXISTS sa_paper_equity (
        snap_date     DATE PRIMARY KEY,
        cash          NUMERIC(14,2) NOT NULL,
        market_value  NUMERIC(14,2) NOT NULL,
        total         NUMERIC(14,2) NOT NULL,
        daily_return  NUMERIC(10,6)
    );
    -- 盘中每 30 分钟一轮的决策周期台账（原来只有盘后单点，没有过程可查）
    CREATE TABLE IF NOT EXISTS sa_paper_cycles (
        id          BIGSERIAL PRIMARY KEY,
        trade_date  DATE NOT NULL,
        slot        VARCHAR(8) NOT NULL,          -- 'HH:MM'，如 '10:30'
        kind        VARCHAR(12) NOT NULL DEFAULT 'intraday',  -- intraday|catchup|manual
        trigger     VARCHAR(16) NOT NULL DEFAULT '',        -- 触发原因（stoploss/interval…）
        planned     INTEGER NOT NULL DEFAULT 0,   -- 计划判断的标的数
        acted       INTEGER NOT NULL DEFAULT 0,   -- 实际成交数
        skipped     INTEGER NOT NULL DEFAULT 0,   -- 已判但未成交（观望/仓位不足）
        holds       INTEGER NOT NULL DEFAULT 0,   -- 观望
        elapsed_ms  INTEGER NOT NULL DEFAULT 0,
        error       TEXT NOT NULL DEFAULT '',
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (trade_date, slot)
    );
    -- 盘中多轮决策：唯一键从 (trade_date,code,side) 放宽为 (trade_date,slot,code,side)，
    -- 否则同一只票当天第二轮就会被唯一约束挡掉（09-24 已踩过同类坑）。
    -- slot 缺省 '' 兼容历史单点决策的老行。
    ALTER TABLE sa_paper_trades ADD COLUMN IF NOT EXISTS slot VARCHAR(8) NOT NULL DEFAULT '';
    -- 交易费用：不扣的话「总资产涨幅」会系统性高于真实可实现收益（双边+最低5元，
    -- 小额高频下偏差被放大）。2026-09-28 加，历史行 fee_total=0。
    ALTER TABLE sa_paper_trades ADD COLUMN IF NOT EXISTS fee_total NUMERIC(12,4) NOT NULL DEFAULT 0;
    ALTER TABLE sa_paper_trades ADD COLUMN IF NOT EXISTS fee_detail JSONB NOT NULL DEFAULT '{}'::jsonb;
    -- 注意：建表里的 UNIQUE(...) 生成的是**约束**不是裸索引，必须用 DROP CONSTRAINT，
    -- 用 DROP INDEX 会报 DependentObjectsStillExist（PG 里约束底层就是那个索引）。
    ALTER TABLE sa_paper_trades
        DROP CONSTRAINT IF EXISTS sa_paper_trades_trade_date_code_side_key;
    DROP INDEX IF EXISTS sa_paper_trades_trade_date_code_side_key;
    CREATE UNIQUE INDEX IF NOT EXISTS sa_paper_trades_trade_date_slot_code_side_key
        ON sa_paper_trades (trade_date, slot, code, side);
    CREATE INDEX IF NOT EXISTS idx_sa_paper_trades_slot
        ON sa_paper_trades (trade_date, slot);
    -- 板块轮动快照（建表 DDL 以 sector.py 的 SNAPSHOT_TABLE_DDL 为准，那里也幂等）
    """
    last_exc: Exception | None = None
    for stmt in [s.strip() for s in ddl.split(";") if s.strip()]:
        if stmt.startswith("--"):  # 纯注释段（split 后残留）没有可执行语句
            stmt = "\n".join(l for l in stmt.splitlines() if not l.strip().startswith("--"))
        if not stmt:
            continue
        ok = False
        for attempt in range(3):
            try:
                with get_conn() as conn, conn.cursor() as cur:
                    cur.execute(stmt)
                ok = True
                break
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
                last_exc = exc
                print(f"[init_db] stmt failed (attempt {attempt + 1}): "
                      f"{' '.join(stmt.split())[:60]}...: {exc}", flush=True)
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
        if not ok:
            print(f"[init_db] giving up on stmt: {' '.join(stmt.split())[:60]}...",
                  flush=True)
    try:
        with get_conn() as conn, conn.cursor() as cur:
            _alter_column_widths(cur)
        _migrate_json_if_any()
    except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
        print(f"[init_db] post-DDL step failed (non-fatal): {exc}", flush=True)
    return


def _alter_column_widths(cur):
    """旧表 code 列宽 6 -> 8（兼容港股 5 位代码），幂等。"""
    for table in ("sa_watchlist", "sa_holdings"):
        cur.execute(f"ALTER TABLE {table} ALTER COLUMN code TYPE VARCHAR(8)")
    cur.execute("ALTER TABLE sa_news ALTER COLUMN code TYPE VARCHAR(8)")


def _migrate_json_if_any():
    """旧版 JSON 存储的数据一次性迁入 PG（迁完改名留档，不重复导入）。"""
    items = [("watchlist.json", "sa_watchlist"), ("holdings.json", "sa_holdings")]
    for fname, table in items:
        path = DATA_DIR / fname
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, list) or not data:
            continue
        with get_conn() as conn, conn.cursor() as cur:
            for row in data:
                if table == "sa_watchlist":
                    cur.execute(
                        "INSERT INTO sa_watchlist (code, name, note) VALUES (%s,%s,%s) "
                        "ON CONFLICT (code) DO NOTHING",
                        (row.get("code"), row.get("name", ""), row.get("note", "")))
                else:
                    cur.execute(
                        "INSERT INTO sa_holdings (code, name, shares, cost, buy_date) "
                        "VALUES (%s,%s,%s,%s,NULLIF(%s,'')::date) ON CONFLICT (code) DO NOTHING",
                        (row.get("code"), row.get("name", ""), row.get("shares"),
                         row.get("cost"), row.get("buy_date", "")))
        path.rename(path.with_suffix(".json.migrated"))
        print(f"[migrate] {fname} -> {table} ({len(data)} rows)")


app = FastAPI(title="Stock Advisor", version="0.2.0")

# ---------------- 免费行情接口（无需 key，零成本） ----------------
# 腾讯行情接口（qt.gtimg.cn）：稳定、无鉴权，字段顺序固定
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _market_of(code: str) -> str:
    """按代码形态判市场：6 位=A股，5 位=港股。"""
    return "hk" if re.fullmatch(r"\d{5}", code) else "a"


def _tx_symbol(code: str) -> str:
    """腾讯接口的证券符号：沪 sh / 深 sz / 港 hk，按代码形态与首位判断。"""
    if re.fullmatch(r"\d{5}", code):        # 港股 5 位
        return f"hk{code}"
    if code.startswith(("6", "9", "5")):    # A股沪市
        return f"sh{code}"
    return f"sz{code}"                      # A股深市


def _fmt_quote_time(raw: str):
    """行情快照时间归一化：支持 YYYYMMDDHHMMSS（A股）与 YYYY/MM/DD HH:MM:SS（港股）。"""
    if not raw:
        return None
    if re.fullmatch(r"\d{14}", raw):
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]} {raw[8:10]}:{raw[10:12]}:{raw[12:14]}"
    if "/" in raw and ":" in raw:  # 港股格式 2026/09/04 16:08:05
        date_part, _, time_part = raw.partition(" ")
        y, m, d = date_part.split("/")
        return f"{y}-{m}-{d} {time_part}"
    return None


def fetch_quotes(codes: list[str]) -> dict[str, dict]:
    """批量抓取实时行情快照。失败时返回的条目带 error 字段。

    A股走腾讯 qt.gtimg.cn（实测 0 延迟）；港股走新浪 rt_hk 接口 —— 腾讯对港股
    是 15 分钟延迟数据（2026-09-08 实测 quote_time 恒落后本机 ~900s，而 A股同
    接口 0 延迟），新浪 rt_hk 实测 0 延迟。两个市场分开请求后合并。

    腾讯响应形如 v_sh600519="1~贵州茅台~600519~1330.00~1298.88~...~"；
    新浪 rt_hk 响应形如 var hq_str_rt_hk00700="TENCENT,腾讯控股,437.0,438.4,..."。
    """
    result: dict[str, dict] = {}
    if not codes:
        return result
    hk_codes = [c for c in codes if _market_of(c) == "hk"]
    a_codes = [c for c in codes if _market_of(c) != "hk"]
    if a_codes:
        result.update(_fetch_quotes_tencent(a_codes))
    if hk_codes:
        result.update(_fetch_quotes_sina_hk(hk_codes))
        _fill_hk_market_cap(result)
    # 保证顺序与请求一致，未返回的补占位
    for c in codes:
        result.setdefault(c, {"code": c, "error": "行情未返回"})
    return result


def _fetch_quotes_tencent(codes: list[str]) -> dict[str, dict]:
    """腾讯接口抓 A股行情（~ 分隔，索引依据其固定字段顺序）。"""
    result: dict[str, dict] = {}
    symbols = {c: _tx_symbol(c) for c in codes}
    try:
        resp = requests.get(TENCENT_QUOTE_URL + ",".join(symbols.values()),
                            headers=HEADERS, timeout=20)
        resp.raise_for_status()
        text = resp.text
        for code, sym in symbols.items():
            m = re.search(rf'{sym}="([^"]*)"', text)
            if not m or not m.group(1):
                result[code] = {"code": code, "error": "行情未返回"}
                continue
            f = m.group(1).split("~")
            if len(f) < 50 or not f[3]:
                result[code] = {"code": code, "error": "行情数据异常"}
                continue
            result[code] = {
                "code": code,
                "name": f[1],
                "market": _market_of(code),
                "currency": "CNY",
                "price": _to_float(f[3]),        # 现价
                "prev_close": _to_float(f[4]),   # 昨收
                "open": _to_float(f[5]),         # 今开
                "volume": _to_float(f[6]),       # 成交量(手)
                "change": _to_float(f[31]),      # 涨跌额
                "change_pct": _to_float(f[32]),  # 涨跌幅 %
                "high": _to_float(f[33]),        # 最高
                "low": _to_float(f[34]),         # 最低
                "amount": _quote_amount(f[37], "a"),  # 成交额（统一为元）
                "market_cap": _to_float(f[45]),   # 总市值（亿元；f44 是流通市值）
                "updated_at": _fmt_quote_time(f[30]),  # 行情快照时间（交易所侧）
            }
    except Exception as exc:  # 行情抓取失败不影响 CRUD
        for c in codes:
            result.setdefault(c, {"code": c, "error": str(exc)})
    return result


SINA_HK_URL = "https://hq.sinajs.cn/list="
SINA_HK_HEADERS = {"Referer": "https://finance.sina.com.cn", **HEADERS}


def _fetch_quotes_sina_hk(codes: list[str]) -> dict[str, dict]:
    """新浪 rt_hk 接口抓港股行情（逗号分隔，字段序：英文名,中文名,今开,昨收,
    最高,最低,现价,涨跌额,涨跌%,买一,卖一,成交额,成交量,...,日期,时间）。

    港股成交量单位是股；成交额是绝对港元。无效代码返回空串 -> error 条目。
    """
    result: dict[str, dict] = {}
    try:
        url = SINA_HK_URL + ",".join(f"rt_hk{c}" for c in codes)
        resp = requests.get(url, headers=SINA_HK_HEADERS, timeout=20)
        resp.raise_for_status()
        resp.encoding = "gbk"
        text = resp.text
        for code in codes:
            m = re.search(rf'rt_hk{code}="([^"]*)"', text)
            if not m or not m.group(1):
                result[code] = {"code": code, "error": "行情未返回"}
                continue
            f = m.group(1).split(",")
            if len(f) < 19 or not f[6]:
                result[code] = {"code": code, "error": "行情数据异常"}
                continue
            result[code] = {
                "code": code,
                "name": f[1],
                "market": "hk",
                "currency": "HKD",
                "price": _to_float(f[6]),         # 现价
                "prev_close": _to_float(f[3]),    # 昨收
                "open": _to_float(f[2]),          # 今开
                "volume": _to_float(f[12]),       # 成交量(股)
                "change": _to_float(f[7]),        # 涨跌额
                "change_pct": _to_float(f[8]),    # 涨跌幅 %
                "high": _to_float(f[4]),          # 最高
                "low": _to_float(f[5]),           # 最低
                "amount": _to_float(f[11]),       # 成交额（绝对港元）
                "updated_at": _fmt_quote_time(f"{f[17]} {f[18]}"),  # 快照时间
            }
    except Exception as exc:
        for c in codes:
            result.setdefault(c, {"code": c, "error": str(exc)})
    return result


# 总市值（market_cap，单位亿元）：A股直接来自腾讯行情 f45（f44 是流通市值）；
# 港股行情走新浪 rt_hk（无市值字段），借腾讯 hk 行情补——腾讯对港股价格有
# 15 分钟延迟（所以行情换了新浪），但市值不时效敏感，可接受。港股市值按
# 代码缓存 1 小时（批量拉一次），避免 30s 一轮的行情刷新打爆接口。
_HK_CAP_TTL = 3600
_hk_cap_cache: dict[str, tuple[float | None, float]] = {}


def _fill_hk_market_cap(result: dict) -> None:
    """给行情结果里的港股条目补 market_cap 字段（缺失且缓存过期的才发请求）。"""
    hk = [c for c, q in result.items()
          if q.get("market") == "hk" and q.get("name") and "market_cap" not in q]
    now = time.time()
    fresh = {c: _hk_cap_cache[c][0] for c in hk
             if c in _hk_cap_cache and now - _hk_cap_cache[c][1] < _HK_CAP_TTL}
    for c, v in fresh.items():
        result[c]["market_cap"] = v
    stale = [c for c in hk if c not in fresh]
    if not stale:
        return
    caps: dict[str, float | None] = {}
    try:
        resp = requests.get(TENCENT_QUOTE_URL + ",".join(f"hk{c}" for c in stale),
                            headers=HEADERS, timeout=10)
        text = resp.content.decode("gbk", "ignore")
        for c in stale:
            m = re.search(rf'hk{c}="([^"]*)"', text)
            f = m.group(1).split("~") if m else []
            caps[c] = _to_float(f[45]) if len(f) > 45 else None
    except Exception as exc:
        print(f"[quote] 港股市值抓取失败: {exc}", flush=True)
    for c in stale:
        if c in caps:  # 请求成功才更新缓存；失败沿用旧值，下一轮重试
            _hk_cap_cache[c] = (caps[c], now)
            result[c]["market_cap"] = caps[c]
        else:
            result[c]["market_cap"] = _hk_cap_cache[c][0] if c in _hk_cap_cache else None


def _to_float(v: str):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _quote_amount(raw: str, market: str):
    """腾讯 f[37] 成交额统一成绝对金额（元）。

    A股该字段单位是万元（600519 返回 602259 = 60.2 亿元），必须换算；
    港股已改走新浪接口（成交额本身就是绝对港元），此处只剩 A股路径。
    """
    v = _to_float(raw)
    if v is None:
        return None
    return v * 1e4 if market == "a" else v


# ---------------- 量能分析（放量/缩量判断） ----------------
# 数据源1：实时行情 f[49] = 量比（当日每分钟均量 / 过去5日每分钟均量），
#          盘中实时有效；A股有值，港股该字段是别的含义，不可用。
# 数据源2：日K线（web.ifzq.gtimg.cn，前复权），取近 N 日成交量算均量比。
#          收盘后稳定口径；指数/港股/A股通用。两者互补：
#          盘中看量比，收盘后（量比退化为 1.00）以日K均量比为准。

INDEX_POOL = [  # 大盘量能观察池：code -> 名称
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
    ("hkHSI", "恒生指数"),
]

VOL_MA_DAYS = 5        # 均量基准窗口（不含今日）
VOL_RATIO_FLAT = (0.9, 1.15)   # 平量区间：0.9x ~ 1.15x 视为量能持平


def _em_secid(symbol: str) -> str | None:
    """腾讯符号 -> 东财 secid：sh600519->1.600519 / sz000858->0.000858 /
    hk00700->116.00700 / hkHSI->100.HSI（港股指数）。"""
    if symbol.startswith("hk"):
        rest = symbol[2:]
        return f"100.{rest}" if not rest.isdigit() else f"116.{rest}"
    if symbol.startswith("sh"):
        return "1." + symbol[2:]
    if symbol.startswith("sz"):
        return "0." + symbol[2:]
    return None


def _em_kline_fields(symbol: str, days: int, fields: str) -> list[str]:
    """东财日K（push2his）：腾讯 fqkline 被 WAF 拦时兜底（2026-09-16 实测可用）。
    fields 如 "f51,f56"（日期+成交量）/ "f51,f57"（日期+成交额元）。失败返回 []。"""
    secid = _em_secid(symbol)
    if not secid:
        return []
    try:
        resp = requests.get(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            params={"secid": secid, "klt": 101, "fqt": 1, "lmt": days,
                    "fields1": "f1,f2,f3", "fields2": fields, "end": "20500101"},
            headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return (resp.json().get("data") or {}).get("klines") or []
    except Exception:
        return []


def fetch_kline_volumes(symbol: str, days: int = VOL_MA_DAYS + 2) -> list[float]:
    """取某证券近 N 个交易日成交量列表（升序，最后一个元素=最近交易日）。
    symbol 用腾讯符号（sh600519 / hkHSI / hk00700 / usAAPL.OQ）。主源腾讯日K；
    腾讯 web.ifzq 域名被 WAF 拦（501，2026-09-16 起频发）时依次换镜像、再兜底
    东财 push2his（成交量单位与腾讯一致，比值口径不受影响）。都失败返回 []。

    ⚠️ 2026-09-26 实测：`web.ifzq.gtimg.cn` 是 501，但**去掉 web. 前缀**的
    `ifzq.gtimg.cn` 和 `proxy.finance.qq.com/ifzqgtimg` 都正常（A股/港股/美股全通）。
    原来的域名在东财整域不可达时会让本函数完全失效——量能列整片显示「—」。
    """
    for host in ("https://ifzq.gtimg.cn/appstock/app/fqkline/get",
                 "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get",
                 "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"):
        try:
            resp = requests.get(host, params={"param": f"{symbol},day,,,{days},qfq"},
                                headers=HEADERS, timeout=15)
            resp.raise_for_status()
            data = resp.json().get("data", {}).get(symbol, {})
            bars = data.get("qfqday") or data.get("day") or []
            out = [_to_float(bar[5]) for bar in bars if len(bar) > 5]
            out = [v for v in out if v]
            if out:
                return out
        except Exception:
            continue
    ks = _em_kline_fields(symbol, days, "f51,f56")
    return [v for v in (_to_float(k.split(",")[1]) for k in ks if len(k.split(",")) > 1) if v]


def fetch_kline_amounts(symbol: str, days: int = VOL_MA_DAYS + 2) -> list[float]:
    """近 N 个交易日成交额（元，升序）。东财 f57 为绝对元；A股指数/个股通用。"""
    ks = _em_kline_fields(symbol, days, "f51,f57")
    return [v for v in (_to_float(k.split(",")[1]) for k in ks if len(k.split(",")) > 1) if v]


def _vol_label(ratio) -> str:
    if ratio is None:
        return "—"
    if ratio >= VOL_RATIO_FLAT[1]:
        return "放量"
    if ratio <= VOL_RATIO_FLAT[0]:
        return "缩量"
    return "平量"


def _vol_color(ratio) -> str:
    """前端着色提示：放量=红、缩量=绿、平量=灰。"""
    if ratio is None:
        return ""
    if ratio >= VOL_RATIO_FLAT[1]:
        return "up"
    if ratio <= VOL_RATIO_FLAT[0]:
        return "down"
    return "muted"


def _latest_vs_ma(vols: list[float], ma_days: int = VOL_MA_DAYS) -> dict:
    """最新一日成交量 / 前 ma_days 日均量。数据不足返回 {"ratio": None}。"""
    if len(vols) < ma_days + 1 or not vols[-1]:
        return {"ratio": None, "label": "—", "color": ""}
    today, base = vols[-1], vols[-ma_days - 1:-1]
    ratio = round(today / (sum(base) / ma_days), 2) if sum(base) > 0 else None
    return {"ratio": ratio, "label": _vol_label(ratio), "color": _vol_color(ratio)}


def analyze_volume(symbol: str) -> dict:
    """单只证券量能：日K均量比为主 + 实时量比（若有）为辅。"""
    out = _latest_vs_ma(fetch_kline_volumes(symbol))
    out["today_volume"] = None
    return out


def market_volume_status() -> dict:
    """大盘量能：上证/深成/创业板/恒指 各自 均量比 + 放量缩量标签。"""
    items = []
    for sym, name in INDEX_POOL:
        vols = fetch_kline_volumes(sym)
        stat = _latest_vs_ma(vols)
        items.append({"symbol": sym, "name": name,
                      "today_volume": vols[-1] if vols else None,
                      **stat})
    # 大盘整体倾向：取有数据的主流 A 股指数均量比的均值（恒指不参与定性）
    ratios = [i["ratio"] for i in items if i["ratio"] is not None and "HSI" not in i["symbol"]]
    overall_ratio = round(sum(ratios) / len(ratios), 2) if ratios else None
    return {"items": items, "overall_ratio": overall_ratio,
            "overall_label": _vol_label(overall_ratio),
            "overall_color": _vol_color(overall_ratio),
            "checked_at": datetime.now().isoformat(timespec="seconds")}


def watchlist_volume_status(codes: list[str]) -> dict[str, dict]:
    """批量个股量能：{code: {ratio, label, color, today_volume}}。"""
    out: dict[str, dict] = {}
    for code in codes:
        vols = fetch_kline_volumes(_tx_symbol(code))
        stat = _latest_vs_ma(vols)
        out[code] = {"today_volume": vols[-1] if vols else None, **stat}
    return out


# ---------------- 盘中大盘量能监控（放量/缩量异动告警） ----------------
# 日K均量比是收盘后口径，盘中要盯「现在这个时点算不算放量/缩量」用行情
# f49 量比：当日每分钟均量 ÷ 过去5日每分钟均量，腾讯实时算好，三大指数一次请求拿齐。
# 状态翻转（如 平量->放量）连续两次采样确认才推微信/邮件——单次毛刺不轰炸。

_intraday_vol = {"lock": threading.Lock(), "ratio": None, "label": None,
                 "items": [], "updated_at": None,
                 "confirmed_label": None, "pending_label": None, "pending_count": 0,
                 "last_push_ts": 0.0, "last_push_day": None, "push_count_today": 0,
                 "last_alert": None}

VOL_PUSH_COOLDOWN = 45 * 60     # 同方向翻转告警冷却（秒）
VOL_PUSH_MAX_PER_DAY = 4        # 每日最多推送条数


def _a_share_session(now: datetime) -> bool:
    """A股交易时段（9:15–11:35 / 12:55–15:05，工作日）。"""
    if now.weekday() >= 5:
        return False
    hm = (now.hour, now.minute)
    return (9, 15) <= hm <= (11, 35) or (12, 55) <= hm <= (15, 5)


def sample_market_volume_now() -> dict:
    """实时采一次三大指数量比（行情 f49）+ 成交额，写入 _intraday_vol 并返回快照。"""
    a_indices = [(s, n) for s, n in INDEX_POOL if n != "恒生指数"]
    items = []
    try:
        resp = requests.get(TENCENT_QUOTE_URL + ",".join(s for s, _ in a_indices),
                            headers=HEADERS, timeout=15)
        text = resp.content.decode("gbk", "ignore")
        for sym, name in a_indices:
            m = re.search(rf'{sym}="([^"]*)"', text)
            if not m:
                continue
            f = m.group(1).split("~")
            items.append({"name": name,
                          "ratio": _to_float(f[49]) if len(f) > 49 else None,
                          "amount": _quote_amount(f[37], "a") if len(f) > 37 else None})
    except Exception as exc:
        print(f"[mktvol] sample failed: {exc}", flush=True)
    ratios = [i["ratio"] for i in items if isinstance(i["ratio"], (int, float))]
    overall = round(sum(ratios) / len(ratios), 2) if ratios else None
    snapshot = {"ratio": overall, "label": _vol_label(overall), "items": items,
                "updated_at": datetime.now().isoformat(timespec="seconds")}
    with _intraday_vol["lock"]:
        _intraday_vol.update(snapshot)
    return snapshot


def _market_volume_alert(snapshot: dict) -> dict | None:
    """状态翻转判定（带一次确认）：新标签连续两次采样出现才触发。返回推送文本或 None。"""
    now = datetime.now()
    day = now.strftime("%Y-%m-%d")
    new = snapshot["label"]
    if new not in ("放量", "缩量"):
        new = None  # 平量不告警（回到平量视为噪音，只有明确放量/缩量才推）
    with _intraday_vol["lock"]:
        cur = _intraday_vol.get("confirmed_label")
        if new and new != cur:
            if _intraday_vol["pending_label"] == new:
                _intraday_vol["pending_count"] += 1
            else:
                _intraday_vol["pending_label"] = new
                _intraday_vol["pending_count"] = 1
            confirmed = _intraday_vol["pending_count"] >= 2  # 连续两次同向才确认
        else:
            _intraday_vol["pending_label"] = None
            _intraday_vol["pending_count"] = 0
            confirmed = False
        if confirmed:
            _intraday_vol["confirmed_label"] = new
            _intraday_vol["pending_label"] = None
            _intraday_vol["pending_count"] = 0
            # 冷却 + 每日上限
            if day != _intraday_vol["last_push_day"]:
                _intraday_vol["last_push_day"] = day
                _intraday_vol["push_count_today"] = 0
            if (_intraday_vol["push_count_today"] >= VOL_PUSH_MAX_PER_DAY
                    or now.timestamp() - _intraday_vol["last_push_ts"] < VOL_PUSH_COOLDOWN):
                return None
            _intraday_vol["last_push_ts"] = now.timestamp()
            _intraday_vol["push_count_today"] += 1
            detail = "、".join(f"{i['name']} {i['ratio']}x" for i in snapshot["items"]
                              if isinstance(i.get("ratio"), (int, float)))
            amt = sum(i["amount"] for i in snapshot["items"]
                      if isinstance(i.get("amount"), (int, float)) and i["name"] != "创业板指")
            text = (f"📊 大盘转为【{new}】：沪深京三大指数平均量比 {snapshot['ratio']}x"
                    f"（{detail}）\n沪+深成交额已 {amt / 1e8:,.0f} 亿元"
                    f"\n（{now.strftime('%H:%M')} 采样，量比=当日每分钟均量/前5日同期；"
                    "来自 stock-advisor 盘中量能监控）")
            _intraday_vol["last_alert"] = {"time": now.isoformat(timespec="seconds"),
                                           "label": new, "text": text}
            return {"title": f"📊 盘中大盘{new}", "content": text}
        return None


@app.get("/api/volume/intraday")
def volume_intraday():
    """盘中量能快照（含最近一次告警）；无缓存时现场采一次。"""
    with _intraday_vol["lock"]:
        fresh = _intraday_vol["updated_at"] and \
            (datetime.now() - datetime.fromisoformat(_intraday_vol["updated_at"])
             ).total_seconds() < 120
        snap = dict(_intraday_vol) if fresh else None
    if snap is None:
        snap = sample_market_volume_now()
        with _intraday_vol["lock"]:
            snap = dict(_intraday_vol)
    snap.pop("lock", None)
    return snap


def _market_volume_loop():
    """盘中大盘量能守护线程：交易时段按 config volume.interval_minutes 采样，
    放量/缩量状态翻转（双采样确认）推微信/邮件；收盘后补一次全天总结可选。"""
    while True:
        try:
            conf = _conf_section("volume")
            interval = max(int(conf.get("interval_minutes") or 5), 1)
            if conf.get("enabled", True) and _a_share_session(datetime.now()):
                snap = sample_market_volume_now()
                if conf.get("notify", True) and snap["ratio"] is not None:
                    alert = _market_volume_alert(snap)
                    if alert:
                        try:
                            notifier.notify(alert["title"], alert["content"])
                            print(f"[mktvol] alert pushed: {alert['title']}", flush=True)
                        except Exception as exc:
                            print(f"[mktvol] notify failed: {exc}", flush=True)
                time.sleep(interval * 60)
            else:
                time.sleep(300)
        except Exception as exc:
            print(f"[mktvol] loop error: {exc}", flush=True)
            time.sleep(300)


@app.get("/api/volume/status")
def volume_status():
    """量能总览：大盘 4 指数 + 全部自选股 + 全部持仓（去重）。"""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT code FROM sa_watchlist")
        codes = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT DISTINCT code FROM sa_trades")
        codes |= {r[0] for r in cur.fetchall()}
    stock_vol = watchlist_volume_status(sorted(codes))
    return {"market": market_volume_status(), "stocks": stock_vol}


# ---------------- Pydantic 模型 ----------------

class StockIn(BaseModel):
    code: str
    note: str = ""
    keywords: str = Field(default="", max_length=255,
                          description="中性搜索词，逗号分隔（如：Kimi,OpenAI）")
    keywords_pos: str = Field(default="", max_length=255,
                              description="利好搜索词，逗号分隔（如：中标,增持,回购）")
    keywords_neg: str = Field(default="", max_length=255,
                              description="利空搜索词，逗号分隔（如：解禁,减持,降价）")


def _split_keywords(raw: str) -> list[str]:
    """逗号/中文逗号分隔的搜索词 → 去空去重列表。"""
    return [k.strip() for k in re.split(r"[,，]", raw or "") if k.strip()]


# ---------------- 自选股 CRUD ----------------

@app.get("/api/watchlist")
def list_watchlist(with_quotes: bool = True):
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT code, name, note, keywords, keywords_pos, keywords_neg, added_at "
                    "FROM sa_watchlist ORDER BY added_at")
        items = cur.fetchall()
    items = [dict(i) for i in items]
    for i in items:
        i["added_at"] = i["added_at"].isoformat(timespec="seconds") if i["added_at"] else None
    if with_quotes:
        quotes = fetch_quotes([i["code"] for i in items])
        vol_stats = watchlist_volume_status([i["code"] for i in items])
        for item in items:
            item["quote"] = quotes.get(item["code"])
            item["volume"] = vol_stats.get(item["code"])  # 量能：均量比+放量缩量标签
    return items


@app.post("/api/watchlist")
def add_watchlist(stock: StockIn):
    code = _normalize_code(stock.code)
    quote = fetch_quotes([code]).get(code, {})
    if quote.get("error") or not quote.get("name"):
        raise HTTPException(404, f"未找到股票 {code}，请确认代码是否正确")
    kw = ("，".join(_split_keywords(stock.keywords)),
          "，".join(_split_keywords(stock.keywords_pos)),
          "，".join(_split_keywords(stock.keywords_neg)))
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sa_watchlist (code, name, note, keywords, keywords_pos, keywords_neg) "
            "VALUES (%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (code) DO UPDATE SET note = EXCLUDED.note, "
            "keywords = EXCLUDED.keywords, keywords_pos = EXCLUDED.keywords_pos, "
            "keywords_neg = EXCLUDED.keywords_neg, name = EXCLUDED.name",
            (code, quote["name"], stock.note.strip(), *kw))
    item = {"code": code, "name": quote["name"], "note": stock.note.strip(),
            "keywords": kw[0], "keywords_pos": kw[1], "keywords_neg": kw[2]}
    item["quote"] = quote
    return item


@app.put("/api/watchlist/{code}")
def update_watchlist(code: str, stock: StockIn):
    """改备注/搜索词（代码以路径为准，body.code 忽略）。"""
    code = _normalize_code(code)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE sa_watchlist SET note = %s, keywords = %s, "
            "keywords_pos = %s, keywords_neg = %s WHERE code = %s",
            (stock.note.strip(),
             "，".join(_split_keywords(stock.keywords)),
             "，".join(_split_keywords(stock.keywords_pos)),
             "，".join(_split_keywords(stock.keywords_neg)), code))
        if cur.rowcount == 0:
            raise HTTPException(404, f"{code} 不在自选列表中")
    return {"ok": True, "code": code}


@app.delete("/api/watchlist/{code}")
def remove_watchlist(code: str):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM sa_watchlist WHERE code = %s", (code,))
        if cur.rowcount == 0:
            raise HTTPException(404, f"{code} 不在自选列表中")
    return {"ok": True, "removed": code}


# ---------------- 持仓流水账（每笔交易一条，持仓由流水推导） ----------------

class TradeIn(BaseModel):
    code: str
    side: str = Field(description="buy=买入 / sell=卖出")
    shares: int = Field(gt=0, description="股数")
    price: float = Field(gt=0, description="每股成交价")
    trade_date: str = Field(default="", description="交易日期 YYYY-MM-DD，空=今天")
    note: str = Field(default="", max_length=255)
    strategy_id: int | None = Field(default=None, description="关联止盈策略 id（可空）")


def calc_position(trades: list[dict]) -> dict:
    """按时间序回放交易流水（平均成本法），返回持仓汇总。

    买入摊薄成本：avg = (avg*net + price*n) / (net + n)
    卖出锁定盈亏：realized += (price - avg) * n（成本价不变）
    卖出超过当前持股 -> ValueError（账目不合法，调用方负责拦截）。
    """
    net, avg_cost = 0, 0.0
    realized = total_buy = total_sell = 0.0
    first_date, last_date = None, None
    for t in sorted(trades, key=lambda x: (x["trade_date"], x["id"] if "id" in x else 0)):
        n, price = int(t["shares"]), float(t["price"])
        if t["side"] == "buy":
            avg_cost = (avg_cost * net + price * n) / (net + n)
            net += n
            total_buy += price * n
        else:
            if n > net:
                raise ValueError(
                    f"卖出 {n} 股超过当前持股 {net} 股（{t['trade_date']} 那笔）")
            realized += (price - avg_cost) * n
            net -= n
            total_sell += price * n
        first_date = first_date or t["trade_date"]
        last_date = t["trade_date"]
    return {"net_shares": net, "avg_cost": round(avg_cost, 4) if net else 0.0,
            "realized_pnl": round(realized, 2), "total_buy": round(total_buy, 2),
            "total_sell": round(total_sell, 2),
            "first_date": first_date, "last_date": last_date}


def _query_trades(code: str | None = None) -> list[dict]:
    sql = ("SELECT t.id, t.code, t.trade_date, t.side, t.shares, t.price, t.note, "
           "t.created_at, t.strategy_id, t.strategy_triggered_at, t.strategy_peak_price, "
           "t.strategy_ladder_step, "
           "s.name AS strategy_name, s.kind AS strategy_kind, "
           "s.target_pct AS strategy_target_pct, s.drawdown_pct AS strategy_drawdown_pct, "
           "s.config AS strategy_config "
           "FROM sa_trades t "
           "LEFT JOIN sa_strategies s ON s.id = t.strategy_id")
    params: list = []
    if code:
        sql += " WHERE t.code = %s"
        params.append(code)
    sql += " ORDER BY t.trade_date, t.id"
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["price"] = float(r["price"])
        r["trade_date"] = r["trade_date"].isoformat()
        r["created_at"] = r["created_at"].isoformat(timespec="seconds")
        if r.get("strategy_triggered_at"):
            r["strategy_triggered_at"] = r["strategy_triggered_at"].isoformat(timespec="seconds")
        for k in ("strategy_peak_price", "strategy_target_pct", "strategy_drawdown_pct"):
            if r.get(k) is not None:
                r[k] = float(r[k])
        cfg = r.get("strategy_config")
        r["strategy_config"] = cfg if isinstance(cfg, dict) else (json.loads(cfg or "{}") if cfg else {})
    return rows


def _attach_lot_pnl(trades: list[dict]) -> None:
    """给每笔交易单独标注盈亏（就地修改 rows，附带 lot_* 字段）。

    口径（平均成本法，与 calc_position 一致，逐笔回放）：
    - 买入：该笔成本 = 买入时点的摊薄成本，浮动盈亏 = (现价-成本)*股数，
      仅当前仍持有（回放结束时净持股 > 0）才有意义；已被后续卖出消耗的
      不单独拆（流水账不追踪具体哪笔卖哪笔买）。
    - 卖出：已实现盈亏 = (卖价 - 卖出时点摊薄成本) * 股数，立即锁定。
    现价取自实时行情；无行情时买入笔 pnl 为 None。
    """
    if not trades:
        return
    codes = {t["code"] for t in trades}
    quotes = fetch_quotes(sorted(codes))
    groups: dict[str, list[dict]] = {}
    for t in trades:
        groups.setdefault(t["code"], []).append(t)
    final_net: dict[str, int] = {}
    for code, ts in groups.items():
        net = 0
        try:
            net = calc_position(ts)["net_shares"]
        except ValueError:
            pass
        final_net[code] = net
        avg_cost, net_replay = 0.0, 0
        for t in sorted(ts, key=lambda x: (x["trade_date"], x["id"])):
            n, price = int(t["shares"]), float(t["price"])
            if t["side"] == "buy":
                avg_cost = (avg_cost * net_replay + price * n) / (net_replay + n)
                net_replay += n
                t["lot_cost"] = round(avg_cost, 4)
                t["lot_realized"] = None
            else:
                if n > net_replay:
                    t["lot_cost"] = t["lot_realized"] = None
                    continue
                t["lot_cost"] = round(avg_cost, 4)
                t["lot_realized"] = round((price - avg_cost) * n, 2)
                net_replay -= n
    for t in trades:
        q = quotes.get(t["code"], {})
        t["lot_price"] = q.get("price")
        if t["side"] == "buy":
            held = final_net.get(t["code"], 0) > 0
            if held and isinstance(t.get("lot_price"), (int, float)) and t.get("lot_cost"):
                t["lot_pnl"] = round((t["lot_price"] - t["lot_cost"]) * int(t["shares"]), 2)
                t["lot_pnl_pct"] = round((t["lot_price"] / t["lot_cost"] - 1) * 100, 2)
            else:
                t["lot_pnl"] = None      # 已清仓/无行情：买入笔无浮动可算
                t["lot_pnl_pct"] = None
        else:
            t["lot_pnl"] = t.get("lot_realized")
            t["lot_pnl_pct"] = (round((t["lot_price"] / t["lot_cost"] - 1) * 100, 2)
                                if t.get("lot_realized") is not None and t.get("lot_cost")
                                else None)


def _derive_holdings() -> list[dict]:
    """全部流水按股分组回放，得到每只股票的持仓汇总（含已清仓的）。"""
    trades = _query_trades()
    groups: dict[str, list[dict]] = {}
    for t in trades:
        groups.setdefault(t["code"], []).append(t)
    names = _stock_names(list(groups))
    out = []
    for code, ts in groups.items():
        try:
            pos = calc_position(ts)
        except ValueError as exc:
            # 流水不合法（历史数据/删账导致），仍展示但标记错误
            pos = {"net_shares": 0, "avg_cost": 0.0, "realized_pnl": 0.0,
                   "total_buy": 0.0, "total_sell": 0.0, "error": str(exc)}
            pos["first_date"] = ts[0]["trade_date"]
            pos["last_date"] = ts[-1]["trade_date"]
        pos["code"] = code
        pos["name"] = names.get(code, code)
        pos["trade_count"] = len(ts)
        out.append(pos)
    out.sort(key=lambda x: (x["net_shares"] == 0, x["code"]))
    return out


def _stock_names(codes: list[str]) -> dict[str, str]:
    """优先取自选股表里的名字，缺失时用代码本身。"""
    names = {}
    if codes:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT code, name FROM sa_watchlist WHERE code = ANY(%s)", (codes,))
            names = dict(cur.fetchall())
    return {c: names.get(c, c) for c in codes}


# ---------------- 港币->人民币折算 ----------------
# 持仓/盈亏的汇总口径统一成人民币：港股行情与成本都是 HKD，市值/盈亏/已实现
# 全要乘汇率再合计，否则「总市值」是 CNY+HKD 直接相加的错数。
# 数据源腾讯外汇 whHKDCNY（2026-09-10 实测 0.8551），缓存 10 分钟；
# 接口失败时沿用旧值，从未成功过则不折算（*_cny 字段=原币值，前端会标注）。

_FX_CACHE: dict = {"rate": None, "ts": 0.0}


def _hkd_cny_rate() -> float | None:
    import time as _time
    now = _time.time()
    if _FX_CACHE["rate"] and now - _FX_CACHE["ts"] < 600:
        return _FX_CACHE["rate"]
    try:
        resp = requests.get("https://qt.gtimg.cn/q=whHKDCNY", headers=HEADERS, timeout=10)
        m = re.search(r'="([^"]*)"', resp.text)
        f = m.group(1).split("~") if m else []
        rate = _to_float(f[3]) if len(f) > 3 else None
        if rate and 0.5 < rate < 1.5:  # 合理性护栏，脏数据宁可不折
            _FX_CACHE["rate"], _FX_CACHE["ts"] = rate, now
            return rate
    except Exception:
        pass
    return _FX_CACHE["rate"]  # 失败给旧缓存值（可能 None）


def _holdings_with_pnl(positions: list[dict]) -> list[dict]:
    """给持仓汇总挂实时行情，算浮动盈亏与累计总盈亏（浮动 + 已实现）。

    汇总口径：_cny 后缀字段一律人民币（港股按 _hkd_cny_rate 折算，A股=原值），
    无后缀字段保持原币（港股为 HKD）。
    """
    held = [p for p in positions if p["net_shares"] > 0]
    quotes = fetch_quotes([p["code"] for p in held]) if held else {}
    any_hk = any(q.get("market") == "hk" for q in quotes.values())
    rate = _hkd_cny_rate() if any_hk else None
    for p in positions:
        q = quotes.get(p["code"], {}) if p["net_shares"] > 0 else {}
        p["quote"] = q
        price = q.get("price")
        is_hk = q.get("market") == "hk" and rate
        p["fx_rate"] = rate if is_hk else None
        if p["net_shares"] > 0 and isinstance(price, (int, float)) and price > 0:
            p["price"] = price
            p["market_value"] = round(price * p["net_shares"], 2)
            p["cost_value"] = round(p["avg_cost"] * p["net_shares"], 2)
            p["pnl"] = round(p["market_value"] - p["cost_value"], 2)          # 浮动盈亏
            p["pnl_pct"] = round((price / p["avg_cost"] - 1) * 100, 2)
        else:
            p["price"] = price
            p["market_value"] = 0.0
            p["pnl"] = 0.0                                                    # 已清仓无浮动
            p["pnl_pct"] = None
        p["total_pnl"] = round(p["pnl"] + p["realized_pnl"], 2)               # 累计总盈亏
        # 只有港股才折算：rate 是全局汇率（组合里有港股时非空），
        # 不能拿给 A 股用——曾把 A 股盈亏也乘 0.855，688795 一笔 -71546 显示成 -61165
        k = rate if is_hk else 1.0
        p["price_cny"] = round(price * k, 4) if p.get("price") else p.get("price")
        p["market_value_cny"] = round(p["market_value"] * k, 2)
        p["cost_value_cny"] = round(p["cost_value"] * k, 2) if p.get("cost_value") is not None else None
        p["pnl_cny"] = round(p["pnl"] * k, 2)
        p["realized_pnl_cny"] = round(p["realized_pnl"] * k, 2)
        p["total_pnl_cny"] = round(p["total_pnl"] * k, 2)
    return positions


def _query_holdings() -> list[dict]:
    """供报告 snapshot 等使用：只返回当前有持股的行，字段与旧版对齐。"""
    rows = []
    for p in _derive_holdings():
        if p["net_shares"] <= 0:
            continue
        rows.append({"code": p["code"], "name": p["name"], "shares": p["net_shares"],
                     "cost": p["avg_cost"], "buy_date": p["first_date"] or "",
                     "realized_pnl": p["realized_pnl"], "total_pnl": p["total_pnl"]})
    return rows


@app.get("/api/holdings")
def list_holdings():
    rows = _holdings_with_pnl(_derive_holdings())
    # 附带整仓级策略关联（每股可能挂多个），供持仓页直接展示/解绑
    by_code: dict[str, list] = {}
    try:
        for ps in _position_strategies_rows():
            by_code.setdefault(ps["code"], []).append(ps)
    except Exception:
        pass
    for r in rows:
        r["position_strategies"] = by_code.get(r["code"], [])
    return rows


@app.get("/api/trades")
def list_trades(code: str = "", limit: int = 200):
    """交易流水（新→旧），可按股票筛选。每笔附带独立盈亏（lot_*）与关联策略。"""
    code = code.strip()
    if code and not re.fullmatch(r"\d{4,6}", code):
        raise HTTPException(400, "代码格式：A股 6 位；港股 4-5 位")
    target = _normalize_code(code) if code else None
    rows = _query_trades(target)
    rows.sort(key=lambda t: (t["trade_date"], t["id"]), reverse=True)
    rows = rows[:max(1, min(limit, 500))]
    _attach_lot_pnl(rows)
    return rows


@app.post("/api/trades")
def add_trade(trade: TradeIn):
    code = _normalize_code(trade.code)
    side = trade.side.strip().lower()
    if side not in ("buy", "sell"):
        raise HTTPException(400, "side 应为 buy（买入）或 sell（卖出）")
    trade_date = trade.trade_date.strip() or datetime.now().strftime("%Y-%m-%d")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", trade_date):
        raise HTTPException(400, "交易日期格式应为 YYYY-MM-DD")

    with LOCK:
        existing = _query_trades(code)
        # 卖出校验：按日期归位后回放，若该日还有更早插入的同日单，按 id 序自然处理
        replay = existing + [{"side": side, "shares": trade.shares, "price": trade.price,
                              "trade_date": trade_date, "id": 10**9}]
        try:
            calc_position(replay)
        except ValueError as exc:
            raise HTTPException(400, f"记账失败：{exc}")
        name = _stock_names([code]).get(code, code)
        if trade.strategy_id is not None:
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT id FROM sa_strategies WHERE id = %s",
                            (trade.strategy_id,))
                row = cur.fetchone()
            if not row:
                raise HTTPException(404, f"止盈策略 #{trade.strategy_id} 不存在")
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sa_trades (code, trade_date, side, shares, price, note, strategy_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (code, trade_date, side, trade.shares, trade.price, trade.note.strip(),
                 trade.strategy_id))
            trade_id = cur.fetchone()[0]
    return {"ok": True, "id": trade_id, "code": code, "side": side,
            "shares": trade.shares, "price": trade.price, "trade_date": trade_date}


@app.delete("/api/trades/{trade_id}")
def remove_trade(trade_id: int):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM sa_trades WHERE id = %s", (trade_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, f"交易记录 #{trade_id} 不存在")
    return {"ok": True, "removed": trade_id}


@app.delete("/api/holdings/{code}")
def remove_holding(code: str):
    """清账：删除该股票的全部交易流水（持仓由流水推导，删完即清）。"""
    normalized = _normalize_code(code)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM sa_trades WHERE code = %s", (normalized,))
        if cur.rowcount == 0:
            raise HTTPException(404, f"{normalized} 无交易流水")
    return {"ok": True, "removed": normalized, "trades_deleted": cur.rowcount}


# ---------------- 止盈策略（与股票无关的模板，供「记一笔」引用；触发即通知） ----------------
# 内置常见量化退出策略（kind）：
#   pct        固定止盈：现价 >= 成本*(1+target_pct%)。
#   drawdown   回撤止盈：盈利超 target_pct%（激活线）后跟踪峰值，离峰值回撤
#              drawdown_pct% 触发（利润落袋型，先扬后抑场景）。
#   trailing   移动止盈：买入即跟踪峰值，无激活线，回撤 drawdown_pct% 触发。
#   stop_loss  止损：现价 <= 成本*(1-target_pct%)（target_pct 存的是损失%）。
#   ladder     分批止盈：config.steps=[{pct, ratio}...]，涨幅逐档到达逐档通知
#              （通知不终结策略，strategy_ladder_step 记录已到档位）。
#   time_stop  时间止盈：config.hold_days 个交易日后通知复盘卖出。

STRATEGY_KINDS = ("pct", "drawdown", "trailing", "stop_loss", "ladder", "time_stop")

STRATEGY_KIND_LABEL = {
    "pct": "固定止盈", "drawdown": "回撤止盈", "trailing": "移动止盈",
    "stop_loss": "止损", "ladder": "分批止盈", "time_stop": "时间止盈",
}


def _strategy_config_display(row: dict) -> str:
    """把一行策略翻译成人话（列表/通知共用）。"""
    kind, tp = row["kind"], float(row["target_pct"])
    dd = float(row["drawdown_pct"]) if row.get("drawdown_pct") else None
    cfg = row.get("config") or {}
    if kind == "pct":
        return f"+{tp:g}% 止盈"
    if kind == "stop_loss":
        return f"-{tp:g}% 止损"
    if kind == "drawdown":
        return f"盈利>{tp:g}%后回撤{dd:g}%触发" if dd else f"回撤 {tp:g}%"
    if kind == "trailing":
        return f"峰值回撤{dd:g}%触发" if dd else f"回撤 {tp:g}%"
    if kind == "ladder":
        steps = cfg.get("steps") or []
        return " ".join(f"{s['pct']:g}%卖{s['ratio']*100:g}%" for s in steps) or "未配置档位"
    if kind == "time_stop":
        return f"持有 {cfg.get('hold_days', tp):g} 个交易日"
    return "?"


class StrategyIn(BaseModel):
    name: str = Field(default="", max_length=128, description="策略名，空则自动生成")
    kind: str = Field(default="pct")
    target_pct: float = Field(gt=0, description="止盈涨幅%/止损幅度%/回撤激活线")
    drawdown_pct: float | None = Field(default=None, gt=0, lt=100,
                                       description="drawdown/trailing 型：峰值回撤%")
    steps: list | None = Field(default=None, description="ladder 型：[{pct, ratio}] 档位数组")
    hold_days: int | None = Field(default=None, gt=0, description="time_stop 型：持有交易日数")
    note: str = Field(default="", max_length=255)


@app.get("/api/strategies")
def list_strategies():
    """止盈策略模板列表（新→旧），附引用它们的交易数与整仓持仓数。"""
    sql = ("SELECT s.*, COUNT(DISTINCT t.id) FILTER (WHERE t.id IS NOT NULL) AS used_count, "
           "COUNT(DISTINCT t.id) FILTER (WHERE t.strategy_triggered_at IS NOT NULL) AS triggered_count, "
           "COUNT(DISTINCT ps.id) AS pos_count "
           "FROM sa_strategies s "
           "LEFT JOIN sa_trades t ON t.strategy_id = s.id "
           "LEFT JOIN sa_position_strategies ps ON ps.strategy_id = s.id "
           "GROUP BY s.id ORDER BY s.created_at DESC, s.id DESC")
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["kind_label"] = STRATEGY_KIND_LABEL.get(r["kind"], r["kind"])
        r["target_display"] = _strategy_config_display(r)
    return rows


@app.post("/api/strategies")
def create_strategy(s: StrategyIn):
    kind = (s.kind or "pct").strip().lower()
    if kind not in STRATEGY_KINDS:
        raise HTTPException(400, f"kind 应为 {'/'.join(STRATEGY_KINDS)}")
    cfg: dict = {}
    if kind in ("drawdown", "trailing"):
        if not s.drawdown_pct or s.drawdown_pct <= 0:
            raise HTTPException(400, "该策略需要填回撤% drawdown_pct（如 5）")
    elif kind == "ladder":
        steps = s.steps or []
        if not steps or any(not st.get("pct") or not st.get("ratio") for st in steps):
            raise HTTPException(400, "分批止盈需要至少一档 {pct: 涨幅%, ratio: 卖出比例0~1}")
        if sum(float(st["ratio"]) for st in steps) > 1.0001:
            raise HTTPException(400, "各档卖出比例之和不能超过 100%")
        cfg["steps"] = sorted(
            [{"pct": float(st["pct"]), "ratio": float(st["ratio"])} for st in steps],
            key=lambda x: x["pct"])
    elif kind == "time_stop":
        if not s.hold_days or s.hold_days <= 0:
            raise HTTPException(400, "时间止盈需要填 hold_days（持有交易日数）")
        cfg["hold_days"] = int(s.hold_days)
    name = s.name.strip() or {
        "pct": f"止盈 +{s.target_pct:g}%",
        "stop_loss": f"止损 -{s.target_pct:g}%",
        "drawdown": f"回撤止盈 盈利>{s.target_pct:g}%回撤{s.drawdown_pct:g}%",
        "trailing": f"移动止盈 回撤{s.drawdown_pct:g}%",
        "ladder": f"分批止盈 {len(cfg.get('steps', []))} 档",
        "time_stop": f"时间止盈 {cfg.get('hold_days', '?')} 交易日",
    }.get(kind, "策略")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sa_strategies (name, kind, target_pct, drawdown_pct, config, note) "
            "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            (name, kind, s.target_pct,
             s.drawdown_pct if kind in ("drawdown", "trailing") else None,
             json.dumps(cfg), s.note.strip()))
        sid = cur.fetchone()[0]
    return {"ok": True, "id": sid, "name": name}


@app.delete("/api/strategies/{strategy_id}")
def delete_strategy(strategy_id: int):
    """删除策略模板。若已有交易/持仓引用则拒绝（先解绑），避免历史悬空。"""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM sa_trades WHERE strategy_id = %s", (strategy_id,))
        used = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM sa_position_strategies WHERE strategy_id = %s",
                    (strategy_id,))
        used_pos = cur.fetchone()[0]
        if used or used_pos:
            raise HTTPException(
                400, f"该策略被 {used} 笔交易、{used_pos} 个持仓引用，不能删除（可先解绑）")
        cur.execute("DELETE FROM sa_strategies WHERE id = %s RETURNING id", (strategy_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, f"策略 #{strategy_id} 不存在")
    return {"ok": True, "removed": strategy_id}


# ---------------- 持仓（整仓级）关联策略 ----------------
# 一笔一笔引用太繁琐：直接给某只股票的当前持仓挂一个策略，按整仓摊薄成本判定，
# 触发状态记在 sa_position_strategies 行上（peak_price/ladder_step/triggered_at）。

class PositionStrategyIn(BaseModel):
    strategy_id: int = Field(description="要关联的策略模板 id")


def _position_strategies_rows() -> list[dict]:
    """全部持仓级策略关联（join 策略模板），NUMERIC/JSONB 已转 python 类型。"""
    sql = ("SELECT ps.id, ps.code, ps.strategy_id, ps.triggered_at, ps.peak_price, "
           "ps.ladder_step, s.name AS strategy_name, s.kind, s.target_pct, "
           "s.drawdown_pct, s.config "
           "FROM sa_position_strategies ps JOIN sa_strategies s ON s.id = ps.strategy_id")
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["peak_price"] = float(r["peak_price"]) if r.get("peak_price") is not None else None
        r["target_pct"] = float(r["target_pct"])
        r["drawdown_pct"] = float(r["drawdown_pct"]) if r.get("drawdown_pct") else None
        cfg = r.get("config")
        r["config"] = cfg if isinstance(cfg, dict) else (json.loads(cfg or "{}"))
        if r.get("triggered_at"):
            r["triggered_at"] = r["triggered_at"].isoformat(timespec="seconds")
    return rows


@app.get("/api/position-strategies")
def list_position_strategies():
    return _position_strategies_rows()


@app.post("/api/holdings/{code}/strategies")
def add_position_strategy(code: str, body: PositionStrategyIn):
    """给持仓整仓挂策略。要求：当前有持股、策略存在、同股同策略不重复。"""
    normalized = _normalize_code(code)
    held = {p["code"]: p for p in _derive_holdings()}
    pos = held.get(normalized)
    if not pos or pos["net_shares"] <= 0:
        raise HTTPException(400, f"{normalized} 当前无持股，不能挂策略")
    with LOCK, get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, name FROM sa_strategies WHERE id = %s", (body.strategy_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, f"止盈策略 #{body.strategy_id} 不存在")
        try:
            cur.execute(
                "INSERT INTO sa_position_strategies (code, strategy_id) VALUES (%s,%s)",
                (normalized, body.strategy_id))
        except psycopg2.IntegrityError:
            raise HTTPException(400, f"该持仓已关联策略「{row[1]}」")
    return {"ok": True, "code": normalized, "strategy_id": body.strategy_id,
            "strategy_name": row[1]}


@app.delete("/api/position-strategies/{ps_id}")
def remove_position_strategy(ps_id: int):
    """解绑持仓级策略（含已触发的，触发历史随关联行一起删除——通知已发过，不留悬档）。"""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM sa_position_strategies WHERE id = %s RETURNING id", (ps_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, f"持仓策略关联 #{ps_id} 不存在")
    return {"ok": True, "removed": ps_id}


def _eval_strategy(kind: str, target_pct: float, drawdown_pct, cfg: dict,
                   base: float, price: float, peak, gain_pct: float,
                   held_days) -> tuple[bool, str, float]:
    """按策略类型判定一次。返回 (hit终结, note_extra, 新峰值)。

    hit=True 表示本轮触发且策略终结（记 triggered_at）；ladder 到档不终结，
    由调用方处理档位推进。peak 未被该类型使用时原样返回。
    """
    if kind == "pct":
        return price >= base * (1 + target_pct / 100), "", peak
    if kind == "stop_loss":
        return price <= base * (1 - target_pct / 100), "", peak
    if kind == "time_stop":
        need = cfg.get("hold_days") or 10**9
        if held_days is not None and held_days >= need:
            return True, f"已持有 {held_days} 个交易日", peak
        return False, "", peak
    if kind in ("drawdown", "trailing"):
        armed = peak is not None or (kind == "trailing" or gain_pct >= target_pct)
        if not armed:
            return False, "", peak
        new_peak = max(price, peak or price)
        dd = (new_peak - price) / new_peak * 100 if new_peak > 0 else 0
        if peak is not None and dd > drawdown_pct:
            return True, f"较峰值 {new_peak:g} 回撤超 {drawdown_pct:g}%", new_peak
        return False, "", new_peak
    return False, "", peak


def _trading_days_between(start: str, end: datetime) -> int:
    """start(YYYY-MM-DD) 到 now 之间经过了多少个交易日（粗算：跳过周末）。"""
    try:
        d0 = datetime.strptime(str(start)[:10], "%Y-%m-%d").date()
    except ValueError:
        return 0
    days, d = 0, d0
    while d < end.date():
        d = d.fromordinal(d.toordinal() + 1)
        if d.weekday() < 5:  # 周末不计（节假日从简）
            days += 1
    return days


def check_strategies_once() -> list[dict]:
    """扫一遍引用了策略且未触发的买入笔，按策略类型分派判定。

    返回本轮触发（或到档）的交易列表。通知在函数尾部统一发送。
    ladder 分批止盈特殊：到档不终结策略，只更新 strategy_ladder_step 并通知该档。
    """
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT t.id AS trade_id, t.code, t.trade_date, t.strategy_id, t.shares, t.price, "
            "t.strategy_peak_price, t.strategy_ladder_step, "
            "s.kind, s.target_pct, s.drawdown_pct, s.config, s.name AS strategy_name "
            "FROM sa_trades t JOIN sa_strategies s ON s.id = t.strategy_id "
            "WHERE t.strategy_triggered_at IS NULL AND t.side = 'buy'")
        pending = [dict(r) for r in cur.fetchall()]
    if not pending:
        return []
    for p in pending:  # NUMERIC/JSONB -> python 类型
        p["strategy_peak_price"] = (float(p["strategy_peak_price"])
                                    if p.get("strategy_peak_price") else None)
        p["target_pct"] = float(p["target_pct"])
        p["drawdown_pct"] = float(p["drawdown_pct"]) if p.get("drawdown_pct") else None
        cfg = p.get("config")
        p["config"] = cfg if isinstance(cfg, dict) else (json.loads(cfg or "{}"))
        p["trade_date_iso"] = p["trade_date"].isoformat() if hasattr(p["trade_date"], "isoformat") \
            else str(p["trade_date"])
    quotes = fetch_quotes(sorted({p["code"] for p in pending}))
    triggered: list[dict] = []
    for p in pending:
        q = quotes.get(p["code"], {})
        price = q.get("price")
        if not isinstance(price, (int, float)) or price <= 0:
            continue
        base = None
        ts = [t for t in _query_trades(p["code"]) if t["id"] == p["trade_id"]]
        if ts and ts[0].get("lot_cost"):
            base = ts[0]["lot_cost"]
        if base is None:
            for h in _derive_holdings():
                if h["code"] == p["code"] and h["net_shares"] > 0 and h["avg_cost"]:
                    base = h["avg_cost"]
                    break
        if not base:
            continue
        gain_pct = (price / base - 1) * 100
        peak = p.get("strategy_peak_price")
        hit, note_extra = False, ""
        kind = p["kind"]

        if kind == "pct":
            hit = price >= base * (1 + p["target_pct"] / 100)
        elif kind == "stop_loss":
            hit = price <= base * (1 - p["target_pct"] / 100)
        elif kind == "time_stop":
            held = _trading_days_between(p["trade_date_iso"], datetime.now())
            if held >= (p["config"].get("hold_days") or 10**9):
                hit, note_extra = True, f"已持有 {held} 个交易日"
        elif kind in ("drawdown", "trailing"):
            # drawdown 有激活线（target_pct=盈利%），trailing 无（买入即跟踪）
            armed = peak is not None or (kind == "trailing"
                                         or gain_pct >= p["target_pct"])
            if armed:
                new_peak = max(price, peak or price)
                dd = (new_peak - price) / new_peak * 100 if new_peak > 0 else 0
                if peak is not None and dd > p["drawdown_pct"]:
                    hit = True
                else:
                    # 未触发也要把峰值推进持久化（跨轮次跟踪最高利润）
                    with get_conn() as conn, conn.cursor() as cur:
                        cur.execute(
                            "UPDATE sa_trades SET strategy_peak_price = %s "
                            "WHERE id = %s AND strategy_triggered_at IS NULL "
                            "AND (strategy_peak_price IS NULL OR strategy_peak_price < %s)",
                            (new_peak, p["trade_id"], new_peak))
                    peak = new_peak
        elif kind == "ladder":
            steps = p["config"].get("steps") or []
            done = p.get("strategy_ladder_step") or 0
            for i, st in enumerate(steps[done:], start=done):
                if price >= base * (1 + float(st["pct"]) / 100):
                    with get_conn() as conn, conn.cursor() as cur:
                        cur.execute(
                            "UPDATE sa_trades SET strategy_ladder_step = %s "
                            "WHERE id = %s AND strategy_triggered_at IS NULL", (i + 1, p["trade_id"]))
                    if cur.rowcount:
                        note_extra = (f"到第 {i + 1} 档（涨幅 {float(st['pct']):g}%），"
                                      f"建议卖出 {float(st['ratio']) * 100:g}% 仓位")
                        p["ladder_hit"] = {"step": i + 1, "ratio": float(st["ratio"])}
                        p["note_extra"] = note_extra
                        triggered.append({**p, "triggered_price": price, "cost": base,
                                          "peak_price": peak})
                    if i + 1 >= len(steps):  # 最后一档：终结策略
                        with get_conn() as conn, conn.cursor() as cur:
                            cur.execute(
                                "UPDATE sa_trades SET strategy_triggered_at = now() "
                                "WHERE id = %s AND strategy_triggered_at IS NULL", (p["trade_id"],))
                        hit = True
                    break
            continue  # ladder 已自行 append，不走统一的 hit 分支
        if hit:
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE sa_trades SET strategy_triggered_at = now() "
                    "WHERE id = %s AND strategy_triggered_at IS NULL", (p["trade_id"],))
            if cur.rowcount:
                p["triggered_price"] = price
                p["cost"] = base
                p["peak_price"] = peak
                triggered.append({**p, "note_extra": note_extra})
    if triggered:
        names = _stock_names(sorted({t["code"] for t in triggered}))
        lines = []
        for t in triggered:
            nm = names.get(t["code"], t["code"])
            cost = t["cost"]
            extra = t.get("note_extra") or ""
            if t["kind"] == "stop_loss":
                lines.append(f"• {nm}（{t['code']}）现价 {t['triggered_price']:g} "
                             f"跌破止损线（成本 {cost:g} 的 -{float(t['target_pct']):g}%），"
                             f"交易 #{t['trade_id']}（{t['shares']}股）{extra}")
            elif t["kind"] in ("drawdown", "trailing"):
                lines.append(f"• {nm}（{t['code']}）现价 {t['triggered_price']:g}，"
                             f"较峰值 {t['peak_price']:g} 回撤超 {float(t['drawdown_pct']):g}%"
                             f"（买入成本 {cost:g}），交易 #{t['trade_id']}（{t['shares']}股）{extra}")
            elif t["kind"] == "ladder":
                lh = t.get("ladder_hit") or {}
                lines.append(f"• {nm}（{t['code']}）现价 {t['triggered_price']:g}，"
                             f"{extra}（成本 {cost:g}），交易 #{t['trade_id']}（{t['shares']}股）")
            elif t["kind"] == "time_stop":
                lines.append(f"• {nm}（{t['code']}）{extra}（成本 {cost:g}，现价 {t['triggered_price']:g}），"
                             f"交易 #{t['trade_id']}（{t['shares']}股），请复盘是否离场")
            else:  # pct
                lines.append(f"• {nm}（{t['code']}）现价 {t['triggered_price']:g} ≥ "
                             f"买入成本 {cost:g} × (1+{float(t['target_pct']):g}%)，"
                             f"交易 #{t['trade_id']}（{t['shares']}股 @ {float(t['price']):g}）{extra}")
        try:
            notifier.notify(
                "🎯 止盈/止损触发提醒",
                f"以下买入笔触发策略条件，请处理：\n\n" + "\n".join(lines) + "\n\n"
                f"（来自 stock-advisor 策略监控，触发时间 "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}）")
        except Exception as exc:
            print(f"[strategy] 触发通知失败: {exc}", flush=True)
    return triggered


def check_position_strategies_once() -> list[dict]:
    """扫一遍持仓级（整仓）策略关联，判定逻辑与逐笔一致，基准=整仓摊薄成本。

    触发状态（peak_price/ladder_step/triggered_at）记在 sa_position_strategies 行上。
    已清仓的股票跳过判定（关联保留，回补后自动继续跟踪；列表里会标记）。
    """
    pending = [r for r in _position_strategies_rows() if r["triggered_at"] is None]
    if not pending:
        return []
    positions = {p["code"]: p for p in _derive_holdings() if p["net_shares"] > 0}
    quotes = fetch_quotes(sorted({r["code"] for r in pending}))
    triggered: list[dict] = []
    for r in pending:
        pos = positions.get(r["code"])
        price = quotes.get(r["code"], {}).get("price")
        if not pos or not isinstance(price, (int, float)) or price <= 0:
            continue
        base = pos["avg_cost"]
        if not base:
            continue
        gain_pct = (price / base - 1) * 100
        held_days = _trading_days_between(str(pos["first_date"]), datetime.now()) \
            if pos.get("first_date") else None
        kind, peak = r["kind"], r["peak_price"]

        if kind == "ladder":
            steps = r["config"].get("steps") or []
            done = r.get("ladder_step") or 0
            for i, st in enumerate(steps[done:], start=done):
                if price >= base * (1 + float(st["pct"]) / 100):
                    with get_conn() as conn, conn.cursor() as cur:
                        cur.execute(
                            "UPDATE sa_position_strategies SET ladder_step = %s "
                            "WHERE id = %s AND triggered_at IS NULL", (i + 1, r["id"]))
                    if cur.rowcount:
                        triggered.append({**r, "triggered_price": price, "cost": base,
                                          "shares": pos["net_shares"], "peak_price": peak,
                                          "ladder_hit": {"step": i + 1},
                                          "note_extra": (f"到第 {i + 1} 档（涨幅 "
                                                         f"{float(st['pct']):g}%），建议卖出 "
                                                         f"{float(st['ratio']) * 100:g}% 仓位")})
                    if i + 1 >= len(steps):  # 最后一档：终结
                        with get_conn() as conn, conn.cursor() as cur:
                            cur.execute("UPDATE sa_position_strategies SET triggered_at = now() "
                                        "WHERE id = %s AND triggered_at IS NULL", (r["id"],))
                    break
            continue
        hit, note_extra, new_peak = _eval_strategy(
            kind, r["target_pct"], r["drawdown_pct"], r["config"],
            base, price, peak, gain_pct, held_days)
        if new_peak != peak and new_peak is not None:  # 峰值推进持久化
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE sa_position_strategies SET peak_price = %s "
                    "WHERE id = %s AND triggered_at IS NULL "
                    "AND (peak_price IS NULL OR peak_price < %s)",
                    (new_peak, r["id"], new_peak))
            peak = new_peak
        if hit:
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute("UPDATE sa_position_strategies SET triggered_at = now() "
                            "WHERE id = %s AND triggered_at IS NULL", (r["id"],))
            if cur.rowcount:
                triggered.append({**r, "triggered_price": price, "cost": base,
                                  "shares": pos["net_shares"], "peak_price": peak,
                                  "note_extra": note_extra})
    if triggered:
        lines = []
        for t in triggered:
            tag = f"{t['strategy_name']}（{t['code']}，整仓 {t['shares']}股）"
            lines.append(f"• {tag} 现价 {t['triggered_price']:g}，"
                         f"成本 {t['cost']:g}，{t.get('note_extra') or '触发条件达成，请处理'}")
        try:
            notifier.notify(
                "🎯 持仓策略触发提醒",
                "以下持仓的整仓策略触发条件，请处理：\n\n" + "\n".join(lines) + "\n\n"
                f"（来自 stock-advisor 策略监控，触发时间 "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}）")
        except Exception as exc:
            print(f"[strategy] 持仓策略通知失败: {exc}", flush=True)
    return triggered


def _strategy_loop():
    """止盈监控守护线程：交易时段（9:15-15:05 工作日）每 60s 扫一次，其余时段 5 分钟一扫。"""
    while True:
        try:
            now = datetime.now()
            is_weekday = now.weekday() < 5
            in_session = is_weekday and (
                now.hour > 9 or (now.hour == 9 and now.minute >= 15)
            ) and (now.hour < 15 or (now.hour == 15 and now.minute <= 5))
            if in_session:
                check_strategies_once()
                check_position_strategies_once()
                time.sleep(60)
            else:
                check_position_strategies_once()  # 非交易时段也扫（time_stop/补触发）
                time.sleep(300)
        except Exception as exc:
            print(f"[strategy] loop error: {exc}", flush=True)
            time.sleep(300)


@app.post("/api/strategies/check")
def strategies_check():
    """手动触发一次止盈检查（逐笔 + 整仓；也供 Claude 定时任务调用）。"""
    return {"ok": True,
            "triggered": check_strategies_once() + check_position_strategies_once()}


# ---------------- 提款计划（某日期前提出多少钱，系统给达成路径） ----------------
# 思路：目标金额 - 已提款 = 还需要的现金；拿它对比当前持仓总市值：
#   市值够        -> 直接按建议卖出凑钱（按浮盈排序，优先兑现赚得多的）
#   市值不够      -> 算缺口、所需总收益率、剩余交易日、复合日收益率，分级判定难度
#      （轻松/正常/积极/风险极高/不可能），提醒「要么降目标、要么延日期、要么补本金」
# 每日收盘后检查一次：达标/临近截止(10/5/1交易日)/逾期 推微信，里程碑键记在
# notified JSONB 里防重复轰炸。提款流水（sa_withdrawals）累计记进度。

class PlanIn(BaseModel):
    target_date: str = Field(description="截止日期 YYYY-MM-DD")
    target_amount: float = Field(gt=0, description="目标提款金额（元）")
    note: str = Field(default="", max_length=255)


class WithdrawalIn(BaseModel):
    amount: float = Field(gt=0, description="本次提款金额（元）")
    wd_date: str = Field(default="", description="提款日期 YYYY-MM-DD，空=今天")
    note: str = Field(default="", max_length=255)


def _trading_days_until(end_date: str) -> int:
    """今天到 end_date(YYYY-MM-DD) 之间还剩多少个交易日（粗算跳过周末，含当日不算）。"""
    try:
        d1 = datetime.strptime(str(end_date)[:10], "%Y-%m-%d").date()
    except ValueError:
        return 0
    today = datetime.now().date()
    days, d = 0, today
    while d < d1:
        d = d.fromordinal(d.toordinal() + 1)
        if d.weekday() < 5:
            days += 1
    return days


def _sell_suggestion(held: list[dict], need: float) -> list[dict]:
    """按浮盈收益率降序给出「卖哪些、卖多少」凑钱建议，直到凑满 need（人民币口径）。

    need 与累计都用 *_cny；港股回笼资金同时给出原币（HKD）金额。
    """
    ranked = sorted(held, key=lambda h: (h.get("pnl_pct") is None,
                                         -(h.get("pnl_pct") or 0)))
    out, acc = [], 0.0
    for h in ranked:
        if acc >= need:
            break
        mv_cny = h.get("market_value_cny", h["market_value"])
        take = min(mv_cny, need - acc)
        rate = h.get("fx_rate") or 1.0
        out.append({"code": h["code"], "name": h["name"],
                    "pnl_pct": h.get("pnl_pct"), "price": h.get("price"),
                    "currency": "HKD" if rate != 1.0 else "CNY",
                    "sell_value": round(take, 2),                    # 折人民币
                    "sell_value_hkd": round(take / rate, 2) if rate != 1.0 else None,
                    "sell_shares": int(take / rate / h["price"]) if h.get("price") else None,
                    "held_value": mv_cny})
        acc += take
    return out


def _plan_view(plan: dict, held_mv: float, held: list[dict]) -> dict:
    """把一个计划行 + 当前持仓汇总成「达成路径」视图（列表/详情共用）。"""
    need = float(plan["target_amount"]) - plan["withdrawn"]   # 还需要提的钱
    gap = round(need - held_mv, 2)                           # >0 = 市值不够
    tdays = _trading_days_until(plan["target_date"])
    deadline_passed = plan["target_date"] < datetime.now().strftime("%Y-%m-%d")
    view = {**{k: plan[k] for k in ("id", "target_date", "target_amount", "note", "status")},
            "withdrawn": plan["withdrawn"], "need_now": round(need, 2),
            "holdings_mv": round(held_mv, 2), "gap": gap,
            "trading_days_left": tdays, "deadline_passed": deadline_passed,
            "required_total_pct": None, "required_daily_pct": None,
            "difficulty": None, "sell_plan": []}
    if need <= 0:
        view["difficulty"] = "✅ 已完成"
        return view
    if held_mv <= 0:
        view["difficulty"] = "🚫 无持仓可变现"
        return view
    need_ratio = need / held_mv
    view["required_total_pct"] = round((need_ratio - 1) * 100, 2)
    if need_ratio <= 1:  # 市值够：直接卖就行
        view["difficulty"] = "💰 现在就能提"
        view["required_daily_pct"] = 0.0
        view["sell_plan"] = _sell_suggestion(held, need)
        return view
    # 市值不够：需要组合整体涨 need_ratio-1。按剩余交易日折算复合日收益率
    if tdays <= 0:
        view["difficulty"] = "⛔ 已逾期，市值仍不足"
        return view
    r = need_ratio ** (1 / tdays) - 1
    view["required_daily_pct"] = round(r * 100, 4)
    tot = view["required_total_pct"]
    if tot <= 10:
        view["difficulty"] = "🟢 轻松（涨幅要求 ≤10%）"
    elif tot <= 30:
        view["difficulty"] = "🟡 正常（需涨 ≤30%，一两个月牛熊转换级别）"
    elif tot <= 100:
        view["difficulty"] = "🟠 积极（需翻倍以内，要踩对主线）"
    elif tdays < 60:
        view["difficulty"] = "🔴 风险极高（短期翻倍以上不现实，建议降目标/延日期/补本金）"
    else:
        view["difficulty"] = "🟠 积极（时间换空间，需持续复利）"
    return view


def _query_plans(withdrawn_map: dict | None = None) -> list[dict]:
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM sa_withdrawal_plans ORDER BY status, target_date, id")
        rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["target_date"] = r["target_date"].isoformat()
        r["target_amount"] = float(r["target_amount"])
        r["withdrawn"] = (withdrawn_map or {}).get(r["id"], 0.0)
        cfg = r.get("notified")
        r["notified"] = cfg if isinstance(cfg, dict) else (json.loads(cfg or "{}"))
    return rows


def _withdrawn_totals() -> dict[int, float]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT plan_id, COALESCE(SUM(amount),0) FROM sa_withdrawals "
                    "GROUP BY plan_id")
        return {pid: float(v) for pid, v in cur.fetchall()}


def _active_held():
    """当前持仓（有股且有有效市值）：返回 held 列表与总市值（统一人民币口径）。"""
    held = [p for p in _holdings_with_pnl(_derive_holdings())
            if p["net_shares"] > 0 and p.get("market_value_cny", p["market_value"]) > 0]
    return held, sum(p.get("market_value_cny", p["market_value"]) for p in held)


@app.get("/api/plans")
def list_plans():
    held, mv = _active_held()
    return [_plan_view(p, mv, held) for p in _query_plans(_withdrawn_totals())]


@app.post("/api/plans")
def create_plan(plan: PlanIn):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", plan.target_date.strip()):
        raise HTTPException(400, "截止日期格式应为 YYYY-MM-DD")
    if plan.target_date < datetime.now().strftime("%Y-%m-%d"):
        raise HTTPException(400, "截止日期不能是过去的日期")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sa_withdrawal_plans (target_date, target_amount, note) "
            "VALUES (%s,%s,%s) RETURNING id",
            (plan.target_date, plan.target_amount, plan.note.strip()))
        pid = cur.fetchone()[0]
    return {"ok": True, "id": pid}


@app.delete("/api/plans/{plan_id}")
def delete_plan(plan_id: int):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM sa_withdrawal_plans WHERE id = %s RETURNING id", (plan_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, f"计划 #{plan_id} 不存在")  # 提款流水随外键级联删
    return {"ok": True, "removed": plan_id}


@app.post("/api/plans/{plan_id}/withdrawals")
def add_withdrawal(plan_id: int, w: WithdrawalIn):
    """记一笔已提款。提满目标金额自动把计划置为 done。"""
    wd_date = w.wd_date.strip() or datetime.now().strftime("%Y-%m-%d")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", wd_date):
        raise HTTPException(400, "提款日期格式应为 YYYY-MM-DD")
    with LOCK, get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, target_amount FROM sa_withdrawal_plans WHERE id = %s",
                    (plan_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, f"计划 #{plan_id} 不存在")
        cur.execute("INSERT INTO sa_withdrawals (plan_id, wd_date, amount, note) "
                    "VALUES (%s,%s,%s,%s) RETURNING id",
                    (plan_id, wd_date, w.amount, w.note.strip()))
        wid = cur.fetchone()[0]
        cur.execute("SELECT COALESCE(SUM(amount),0) FROM sa_withdrawals WHERE plan_id = %s",
                    (plan_id,))
        total = float(cur.fetchone()[0])
        if total >= float(row[1]):
            cur.execute("UPDATE sa_withdrawal_plans SET status = 'done' WHERE id = %s",
                        (plan_id,))
    return {"ok": True, "id": wid, "withdrawn_total": total,
            "completed": total >= float(row[1])}


@app.get("/api/plans/{plan_id}/detail")
def plan_detail(plan_id: int):
    """计划详情：达成路径 + 卖出凑钱建议 + 提款流水。"""
    totals = _withdrawn_totals()
    plans = [p for p in _query_plans(totals) if p["id"] == plan_id]
    if not plans:
        raise HTTPException(404, f"计划 #{plan_id} 不存在")
    held, mv = _active_held()
    view = _plan_view(plans[0], mv, held)
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT id, wd_date, amount, note FROM sa_withdrawals "
                    "WHERE plan_id = %s ORDER BY wd_date DESC, id DESC", (plan_id,))
        view["withdrawals"] = [
            {**{k: r[k] for k in ("id", "note")},
             "wd_date": r["wd_date"].isoformat(), "amount": float(r["amount"])}
            for r in cur.fetchall()]
    return view


def check_withdrawal_once(notify: bool = True, auto_ai: bool = False) -> list[dict]:
    """每日盘后跑一次：给每个 active 计划算达标状态，推里程碑提醒。

    里程碑键：ready（现在就能提）/ d10 d5 d1（剩余交易日首次 ≤N）/ deadline（逾期）。
    同键只推一次（记在 notified JSONB）；「现在就能提」回落后再次达标会重新推
    （键带日期后缀）。auto_ai=True 时（后台盘后循环）对触发了里程碑的计划
    自动补一条 LLM 提款建议（llm.auto_advice 开启 + 当日未生成过才跑）。
    """
    alerts: list[dict] = []
    totals = _withdrawn_totals()
    held, mv = _active_held()
    today = datetime.now().strftime("%Y-%m-%d")
    for p in _query_plans(totals):
        if p["status"] != "active":
            continue
        v = _plan_view(p, mv, held)
        milestones = []
        if v["need_now"] <= 0:
            milestones.append(("done", "已提满目标金额，计划完成"))
        elif v["gap"] <= 0:
            milestones.append((f"ready:{today}",
                               f"市值已够（{v['holdings_mv']:,.0f} ≥ {v['need_now']:,.0f}），可着手卖出提款"))
        tleft = v["trading_days_left"]
        if not v["deadline_passed"] and v["gap"] > 0:
            for n in (10, 5, 1):
                if tleft <= n:
                    milestones.append((f"d{n}", f"距截止仅剩 {tleft} 个交易日且市值不足"))
                    break
        if v["deadline_passed"]:
            milestones.append(("deadline", "已过截止日仍未提满"))
        for key, msg in milestones:
            if p["notified"].get(key):
                continue
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE sa_withdrawal_plans SET notified = notified || %s::jsonb "
                    "WHERE id = %s",
                    (json.dumps({key: today}), p["id"]))
            alerts.append({"plan_id": p["id"], "milestone": key, "message": msg, **v})
            if notify:
                try:
                    notifier.notify(
                        "🏧 提款计划提醒",
                        f"计划 #{p['id']}（{p['target_date']} 前提取 "
                        f"{p['target_amount']:,.0f} 元）\n\n• {msg}\n"
                        f"• 还需 {v['need_now']:,.0f} 元，当前持仓市值 {v['holdings_mv']:,.0f} 元\n"
                        f"• 状态：{v['difficulty'] or '—'}"
                        + (f"\n• 需涨 {v['required_total_pct']:g}%（剩 {tleft} 个交易日）"
                           if v.get("required_total_pct") and v["gap"] > 0 else ""))
                except Exception as exc:
                    print(f"[withdrawal] 通知失败: {exc}", flush=True)
    if auto_ai and alerts:
        _auto_plan_advice(alerts)
    return alerts


def _auto_plan_advice(alerts: list[dict]) -> None:
    """盘后里程碑触发后自动补 LLM 建议（llm.auto_advice 开关 + 当日去重）。

    去重用计划行的 notified JSONB（ai:{plan_id}:{date} 键），不另建状态文件。
    每个计划独立 try/except：一个失败（无 key / API 超时）不影响其余与主流程。
    """
    try:
        conf = llm_advisor.load_llm_conf()
    except Exception:
        return
    if not (conf.get("enabled") and conf.get("auto_advice")):
        return
    today = datetime.now().strftime("%Y-%m-%d")
    done: set[int] = set()
    for a in alerts:
        pid = a["plan_id"]
        if pid in done:
            continue
        done.add(pid)
        try:
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT notified -> %s FROM sa_withdrawal_plans WHERE id = %s",
                            (f"ai:{today}", pid))
                row = cur.fetchone()
            if not row or row[0] is None:
                generate_plan_advice(pid, auto=True)
                with get_conn() as conn, conn.cursor() as cur:
                    cur.execute("UPDATE sa_withdrawal_plans "
                                "SET notified = notified || %s::jsonb WHERE id = %s",
                                (json.dumps({f"ai:{today}": datetime.now().isoformat(
                                    timespec="seconds")}), pid))
                print(f"[withdrawal] AI 建议已生成（计划 #{pid}）", flush=True)
        except Exception as exc:
            print(f"[withdrawal] AI 建议生成失败（计划 #{pid}）: {exc}", flush=True)


def _withdrawal_loop():
    """提款计划守护线程：交易日 check_time（默认 15:10，可网页改）之后每半小时查一次
    达标/临期状态（当日只推一次）。"""
    while True:
        try:
            now = datetime.now()
            ch, cm = _sched_time("withdrawal", "check_time")
            if (_conf_enabled("withdrawal", True)
                    and now.weekday() < 5
                    and (now.hour, now.minute) >= (ch, cm)):
                check_withdrawal_once(auto_ai=True)
            time.sleep(1800)
        except Exception as exc:
            print(f"[withdrawal] loop error: {exc}", flush=True)
            time.sleep(300)


@app.post("/api/plans/check")
def plans_check():
    """手动跑一次提款计划检查（也供 Claude 定时任务调用）。"""
    return {"ok": True, "alerts": check_withdrawal_once()}


# ---------------- LLM 提款建议（llm_advisor.py：Claude API，配置在 config.yaml llm 段） ----------------

import llm_advisor


class LLMConfIn(BaseModel):
    enabled: bool | None = None
    api_key: str | None = None      # 留空/不传 = 不修改已存的 key
    base_url: str | None = None
    model: str | None = None
    auto_advice: bool | None = None


def _plan_market_note() -> str:
    """给模型的市况一句话：大盘量能 + 指数涨跌 + 涨停家数（失败静默降级）。"""
    parts = []
    try:
        mv = market_volume_status()
        if mv.get("overall_ratio") is not None:
            parts.append(f"大盘量能{mv['overall_label']}（均量比 {mv['overall_ratio']}x）")
    except Exception:
        pass
    try:
        ov = sector_mod.build_overview(with_details=False)
        idx = [f"{i['name']} {i['pct']:+.2f}%" for i in (ov.get("indexes") or [])
               if isinstance(i.get("pct"), (int, float))]
        if idx:
            parts.append("、".join(idx[:4]))
        zt_total = (ov.get("zt") or {}).get("total")
        breadth = ov.get("breadth") or {}
        if zt_total:
            parts.append(f"涨停 {zt_total} 家")
        if breadth.get("up") is not None and breadth.get("down") is not None:
            parts.append(f"涨跌家数 {breadth['up']}:{breadth['down']}")
    except Exception:
        pass
    return "；".join(parts)


def _plan_recent_news_titles(held: list[dict]) -> list[str]:
    """持仓股近 3 天新闻标题（最多 12 条，去重），给模型判断消息面。"""
    codes = [h["code"] for h in held][:12]
    if not codes:
        return []
    since = datetime.now().isoformat(timespec="seconds")
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT ON (n.url) n.title FROM sa_news n "
                "LEFT JOIN sa_news_related r ON r.url = n.url "
                "WHERE (n.code = ANY(%s) OR r.code = ANY(%s)) "
                "AND COALESCE(n.publish_time, n.fetched_at) > now() - interval '3 days' "
                "ORDER BY n.url, COALESCE(n.publish_time, n.fetched_at) DESC LIMIT 12",
                (codes, codes))
            return [r[0] for r in cur.fetchall()]
    except Exception:
        return []


def _advice_watch_lines() -> list[str]:
    """自选观察池（排除已持仓）格式化行：行情快照 + 市值 + 量能 + 行业板块。"""
    held_codes = {p["code"] for p in _derive_holdings() if p["net_shares"] > 0}
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT code, name, note FROM sa_watchlist")
        rows = [r for r in cur.fetchall() if r[0] not in held_codes]
    if not rows:
        return []
    quotes = fetch_quotes([r[0] for r in rows])
    try:
        vols = watchlist_volume_status([r[0] for r in rows])
    except Exception:
        vols = {}
    try:
        boards = sector_mod.stock_board_exposure([r[0] for r in rows])
    except Exception:
        boards = {}
    lines = []
    for code, name, note in rows:
        q = quotes.get(code) or {}
        if q.get("error") or not q.get("price"):
            continue
        vol = (vols.get(code) or {}).get("label") or ""
        ind = (boards.get(code) or {}).get("industry") or ""
        bits = [f"- {name or q.get('name', code)}（{code}）：现价 {q['price']:g} "
                f"{signed_pct_str(q.get('change_pct'))}，总市值 {q.get('market_cap') or '—'}亿"
                f" {q.get('currency', '')}"]
        if vol:
            bits.append(f"量能{vol}")
        if ind:
            bits.append(f"行业:{ind}")
        if note:
            bits.append(f"备注:{note}")
        lines.append("，".join(bits))
    return lines


def signed_pct_str(v) -> str:
    return f"（{v:+.2f}%）" if isinstance(v, (int, float)) else ""


def _advice_sector_lines(held_codes: list[str]) -> list[str]:
    """板块轮动摘要：当日评分前 5 板块 + 持仓股的行业归属（判断卖出时点/买入方向）。"""
    lines = []
    try:
        ov = sector_mod.build_overview(with_details=False)
        for b in (ov.get("boards") or [])[:5]:
            mom = f"，3日{b['mom3']:+.1f}%" if b.get("mom3") is not None else ""
            lines.append(f"- 轮动评分前5：{b['name']}（评分{b['score']}，"
                         f"今日{b['pct']:+.2f}%{mom}）" if isinstance(b.get('pct'), (int, float))
                         else f"- 轮动评分前5：{b['name']}（评分{b['score']}）")
    except Exception:
        pass
    try:
        exp = sector_mod.stock_board_exposure(held_codes)
        for code, info in exp.items():
            if info.get("industry"):
                lines.append(f"- {code} 所属行业板块：{info['industry']}")
    except Exception:
        pass
    return lines


def _advice_event_lines(codes: list[str]) -> list[str]:
    """未来 21 天自选+持仓的解禁/增发事件行。"""
    try:
        evs = alerts_mod.upcoming_events(codes, days=21)
    except Exception:
        return []
    return [f"- {'解禁' if e['type'] == 'lift' else '增发新股上市'} {e['event_date']} "
            f"{e['name']}（{e['code']}）{e['shares_yi']}亿股/约{e['cap_yi']}亿元 {e['detail']}"
            for e in evs[:10]]


def generate_plan_advice(plan_id: int, auto: bool = False) -> dict:
    """组上下文 -> 调 Claude -> 存 sa_plan_advice。返回存好的行。失败抛 HTTPException。"""
    totals = _withdrawn_totals()
    plans = [p for p in _query_plans(totals) if p["id"] == plan_id]
    if not plans:
        raise HTTPException(404, f"计划 #{plan_id} 不存在")
    held, mv = _active_held()
    view = _plan_view(plans[0], mv, held)
    conf = llm_advisor.load_llm_conf()
    if not conf["enabled"]:
        raise HTTPException(400, "LLM 建议未启用（通知/提款页的 AI 配置里打开开关并填 API key）")
    # 量能信息对凑钱顺序有用：按持仓码批量取（失败不阻塞）
    try:
        vols = watchlist_volume_status([h["code"] for h in held])
        for h in held:
            h["vol_ratio"] = (vols.get(h["code"]) or {}).get("ratio")
    except Exception:
        pass
    held_codes = [h["code"] for h in held]
    # 融合全系统信息：自选观察池（买入候选）/ 板块轮动 / 解禁增发事件，全部失败降级不阻塞
    try:
        watch_lines = _advice_watch_lines()
    except Exception:
        watch_lines = []
    try:
        sector_lines = _advice_sector_lines(held_codes)
    except Exception:
        sector_lines = []
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT code FROM sa_watchlist")
            all_codes = sorted(held_codes + [r[0] for r in cur.fetchall()])
        event_lines = _advice_event_lines(all_codes)
    except Exception:
        event_lines = []
    context = llm_advisor.build_context_text(
        view, held, _plan_recent_news_titles(held), _plan_market_note(),
        watch_lines=watch_lines, event_lines=event_lines, sector_lines=sector_lines)
    try:
        content = llm_advisor.ask_advice(context, conf)
    except RuntimeError as exc:
        raise HTTPException(502, str(exc))
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sa_plan_advice (plan_id, content, model, auto) "
            "VALUES (%s,%s,%s,%s) RETURNING id, created_at",
            (plan_id, content, conf["model"], auto))
        aid, created = cur.fetchone()
    return {"ok": True, "id": aid, "plan_id": plan_id, "model": conf["model"],
            "auto": auto, "created_at": created.isoformat(timespec="seconds"),
            "content": content}


@app.post("/api/plans/{plan_id}/advice")
def plan_advice_generate(plan_id: int):
    """手动生成一条 AI 提款建议（前端按钮触发，约 10-60s）。"""
    return generate_plan_advice(plan_id)


@app.get("/api/plans/{plan_id}/advice")
def plan_advice_latest(plan_id: int):
    """取某计划最新一条 AI 建议（无则 content=None，前端显示「生成建议」按钮）。"""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, content, model, auto, created_at FROM sa_plan_advice "
                    "WHERE plan_id = %s ORDER BY created_at DESC, id DESC LIMIT 1",
                    (plan_id,))
        row = cur.fetchone()
    if not row:
        return {"content": None}
    return {"id": row[0], "content": row[1], "model": row[2], "auto": row[3],
            "created_at": row[4].isoformat(timespec="seconds")}


@app.get("/api/plans/{plan_id}/advice/history")
def plan_advice_history(plan_id: int, limit: int = 20):
    """某计划的 AI 建议历史（新→旧）。每次生成都存一行，页面默认只显示最新一条。"""
    limit = max(1, min(limit, 100))
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, model, auto, created_at, left(content, 400) AS brief "
                    "FROM sa_plan_advice WHERE plan_id = %s "
                    "ORDER BY created_at DESC, id DESC LIMIT %s", (plan_id, limit))
        rows = cur.fetchall()
    return [{"id": r[0], "model": r[1], "auto": r[2],
             "created_at": r[3].isoformat(timespec="seconds"), "brief": r[4]}
            for r in rows]


@app.get("/api/llm/config")
def llm_get_config():
    conf = llm_advisor.load_llm_conf()
    conf.pop("api_key")  # 不回显 key 本体
    conf.update(llm_advisor.mask_key(llm_advisor.load_llm_conf()["api_key"]))
    return conf


@app.put("/api/llm/config")
def llm_update_config(body: LLMConfIn):
    conf = llm_advisor.load_llm_conf()
    if body.enabled is not None:
        conf["enabled"] = body.enabled
    if body.api_key:                      # 空串视为不修改
        conf["api_key"] = body.api_key.strip()
    if body.base_url is not None:
        conf["base_url"] = body.base_url.strip()
    if body.model:
        conf["model"] = body.model.strip()
    if body.auto_advice is not None:
        conf["auto_advice"] = body.auto_advice
    llm_advisor.save_llm_conf(conf)
    return llm_get_config()


# ---------------- 财经日历（econ_calendar.py：规则事件 + 手动事件 + 盘前提醒） ----------------

import econ_calendar


class CalendarEventIn(BaseModel):
    event_date: str = Field(description="日期 YYYY-MM-DD")
    title: str = Field(min_length=1, max_length=128, description="事件名，如：美联储议息 FOMC")
    time_hint: str = Field(default="", max_length=16, description="北京时间提示，如 02:00")
    note: str = Field(default="", max_length=255)


@app.get("/api/calendar")
def calendar_upcoming(months: int = 3):
    """未来 N 个月的财经日历（规则事件自动生成 + 手动事件合并，按日期排序）。"""
    months = max(1, min(months, 6))
    with get_conn() as conn:
        return {"events": econ_calendar.upcoming_events(conn, months=months)}


@app.post("/api/calendar/events")
def calendar_add_event(e: CalendarEventIn):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", e.event_date.strip()):
        raise HTTPException(400, "日期格式应为 YYYY-MM-DD")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sa_calendar_events (event_date, time_hint, title, note) "
            "VALUES (%s,%s,%s,%s) RETURNING id",
            (e.event_date, e.time_hint.strip(), e.title.strip(), e.note.strip()))
        return {"ok": True, "id": cur.fetchone()[0]}


@app.delete("/api/calendar/events/{event_id}")
def calendar_del_event(event_id: int):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM sa_calendar_events WHERE id = %s RETURNING id", (event_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, f"事件 #{event_id} 不存在")
    return {"ok": True, "removed": event_id}


@app.post("/api/calendar/check")
def calendar_check():
    """手动跑一次日历盘前提醒（同键只推一次，重复调用不会轰炸）。"""
    with get_conn() as conn:
        return {"alerts": econ_calendar.check_calendar_once(conn, notify_fn=notifier.notify)}


def _calendar_loop():
    """财经日历守护线程：交易日 start_time–end_time 窗口（默认 8:00–12:00，可网页改）
    内每 5 分钟查一次（同键只推一次，实际每天只会推一条今明事件/重要预告），盘前及时推微信。"""
    while True:
        try:
            now = datetime.now()
            sh, sm = _sched_time("calendar", "start_time")
            eh, em = _sched_time("calendar", "end_time")
            if now.weekday() < 5 and (now.hour, now.minute) >= (sh, sm) \
                    and (now.hour, now.minute) < (eh, em) \
                    and _conf_enabled("calendar", True):
                with get_conn() as conn:
                    econ_calendar.check_calendar_once(conn, notify_fn=notifier.notify)
            time.sleep(300)
        except Exception as exc:
            print(f"[calendar] loop error: {exc}", flush=True)
            time.sleep(600)


def _conf_enabled(section: str, default: bool) -> bool:
    """config.yaml 某段的 enabled 开关读取（calendar/withdrawal 等轻量段共用）。"""
    try:
        import yaml
        data = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        return bool((data.get(section) or {}).get("enabled", default))
    except Exception:
        return default


# ---------------- 自选股事件告警（限售解禁 / 增发上市，见 alerts.py） ----------------

import alerts as alerts_mod


def _conf_section(section: str) -> dict:
    try:
        import yaml
        data = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        return data.get(section) or {}
    except Exception:
        return {}


# ---------------- 调度设置（config.yaml schedule 段 + 各模块抓取周期，网页可改） ----------------

def _parse_hhmm(raw: str) -> tuple[int, int] | None:
    """'HH:MM' → (h, m)；格式非法返回 None。"""
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(raw or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def _sched_time(group: str, key: str) -> tuple[int, int]:
    """读 schedule.<group>.<key> 的 HH:MM。缺段/缺键/坏值一律回落默认值。"""
    raw = (_conf_section("schedule").get(group) or {}).get(key)
    parsed = _parse_hhmm(raw)
    if parsed:
        return parsed
    fallback = _parse_hhmm(DEFAULT_SCHEDULE.get(group, {}).get(key, "00:00"))
    return fallback or (0, 0)


def _load_schedule_conf() -> dict:
    """schedule 段 + 各模块抓取周期的合并视图（GET /api/schedule 用）。"""
    section = _conf_section("schedule")
    merged = {}
    for group, defaults in DEFAULT_SCHEDULE.items():
        merged[group] = {**defaults, **(section.get(group) or {})}
    intervals = {}
    for name in DEFAULT_INTERVALS:
        conf = _conf_section(name)
        defaults = DEFAULT_INTERVALS[name]
        ikey = _interval_key(name)
        intervals[name] = {
            # news 没有真身 enabled（0 分钟即关闭），用周期反推一个给 UI 显示
            "enabled": (int(conf.get(ikey, defaults["interval_minutes"]) or 0) > 0
                        if name in INTERVAL_NO_ENABLED
                        else bool(conf.get("enabled", defaults["enabled"]))),
            "has_enabled": name not in INTERVAL_NO_ENABLED,
            "key": ikey,
            "interval_minutes": int(conf.get(ikey, defaults["interval_minutes"]) or 0),
        }
    return {"schedule": merged, "intervals": intervals}


def _render_schedule_block(schedule: dict) -> str:
    """把 schedule 段渲染成 config.yaml 文本块（供 conf_util 整段替换）。"""
    def q(v) -> str:
        return "'" + str(v).replace("'", "''") + "'"
    out = ["# 定时任务时间（网页「⚙️ 调度设置」可改，1 分钟内生效，无需重启；本机时间，周一~周五）",
           "# premarket/postmarket_report：盘前简报 premarket-brief.md / 盘后复盘 daily-summary.md",
           "schedule:"]
    for group, defaults in DEFAULT_SCHEDULE.items():
        cur = {**defaults, **(schedule.get(group) or {})}
        out.append(f"  {group}:")
        for key, dv in defaults.items():
            val = cur.get(key, dv)
            if isinstance(val, bool):
                out.append(f"    {key}: {str(val).lower()}")
            else:
                out.append(f"    {key}: {q(val)}")
    return "\n".join(out)


def _render_crypto_block(crypto: dict) -> str:
    c = {**crypto_watch.DEFAULT_CRYPTO_CONF, **(crypto or {})}
    return "\n".join([
        "# 币圈 24h 趋势参考（gate.io 股票永续；欧易 OKX 本机直连不通，此为平替源）",
        "# interval_minutes: 抓价周期，0=关闭；alert_threshold_pct: |24h涨跌| 阈值，推微信",
        "# link_*: 「币↔股」关联统计（日涨跌相关/beta/比价偏离）",
        "# link_alert_dev_pct: 比价偏离超此值(%)推微信，0=关闭；其余 link_* 见 README",
        "crypto:",
        f"  enabled: {str(bool(c['enabled'])).lower()}",
        f"  interval_minutes: {int(c['interval_minutes'])}",
        f"  alert_threshold_pct: {c['alert_threshold_pct']}",
        f"  alert_cooldown_hours: {int(c['alert_cooldown_hours'])}",
        f"  link_enabled: {str(bool(c['link_enabled'])).lower()}",
        f"  link_refresh_hours: {int(c['link_refresh_hours'])}",
        f"  link_lookback_days: {int(c['link_lookback_days'])}",
        f"  link_min_overlap: {int(c['link_min_overlap'])}",
        f"  link_alert_dev_pct: {c['link_alert_dev_pct']}",
        f"  link_alert_cooldown_hours: {int(c['link_alert_cooldown_hours'])}",
    ])


def _render_mp_block(mp: dict) -> str:
    c = {**wechat_mp.DEFAULT_MP_CONF, **(mp or {})}
    return "\n".join([
        "# 微信公众号文章监控：拉上游服务的 RSS 源（推荐 WeRSS，见 README）",
        "# interval_minutes: 拉取周期，0=关闭；notify_new: 有新文章推微信",
        "mp:",
        f"  enabled: {str(bool(c['enabled'])).lower()}",
        f"  interval_minutes: {int(c['interval_minutes'])}",
        f"  notify_new: {str(bool(c['notify_new'])).lower()}",
        f"  max_items: {int(c['max_items'])}",
        f"  keep_days: {int(c['keep_days'])}",
    ])


def _render_x_block(x: dict) -> str:
    c = {**x_monitor.DEFAULT_X_CONF, **(x or {})}
    return "\n".join([
        "# X(Twitter) 指定用户发言监控（twscrape，需 pip install \"twscrape[curl]\"）",
        "# interval_minutes: 抓取周期，0=关闭；notify_new: 有新推文推微信",
        "# include_retweets: 是否收录转发（默认关，噪音大）",
        "x:",
        f"  enabled: {str(bool(c['enabled'])).lower()}",
        f"  interval_minutes: {int(c['interval_minutes'])}",
        f"  notify_new: {str(bool(c['notify_new'])).lower()}",
        f"  max_tweets: {int(c['max_tweets'])}",
        f"  keep_days: {int(c['keep_days'])}",
        f"  include_retweets: {str(bool(c['include_retweets'])).lower()}",
        f"  wait_timeout: {int(c['wait_timeout'])}",
    ])


class ScheduleIn(BaseModel):
    schedule: dict | None = None
    intervals: dict | None = None
    crypto: dict | None = None
    mp: dict | None = None
    x: dict | None = None


# crypto 段里需要按整数校验的键：(键名, 下界, 上界)。上界 1440 = 24 小时上限。
CRYPTO_INT_FIELDS = (
    ("interval_minutes", 0, 1440),
    ("alert_cooldown_hours", 1, 720),
    ("link_refresh_hours", 0, 720),
    ("link_lookback_days", 10, 500),
    ("link_min_overlap", 3, 200),
    ("link_alert_cooldown_hours", 1, 720),
)
# mp / x 段：只有这几个键，其余一律 400（防止前端拼错字段名静默写坏 config）
MP_KEYS = tuple(wechat_mp.DEFAULT_MP_CONF)
X_KEYS = tuple(x_monitor.DEFAULT_X_CONF)
MP_INT_FIELDS = (("interval_minutes", 0, 1440), ("max_items", 1, 100),
                 ("keep_days", 1, 3650), ("timeout", 3, 300))
X_INT_FIELDS = (("interval_minutes", 0, 1440), ("max_tweets", 1, 200),
                ("keep_days", 1, 3650), ("wait_timeout", 1, 300),
                ("max_text", 100, 20000))


@app.get("/api/schedule")
def schedule_get():
    """调度页初始数据：schedule 段（各任务几点做）+ 各模块抓取周期 + 币圈/公众号/X 监控。"""
    data = _load_schedule_conf()
    data["crypto"] = crypto_watch.load_crypto_conf()
    data["mp"] = wechat_mp.load_mp_conf()
    data["x"] = x_monitor.load_x_conf()
    return data


@app.put("/api/schedule")
def schedule_update(body: ScheduleIn):
    """保存调度设置：整段替换 schedule / crypto 段，抓取周期逐键手术式改写
    （各模块段里还有别的键，不能整段替换）。写操作走 conf_util.WRITE_LOCK，
    与 news 段的整文件重写互斥，不会丢段。"""
    ops: list[tuple[str, str, object]] = []
    schedule = body.schedule
    if schedule is not None:
        # 校验：时间字段必须 HH:MM，enabled 必须布尔
        merged = _load_schedule_conf()["schedule"]
        for group, values in schedule.items():
            if group not in DEFAULT_SCHEDULE or not isinstance(values, dict):
                raise HTTPException(400, f"未知的调度分组：{group}")
            for key, val in values.items():
                if key not in DEFAULT_SCHEDULE[group]:
                    raise HTTPException(400, f"{group} 不支持字段 {key}")
                if key == "enabled":
                    merged[group][key] = bool(val)
                elif not _parse_hhmm(val):
                    raise HTTPException(400, f"{group}.{key} 时间格式应为 HH:MM，收到 {val!r}")
                else:
                    merged[group][key] = str(val).strip()
        schedule = merged
    for name, values in (body.intervals or {}).items():
        if name not in DEFAULT_INTERVALS or not isinstance(values, dict):
            raise HTTPException(400, f"未知的抓取模块：{name}")
        ikey = _interval_key(name)
        for key, val in values.items():
            if key == "enabled":
                # news 的真身没有 enabled：勾上就保证周期 ≥1，取消就置 0
                if name in INTERVAL_NO_ENABLED:
                    cur = int((_conf_section(name).get(ikey) or 0))
                    ops.append((name, ikey, max(cur, 1) if val else 0))
                else:
                    ops.append((name, "enabled", bool(val)))
            elif key == "interval_minutes":
                try:
                    minutes = int(val)
                except (TypeError, ValueError):
                    raise HTTPException(400, f"{name}.interval_minutes 必须是整数")
                if not 0 <= minutes <= 1440:
                    raise HTTPException(400, f"{name}.interval_minutes 需在 0~1440 之间")
                ops.append((name, ikey, minutes))
            else:
                raise HTTPException(400, f"{name} 不支持字段 {key}")
    crypto = body.crypto
    if crypto is not None:
        for key in crypto:
            if key not in crypto_watch.DEFAULT_CRYPTO_CONF:
                raise HTTPException(400, f"crypto 不支持字段 {key}")
        crypto = {**crypto_watch.DEFAULT_CRYPTO_CONF,
                  **{k: v for k, v in crypto.items() if v is not None}}
        # 逐个规范化整数键：不校验的话，一个 "abc" 会原样写进 config.yaml，
        # 之后每一轮守护线程 int() 转换都炸，且该模块静默停摆
        for key, lo, hi in CRYPTO_INT_FIELDS:
            if key not in crypto:
                continue
            try:
                val = int(crypto[key])
            except (TypeError, ValueError):
                raise HTTPException(400, f"crypto.{key} 必须是整数")
            if not lo <= val <= hi:
                raise HTTPException(400, f"crypto.{key} 需在 {lo}~{hi} 之间")
            crypto[key] = val
        for key in ("enabled", "link_enabled"):
            if key in crypto:
                crypto[key] = bool(crypto[key])
        for key in ("alert_threshold_pct", "link_alert_dev_pct"):
            if key in crypto:
                crypto[key] = float(crypto[key])

    def _norm_extra(body_obj, defaults, int_fields, section):
        """mp / x 两段结构一样：白名单校验 + 整数规范化。返回 None 表示没传。"""
        if body_obj is None:
            return None
        for key in body_obj:
            if key not in defaults:
                raise HTTPException(400, f"{section} 不支持字段 {key}")
        out = {**defaults, **{k: v for k, v in body_obj.items() if v is not None}}
        for key, lo, hi in int_fields:
            if key in body_obj:
                try:
                    val = int(out[key])
                except (TypeError, ValueError):
                    raise HTTPException(400, f"{section}.{key} 必须是整数")
                if not lo <= val <= hi:
                    raise HTTPException(400, f"{section}.{key} 需在 {lo}~{hi} 之间")
                out[key] = val
        for key in ("enabled", "notify_new", "include_retweets"):
            if key in body_obj:
                out[key] = bool(out[key])
        return out

    mp = _norm_extra(body.mp, wechat_mp.DEFAULT_MP_CONF, MP_INT_FIELDS, "mp")
    xconf = _norm_extra(body.x, x_monitor.DEFAULT_X_CONF, X_INT_FIELDS, "x")
    with conf_util.WRITE_LOCK:
        if schedule is not None:
            conf_util.replace_or_append_section(
                CONFIG_FILE, "schedule", _render_schedule_block(schedule))
        if crypto is not None:
            conf_util.replace_or_append_section(
                CONFIG_FILE, "crypto", _render_crypto_block(crypto))
        if mp is not None:
            conf_util.replace_or_append_section(CONFIG_FILE, "mp", _render_mp_block(mp))
        if xconf is not None:
            conf_util.replace_or_append_section(CONFIG_FILE, "x", _render_x_block(xconf))
        for section_key, key, value in ops:
            conf_util.set_section_key(CONFIG_FILE, section_key, key, value)
    return schedule_get()


@app.get("/api/alerts/upcoming")
def alerts_upcoming(days: int = 14):
    """自选股未来 N 天的解禁/增发事件（页面每次现拉，不受已推送状态影响）。"""
    days = max(1, min(days, 90))
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT code FROM sa_watchlist")
        codes = [r[0] for r in cur.fetchall()]
    return {"days": days, "events": alerts_mod.upcoming_events(codes, days=days)}


@app.post("/api/alerts/check")
def alerts_check():
    """手动触发一次告警扫描（新事件推微信/邮件；同事件只推一次）。"""
    with get_conn() as conn:
        fresh = alerts_mod.check_alerts_once(conn, notify_fn=notifier.notify)
    return {"ok": True, "alerted": fresh}


def _alerts_loop():
    """事件告警守护线程：工作日 check_time（默认 8:30，可网页改）后查一轮（每日一次；
    事件去重记在 data/alerts_state.json，重启不会重复轰炸，首轮发现的历史事件也会推）。"""
    last_day = None
    while True:
        try:
            now = datetime.now()
            conf = _conf_section("alerts")
            ah, am = _sched_time("alerts", "check_time")
            if (conf.get("enabled", True) and now.weekday() < 5
                    and (now.hour, now.minute) >= (ah, am) and now.hour < 21
                    and last_day != now.date()):
                with get_conn() as conn:
                    fresh = alerts_mod.check_alerts_once(
                        conn, notify_fn=notifier.notify,
                        days=int(conf.get("days", 14)))
                last_day = now.date()
                if fresh:
                    print(f"[alerts] pushed {len(fresh)} new events", flush=True)
            time.sleep(600)
        except Exception as exc:
            print(f"[alerts] loop error: {exc}", flush=True)
            time.sleep(600)


# ---------------- 模拟交易（LLM 决策 + 结算反思沉淀，见 paper_trading.py） ----------------

import paper_trading
import paper_memory


def _paper_deps(conf: dict | None = None) -> dict:
    """给 paper_trading 注入依赖（不 import app 的模块靠这个拿数据函数）。"""
    return {
        "get_conn": get_conn,
        "em_kline_fn": _em_kline_fields,
        "tx_symbol_fn": _tx_symbol,
        "quote_fn": fetch_quotes,
        "conf": conf if conf is not None else _conf_section("paper"),
        "news_fn": _paper_news_rows,
        "events_fn": _paper_event_lines,
        "market_fn": market_volume_status,
        "trading_days_fn": _trading_days_between,
        "notify_fn": notifier.notify,
    }


def _paper_news_rows(code: str, limit: int = 10) -> list[dict]:
    """该股近 7 天新闻（带 sentiment），供决策上下文。"""
    try:
        with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT DISTINCT ON (n.url) n.title, n.media, n.source, n.sentiment, "
                "n.publish_time, n.fetched_at FROM sa_news n "
                "LEFT JOIN sa_news_related r ON r.url = n.url "
                "WHERE (n.code = %s OR r.code = %s) "
                "AND COALESCE(n.publish_time, n.fetched_at) > now() - interval '7 days' "
                "ORDER BY n.url, COALESCE(n.publish_time, n.fetched_at) DESC LIMIT %s",
                (code, code, limit))
            rows = [dict(r) for r in cur.fetchall()]
            rows.sort(key=lambda r: r["publish_time"] or r["fetched_at"], reverse=True)
            return rows
    except Exception:
        return []


def _paper_event_lines(code: str, days: int = 14) -> list[str]:
    """该股未来 N 天解禁/增发事件（格式化行），供决策上下文。"""
    try:
        events = alerts_mod.upcoming_events([code], days=days)
    except Exception:
        return []
    lines = []
    for e in events[:8]:
        kind = "解禁" if e.get("type") == "lift" else "增发上市"
        cap = f"市值约 {e['cap_yi']:g} 亿" if e.get("cap_yi") else ""
        lines.append(f"{e.get('event_date')} {kind} {cap}（{e.get('detail') or ''}）"
                     .rstrip("（）"))
    return lines


_paper_state = {"running": False, "settling": False, "current_slot": None,
                "last_decision": None, "last_settle": None}


@app.get("/api/paper/overview")
def paper_overview():
    """账户 + 持仓估值 + 近30日快照 + 胜率统计（tab 打开即调）。"""
    return paper_trading.account_overview(_paper_deps())


@app.get("/api/paper/trades")
def paper_trades(code: str = "", limit: int = 100):
    """交易记录（含 reasoning/report 全文），新→旧。"""
    limit = max(1, min(limit, 500))
    sql = ("SELECT id, code, name, trade_date, side, shares, price, value, confidence, "
           "stop_loss_pct, reasoning, status, auto_closed, settle_date, settle_price, "
           "raw_return, alpha_return, benchmark, created_at FROM sa_paper_trades ")
    params: list = []
    if re.fullmatch(r"\d{4,6}", code or ""):
        sql += "WHERE code = %s "
        params.append(code)
    sql += "ORDER BY id DESC LIMIT %s"
    params.append(limit)
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        for key in ("trade_date", "settle_date", "created_at"):
            r[key] = r[key].isoformat() if r[key] and hasattr(r[key], "isoformat") else r[key]
        for key in ("price", "value", "raw_return", "alpha_return", "stop_loss_pct"):
            r[key] = float(r[key]) if r[key] is not None else None
    return rows


@app.get("/api/paper/rounds")
def paper_rounds(code: str = "", only_closed: bool = False,
                 only_open: bool = False, limit: int = 300):
    """每笔模拟交易的完整过程：买入 -> 卖出配对，带两边的决策理由。

    与 /api/paper/trades 的区别：后者是**决策流水**（一行一个决策，含大量
    「观望」），这个是**回合**（一笔交易从建仓到平仓/持有中），直接回答
    「这笔为什么买、为什么卖、赚了多少」。FIFO 配对，与账务口径一致。
    """
    return {"items": paper_trading.rounds(
        _paper_deps(), code=code, only_closed=only_closed,
        only_open=only_open)[:max(1, min(limit, 1000))]}


@app.get("/api/paper/lessons")
def paper_lessons(code: str = "", limit: int = 50):
    """经验库（新→旧）。code 传空串=只要全局规则，不传=全部。"""
    limit = max(1, min(limit, 200))
    params: list = []
    sql = ("SELECT id, trade_id, code, action, decision_digest, raw_return, "
           "alpha_return, holding_days, lesson, resolved_at, created_at "
           "FROM sa_paper_reflections ")
    if code == "":
        sql += "WHERE code = '' "
    elif re.fullmatch(r"\d{4,6}", code):
        sql += "WHERE code = %s "
        params.append(code)
    sql += "ORDER BY id DESC LIMIT %s"
    params.append(limit)
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        for key in ("resolved_at", "created_at"):
            r[key] = r[key].isoformat() if r[key] and hasattr(r[key], "isoformat") else r[key]
        for key in ("raw_return", "alpha_return"):
            r[key] = float(r[key]) if r[key] is not None else None
    return rows


class PaperResetIn(BaseModel):
    confirm: bool = False
    initial_cash: float = Field(default=100000.0, gt=0, le=100000000)


@app.post("/api/paper/run")
def paper_run(force_slot: bool = False):
    """手动触发一轮决策（后台线程执行，立即返回）。

    收市后拒绝：真实市场那时接不到单，手动触发也不该绕过时段闸门
    （force_slot 只影响 slot 命名，不放宽交易时段）。
    """
    if _paper_state["running"]:
        return {"ok": True, "status": "已在决策中，请稍后"}
    conf = _conf_section("paper")
    now = datetime.now()
    st = paper_trading.market_session_state("", now)
    lh, lm = _hhmm(conf.get("latest_trade_time"), 15, 5)
    if not (st["open"] and now.hour * 60 + now.minute <= lh * 60 + lm):
        return {"ok": False, "status": f"已收市（{st['reason']}，现在 {now:%H:%M}），"
                                       f"不成交。需要复盘请用「立即结算复盘」。",
                "trading": st}
    slot = now.strftime("%H:%M") + ("-m" if force_slot else "")
    _paper_state["running"] = True
    threading.Thread(target=_paper_run_cycle, args=(conf, slot),
                     kwargs={"kind": "manual", "trigger": "api"}, daemon=True).start()
    return {"ok": True, "status": "决策已启动", "slot": slot}


@app.post("/api/paper/settle")
def paper_settle():
    """手动触发结算+复盘（后台线程执行，立即返回）。"""
    if _paper_state["settling"]:
        return {"ok": True, "status": "已在结算中，请稍后"}
    _paper_state["settling"] = True

    def _run():
        try:
            _paper_state["last_settle"] = paper_trading.settle_and_reflect(_paper_deps())
        except Exception as exc:
            traceback.print_exc()
            _paper_state["last_settle"] = {"error": str(exc)}
        finally:
            _paper_state["settling"] = False

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "status": "结算复盘已启动"}


@app.get("/api/paper/cycles")
def paper_cycles(trade_date: str = "", limit: int = 40):
    """盘中判断轮次台账（哪天哪些 slot 跑过、判了多少、成交多少、耗时多少）。

    参数名刻意不叫 date：那会遮蔽 datetime.date，导致下面 isinstance 报
    TypeError（arg 2 must be a type）。明细聚合也必须在 with 块**内**跑 ——
    出块连接已关，再 execute 就是 500。
    """
    n = max(1, min(limit, 200))
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if trade_date:
            cur.execute("SELECT * FROM sa_paper_cycles WHERE trade_date = %s "
                        "ORDER BY slot LIMIT %s", (trade_date, n))
        else:
            cur.execute("SELECT * FROM sa_paper_cycles "
                        "ORDER BY trade_date DESC, slot LIMIT %s", (n,))
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            for k, v in list(d.items()):
                if isinstance(v, _DT_TYPES):
                    d[k] = v.isoformat()
            # 该轮实际写出的决策明细，便于网页展开看
            cur.execute("""SELECT side, status, count(*) AS n FROM sa_paper_trades
                           WHERE trade_date = %s AND slot = %s
                           GROUP BY side, status ORDER BY side, status""",
                        (r["trade_date"], r["slot"]))
            d["breakdown"] = [{"side": x["side"], "status": x["status"], "n": x["n"]}
                              for x in cur.fetchall()]
            rows.append(d)
    return {"items": rows}


@app.get("/api/paper/reconcile")
def paper_reconcile(fix: bool = False):
    """用成交流水重算现金并与账面比对。fix=true 才改写账面（不可逆，谨慎）。

    出现 drift > 0 说明某笔现金增减没落地过（历史上是守护线程重复启动导致
    的并发下单）。这类偏差不会自己报警，只会表现为「总资产涨了但对不上持仓」。
    """
    return paper_trading.reconcile_account(_paper_deps(), fix=fix)


@app.post("/api/paper/reset")
def paper_reset(body: PaperResetIn):
    """重置模拟账户（清空交易/经验/快照，回填初始资金）。须 confirm=true。"""
    if not body.confirm:
        raise HTTPException(400, "须传 confirm=true 才能重置")
    return paper_trading.reset_account(_paper_deps(), body.initial_cash)


@app.get("/api/paper/status")
def paper_status():
    """顺带把进程内线程清单回出来：用来确认守护线程没有起两份。

    历史上因为 app 被 `__main__` 和 `app` 两个模块对象各执行了一遍模块体，
    十几个守护线程全都起了两份（实测 _paper_loop 同时有 Thread-11 和 Thread-28，
    各自持有独立的 last_slot，于是同一个 slot 被判了两轮）。
    """
    conf = _conf_section("paper")
    now = datetime.now()
    iv = max(1, int(conf.get("interval_minutes", 30) or 30))
    sm_h, sm_m = _hhmm(conf.get("start_time"), 9, 35)
    em_h, em_m = _hhmm(conf.get("end_time"), 14, 40)
    cur_min = now.hour * 60 + now.minute
    in_win = sm_h * 60 + sm_m <= cur_min <= em_h * 60 + em_m
    # 下一轮还有几分钟（窗口外返回 None）
    nxt = None
    if in_win:
        nxt = iv - (cur_min % iv)
    elif cur_min < sm_h * 60 + sm_m:
        nxt = sm_h * 60 + sm_m - cur_min
    return {"running": _paper_state["running"], "settling": _paper_state["settling"],
            "current_slot": _paper_state["current_slot"],
            "interval_minutes": iv,
            "in_window": in_win,
            "trading_day": now.weekday() < 5,
            "next_in_minutes": nxt,
            "trading": paper_trading.market_session_state("", now),
            "latest_trade_time": _hhmm(conf.get("latest_trade_time"), 15, 5),
            "daemon_threads": sorted(
                t.name for t in threading.enumerate() if t.name.startswith("sa_")),
            "last_decision": _paper_state["last_decision"],
            "last_settle": _paper_state["last_settle"]}


def _paper_loop():
    """模拟交易守护线程：交易日按固定节奏反复判断，而不是盘后只判一次。

    一个交易日分三类时刻：
      1. 盘中轮次  —— [start_time, end_time] 内每 interval_minutes 跑一轮。
                     每轮先做**免 LLM 的止损扫描**（该跌的立刻走），再按需让 LLM
                     重新判断。轮次标签 slot='HH:MM'，同一 slot 内同股只判一次。
      2. 兜底补跑  —— 过了 decide_time 若当天一轮没跑过（服务中途才起来），
                     补一轮，避免整天被跳过。
      3. 收盘结算  —— settle_time 后 settle_and_reflect（错开相位给日K落库留时间）。

    幂等有两层：进程内 last_slot 防重复触发，DB 里 UNIQUE(trade_date,slot,code,side)
    兜底，所以手动触发和线程并发都不会写出重复行。
    config paper.enabled=false 时整轮跳过（改配置即生效，
    但本线程本身是进程启动时创建的，新增需重启一次）。"""
    last_slot = None          # 已跑过的 'YYYY-MM-DD HH:MM'
    last_decision_day = None  # 当天是否至少跑过一轮（兜底补跑用）
    last_settle_day = None
    while True:
        try:
            now = datetime.now()
            conf = _conf_section("paper")
            if conf.get("enabled") and now.weekday() < 5:
                dh, dm = _sched_time("paper", "decide_time")
                sh, sm = _sched_time("paper", "settle_time")
                iv = max(1, int(conf.get("interval_minutes", 30) or 30))
                sm_h, sm_m = _hhmm(conf.get("start_time"), 9, 35)
                em_h, em_m = _hhmm(conf.get("end_time"), 14, 40)
                cur_min = now.hour * 60 + now.minute
                slot = now.strftime("%H:%M")
                # 总闸门：A股/港股都没开市就不跑任何一轮。
                # 原来兜底补跑只判 `>= decide_time` 没有上界，于是 17:24/17:48
                # 还在成交（2026-09-28 实测），而那会儿行情接口返回的是当日
                # 收盘价 —— 真实盘接不到这种单。latest_trade_time 是最后
                # 允许成交的时刻（默认 15:05，A股 15:00 收市，留 5 分钟余量）。
                mkt = paper_trading.market_session_state("", now)
                last_h, last_m = _hhmm(conf.get("latest_trade_time"), 15, 5)
                within = cur_min <= last_h * 60 + last_m
                can_trade = within and mkt["open"]

                # 1) 盘中轮次：到点就跑（>= 而非 ==，迟一点起也能补上）
                due = (sm_h * 60 + sm_m) <= cur_min <= (em_h * 60 + em_m)
                on_mark = cur_min % iv == 0
                if can_trade and due and on_mark and last_slot != slot:
                    last_slot = slot
                    last_decision_day = now.date()
                    _paper_run_cycle(conf, slot, kind="intraday", trigger="interval")

                # 2) 兜底补跑：过了 decide_time 且当天还没跑过任何一轮。
                #    同样受 can_trade 约束 —— 收市后不再成交（结算不受影响）。
                elif (can_trade and (now.hour, now.minute) >= (dh, dm)
                      and last_decision_day != now.date()
                      and last_slot != slot):
                    last_slot = slot
                    last_decision_day = now.date()
                    _paper_run_cycle(conf, slot, kind="catchup", trigger="decide_time")

                # 3) 收盘结算
                if (now.hour, now.minute) >= (sh, sm) and last_settle_day != now.date():
                    _paper_state["settling"] = True
                    try:
                        result = paper_trading.settle_and_reflect(_paper_deps(conf))
                        _paper_state["last_settle"] = result
                        last_settle_day = now.date()
                        print(f"[paper] settled {len(result.get('settled', []))} trades",
                              flush=True)
                    except Exception as exc:
                        _paper_state["last_settle"] = {"error": str(exc)}
                        print(f"[paper] settle error: {exc}", flush=True)
                    finally:
                        _paper_state["settling"] = False
            time.sleep(30)
        except Exception as exc:
            print(f"[paper] loop error: {exc}", flush=True)
            time.sleep(30)


def _hhmm(v, default_h: int, default_m: int) -> tuple[int, int]:
    """解析 'HH:MM'，坏了就用默认值（不抛异常，免得整个轮询线程挂掉）。"""
    try:
        h, m = str(v).split(":")
        return max(0, min(23, int(h))), max(0, min(59, int(m)))
    except Exception:
        return default_h, default_m


_paper_cycle_lock = threading.Lock()   # 进程内互斥（快速失败，不等）
# 跨进程互斥：PG 咨询锁。同一把钥匙（session 级）在同一时刻只允许一个持有者，
# 老老实实建一条连接持到整轮结束。**不能只靠进程内锁** —— 调试时起第二个实例、
# 或者上一个实例没退干净，都会让两轮同时下单，而 UNIQUE 只挡得住「同 slot 同方向」，
# 挡不住「同一天不同 slot 各买一笔」。
_PAPER_LOCK_KEY = 918273645


class _PaperAdvisoryLock:
    """PG 咨询锁 contextmanager。拿不到就让调用方跳过本轮。"""

    def __init__(self, key: int = _PAPER_LOCK_KEY):
        self.key = key
        self.conn = None
        self.got = False

    def __enter__(self):
        try:
            self.conn = get_conn()
            cur = self.conn.cursor()
            cur.execute("SELECT pg_try_advisory_lock(%s)", (self.key,))
            self.got = bool(cur.fetchone()[0])
            self.conn.commit()
        except Exception as exc:
            print(f"[paper] 取咨询锁失败: {exc}", flush=True)
            self.got = False
        return self.got

    def __exit__(self, *a):
        if self.conn is not None:
            try:
                cur = self.conn.cursor()
                cur.execute("SELECT pg_advisory_unlock(%s)", (self.key,))
                self.conn.commit()
            except Exception:
                pass
            try:
                self.conn.close()
            except Exception:
                pass
        return False


def _paper_run_cycle(conf: dict, slot: str, kind: str = "intraday",
                     trigger: str = "", only: set | None = None) -> dict:
    """跑一轮盘中判断：先免 LLM 止损，再（按需）LLM 重新决策，最后记台账。

    两道互斥：进程内 Lock + PG 咨询锁。任一拿不到就**跳过本轮**而不是并发跑 ——
    并发会让同一天同一只票下两笔单，正是 09-24 踩过的那个坑。
    35 只票一轮要十几分钟，很容易撞上下一个 30 分钟刻度，所以这个跳过是常态。
    """
    t0 = time.time()
    me = f"pid={os.getpid()} thread={threading.current_thread().name}"
    if not _paper_cycle_lock.acquire(blocking=False):
        print(f"[paper] {slot} {me} 上一轮还在跑（进程内锁），跳过本轮", flush=True)
        return {"slot": slot, "kind": kind, "skipped_reason": "上一轮进行中"}
    adv = _PaperAdvisoryLock()
    if not adv.__enter__():
        _paper_cycle_lock.release()
        print(f"[paper] {slot} {me} 别的进程正在跑（咨询锁），跳过本轮", flush=True)
        return {"slot": slot, "kind": kind, "skipped_reason": "他进程在跑"}
    _paper_state["running"] = True
    _paper_state["current_slot"] = slot
    acted = holds = skipped = 0
    err = ""
    print(f"[paper] {slot} {me} {kind}/{trigger} 开跑", flush=True)
    try:
        deps = _paper_deps(conf)
        # 1) 止损：不需要 LLM，先做，保证该跑的止损不会被 LLM 延迟/解析失败拖掉
        sl = paper_trading.check_stop_loss(deps, slot)
        acted += len(sl.get("sold", []))
        # 2) 重新决策
        targets = only
        if targets is None and conf.get("judge_holdings_only"):
            with deps["get_conn"]() as conn:
                cur = conn.cursor()
                cur.execute("SELECT code FROM sa_watchlist")
                wset = {r[0] for r in cur.fetchall()}
            from paper_trading import _derive_paper_positions
            with deps["get_conn"]() as conn:
                targets = set(_derive_paper_positions(conn.cursor()).keys()) & wset
        res = paper_trading.run_decisions(deps, slot=slot, only=targets)
        decided = res.get("decided", [])
        for d in decided:
            if d.get("executed"):
                acted += 1
            elif d.get("action") == "hold":
                holds += 1
            else:
                skipped += 1
        planned = len(decided) + len(sl.get("sold", []))
        _paper_state["last_decision"] = res
        print(f"[paper] {slot} {me} {kind}: 判断 {planned} 成交 {acted} 观望 {holds} "
              f"未成交 {skipped} 用时 {time.time()-t0:.0f}s", flush=True)
    except Exception as exc:
        err = str(exc)
        traceback.print_exc()
        print(f"[paper] cycle {slot} error: {exc}", flush=True)
    finally:
        elapsed = int((time.time() - t0) * 1000)
        try:
            paper_trading.record_cycle(
                _paper_deps(conf), datetime.now().strftime("%Y-%m-%d"), slot,
                kind=kind, trigger=trigger, planned=acted + holds + skipped,
                acted=acted, holds=holds, skipped=skipped, elapsed_ms=elapsed, error=err)
        except Exception:
            pass
        _paper_state["running"] = False
        _paper_state["current_slot"] = None
        adv.__exit__(None, None, None)     # 先放 PG 锁
        _paper_cycle_lock.release()        # 再放进程内锁
    return {"slot": slot, "kind": kind, "acted": acted, "holds": holds,
            "skipped": skipped, "elapsed_ms": elapsed, "error": err}


# ---------------- 新闻（多渠道免费抓取，见 news_fetcher.py） ----------------

import news_fetcher
import notifier

CONFIG_FILE = BASE_DIR / "config.yaml"

_news_state = {"fetching": False, "last_run": None, "last_result": None}


def _read_conf() -> dict:
    return news_fetcher.load_config()


def _write_conf(conf: dict) -> None:
    """把新闻配置写回 config.yaml（notify / bili 段由各自模块维护）。

    config.yaml 有 news / bili / notify 等多个模块段，整文件重写会互相踩，
    所以只替换 news: 块本身的行（含其上方紧邻的注释头），其余原样保留。

    整个函数体在 conf_util.WRITE_LOCK 内：它与「调度设置」页的整文件重写
    （PUT /api/schedule）都是读-改-写整文件，并发会丢段。
    """
    with conf_util.WRITE_LOCK:
        _write_conf_locked(conf)


def _write_conf_locked(conf: dict) -> None:
    """_write_conf 的实际写盘逻辑（调用方需已持有 conf_util.WRITE_LOCK）。"""
    ch = conf["channels"]
    news_block = "\n".join([
        "# Stock Advisor 新闻抓取配置",
        "# 网页「新闻」标签页也可修改以下配置（保存后下一轮抓取生效）",
        "news:",
        "  channels:",
        f"    eastmoney: {{enabled: {str(ch['eastmoney']['enabled']).lower()}, min_interval: {ch['eastmoney']['min_interval']}}}",
        f"    baidu: {{enabled: {str(ch['baidu']['enabled']).lower()}, min_interval: {ch['baidu']['min_interval']}}}",
        f"    sina: {{enabled: {str(ch['sina']['enabled']).lower()}, min_interval: {ch['sina']['min_interval']}}}",
        f"    duckduckgo: {{enabled: {str(ch['duckduckgo']['enabled']).lower()}, min_interval: {ch['duckduckgo']['min_interval']}, timeout: {ch['duckduckgo'].get('timeout', 30)}}}",
        f"  fetch_interval_minutes: {conf['fetch_interval_minutes']}",
        f"  items_per_query: {conf['items_per_query']}",
        f"  keywords_extra: {json.dumps(conf.get('keywords_extra', []), ensure_ascii=False)}",
    ])
    old = CONFIG_FILE.read_text(encoding="utf-8") if CONFIG_FILE.exists() else ""
    lines = old.splitlines()
    # news: 块范围 = 其注释头 + news: 行 + 块体 + 块后空行。
    # 下一段的注释头（空行之后、下一个顶层 key 之前）属于下一段，必须保留。
    start = next((i for i, l in enumerate(lines) if l.rstrip() == "news:"), None)
    if start is None:
        new_text = old.rstrip("\n") + ("\n" if old.strip() else "") + news_block + "\n"
    else:
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if lines[j] and not lines[j][0].isspace() and not lines[j].startswith("#"):
                end = j
                break
        head = start
        # 上方紧邻的注释行和空行都归属新闻段（避免旧注释/空行残留堆积）
        while head > 0 and (lines[head - 1].startswith("#") or not lines[head - 1].strip()):
            head -= 1
        # 块尾到下一段之间：先跳过空行，再跳过下一段自己的注释头 → 从注释头处接上
        seg = end
        while seg < len(lines) and not lines[seg].strip():
            seg += 1
        seg_with_comment = seg
        while seg_with_comment > 0 and lines[seg_with_comment - 1].startswith("#"):
            seg_with_comment -= 1
        new_text = "\n".join(lines[:head]).rstrip("\n")
        if new_text:
            new_text += "\n\n"
        new_text += news_block + "\n"
        if seg < len(lines):  # 后面还有别的段（含其注释头）：补空行分隔后原样接上
            new_text += "\n" + "\n".join(lines[seg_with_comment:]) + "\n"
    CONFIG_FILE.write_text(new_text, encoding="utf-8")


class ConfIn(BaseModel):
    fetch_interval_minutes: int | None = Field(default=None, ge=0, le=1440)
    items_per_query: int | None = Field(default=None, ge=1, le=50)
    channels: dict | None = None


@app.get("/api/news")
def list_news(code: str = "", limit: int = 30):
    """读已抓取的新闻（可按股票过滤），新→旧。每条附 related：关联的全部自选股代码。

    code 过滤时同时匹配主 code 与关联表（sa_news_related），保证按股筛选不漏关联新闻。
    """
    limit = max(1, min(limit, 200))
    params: list = []
    # DISTINCT ON (url) 按 URL 去重（同一新闻只显示一次，关联股票在 related 里给出）
    sql = ("SELECT DISTINCT ON (n.url) n.code, n.title, n.url, n.source, n.media, "
           "n.publish_time, n.fetched_at, n.sentiment FROM sa_news n ")
    if re.fullmatch(r"\d{5,6}", code or ""):
        target = code if len(code) == 6 else code.zfill(5)
        sql += ("LEFT JOIN sa_news_related r ON r.url = n.url "
                "WHERE (n.code = %s OR r.code = %s) ")
        params += [target, target]
    sql += "ORDER BY n.url, COALESCE(n.publish_time, n.fetched_at) DESC LIMIT %s"
    params.append(limit)
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        # DISTINCT ON 的排序含 url，这里重排为时间序
        rows.sort(key=lambda r: r["publish_time"] or r["fetched_at"], reverse=True)
        # 批量取这批 URL 的全部关联（含主 code）
        if rows:
            urls = [r["url"] for r in rows]
            cur.execute(
                "SELECT url, code FROM sa_news_related WHERE url = ANY(%s)",
                (urls,))
            rel: dict[str, list[str]] = {}
            for u, c in cur.fetchall():
                rel.setdefault(u, []).append(c)
            for r in rows:
                codes = rel.get(r["url"], [r["code"]])
                r["related"] = codes
    for r in rows:
        for key in ("publish_time", "fetched_at"):
            r[key] = r[key].isoformat(timespec="seconds") if r[key] else None
    return rows


@app.post("/api/news/fetch")
def trigger_news_fetch():
    """立即抓一轮（后台线程执行，立即返回）。"""
    if _news_state["fetching"]:
        return {"ok": True, "status": "已在抓取中，请稍后"}
    _news_state["fetching"] = True

    def _run():
        try:
            _news_state["last_result"] = news_fetcher.fetch_watchlist(_read_conf())
        except Exception as exc:
            _news_state["last_result"] = {"error": str(exc)}
        finally:
            _news_state["last_run"] = datetime.now().isoformat(timespec="seconds")
            _news_state["fetching"] = False

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "status": "抓取已启动"}


@app.get("/api/news/status")
def news_status():
    return {"fetching": _news_state["fetching"], "last_run": _news_state["last_run"],
            "last_result": _news_state["last_result"]}


@app.get("/api/config")
def get_config():
    return _read_conf()


@app.put("/api/config")
def update_config(body: ConfIn):
    conf = _read_conf()
    if body.fetch_interval_minutes is not None:
        conf["fetch_interval_minutes"] = body.fetch_interval_minutes
    if body.items_per_query is not None:
        conf["items_per_query"] = body.items_per_query
    if body.channels:
        for name, enabled in body.channels.items():
            if name in conf["channels"]:
                conf["channels"][name]["enabled"] = bool(enabled)
    _write_conf(conf)
    return _read_conf()


# ---------------- 完整配置（config_schema.py：所有 config.yaml 项都能在网页改） ----------------
#
# 这是**通用入口**：模式里声明的每个键都能在这里读写，不必为每个模块手写输入框。
# 与并存的专用入口（/api/schedule、/api/llm/config、/api/notify/config…）写的是同一个
# config.yaml，都走 conf_util.WRITE_LOCK。
#
# 三条安全约定：
#  1. 写回前**逐项 coerce + 范围校验**，任何一项不合法就整批拒绝（400），
#     避免「写了一半、剩下的还是旧的」这种半成品配置。
#  2. 只写**真的变了**的键（set_nested_key 是行级手术），其余键、注释、
#     兄弟段一律原样保留。
#  3. secret 项（api_key / auth_code / mp.auth）前端只回掩码；回传掩码或空串
#     一律视为「不修改」，绝不用掩码覆盖真值。

def _load_config_yaml() -> dict:
    try:
        import yaml
        return yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _unknown_keys(conf: dict) -> list[str]:
    """config.yaml 里有、但模式未覆盖的键（返回 `段.点号键` 形式）。

    刻意**不删除**它们（可能是我不知道的手工配置），但要在页面上如实报出来——
    「所有配置项都能改」这句话必须诚实，否则用户改完却发现某个键管不了。
    """
    known = config_schema.field_index()          # {(段, 点号键): 字段}
    out: list[str] = []

    def walk(node: dict, sec: str, prefix: str) -> None:
        for k, v in node.items():
            dotted = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                # 该 dict 只要有任一字段落在它下面，就认为「已覆盖」，继续往下走
                if not any(s == sec and (d == dotted or d.startswith(dotted + "."))
                           for s, d in known):
                    out.append(f"{sec}.{dotted}")
                else:
                    walk(v, sec, dotted)
            elif (sec, dotted) not in known:
                out.append(f"{sec}.{dotted}")

    for sec, val in conf.items():
        if sec not in config_schema.SCHEMAS:
            continue                       # 完全未知的段：不逐键报，只报段名
        if isinstance(val, dict):
            walk(val, sec, "")
        elif (sec, "") not in known:
            out.append(sec)
    return out


@app.get("/api/config/schema")
def config_schema_get():
    """配置模式（前端按它动态生成表单）。"""
    return {"sections": config_schema.schema_json()}


@app.get("/api/config/all")
def config_all_get():
    """全部配置项的当前值（模式默认值 → 各模块 DEFAULT_* → config.yaml 依次覆盖）。

    每个字段回：{value, is_default, has_value(secret 项)}。
    """
    conf = _load_config_yaml()
    dflt = config_schema.defaults()
    sections = []
    for meta in config_schema.schema_json():
        rows = []
        for f in meta["fields"]:
            sec = meta["section"]
            cur = conf_util.get_nested(conf.get(sec) or {}, f["key"])
            dft = conf_util.get_nested(dflt.get(sec) or {}, f["key"], f["default"])
            is_default = (cur is None) or (cur == dft)
            if f["secret"]:
                val, extra = ("", {"has_value": bool(cur)}) if cur else ("", {"has_value": False})
            else:
                val, extra = (dft if cur is None else cur), {}
            rows.append({**f, "value": val, "is_default": is_default, **extra})
        sections.append({**meta, "rows": rows})
    return {"sections": sections, "unknown": _unknown_keys(conf)}


class ConfigAllIn(BaseModel):
    # {段: {点号键: 值}}。允许空 dict（= 什么都不改）
    values: dict = Field(default_factory=dict)


@app.put("/api/config/all")
def config_all_put(body: ConfigAllIn):
    """批量写配置。整批校验通过后才落盘；只写变化的键。"""
    known = config_schema.field_index()
    ops: list[tuple[str, str, object]] = []
    skipped: list[str] = []
    errors: list[str] = []
    for sec, kv in (body.values or {}).items():
        if sec not in config_schema.SCHEMAS:
            errors.append(f"未知的配置段：{sec}")
            continue
        if not isinstance(kv, dict):
            errors.append(f"{sec} 的值必须是对象")
            continue
        for key, raw in kv.items():
            field = known.get((sec, key))
            if not field:
                errors.append(f"{sec} 不支持字段 {key}")
                continue
            try:
                val, skip = config_schema.coerce(sec, key, raw, field)
            except ValueError as exc:
                errors.append(str(exc))
                continue
            if skip:
                skipped.append(f"{sec}.{key}")
                continue
            ops.append((sec, key, val))
    if errors:
        # 一项都不写：半成品配置比不改更危险
        raise HTTPException(400, "；".join(errors[:8]))
    if not ops:
        return {"ok": True, "written": [], "skipped": skipped}
    conf_util.write_many(CONFIG_FILE, key_ops=ops)
    print(f"[config] 页面改了 {len(ops)} 项: "
          + ", ".join(f"{s}.{k}" for s, k, _ in ops[:12])
          + ("…" if len(ops) > 12 else ""), flush=True)
    return {"ok": True, "written": [f"{s}.{k}" for s, k, _ in ops], "skipped": skipped}


# ---------------- 通知（微信走腾讯官方 iLink Bot / 邮件 SMTP，见 notifier.py） ----------------

class NotifyConfIn(BaseModel):
    wx_enabled: bool | None = None
    email_enabled: bool | None = None
    smtp_host: str | None = None
    smtp_port: int | None = Field(default=None, ge=0, le=65535)
    from_addr: str | None = None
    auth_code: str | None = None
    to_addr: str | None = None


class NotifySendIn(BaseModel):
    title: str
    content: str = ""
    channels: list[str] | None = None  # ["wx","email"]，空 = 按启用渠道广播


class IlinkVerifyIn(BaseModel):
    verify_code: str = ""  # need_verifycode 时手机上显示的数字


_bind_state = {"qrcode": None, "created_at": None,
               "status": None, "bound": False, "error": None}


@app.get("/api/notify/config")
def notify_get_config():
    """通知配置。微信绑定关系（iLink 凭据）在云库，只返回「是否已登录」。"""
    conf = notifier.load_notify_conf()
    conf["wx"]["bound"] = bool(notifier.ilink_creds().get("bot_token"))
    return conf


@app.put("/api/notify/config")
def notify_update_config(body: NotifyConfIn):
    conf = notifier.load_notify_conf()
    wx, email = conf["wx"], conf["email"]
    if body.wx_enabled is not None:
        wx["enabled"] = body.wx_enabled
    if body.smtp_host is not None:
        email["smtp_host"] = body.smtp_host.strip()
    if body.smtp_port is not None:
        email["smtp_port"] = body.smtp_port
    if body.from_addr is not None:
        email["from_addr"] = body.from_addr.strip()
    if body.auth_code is not None:
        email["auth_code"] = body.auth_code
    if body.to_addr is not None:
        email["to_addr"] = body.to_addr.strip()
    if body.email_enabled is not None:
        email["enabled"] = body.email_enabled
    notifier.save_notify_conf(conf)
    return notify_get_config()


@app.post("/api/notify/wx/bind")
def notify_wx_bind_start():
    """发起微信 iLink 扫码登录：生成二维码（base64 PNG 直接给前端 <img>）。

    返回 {qrcode_key, qrcode_png_b64, valid_seconds}；qrcode_key 仅为本次
    登录会话的引用，凭据轮询在服务端，二维码句柄不下发。
    """
    qr = notifier.create_login_qrcode()
    _bind_state.update(qrcode=qr["qrcode"], created_at=time.time(),
                       status="wait", bound=False, error=None)

    def _poll():
        """后台线程连续长轮询（每次调用服务端挂起约 35s），直到终态。"""
        deadline = time.time() + 300
        try:
            while time.time() < deadline:
                res = notifier.poll_login_status(_bind_state["qrcode"])
                _bind_state["status"] = res.get("status")
                if res.get("status") in ("confirmed", "error", "expired"):
                    if res.get("status") == "confirmed":
                        _bind_state["bound"] = True
                    if res.get("error"):
                        _bind_state["error"] = res["error"]
                    return
                time.sleep(1)
            _bind_state["status"] = "expired"
        except Exception as exc:
            _bind_state["error"] = str(exc)
            _bind_state["status"] = "error"

    threading.Thread(target=_poll, daemon=True).start()
    return {"ok": True, "qrcode_png_b64": qr["qrcode_png_b64"],
            "valid_seconds": qr["valid_seconds"]}


@app.get("/api/notify/wx/bind")
def notify_wx_bind_status():
    """前端 5 秒轮询：status=wait/scaned/need_verifycode/confirmed/expired/error。

    need_verifycode 时前端弹出数字输入框，用户填手机上显示的数字后调
    POST /api/notify/wx/bind/verify 提交，之后继续轮询本接口。
    """
    return {k: v for k, v in _bind_state.items() if k != "qrcode"}


@app.post("/api/notify/wx/bind/verify")
def notify_wx_bind_verify(body: IlinkVerifyIn):
    """提交扫码数字验证码（need_verifycode 状态后调用）。

    把后台轮询线程唤醒重试：直接在请求线程里跑一次带 verify_code 的长轮询，
    结果写回 _bind_state，后台线程下一轮自然感知到新状态。
    """
    if not _bind_state.get("qrcode"):
        raise HTTPException(400, "没有进行中的登录，请先生成二维码")
    code = body.verify_code.strip()
    if not code.isdigit():
        raise HTTPException(400, "验证码应为手机上显示的数字")
    try:
        res = notifier.poll_login_status(_bind_state["qrcode"], verify_code=code)
    except Exception as exc:
        res = {"status": "error", "error": str(exc)}
    _bind_state["status"] = res.get("status")
    if res.get("status") == "confirmed":
        _bind_state["bound"] = True
    if res.get("error"):
        _bind_state["error"] = res["error"]
    return res


@app.post("/api/notify/test")
def notify_test(channel: str = "wx"):
    """给指定渠道发一条测试消息（wx / email）。"""
    title = "Stock Advisor 通知测试"
    if channel == "wx":
        res = notifier.send_wx(title, "微信通知配置成功 ✓\n收到此消息说明绑定已生效。")
    elif channel == "email":
        res = notifier.send_email(title, "邮件通知配置成功 ✓\n收到此邮件说明 SMTP 配置正确。")
    else:
        raise HTTPException(400, "channel 仅支持 wx / email")
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "发送失败"))
    return res


@app.post("/api/notify/send")
def notify_send(body: NotifySendIn):
    """通用通知入口（供 Claude 定时任务 / 脚本 curl 调用），按启用渠道广播。"""
    return notifier.notify(body.title, body.content, channels=body.channels)


@app.get("/api/notify/wx/unbind")
def notify_wx_unbind():
    """解绑：清空云库里的 iLink 凭据与用户缓存，需重新扫码。"""
    removed = notifier.ilink_creds_clear()
    _bind_state.update(qrcode=None, status=None, bound=False, error=None)
    return {"ok": True, "removed": removed}


# ---------------- 自动抓取后台线程 ----------------

def _auto_fetch_loop():
    """按 config.yaml 的 fetch_interval_minutes 周期抓新闻；0 = 关闭。"""
    while True:
        try:
            interval = _read_conf().get("fetch_interval_minutes", 60)
            if interval and not _news_state["fetching"]:
                _news_state["fetching"] = True
                try:
                    _news_state["last_result"] = news_fetcher.fetch_watchlist(_read_conf())
                except Exception as exc:
                    _news_state["last_result"] = {"error": str(exc)}
                finally:
                    _news_state["last_run"] = datetime.now().isoformat(timespec="seconds")
                    _news_state["fetching"] = False
            time.sleep(max(interval or 60, 1) * 60)
        except Exception:
            time.sleep(300)  # 配置读取失败等异常，5 分钟后重试


# ---------------- B站动态（MediaCrawler，见 bili_monitor.py） ----------------

import bili_monitor

@app.get("/api/bili/creators")
def bili_creators_list():
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT uid, name, note, added_at FROM sa_bili_creators ORDER BY added_at")
        return [dict(r) for r in cur.fetchall()]


class BiliCreatorIn(BaseModel):
    uid: str = Field(min_length=1, max_length=255, description="UID 或 space.bilibili.com 链接")
    note: str = Field(default="", max_length=255)


@app.post("/api/bili/creators")
def bili_creators_add(body: BiliCreatorIn):
    raw = (body.uid or "").strip()
    m = re.search(r"space\.bilibili\.com/(\d+)", raw)
    uid = m.group(1) if m else raw
    if not re.fullmatch(r"\d{1,32}", uid):
        raise HTTPException(400, "UID 格式：纯数字（如 946974），或 space.bilibili.com 链接")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sa_bili_creators (uid, note) VALUES (%s,%s) ON CONFLICT (uid) DO NOTHING",
            (uid, body.note),
        )
        if cur.rowcount == 0:
            raise HTTPException(409, f"UID {uid} 已存在")
    return {"ok": True, "uid": uid}


@app.delete("/api/bili/creators/{uid}")
def bili_creators_remove(uid: str):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM sa_bili_creators WHERE uid = %s", (uid,))
        if cur.rowcount == 0:
            raise HTTPException(404, "UID 不存在")
    return {"ok": True}


@app.get("/api/bili/dynamics")
def bili_dynamics(limit: int = 50, unread_only: bool = False, uid: str = ""):
    sql = ("SELECT dynamic_id, uid, author_name, dtype, title, text, bvid, aid, "
           "pub_ts, stats, is_read, fetched_at FROM sa_bili_dynamics ")
    conds, params = [], []
    if unread_only:
        conds.append("is_read = FALSE")
    if uid:
        conds.append("uid = %s")
        params.append(uid)
    if conds:
        sql += "WHERE " + " AND ".join(conds) + " "
    sql += "ORDER BY pub_ts DESC NULLS LAST LIMIT %s"
    params.append(min(max(limit, 1), 200))
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


@app.get("/api/bili/dynamics/unread-count")
def bili_unread_count():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM sa_bili_dynamics WHERE is_read = FALSE")
        return {"unread": cur.fetchone()[0]}


@app.get("/api/bili/dynamics/{dynamic_id}/comments")
def bili_comments(dynamic_id: str):
    # 视频帖的评论在 MediaCrawler 里按 aid 存（video_id 字段=aid），文字/图文按 dynamic_id
    # 存；两种都要查，否则视频帖永远"暂无评论"。
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT aid FROM sa_bili_dynamics WHERE dynamic_id = %s", (dynamic_id,))
        row = cur.fetchone()
        ids = [dynamic_id] + ([row["aid"]] if row and row["aid"] else [])
        cur.execute(
            "SELECT comment_id, dynamic_id, content, author, pub_ts, like_count "
            "FROM sa_bili_comments WHERE dynamic_id = ANY(%s) ORDER BY like_count DESC LIMIT 100",
            (ids,),
        )
        return [dict(r) for r in cur.fetchall()]


class BiliReadIn(BaseModel):
    ids: list[str] = Field(default_factory=list, description="动态ID列表；空列表配合 all=true 全部已读")
    all: bool = False


@app.post("/api/bili/read")
def bili_mark_read(body: BiliReadIn):
    with get_conn() as conn, conn.cursor() as cur:
        if body.all:
            cur.execute("UPDATE sa_bili_dynamics SET is_read = TRUE WHERE is_read = FALSE")
        elif body.ids:
            cur.execute("UPDATE sa_bili_dynamics SET is_read = TRUE WHERE dynamic_id = ANY(%s)",
                        (body.ids,))
        else:
            raise HTTPException(400, "ids 为空时需 all=true")
        return {"ok": True, "updated": cur.rowcount}


@app.post("/api/bili/fetch")
def bili_fetch():
    """线程触发一次完整抓取（读 UP 主 → MediaCrawler → 入库）。"""
    if _bili_state_flag():
        raise HTTPException(409, "已有抓取任务在运行")

    def _run():
        try:
            result = bili_monitor.fetch_bili()
            bili_monitor._state["last_result"] = result
        except Exception as exc:
            bili_monitor._state["last_result"] = {"error": str(exc)}

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "message": "抓取已开始，请稍后刷新状态"}


@app.post("/api/bili/login")
def bili_login():
    """弹出浏览器扫码登录B站（阻塞请求直到流程结束，前端提示用户扫码）。"""
    if _bili_state_flag():
        raise HTTPException(409, "已有抓取/登录任务在运行")
    try:
        result = bili_monitor.login_bili()
        return {"ok": result.get("ok", False),
                "message": "登录成功" if result.get("ok") else "未检测到登录态，请重试并完成扫码"}
    except Exception as exc:
        raise HTTPException(500, f"登录失败：{exc}")


@app.get("/api/bili/status")
def bili_status():
    return bili_monitor.get_status()


@app.get("/api/bili/digest")
def bili_digest(hours: int = 24):
    if not 1 <= hours <= 168:
        raise HTTPException(400, "hours 取值 1-168")
    return {"hours": hours, "markdown": bili_monitor.digest_markdown(hours)}


def _bili_state_flag() -> bool:
    return bili_monitor.get_status()["fetching"]


def _bili_auto_loop():
    """按 config.yaml bili.interval_minutes 周期抓 B站动态；0 = 关闭。"""
    while True:
        try:
            conf = bili_monitor.load_bili_conf()
            interval = int(conf.get("interval_minutes", 30)) if conf.get("enabled", True) else 0
            if interval and not bili_monitor.get_status()["fetching"]:
                try:
                    result = bili_monitor.fetch_bili()
                    bili_monitor._state["last_result"] = result
                except Exception as exc:
                    bili_monitor._state["last_result"] = {"error": str(exc)}
            time.sleep(max(interval or 60, 1) * 60)
        except Exception:
            time.sleep(300)  # 配置读取失败等异常，5 分钟后重试


# ---------------- 微博博主动态（MediaCrawler，见 wb_monitor.py） ----------------

import wb_monitor

@app.get("/api/wb/creators")
def wb_creators_list():
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT uid, name, note, added_at FROM sa_wb_creators ORDER BY added_at")
        return [dict(r) for r in cur.fetchall()]


class WbCreatorIn(BaseModel):
    uid: str = Field(min_length=1, max_length=255, description="博主 UID 数字或 weibo.com/u/<uid> 链接")
    note: str = Field(default="", max_length=255)


@app.post("/api/wb/creators")
def wb_creators_add(body: WbCreatorIn):
    raw = (body.uid or "").strip()
    m = re.search(r"weibo\.com/u/(\d+)", raw) or re.search(r"weibo\.com/(?:n/)?(\d+)", raw)
    uid = m.group(1) if m else raw
    if not re.fullmatch(r"\d{5,32}", uid):
        raise HTTPException(400, "UID 格式：纯数字（如 5756404150），或 weibo.com/u/xxxx 链接")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sa_wb_creators (uid, note) VALUES (%s,%s) ON CONFLICT (uid) DO NOTHING",
            (uid, body.note),
        )
        if cur.rowcount == 0:
            raise HTTPException(409, f"UID {uid} 已存在")
    return {"ok": True, "uid": uid}


@app.delete("/api/wb/creators/{uid}")
def wb_creators_remove(uid: str):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM sa_wb_creators WHERE uid = %s", (uid,))
        if cur.rowcount == 0:
            raise HTTPException(404, "UID 不存在")
    return {"ok": True}


@app.get("/api/wb/posts")
def wb_posts(limit: int = 50, unread_only: bool = False, uid: str = ""):
    sql = ("SELECT note_id, uid, author_name, text, pub_ts, stats, is_read, fetched_at "
           "FROM sa_wb_posts ")
    conds, params = [], []
    if unread_only:
        conds.append("is_read = FALSE")
    if uid:
        conds.append("uid = %s")
        params.append(uid)
    if conds:
        sql += "WHERE " + " AND ".join(conds) + " "
    sql += "ORDER BY pub_ts DESC NULLS LAST LIMIT %s"
    params.append(min(max(limit, 1), 200))
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


@app.get("/api/wb/posts/unread-count")
def wb_unread_count():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM sa_wb_posts WHERE is_read = FALSE")
        return {"unread": cur.fetchone()[0]}


@app.get("/api/wb/posts/{note_id}/comments")
def wb_comments(note_id: str):
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT comment_id, note_id, content, author, pub_ts, like_count "
            "FROM sa_wb_comments WHERE note_id = %s ORDER BY like_count DESC LIMIT 100",
            (note_id,),
        )
        return [dict(r) for r in cur.fetchall()]


class WbReadIn(BaseModel):
    ids: list[str] = Field(default_factory=list, description="微博ID列表；空列表配合 all=true 全部已读")
    all: bool = False


@app.post("/api/wb/read")
def wb_mark_read(body: WbReadIn):
    with get_conn() as conn, conn.cursor() as cur:
        if body.all:
            cur.execute("UPDATE sa_wb_posts SET is_read = TRUE WHERE is_read = FALSE")
        elif body.ids:
            cur.execute("UPDATE sa_wb_posts SET is_read = TRUE WHERE note_id = ANY(%s)",
                        (body.ids,))
        else:
            raise HTTPException(400, "ids 为空时需 all=true")
        return {"ok": True, "updated": cur.rowcount}


@app.post("/api/wb/fetch")
def wb_fetch():
    """线程触发一次完整抓取（读博主 → MediaCrawler → 入库）。"""
    if _wb_state_flag():
        raise HTTPException(409, "已有抓取任务在运行")

    def _run():
        try:
            result = wb_monitor.fetch_wb()
            wb_monitor._state["last_result"] = result
        except Exception as exc:
            wb_monitor._state["last_result"] = {"error": str(exc)}

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "message": "抓取已开始，请稍后刷新状态"}


@app.post("/api/wb/login")
def wb_login():
    """弹出浏览器扫码登录微博（阻塞请求直到流程结束，前端提示用户扫码）。"""
    if _wb_state_flag():
        raise HTTPException(409, "已有抓取/登录任务在运行")
    try:
        result = wb_monitor.login_wb()
        return {"ok": result.get("ok", False),
                "message": "登录成功" if result.get("ok") else "未检测到登录态，请重试并完成扫码"}
    except Exception as exc:
        raise HTTPException(500, f"登录失败：{exc}")


@app.get("/api/wb/status")
def wb_status():
    return wb_monitor.get_status()


@app.get("/api/wb/digest")
def wb_digest(hours: int = 24):
    if not 1 <= hours <= 168:
        raise HTTPException(400, "hours 取值 1-168")
    return {"hours": hours, "markdown": wb_monitor.digest_markdown(hours)}


def _wb_state_flag() -> bool:
    return wb_monitor.get_status()["fetching"]


def _wb_auto_loop():
    """按 config.yaml wb.interval_minutes 周期抓微博；0 = 关闭。"""
    while True:
        try:
            conf = wb_monitor.load_wb_conf()
            interval = int(conf.get("interval_minutes", 60)) if conf.get("enabled", True) else 0
            if interval and not wb_monitor.get_status()["fetching"]:
                try:
                    result = wb_monitor.fetch_wb()
                    wb_monitor._state["last_result"] = result
                except Exception as exc:
                    wb_monitor._state["last_result"] = {"error": str(exc)}
            time.sleep(max(interval or 60, 1) * 60)
        except Exception:
            time.sleep(300)  # 配置读取失败等异常，5 分钟后重试


# ---------------- 板块轮动监控（sector.py） ----------------

import sector as sector_mod


@app.get("/api/sector/overview")
def sector_overview(details: bool = True):
    """板块轮动总览：当日榜 + 轮动评分 + 市场情绪（指数/宽度/涨停）。"""
    return sector_mod.build_overview(with_details=details)


@app.get("/api/sector/board/{bk_code}")
def sector_board(bk_code: str):
    """板块详情：成分股（按涨幅前 30）。"""
    items = sector_mod.fetch_board_constituents(bk_code, limit=30)
    if not items:
        raise HTTPException(404, f"板块 {bk_code} 无成分股数据")
    return {"code": bk_code, "items": items}


@app.post("/api/sector/snapshot")
def sector_snapshot():
    """手动采集一轮板块快照（平时也可用；自动采集在交易日收盘后）。"""
    result = sector_mod.collect_once()
    if result.get("skipped"):
        raise HTTPException(409, result.get("reason", "已有采集在进行"))
    return result


@app.get("/api/sector/status")
def sector_status():
    return {**sector_mod.get_status(), "auto": "交易日收盘后自动采集（15:10 起，半小时一查）"}


@app.get("/api/sector/digest")
def sector_digest(hours: int = 24):
    """供 Claude 定时报告引用的板块轮动 markdown 摘要。"""
    return {"digest": sector_mod.digest(hours=hours)}


@app.get("/api/sector/stock-boards")
def sector_stock_boards(codes: str):
    """个股所属板块 + 板块当日表现（codes 逗号分隔，如 600519,000665）。

    自选/持仓页展开行时调；也用于看持仓的板块暴露。
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:30]
    if not code_list:
        raise HTTPException(400, "codes 参数为空")
    return sector_mod.stock_board_exposure(code_list)


@app.post("/api/sector/alerts/check")
def sector_alerts_check():
    """手动跑一次轮动预警（自动版在每日盘后快照后跑）。返回本次触发的提醒。"""
    alerts = sector_mod.check_rotation_alerts()
    return {"alerts": alerts, "count": len(alerts)}


@app.get("/api/sector/intraday")
def sector_intraday():
    """盘中实时板块视图（内存缓存，交易时段后台线程定时采样）。

    交易时段前端 60s 轮询；非交易时段返回的仍是最后一次盘中采样（updated_at 可判新旧）。
    """
    return sector_mod.get_intraday()


@app.post("/api/sector/intraday/collect")
def sector_intraday_collect():
    """手动采样一轮盘中数据（平时用不到，主要给测试/补采）。"""
    result = sector_mod._intraday_collect()
    return {"ok": True, **result}


# ---------------- 报告 ----------------

@app.get("/api/reports")
def list_reports():
    """返回各日期目录下的报告文件清单（新日期在前）。"""
    reports = []
    for day_dir in sorted(REPORTS_DIR.iterdir(), reverse=True):
        if day_dir.is_dir():
            files = sorted(p.name for p in day_dir.glob("*.md"))
            if files:
                reports.append({"date": day_dir.name, "files": files})
    return reports


@app.get("/api/reports/{date}/{name}")
def get_report(date: str, name: str):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) or "/" in name or "\\" in name:
        raise HTTPException(400, "非法路径")
    path = REPORTS_DIR / date / name
    if not path.exists():
        raise HTTPException(404, "报告不存在")
    return {"date": date, "name": name, "content": path.read_text(encoding="utf-8")}


# ---------------- 每日报告（盘前简报 / 盘后复盘，daily_reports.py） ----------------
# 原为 Claude Code 会话级 cron（会话关掉即失效），现由本进程守护线程按时生成。

import daily_reports


def _report_calendar_lines() -> list[str]:
    """今明两日财经日历（规则事件 + 手动事件），给报告上下文。"""
    today = datetime.now().date()
    try:
        with get_conn() as conn:
            events = econ_calendar.upcoming_events(conn, months=1)
    except Exception:
        return []
    out = []
    for e in events:
        if e["date"] not in (today.isoformat(),
                             (today + timedelta(days=1)).isoformat()):
            continue
        mark = "⚡" if e.get("level") == "high" else "•"
        out.append(f"- {mark} {e['date']} {e.get('time') or ''} {e['title']}".rstrip())
    return out


def _report_paper_decisions() -> list[str]:
    """当日模拟交易决策（给盘后复盘）。"""
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT name, code, side, shares, price, confidence, reasoning "
                        "FROM sa_paper_trades WHERE trade_date = %s ORDER BY id", (datetime.now().date(),))
            rows = cur.fetchall()
    except Exception:
        return []
    label = {"buy": "买入", "sell": "卖出", "hold": "观望"}
    out = []
    for name, code, side, shares, price, conf, reason in rows:
        if side == "hold":
            out.append(f"- 观望 {name}（{code}）：置信度 {conf or 0}，{(reason or '')[:80]}")
        else:
            out.append(f"- {label.get(side, side)} {name}（{code}）{shares} 股 @ {price}，"
                       f"置信度 {conf or 0}，{(reason or '')[:80]}")
    return out


def _report_deps() -> dict:
    """给 daily_reports 注入依赖（该模块不 import app）。"""
    return {
        "get_conn": get_conn,
        "quote_fn": fetch_quotes,
        "volume_fn": watchlist_volume_status,
        "board_fn": sector_mod.stock_board_exposure,
        "holdings_fn": lambda: _holdings_with_pnl(_derive_holdings()),
        "events_fn": _advice_event_lines,
        "sector_fn": _advice_sector_lines,
        "calendar_fn": _report_calendar_lines,
        "news_fn": _paper_news_rows,
        "market_note_fn": _plan_market_note,
        "paper_fn": lambda: paper_trading.account_overview(_paper_deps()),
        "paper_decisions_fn": _report_paper_decisions,
        "crypto_fn": lambda: crypto_watch.crypto_lines(_crypto_deps()),
        "crypto_link_fn": lambda: crypto_watch.link_lines(_crypto_deps()),
        "mp_fn": lambda: wechat_mp.digest_lines(_mp_deps(), hours=36),
        "x_fn": lambda: x_monitor.digest_lines(_x_deps(), hours=24),
        "notify_fn": notifier.notify,
    }


_reports_state = {"premarket": False, "postmarket": False,
                  "last_premarket": None, "last_postmarket": None}


def _run_report(kind: str) -> None:
    """后台线程体：跑一次报告生成，异常记进 state 不外抛。"""
    _reports_state[kind] = True
    try:
        result = daily_reports.GENERATORS[kind](_report_deps())
        _reports_state[f"last_{kind}"] = result
        print(f"[reports] {kind} done: {result.get('path')}", flush=True)
    except Exception as exc:
        traceback.print_exc()
        _reports_state[f"last_{kind}"] = {"error": str(exc)}
    finally:
        _reports_state[kind] = False


@app.post("/api/reports/{kind}")
def report_generate(kind: str):
    """手动触发盘前简报/盘后复盘（后台线程执行，立即返回，约 1-3 分钟）。"""
    if kind not in daily_reports.GENERATORS:
        raise HTTPException(400, "kind 只能是 premarket 或 postmarket")
    if _reports_state[kind]:
        return {"ok": True, "status": "已在生成中，请稍后"}
    threading.Thread(target=_run_report, args=(kind,), daemon=True).start()
    return {"ok": True, "status": "已开始生成"}


@app.get("/api/reports/status")
def report_status():
    return _reports_state


def _daily_reports_loop():
    """日报守护线程：交易日按 schedule 段的两个时间点各跑一次（last_day 幂等守卫，
    仿 _paper_loop）。轮询 60s，改配置即生效，无需重启。

    迟到窗口 2 小时：服务在下午/晚上才启动时不再补一份过期的盘前简报（否则
    22:00 重启会推一份「今早简报」，内容全是昨天的收盘数据）。手动生成不受此限。
    """
    last_day = {"premarket": None, "postmarket": None}
    while True:
        try:
            now = datetime.now()
            if now.weekday() < 5:
                for kind, group, key in (("premarket", "premarket_report", "time"),
                                         ("postmarket", "postmarket_report", "time")):
                    group_conf = _conf_section("schedule").get(group) or {}
                    sh, sm = _sched_time(group, key)
                    due = now.hour * 60 + now.minute
                    if (group_conf.get("enabled", True)
                            and sh * 60 + sm <= due <= sh * 60 + sm + 120
                            and last_day[kind] != now.date()
                            and not _reports_state[kind]):
                        last_day[kind] = now.date()
                        _run_report(kind)   # 同步跑：本分钟只此一件，不占线程池
            time.sleep(60)
        except Exception as exc:
            print(f"[reports] loop error: {exc}", flush=True)
            time.sleep(300)


# ---------------- 币圈 24h 趋势参考（crypto_watch.py，gate.io 股票永续） ----------------

class CryptoWatchIn(BaseModel):
    contract: str = Field(min_length=3, max_length=32, description="合约名，如 TSLA_USDT")
    name: str = Field(default="", max_length=64)


class CryptoRefreshIn(BaseModel):
    links: bool = Field(default=False, description="是否顺便重算一遍「币↔股」关联统计")


def _crypto_deps() -> dict:
    return {"get_conn": get_conn, "notify_fn": notifier.notify}


@app.get("/api/crypto/quotes")
def crypto_quotes():
    """白名单合约的最新 24h 行情（页面卡片；超 5 分钟未刷新则现场取一次）。"""
    deps = _crypto_deps()
    try:
        items = crypto_watch.list_watch(deps)
    except Exception:
        crypto_watch._ensure_tables(deps)
        items = crypto_watch.list_watch(deps)
    fresh = items and all(
        i.get("ts") and
        (datetime.now(timezone.utc) - datetime.fromisoformat(i["ts"])).total_seconds() < 300
        for i in items)
    if not fresh:
        try:
            crypto_watch.run_once(deps)
            items = crypto_watch.list_watch(deps)
        except Exception as exc:
            print(f"[crypto] refresh failed: {exc}", flush=True)
    return {"source": "gate.io 股票永续（欧易 OKX 本机不可达）",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "items": items}


@app.post("/api/crypto/watch")
def crypto_watch_add(body: CryptoWatchIn):
    """添加白名单合约（XXX_USDT）。"""
    try:
        return crypto_watch.add_watch(_crypto_deps(), body.contract, body.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/crypto/watch/{contract}")
def crypto_watch_del(contract: str):
    """移出白名单（历史行情保留）。"""
    try:
        return crypto_watch.remove_watch(_crypto_deps(), contract)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/crypto/refresh")
def crypto_refresh(body: CryptoRefreshIn | None = None):
    """立即跑一轮（取价→入库→异动判断），页面「立即刷新」用。

    body={"links": true} 时额外重算一遍关联统计——set_link 明确不挂在写请求上
    （要发 2N 个 HTTP，最长 30s），由前端拿到 200 后自己带这个参数补算。
    """
    out: dict = {"ok": True, **crypto_watch.run_once(_crypto_deps())}
    if body is not None and body.links:
        out["links"] = crypto_watch.refresh_links(_crypto_deps())
    return out


# ---------------- 币圈 ↔ 股票 关联统计（crypto_watch.link_*，gate.io 永续 vs 日收盘） ----------------
# 回答「这只币和它锚定的那只股票到底是什么关系」：日涨跌相关系数、beta、两侧波动率、
# 比价偏离。数据层在 crypto_watch.py（link_metrics/align_series 纯函数 + 自建历史表
# sa_crypto_link_stats），这里只做 API 与页面数据组装。

@app.post("/api/crypto/links/refresh")
def crypto_links_refresh(contracts: str = ""):
    """重算关联统计（默认全部配了股票的合约）。串行，2N 个 HTTP 请求。

    ⚠️ 这条路由**必须**注册在下面 `/api/crypto/links/{contract}` 之前：FastAPI 按
    注册顺序匹配，否则 "refresh" 会被 {contract} 吃掉、当成合约名，然后因为缺 body
    报 422（2026-09-26 实测踩过）。
    """
    want = [c.strip().upper() for c in contracts.split(",") if c.strip()]
    try:
        return {"ok": True, **crypto_watch.refresh_links(_crypto_deps(), want or None)}
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(502, f"关联统计重算失败：{exc}")


class CryptoLinkIn(BaseModel):
    stock_code: str = Field(default="", max_length=16,
                            description="A股6位/港股4-5位/美股字母；空串=解除关联")


def _us_quote(ticker: str) -> dict:
    """美股实时快照（腾讯 qt.gtimg.cn）。关联标的可能是美股，而 fetch_quotes
    只认 A股/港股（6位/4-5位数字），所以这里单独取一份，只给页面显示用。"""
    t = (ticker or "").strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z.\-]{0,5}", t):
        return {}
    try:
        resp = requests.get(f"https://qt.gtimg.cn/q=us{t.replace('.', '-')}",
                            headers=HEADERS, timeout=15)
        resp.raise_for_status()
        text = resp.content.decode("gbk", "ignore")
    except Exception:
        return {}
    m = re.search(r'="([^"]*)"', text)
    if not m or not m.group(1):
        return {}
    f = m.group(1).split("~")
    if len(f) < 33 or not f[3]:
        return {}
    return {"code": t, "name": f[1], "market": "us", "currency": "USD",
            "price": _to_float(f[3]), "change_pct": _to_float(f[32])}


def _linked_stock_quote(code: str) -> dict:
    """关联股票的实时快照。A股/港股复用 fetch_quotes，美股走 _us_quote。"""
    if not code:
        return {}
    if re.fullmatch(r"\d{4,6}", code):
        q = fetch_quotes([code]).get(code) or {}
        return {k: q.get(k) for k in ("code", "name", "market", "currency",
                                      "price", "change_pct") if q.get(k) is not None}
    return _us_quote(code)


def _links_view(deps) -> dict:
    """白名单 + 关联统计 + 关联股票实时行情，合成页面表格要的一行行数据。"""
    watch = {w["contract"]: w for w in crypto_watch.list_watch(deps)}
    try:
        stats = {r["contract"]: r for r in crypto_watch.recent_links(deps)}
    except Exception as exc:
        print(f"[crypto] links read failed: {exc}", flush=True)
        stats = {}
    out = []
    for contract, w in watch.items():
        st = stats.get(contract)
        code = w.get("stock_code") or ""
        # 关联统计里的 stock_code 可能是刚改的（还没重算），以白名单为准
        if st and code and st.get("stock_code") != code:
            st = None
        if st and not code:
            st = None
        out.append({
            "contract": contract,
            "stock_code": code,
            "label": {"a": "A股", "hk": "港股", "us": "美股"}.get(
                (st or {}).get("market", ""), ""),
            "quote": _linked_stock_quote(code) if code else {},
            "stats": st,
        })
    return {"items": out, "updated_at": datetime.now().isoformat(timespec="seconds")}


@app.get("/api/crypto/links")
def crypto_links():
    """每个白名单合约的关联配置 + 最新关联统计（不触发重算，重算走 POST）。"""
    return _links_view(_crypto_deps())


@app.get("/api/crypto/links/{contract}")
def crypto_link_detail(contract: str):
    """单个合约的已对齐日K 序列（页面画「合约 vs 股票」双线图）。"""
    return crypto_watch.link_series(_crypto_deps(), contract)


@app.post("/api/crypto/links/{contract}")
def crypto_link_set(contract: str, body: CryptoLinkIn):
    """设置/解除关联股票。空 stock_code = 解除。认不出的代码 400。

    注：本路由要排在 `/api/crypto/links/refresh` 之后，否则 refresh 会被当合约名。
    """
    try:
        row = crypto_watch.set_link(_crypto_deps(), contract, body.stock_code)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    # 解除关联时把历史统计删掉，否则页面还会显示一条陈旧的 corr
    if not row.get("stock_code"):
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM sa_crypto_link_stats WHERE contract = %s",
                        (row["contract"],))
    return row


def _crypto_link_loop():
    """关联统计守护线程：与抓价周期解耦，按 crypto.link_refresh_hours（默认 6 小时）
    到点重算一轮 + 比价偏离告警。轮询 5 分钟，改配置即生效。"""
    while True:
        try:
            conf = crypto_watch.load_crypto_conf()
            if conf.get("link_enabled", True) and int(conf.get("link_refresh_hours") or 0) > 0:
                deps = _crypto_deps()
                if crypto_watch.links_due(deps):
                    result = crypto_watch.refresh_links(deps)
                    if result.get("computed"):
                        print(f"[crypto] link recomputed {result['ok']}/{result['computed']}"
                              f" contracts", flush=True)
                    try:
                        crypto_watch._check_link_alerts(
                            deps, crypto_watch.recent_links(deps), conf)
                    except Exception as exc:
                        print(f"[crypto] link alert failed: {exc}", flush=True)
        except Exception as exc:
            print(f"[crypto] link loop error: {exc}", flush=True)
        time.sleep(300)


def _crypto_loop():
    """币圈监控守护线程：按 crypto.interval_minutes（默认 15，0=关闭）取价入库 +
    24h 异动推送（阈值与冷却在 crypto 段，网页可改）。24h 连续交易，不限 A 股时段。"""
    while True:
        try:
            conf = crypto_watch.load_crypto_conf()
            interval = int(conf.get("interval_minutes") or 0)
            if conf.get("enabled", True) and interval > 0:
                result = crypto_watch.run_once(_crypto_deps())
                if result.get("contracts"):
                    print(f"[crypto] {result['contracts']} contracts updated"
                          + (f", {result['pushed']} alerts" if result.get("pushed") else ""),
                          flush=True)
                time.sleep(max(interval, 1) * 60)
            else:
                time.sleep(300)
        except Exception as exc:
            print(f"[crypto] loop error: {exc}", flush=True)
            time.sleep(300)


# ---------------- 微信公众号文章（wechat_mp.py：拉 RSS 源，见该文件头选型说明） ----------------

def _mp_deps() -> dict:
    return {"get_conn": get_conn, "notify_fn": notifier.notify}


class MpSourceIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    kind: str = Field(default="rss", description="werss=WeRSS 服务；rss=完整订阅地址")
    url: str = Field(default="", description="留空则用 config.yaml 的 mp.base_url")
    feed_id: str = Field(default="all", max_length=64)
    note: str = Field(default="", max_length=500)
    use_fresh: bool | None = Field(
        default=None, description="null=跟随全局 mp.use_fresh；true/false=本源覆盖")
    auth: str | None = Field(default=None, max_length=500,
                             description="本源专用 Authorization 头；留空=用全局 mp.auth")


class MpSourcePatchIn(BaseModel):
    name: str | None = None
    kind: str | None = None
    url: str | None = None
    feed_id: str | None = None
    enabled: bool | None = None
    note: str | None = None
    use_fresh: bool | None = None
    auth: str | None = None


class MpReadIn(BaseModel):
    ids: list[int] | None = None
    all: bool = False


@app.get("/api/mp/sources")
def mp_sources():
    return {"items": wechat_mp.list_sources(_mp_deps())}


@app.post("/api/mp/sources")
def mp_source_add(body: MpSourceIn):
    try:
        return wechat_mp.add_source(_mp_deps(), body.name, body.url, body.kind,
                                    body.feed_id, body.note, body.use_fresh, body.auth)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.patch("/api/mp/sources/{sid}")
def mp_source_patch(sid: int, body: MpSourcePatchIn):
    # 2026-09-29 修：原来用 model_dump() 再 `if v is not None` 过滤，
    # 于是「显式传 null」与「没传这个字段」变得无法区分 —— 而对
    # use_fresh / auth 来说 null 恰恰是有意义的值（= 交回全局 config.yaml）。
    # 实际后果：想把某个源改回「跟随全局」时收到 400「没有要改的字段」，
    # 只能绕过接口直接改数据库。
    # model_dump(exclude_unset=True) 只返回请求里**真正出现过**的键，
    # null 也会原样保留，交给 update_source 去写 NULL。
    try:
        return wechat_mp.update_source(_mp_deps(), sid,
                                      **body.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/mp/sources/{sid}")
def mp_source_del(sid: int):
    try:
        return wechat_mp.remove_source(_mp_deps(), sid)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/mp/articles")
def mp_articles(source_id: int = 0, unread_only: bool = False, limit: int = 50):
    return {"items": wechat_mp.list_articles(_mp_deps(), source_id or None,
                                              unread_only, limit),
            "unread": wechat_mp.unread_count(_mp_deps())}


@app.post("/api/mp/read")
def mp_read(body: MpReadIn):
    return {"updated": wechat_mp.mark_read(_mp_deps(), body.ids, body.all)}


@app.post("/api/mp/fetch")
def mp_fetch():
    """手动拉一轮。同步跑（每源 1 个请求，25s 超时），页面按钮转圈等结果。"""
    result = wechat_mp.run_once(_mp_deps())
    if result.get("error"):
        raise HTTPException(502, result["error"])
    return result


@app.get("/api/mp/status")
def mp_status():
    return wechat_mp.get_status(_mp_deps())


@app.get("/api/mp/digest")
def mp_digest(hours: int = 36):
    lines = wechat_mp.digest_lines(_mp_deps(), hours)
    return {"hours": hours, "lines": lines, "markdown": "\n".join(lines)}


def _mp_loop():
    """公众号守护线程：按 mp.interval_minutes（默认 30，0=关闭）拉一轮 + 新文推微信。"""
    while True:
        try:
            conf = wechat_mp.load_mp_conf()
            interval = int(conf.get("interval_minutes") or 0)
            if conf.get("enabled", True) and interval > 0:
                result = wechat_mp.run_once(_mp_deps())
                if result.get("new"):
                    print(f"[mp] {result['new']} new articles from "
                          f"{len(result.get('items') or [])} sources", flush=True)
                time.sleep(max(interval, 1) * 60)
            else:
                time.sleep(300)
        except Exception as exc:
            print(f"[mp] loop error: {exc}", flush=True)
            time.sleep(300)


# ---------------- X(Twitter) 指定用户发言（x_monitor.py + twscrape） ----------------

def _x_deps() -> dict:
    return {"get_conn": get_conn, "notify_fn": notifier.notify}


class XAccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    cookies: str = Field(min_length=10, description="auth_token=…; ct0=…")


class XWatchIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    note: str = Field(default="", max_length=500)
    with_replies: bool = False
    min_likes: int = Field(default=0, ge=0)


class XWatchPatchIn(BaseModel):
    note: str | None = None
    enabled: bool | None = None
    with_replies: bool | None = None
    min_likes: int | None = Field(default=None, ge=0)


class XReadIn(BaseModel):
    ids: list[int] | None = None
    all: bool = False


@app.get("/api/x/status")
def x_status():
    return x_monitor.get_status(_x_deps())


@app.get("/api/x/accounts")
def x_accounts():
    return {"items": x_monitor.list_accounts(_x_deps())}


@app.post("/api/x/accounts")
def x_account_save(body: XAccountIn):
    try:
        return x_monitor.save_account(_x_deps(), body.name, body.cookies)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/x/accounts/test")
def x_account_test(body: XAccountIn):
    return x_monitor.test_account(_x_deps(), body.name, body.cookies)


@app.delete("/api/x/accounts/{aid}")
def x_account_del(aid: int):
    try:
        return x_monitor.remove_account(_x_deps(), aid)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/x/watch")
def x_watch():
    return {"items": x_monitor.list_watch(_x_deps())}


@app.post("/api/x/watch")
def x_watch_add(body: XWatchIn):
    try:
        return x_monitor.add_watch(_x_deps(), body.username, body.note,
                                   body.with_replies, body.min_likes)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.patch("/api/x/watch/{wid}")
def x_watch_patch(wid: int, body: XWatchPatchIn):
    try:
        return x_monitor.update_watch(_x_deps(), wid,
                                      **{k: v for k, v in body.model_dump().items()
                                         if v is not None})
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/x/watch/{wid}")
def x_watch_del(wid: int):
    try:
        return x_monitor.remove_watch(_x_deps(), wid)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/x/tweets")
def x_tweets(watch_id: int = 0, unread_only: bool = False, limit: int = 50):
    return {"items": x_monitor.list_tweets(_x_deps(), watch_id or None,
                                           unread_only, limit),
            "unread": x_monitor.unread_count(_x_deps())}


@app.post("/api/x/read")
def x_read(body: XReadIn):
    return {"updated": x_monitor.mark_read(_x_deps(), body.ids, body.all)}


@app.post("/api/x/fetch")
def x_fetch():
    """手动抓一轮。同步跑（每人一次 GraphQL，账号池内部轮换）。"""
    result = x_monitor.run_once(_x_deps())
    if result.get("error"):
        raise HTTPException(502, result["error"])
    return result


def _x_loop():
    """X 监控守护线程：按 x.interval_minutes（默认 30，0=关闭）抓一轮 + 新推文推微信。
    未配账号 / 未装 twscrape 时轮内直接跳过（每轮只读一次本地表，很便宜）。"""
    while True:
        try:
            conf = x_monitor.load_x_conf()
            interval = int(conf.get("interval_minutes") or 0)
            if conf.get("enabled", True) and interval > 0:
                result = x_monitor.run_once(_x_deps())
                if result.get("new"):
                    print(f"[x] {result['new']} new tweets", flush=True)
                elif result.get("error"):
                    print(f"[x] {result['error']}", flush=True)
                time.sleep(max(interval, 1) * 60)
            else:
                time.sleep(300)
        except Exception as exc:
            print(f"[x] loop error: {exc}", flush=True)
            time.sleep(300)


@app.get("/api/health")
def health():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1")
    # 顺带报一下策略监控状态（pending = 引用了策略且未触发的买入笔数）
    pending = 0
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM sa_trades t "
                        "JOIN sa_strategies s ON s.id = t.strategy_id "
                        "WHERE t.strategy_triggered_at IS NULL AND t.side = 'buy'")
            pending = cur.fetchone()[0]
    except Exception:
        pass
    return {"ok": True, "db": "cloud-pg",
            "strategy_pending": pending,
            "time": datetime.now().isoformat(timespec="seconds")}


# ---------------- 页面 ----------------

@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


def _normalize_code(code: str) -> str:
    """A股：6 位数字；港股：4-5 位数字（统一补齐成 5 位，如 700 -> 00700）。"""
    code = (code or "").strip()
    if re.fullmatch(r"\d{6}", code):
        return code
    if re.fullmatch(r"\d{4,5}", code):
        return code.zfill(5)
    raise HTTPException(400, "股票代码格式：A股 6 位数字（600519）；港股 4-5 位数字（00700 或 700）")


# ---------------------------------------------------------------------------
# 一次性副作用守卫
# ---------------------------------------------------------------------------
# 为什么需要：本文件以 `python app.py` 跑时模块名是 __main__，而
# bili_monitor / news_fetcher / wb_monitor / notifier / sector 里有
# `from app import get_conn` 这种**延迟导入**（为避开循环依赖）。第一次触发时
# Python 会把 app.py 再当作模块 `app` 完整执行一遍 —— 于是 init_db() 跑两次、
# 下面十几个守护线程也全部起了两份（实测 _paper_loop 同时存在 Thread-11 和
# Thread-28，各有各的 last_slot，于是同一个 slot 被判了两轮）。
#
# 这里用**进程级**判据而不是模块级标志：重复的那个是另一个模块对象，
# 模块级变量对它来说是全新的，挡不住。threading.enumerate() 是进程全局的，
# 按线程名去重才有效。
_BOOTSTRAPPED = "sa_bootstrap_done"


def _bootstrap_once() -> bool:
    """整个进程里只执行一次建表/种子/守护线程。返回是否真的执行了。"""
    import sys
    if getattr(sys, _BOOTSTRAPPED, False):
        return False
    setattr(sys, _BOOTSTRAPPED, True)
    return True


def _start_daemon(target, name: str, args=()) -> bool:
    """按名字启动守护线程，**同一进程内每个名字只启动一次**。
    import sys 放在函数内：模块级已经有不少 import，这里不引入新的模块级依赖。

    为什么不用 `threading.enumerate()` 判活：那个判据依赖「线程此刻还活着」，
    一旦某个循环退出过（异常逃逸、或以后改成可结束的形态），重新导入模块时
    就会把它再起一份 —— 2026-09-28 实测 sa_mp / sa_x 就各多了一份。
    改用挂在 sys 上的进程级集合：模块对象有几个都无所谓，进程内唯一。

    sys 里的键不能用一个裸常量（模块各自一份），所以用固定字符串。
    """
    import sys
    started = getattr(sys, "sa_started_daemons", None)
    if started is None:
        started = set()
        setattr(sys, "sa_started_daemons", started)
    if name in started:
        return False
    started.add(name)
    threading.Thread(target=target, args=args, name=name, daemon=True).start()
    return True


# 全局单连接复用不需要：每请求短连接即可（本地工具规模）
_BOOTSTRAP = _bootstrap_once()
if _BOOTSTRAP:
    init_db()

# 币圈白名单/行情表（幂等 DDL + 8 只种子合约，ON CONFLICT DO NOTHING）
if _BOOTSTRAP:
    try:
        crypto_watch._ensure_tables(_crypto_deps())
    except Exception as exc:
        print(f"[crypto] init tables failed: {exc}", flush=True)

    # 公众号订阅源/文章表（幂等 DDL）
    try:
        wechat_mp._ensure_tables(_mp_deps())
    except Exception as exc:
        print(f"[mp] init tables failed: {exc}", flush=True)

    # X 账号/监听用户/推文表（幂等 DDL）
    try:
        x_monitor._ensure_tables(_x_deps())
    except Exception as exc:
        print(f"[x] init tables failed: {exc}", flush=True)

# 自动新闻抓取后台线程（fetch_interval_minutes=0 时轮内直接跳过）
_start_daemon(_auto_fetch_loop, "sa_auto_fetch")

# B站动态定时抓取后台线程（bili.interval_minutes=0 时轮内直接跳过）
_start_daemon(_bili_auto_loop, "sa_bili")

# 微博博主动态定时抓取后台线程（wb.interval_minutes=0 时轮内直接跳过）
_start_daemon(_wb_auto_loop, "sa_wb")

# 止盈策略监控后台线程（交易时段每 60s 扫一次，触发即通知）
_start_daemon(_strategy_loop, "sa_strategy")

# 板块轮动快照后台线程（交易日收盘后自动采集当日板块全量，供轮动分析）
_start_daemon(sector_mod._sector_auto_loop, "sa_sector")

# 板块盘中监控线程（交易时段每 5 分钟采样 + 急拉/涨停骤增预警；config.yaml sector 段可配）
_start_daemon(sector_mod._intraday_loop, "sa_intraday")

# 提款计划检查线程（交易日 15:10 起半小时查达标/临期，里程碑推微信）
_start_daemon(_withdrawal_loop, "sa_withdrawal")

# 财经日历盘前提醒线程（交易日早 8–12 点窗口，今明事件/重要预告推微信）
_start_daemon(_calendar_loop, "sa_calendar")

# 自选股事件告警线程（工作日 8:30 后每日一轮：解禁/增发上市新事件推微信）
_start_daemon(_alerts_loop, "sa_alerts")

# 盘中大盘量能监控线程（交易时段每 5 分钟采量比，放量/缩量翻转推微信；volume 段可配）
_start_daemon(_market_volume_loop, "sa_market_volume")

# 模拟交易线程（交易日盘中每 interval_minutes 判断一轮 / 16:10 结算复盘；
# paper.enabled=false 轮内跳过）
_start_daemon(_paper_loop, "sa_paper")

# 每日报告线程（盘前简报 / 盘后复盘；时间点见 config.yaml schedule 段，网页可改）
_start_daemon(_daily_reports_loop, "sa_daily_reports")

# 币圈 24h 监控线程（gate.io 股票永续；crypto.interval_minutes=0 轮内跳过）
_start_daemon(_crypto_loop, "sa_crypto")

# 币圈↔股票关联统计线程（crypto.link_refresh_hours 控制重算节奏，与抓价周期解耦）
_start_daemon(_crypto_link_loop, "sa_crypto_link")

# 公众号文章监控线程（wechat_mp.py；mp.interval_minutes=0 轮内跳过）
_start_daemon(_mp_loop, "sa_mp")

# X(Twitter) 指定用户发言监控线程（x_monitor.py；x.interval_minutes=0 或未配账号时跳过）
_start_daemon(_x_loop, "sa_x")

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8686)
