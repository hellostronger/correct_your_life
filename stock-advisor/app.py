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
from datetime import datetime
from pathlib import Path

import psycopg2
import psycopg2.extras
import requests
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"          # 兼容保留：迁移前旧 JSON 数据所在目录
REPORTS_DIR = BASE_DIR / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

ENV_FILE = BASE_DIR.parent / ".env"


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


def fetch_kline_volumes(symbol: str, days: int = VOL_MA_DAYS + 2) -> list[float]:
    """取某证券近 N 个交易日成交量列表（升序，最后一个元素=最近交易日）。

    symbol 用腾讯符号（sh600519 / hkHSI / hk00700）。失败返回 []。
    """
    try:
        resp = requests.get(
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            params={"param": f"{symbol},day,,,{days},qfq"},
            headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json().get("data", {}).get(symbol, {})
        bars = data.get("qfqday") or data.get("day") or []
        vols = [_to_float(bar[5]) for bar in bars if len(bar) > 5]
        return [v for v in vols if v]
    except Exception:
        return []


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


# ---------------- 自选股 CRUD ----------------

@app.get("/api/watchlist")
def list_watchlist(with_quotes: bool = True):
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT code, name, note, added_at FROM sa_watchlist ORDER BY added_at")
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
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sa_watchlist (code, name, note) VALUES (%s,%s,%s) "
            "ON CONFLICT (code) DO UPDATE SET note = EXCLUDED.note, name = EXCLUDED.name",
            (code, quote["name"], stock.note.strip()))
    item = {"code": code, "name": quote["name"], "note": stock.note.strip()}
    item["quote"] = quote
    return item


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


def _holdings_with_pnl(positions: list[dict]) -> list[dict]:
    """给持仓汇总挂实时行情，算浮动盈亏与累计总盈亏（浮动 + 已实现）。"""
    held = [p for p in positions if p["net_shares"] > 0]
    quotes = fetch_quotes([p["code"] for p in held]) if held else {}
    for p in positions:
        q = quotes.get(p["code"], {}) if p["net_shares"] > 0 else {}
        p["quote"] = q
        price = q.get("price")
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
    return _holdings_with_pnl(_derive_holdings())


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
    """止盈策略模板列表（新→旧），附引用它们的交易数。"""
    sql = ("SELECT s.*, COUNT(t.id) FILTER (WHERE t.id IS NOT NULL) AS used_count, "
           "COUNT(t.id) FILTER (WHERE t.strategy_triggered_at IS NOT NULL) AS triggered_count "
           "FROM sa_strategies s LEFT JOIN sa_trades t ON t.strategy_id = s.id "
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
    """删除策略模板。若已有交易引用则拒绝（先删/改那些交易），避免流水历史悬空。"""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM sa_trades WHERE strategy_id = %s", (strategy_id,))
        used = cur.fetchone()[0]
        if used:
            raise HTTPException(400, f"该策略已被 {used} 笔交易引用，不能删除（可先删对应交易）")
        cur.execute("DELETE FROM sa_strategies WHERE id = %s RETURNING id", (strategy_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, f"策略 #{strategy_id} 不存在")
    return {"ok": True, "removed": strategy_id}


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
                time.sleep(60)
            else:
                time.sleep(300)
        except Exception as exc:
            print(f"[strategy] loop error: {exc}", flush=True)
            time.sleep(300)


@app.post("/api/strategies/check")
def strategies_check():
    """手动触发一次止盈检查（也供 Claude 定时任务调用）。"""
    return {"ok": True, "triggered": check_strategies_once()}


# ---------------- 新闻（多渠道免费抓取，见 news_fetcher.py） ----------------

import news_fetcher
import notifier

CONFIG_FILE = BASE_DIR / "config.yaml"

_news_state = {"fetching": False, "last_run": None, "last_result": None}


def _read_conf() -> dict:
    return news_fetcher.load_config()


def _write_conf(conf: dict) -> None:
    """把新闻配置写回 config.yaml（notify / bili 段由各自模块维护）。

    config.yaml 有 news / bili / notify 三个模块段，整文件重写会互相踩，
    所以只替换 news: 块本身的行（含其上方紧邻的注释头），其余原样保留。
    """
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
           "n.publish_time, n.fetched_at FROM sa_news n ")
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


# 全局单连接复用不需要：每请求短连接即可（本地工具规模）
init_db()

# 自动新闻抓取后台线程（fetch_interval_minutes=0 时轮内直接跳过）
threading.Thread(target=_auto_fetch_loop, daemon=True).start()

# B站动态定时抓取后台线程（bili.interval_minutes=0 时轮内直接跳过）
threading.Thread(target=_bili_auto_loop, daemon=True).start()

# 微博博主动态定时抓取后台线程（wb.interval_minutes=0 时轮内直接跳过）
threading.Thread(target=_wb_auto_loop, daemon=True).start()

# 止盈策略监控后台线程（交易时段每 60s 扫一次，触发即通知）
threading.Thread(target=_strategy_loop, daemon=True).start()

# 板块轮动快照后台线程（交易日收盘后自动采集当日板块全量，供轮动分析）
threading.Thread(target=sector_mod._sector_auto_loop, daemon=True).start()

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8686)
