"""币圈 24h 趋势参考：gate.io 上的股票永续合约行情（crypto_watch.py）。

为什么不是欧易(OKX)：2026-09-25 实测本机直连 okx.com / www / aws / app 全部
ConnectionError，走本机 WinINET 代理(127.0.0.1:6478) 也 ProxyError——OKX 在这台
机器上不可用。gate.io(api.gateio.ws) 直连全通，15/15 连续请求无 429，作为平替源。

为什么是永续而不是 xStocks 现货：gate.io 上 AAPL/TSLA/NVDA/HOOD/CRCL/MSTR/COIN
_USDT 永续 24h 成交额 2.2M~16.8M USDT，而 xStocks 现货（*X_USDT）只有
4.7万~64万，差 10~30 倍。永续价格锚定美股现货（可能有小幅溢价/折价），
作趋势参考够用，页面会标注这一点。

接口（2026-09-25 实测字段）：
- GET /api/v4/futures/usdt/tickers            全量 1013 合约，一次取回再本地筛
    contract / last / change_percentage / high_24h / low_24h / volume_24h_quote
    / mark_price / funding_rate（股票永续恒为 0，无参考价值，不用）
- GET /api/v4/futures/usdt/candlesticks?contract=X&interval=1d&limit=N
    命名字典 [{t, o, h, l, c, v, sum}]，t = UTC 当日 0 点
- GET /api/v4/spot/tickers?currency_pair=BTC_USDT   现货（盘前简报的全球情绪参考）

本模块不 import app（避免循环依赖）：数据函数与配置由 app.py 注入。
"""

import json
import re
import threading
import time

from psycopg2.extras import execute_values
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "data" / "crypto_alert_state.json"

FUTURES_TICKERS_URL = "https://api.gateio.ws/api/v4/futures/usdt/tickers"
FUTURES_KLINE_URL = "https://api.gateio.ws/api/v4/futures/usdt/candlesticks"
SPOT_TICKERS_URL = "https://api.gateio.ws/api/v4/spot/tickers"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

DEFAULT_CRYPTO_CONF = {
    "enabled": True,
    "interval_minutes": 15,     # 抓价周期；0 = 关闭
    "alert_threshold_pct": 3,   # |24h 涨跌| 超此值推微信
    "alert_cooldown_hours": 6,  # 同一标的同方向推送冷却
    # 关联统计（合约 ↔ 股票 的相关系数/beta，见 link_metrics）
    "link_enabled": True,
    "link_refresh_hours": 6,    # 关联统计最长多久重算一次（抓价周期独立于它）
    "link_lookback_days": 45,   # 币圈日K 取多少根
    "link_min_overlap": 9,      # 重叠收盘价点数下限（保证收益样本 >= 8）
    # 比价偏离告警：|比价/中位比价-1| 超此值(%) 推微信；0 = 关闭。
    # 比价=股票收盘/合约收盘，永续锚定现货时它应该是一条横线；突然偏离
    # 说明合约被溢价/折价（或股票有除权跳空），是最值得盯的一类异动。
    "link_alert_dev_pct": 5,
    "link_alert_cooldown_hours": 12,
}

# 种子白名单：gate.io 上成交额靠前的合约（2026-09-25 实测流动性排序）
# 第三项 = 关联股票代码（留空 = 不关联）。合约名和股票代码对不上是常态
# （ZHIPU_USDT 对应港股 02513 智谱、MINIMAX_USDT 对应 00100），只有美股那批同名，
# 所以映射必须显式写，页面上也能改。
SEED_CONTRACTS = [
    ("CRCL_USDT", "Circle", "CRCL"),
    ("MSTR_USDT", "MicroStrategy", "MSTR"),
    ("NVDA_USDT", "英伟达", "NVDA"),
    ("ZHIPU_USDT", "智谱", "02513"),
    ("TSLA_USDT", "特斯拉", "TSLA"),
    ("COIN_USDT", "Coinbase", "COIN"),
    ("AAPL_USDT", "苹果", "AAPL"),
    ("HOOD_USDT", "Robinhood", "HOOD"),
]

# 盘前简报固定附带的全球情绪参考（现货，24h 连续交易）
SPOT_REFS = [("BTC_USDT", "BTC"), ("ETH_USDT", "ETH")]

CONTRACT_RE = re.compile(r"^[A-Z0-9]{2,20}_USDT$")

_state_lock = threading.Lock()


# ---------------- 配置 ----------------

def load_crypto_conf() -> dict:
    """读 config.yaml 的 crypto 段，补齐默认（每轮现读，改配置即生效）。"""
    conf = dict(DEFAULT_CRYPTO_CONF)
    try:
        import yaml
        path = BASE_DIR / "config.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        conf.update({k: v for k, v in (data.get("crypto") or {}).items() if v is not None})
    except Exception:
        pass
    return conf


# ---------------- 行情抓取 ----------------

def _get_json(url: str, params: dict | None = None, timeout: int = 15):
    """GET 一个 JSON 接口。requests 显式 trust_env=False 绕开系统代理
    （与 app.py 的 NO_PROXY=* 同策；这里再显式关一次，双保险）。"""
    import requests
    sess = requests.Session()
    sess.trust_env = False
    resp = sess.get(url, params=params, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _f(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def fetch_tickers(contracts: list[str]) -> dict:
    """取白名单合约的 24h 行情。全量 tickers 一次取回再本地筛（单次约 200KB，
    7 个合约各发一次请求反而慢且更容易被限流）。返回 {contract: {...}}。"""
    wanted = {c.upper() for c in contracts if CONTRACT_RE.match(c.upper())}
    if not wanted:
        return {}
    data = _get_json(FUTURES_TICKERS_URL, timeout=20)
    out: dict[str, dict] = {}
    for row in data or []:
        name = (row.get("contract") or "").upper()
        if name not in wanted:
            continue
        out[name] = {
            "contract": name,
            "last": _f(row.get("last")),
            "change_pct": _f(row.get("change_percentage")),
            "high_24h": _f(row.get("high_24h")),
            "low_24h": _f(row.get("low_24h")),
            "vol_quote": _f(row.get("volume_24h_quote")),
            "mark_price": _f(row.get("mark_price")),
        }
    return out


def fetch_spot(pairs: list[str] | None = None) -> dict:
    """现货 24h 行情（盘前简报用）。逐对请求（现货全量更大且无需全量）。"""
    out: dict[str, dict] = {}
    for pair in (pairs or [p for p, _ in SPOT_REFS]):
        pair = pair.upper()
        if not CONTRACT_RE.match(pair):
            continue
        try:
            rows = _get_json(SPOT_TICKERS_URL, params={"currency_pair": pair}, timeout=12)
        except Exception:
            continue
        if not rows:
            continue
        row = rows[0] if isinstance(rows, list) else rows
        out[pair] = {
            "pair": pair,
            "last": _f(row.get("last")),
            "change_pct": _f(row.get("change_percentage")),
            "high_24h": _f(row.get("high_24h")),
            "low_24h": _f(row.get("low_24h")),
        }
    return out


def fetch_daily_closes(contract: str, days: int = 30) -> list[dict]:
    """日 K 收盘序列（新→旧），供后续画迷你趋势。当前只存不用。"""
    rows = _get_json(FUTURES_KLINE_URL,
                     params={"contract": contract.upper(), "interval": "1d", "limit": days},
                     timeout=15)
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        out.append({"t": r.get("t"), "close": _f(r.get("c")),
                    "high": _f(r.get("h")), "low": _f(r.get("l"))})
    out.sort(key=lambda x: x["t"] or 0, reverse=True)
    return out


# ---------------- 白名单（sa_crypto_watch） ----------------

def _ensure_tables(deps) -> None:
    """建表 + 首次种子。

    种子只在**表刚建出来**这一次播：白名单是用户自己维护的清单，若每次启动都
    ON CONFLICT DO NOTHING 补一遍，用户删掉的标的会在下次重启时复活
    （2026-09-25 实测：用户删掉 7 只，重启后又全回来了）。
    """
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('sa_crypto_watch')")
        fresh_install = cur.fetchone()[0] is None
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sa_crypto_watch (
                id          BIGSERIAL PRIMARY KEY,
                contract    VARCHAR(32) NOT NULL UNIQUE,
                name        VARCHAR(64) NOT NULL DEFAULT '',
                enabled     BOOLEAN NOT NULL DEFAULT TRUE,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sa_crypto_quotes (
                id          BIGSERIAL PRIMARY KEY,
                contract    VARCHAR(32) NOT NULL,
                last        NUMERIC(18,6),
                change_pct  NUMERIC(10,4),
                high_24h    NUMERIC(18,6),
                low_24h     NUMERIC(18,6),
                vol_quote   NUMERIC(20,2),
                ts          TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sa_crypto_quotes "
                    "ON sa_crypto_quotes (contract, ts DESC)")
        # 迁移：CREATE TABLE IF NOT EXISTS 不会给已存在的表加列，必须显式 ALTER。
        # ⚠️ fresh_install 的求值位置不能动——它隐式依赖「SELECT to_regclass
        # 早于所有 DDL」，挪到最后就变成恒 False，用户删掉的合约每次重启复活。
        # 但这条 ALTER 必须在下面的种子 INSERT 之前：建表 DDL 里没有 stock_code 列，
        # 新库上先 INSERT 会报 column does not exist 并回滚整个事务，
        # sa_crypto_watch/quotes/link_stats 三张表一起建不出来（2026-09-29 首次部署实测）。
        cur.execute("ALTER TABLE sa_crypto_watch ADD COLUMN IF NOT EXISTS "
                    "stock_code VARCHAR(16) NOT NULL DEFAULT ''")

        if fresh_install:
            for contract, name, stock_code in SEED_CONTRACTS:
                cur.execute("INSERT INTO sa_crypto_watch (contract, name, stock_code) "
                            "VALUES (%s, %s, %s) ON CONFLICT (contract) DO NOTHING",
                            (contract, name, stock_code))
        # 首次建表时种下的关联写进老行（此时表是空的，不存在"覆盖用户选择"的问题）
        if fresh_install:
            for contract, _name, stock_code in SEED_CONTRACTS:
                if stock_code:
                    cur.execute("UPDATE sa_crypto_watch SET stock_code = %s "
                                "WHERE contract = %s", (stock_code, contract))

        cur.execute("""
            CREATE TABLE IF NOT EXISTS sa_crypto_link_stats (
                stat_date     DATE        NOT NULL,   -- 最后一个重叠交易日，不是计算当天
                contract      VARCHAR(32) NOT NULL,
                stock_code    VARCHAR(16) NOT NULL DEFAULT '',
                stock_symbol  VARCHAR(24) NOT NULL DEFAULT '',
                tx_symbol     VARCHAR(32) NOT NULL DEFAULT '',  -- 腾讯日K 实际用的符号（审计用）
                market        VARCHAR(8)  NOT NULL DEFAULT '',
                currency      VARCHAR(8)  NOT NULL DEFAULT '',
                overlap_days  INTEGER     NOT NULL DEFAULT 0,  -- 重叠的收盘价点数
                n_obs         INTEGER     NOT NULL DEFAULT 0,  -- 收益样本数 = overlap_days - 1
                corr          NUMERIC(10,4),
                beta          NUMERIC(12,4),
                alpha_daily   NUMERIC(12,4),
                vol_crypto    NUMERIC(10,4),
                vol_stock     NUMERIC(10,4),
                ratio         NUMERIC(14,4),
                ratio_ma      NUMERIC(14,4),
                ratio_dev_pct NUMERIC(10,4),
                crypto_close  NUMERIC(18,6),
                stock_close   NUMERIC(18,4),
                series        JSONB NOT NULL DEFAULT '{}'::jsonb,
                status        VARCHAR(16) NOT NULL DEFAULT 'ok',
                error         VARCHAR(255) NOT NULL DEFAULT '',
                computed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (stat_date, contract)
            )""")
        # 读路径是 DISTINCT ON (contract) ... ORDER BY contract, stat_date DESC，
        # 而 PK 顺序是 (stat_date, contract) 正好相反，帮不上忙，所以要这个索引。
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sa_crypto_link_recent "
                    "ON sa_crypto_link_stats (contract, stat_date DESC)")
        # 2026-09-26 补：secid（东财日K 用的市场号）——美股要先解析交易所后缀才能定，
        # 而东财是唯一能一条 secid 打通 A股/港股/美股日K 的源，故落库备查。
        cur.execute("ALTER TABLE sa_crypto_link_stats ADD COLUMN IF NOT EXISTS "
                    "secid VARCHAR(24) NOT NULL DEFAULT ''")
        # 每合约每天一行，不清会一直涨（一年 365 行/合约）
        cur.execute("DELETE FROM sa_crypto_link_stats "
                    "WHERE stat_date < current_date - 90")


def list_watch(deps) -> list[dict]:
    """白名单（按创建顺序），带最新一条行情（没有则字段为 None）。"""
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, contract, name, stock_code, enabled, created_at "
                    "FROM sa_crypto_watch ORDER BY id")
        cols = ("id", "contract", "name", "stock_code", "enabled", "created_at")
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    quotes = latest_quotes(deps, [r["contract"] for r in rows])
    for r in rows:
        q = quotes.get(r["contract"]) or {}
        r["last"] = q.get("last")
        r["change_pct"] = q.get("change_pct")
        r["high_24h"] = q.get("high_24h")
        r["low_24h"] = q.get("low_24h")
        r["vol_quote"] = q.get("vol_quote")
        r["ts"] = q.get("ts")
    return rows


def latest_quotes(deps, contracts: list[str]) -> dict:
    """每个合约最新一行 sa_crypto_quotes。"""
    if not contracts:
        return {}
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT ON (contract) contract, last, change_pct, high_24h, "
                    "low_24h, vol_quote, ts FROM sa_crypto_quotes "
                    "WHERE contract = ANY(%s) ORDER BY contract, ts DESC, id DESC",
                    (contracts,))
        out = {}
        for r in cur.fetchall():
            d = {"contract": r[0], "last": _f(r[1]), "change_pct": _f(r[2]),
                 "high_24h": _f(r[3]), "low_24h": _f(r[4]), "vol_quote": _f(r[5])}
            d["ts"] = r[6].isoformat(timespec="seconds") if r[6] else None
            out[r[0]] = d
        return out


def add_watch(deps, contract: str, name: str = "") -> dict:
    contract = (contract or "").strip().upper()
    if not CONTRACT_RE.match(contract):
        raise ValueError("合约名格式应为 XXX_USDT（如 TSLA_USDT）")
    # 已知合约自动带上关联代码（用户之后可以在页面上改）
    auto = next((c for c, _n, s in SEED_CONTRACTS if c == contract and s), "")
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO sa_crypto_watch (contract, name, stock_code) "
                    "VALUES (%s, %s, %s) "
                    "ON CONFLICT (contract) DO UPDATE SET enabled = TRUE RETURNING id",
                    (contract, name.strip() or contract.split("_")[0], auto))
        cid = cur.fetchone()[0]
    return {"id": cid, "contract": contract,
            "name": name.strip() or contract.split("_")[0], "stock_code": auto}


def set_link(deps, contract: str, stock_code: str) -> dict:
    """设置/解除关联股票代码（空串解除）。认不出的代码抛 ValueError → API 400。

    不在这里触发重算：重算要发 HTTP（最多 30s），不能挂在写请求上。由调用方
    （前端）在拿到 200 后自己调 POST /api/crypto/refresh {links:true}。
    """
    contract = (contract or "").strip().upper()
    code = (stock_code or "").strip().upper()
    market = ""
    if code:
        sym = resolve_symbol(code)
        if not sym:
            raise ValueError(
                f"无法识别代码「{stock_code}」：请填 6 位 A 股（如 600519）、"
                f"5 位港股（如 02513）或美股字母代码（如 TSLA）；中文名不支持")
        code = sym["code"]          # 归一（港股左侧补零、美股大写）
        market = sym["market"]
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("UPDATE sa_crypto_watch SET stock_code = %s WHERE contract = %s "
                    "RETURNING id, contract, name, stock_code", (code, contract))
        row = cur.fetchone()
    if not row:
        raise ValueError(f"{contract} 不在白名单里")
    return {"id": row[0], "contract": row[1], "name": row[2],
            "stock_code": row[3], "market": market}


def remove_watch(deps, contract: str) -> dict:
    contract = (contract or "").strip().upper()
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM sa_crypto_watch WHERE contract = %s RETURNING id", (contract,))
        row = cur.fetchone()
    if not row:
        raise ValueError(f"{contract} 不在白名单里")
    return {"ok": True, "contract": contract}


def enabled_contracts(deps) -> list[str]:
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("SELECT contract FROM sa_crypto_watch WHERE enabled ORDER BY id")
        return [r[0] for r in cur.fetchall()]


# ---------------- 关联统计：合约 ↔ 股票 的符号解析与相关性 ----------------
#
# 目标：量化「这只币和它锚定的那只股票是什么关系」——日涨跌相关系数、beta、
# 两侧波动率、比价。2026-09-25 实测 ZHIPU_USDT↔02513 相关 0.85、
# MINIMAX_USDT↔00100 相关 0.88，且两者比价都 ≈7.83（正是 USD/HKD），
# 说明这些永续确实锚定标的股价，不只是"名字像"。

_A_RE = re.compile(r"^\d{6}$")               # A股 6 位
_HK_RE = re.compile(r"^\d{4,5}$")            # 港股 4~5 位
_US_RE = re.compile(r"^[A-Z][A-Z.\-]{0,5}$")  # 美股 TSLA / BRK.B / BF-B

# 腾讯行情 f[2] 回报的交易所后缀 → 东财 secid 市场号
# （2026-09-25 实测双向验证：JPM 在 106 通/在 105 rc=100，AAPL 反之）
_US_SECID = {".OQ": "105", ".N": "106", ".AM": "107"}


def resolve_symbol(code: str) -> dict | None:
    """用户输入的股票代码 -> 各数据源需要的符号。纯函数，认不出返回 None。

    为什么一个代码要解析出好几种符号：三个源要的写法互不相同，没有任何单一
    变换能同时满足——
      腾讯行情  q=usAAPL      （不带后缀）
      腾讯日K   usAAPL.OQ     （必须带后缀，见 _resolve_us_suffix 里的警告）
      东财日K   105.AAPL
    A股/港股三家碰巧一致，所以只有美股需要额外解析交易所。

    中文名一律拒绝：交易所归属无法确定，同名跨市场会让映射静默错到另一只票。
    显示名由行情接口回填。
    """
    raw = (code or "").strip().upper()
    if not raw or not raw.isascii():
        return None
    if _A_RE.match(raw):
        # 北交所 4/8 开头腾讯是 bj 前缀；东财仍归 0.（与深市同前缀）
        prefix = "bj" if raw[0] in "48" else ("sh" if raw[0] in "695" else "sz")
        sym = prefix + raw
        secid = ("1." if prefix == "sh" else "0.") + raw
        return {"code": raw, "market": "a", "tx_quote": sym, "tx_kline": sym,
                "secid": secid, "currency": "CNY", "label": "A股"}
    if _HK_RE.match(raw):
        hk = raw.zfill(5)          # 700 → 00700。必须补零：不补会落进 A 股分支
        return {"code": hk, "market": "hk", "tx_quote": "hk" + hk,
                "tx_kline": "hk" + hk, "secid": "116." + hk,
                "currency": "HKD", "label": "港股"}
    if _US_RE.match(raw):
        t = raw.replace(".", "-")
        return {"code": t, "market": "us", "tx_quote": "us" + t,
                "tx_kline": None,   # 缺交易所后缀，运行时解析
                "secid": None, "currency": "USD", "label": "美股"}
    return None


_us_suffix_cache: dict[str, str] = {}   # TICKER -> ".OQ" / ".N" / ".AM"
_us_suffix_lock = threading.Lock()


def _resolve_us_suffix(tickers: list[str]) -> dict[str, str]:
    """美股 ticker -> 交易所后缀。一次批量腾讯行情请求取回（2026-09-25 实测
    20 个 symbol 0.074s、25 次连发无 429），从 f[2] 读回 `AAPL.OQ`/`JPM.N`/`SPY.AM`。
    进程内缓存，命中就不再发请求。

    ⚠️ 为什么非要这个后缀：腾讯日K 用不带后缀的 `usAAPL` 会返回 **2 根 2011 年的
    陈旧数据且非空**，能通过下游 `if out: return out` 的守卫——15 年前价格算出的
    相关系数不报错、不断言、只是完全错误。所以宁可多一次请求也不能省。

    ⚠️ 三个实测踩过的坑（2026-09-26，全部会让美股静默退化成「没有关联统计」）：
      1. 字段下标：f[1] 是**中文名**（A/港/美都一样），带交易所的完整代码在 f[2]。
      2. 变量名：`v_usAAPL` 去掉前缀要连下划线一起去（`head[1:]` 会留下
         `_usAAPL`，把下面 startswith("us") 的守卫全部拒掉）。
      3. 全部命中缓存时**不能直接 return {}** —— 调用方（_full_symbol）要的正是
         缓存里的值；早退会让它拿到空 dict，于是 tx_kline 一直是 None。
    """
    want = [t for t in dict.fromkeys(tickers) if t]
    if not want:
        return {}
    missing = [t for t in want if _norm_ticker(t) not in _us_suffix_cache]
    if not missing:
        return {t: _us_suffix_cache[_norm_ticker(t)] for t in want
                if _norm_ticker(t) in _us_suffix_cache}
    try:
        import requests
        sess = requests.Session()
        sess.trust_env = False      # 与 app.py 的 NO_PROXY=* 同策
        resp = sess.get("https://qt.gtimg.cn/q=" + ",".join(
            "us" + t.replace("-", ".") for t in missing),
            headers=HEADERS, timeout=15)
        resp.raise_for_status()
        text = resp.content.decode("gbk", "ignore")
    except Exception as exc:
        print(f"[crypto] 美股交易所后缀解析失败: {exc}", flush=True)
        return {}
    found: dict[str, str] = {}
    want_keys = {_norm_ticker(t) for t in missing}
    for line in text.split(";"):
        line = line.strip()
        if not line.startswith("v_us"):
            continue
        body = line.partition("=")[2]
        parts = body.strip('"').split("~")
        full = parts[2].strip() if len(parts) > 2 else ""
        if "." not in full or full == "pv_none_match":
            continue
        # 用 rpartition：BRK.B 在腾讯那边是 "BRK.B.N"（B 类股 + 纽交所），
        # 正着 partition 只会得到 base="BRK" / suffix="B.N" 这种废值。
        base, _, suffix = full.rpartition(".")
        if not base or not suffix:
            continue
        # 不存在的代码腾讯也会回一行，价格为 0 或空——这种必须拒掉，
        # 否则会给一个查无此人的 ticker 编出后缀。
        price = _f(parts[3]) if len(parts) > 3 else None
        if price is None or price <= 0:
            continue
        key = _norm_ticker(base)
        # 只认本次请求过的 ticker。**不要拿变量名做交叉校验**：变量名里的
        # 点号会被写成下划线（usBRK.B → v_usBRK_B），归一后与请求值对不上，
        # 会把合法的 class-share ticker 全部误杀（2026-09-26 实测踩过）。
        if key not in want_keys:
            continue
        found[key] = "." + suffix.upper()
    with _us_suffix_lock:
        _us_suffix_cache.update(found)
    return {t: _us_suffix_cache[_norm_ticker(t)] for t in want
            if _norm_ticker(t) in _us_suffix_cache}


def _norm_ticker(t: str) -> str:
    """ticker 归一：去空白大写、'-' 与 '.' 视为同一个分隔符（BRK-B == BRK.B）。"""
    return (t or "").strip().upper().replace("-", ".")


# 腾讯后缀 → 东财 secid 市场号。字母交易所只有这三个（.OQ=纳斯达克、.N=纽交所、
# .AM=美交所/AMEX），与 resolve_symbol 里 _US_SECID 的方向相反（那边是「后缀由
# secid 推」，这里不是——后缀要从腾讯行情读，secid 只能由后缀推）。
_US_SECID_FROM_SUFFIX = {"OQ": "105", "N": "106", "AM": "107"}


def _full_symbol(sym: dict) -> tuple[dict, str]:
    """补齐 resolve_symbol 的缺口，返回 (可用的 sym 副本, 审计用的 secid)。

    resolve_symbol 对美股故意把 secid/tx_kline 留空（它不联网）。这里补：
      · 用一次批量腾讯行情把交易所后缀解出来 → 东财 secid（105/106/107）
      · tx_kline = us<CODE>.<后缀>，供腾讯兜底源用
    """
    out = dict(sym)
    if out.get("market") != "us":
        return out, out.get("secid") or ""
    # _resolve_us_suffix 的返回是按传入的原始 ticker 做 key 的（内部缓存才归一）
    suffix = _resolve_us_suffix([out["code"]]).get(out["code"])
    if not suffix:
        return out, ""
    # 用点号写法拼：resolve_symbol 把 BRK.B 归一成了 BRK-B，但腾讯要的是
    # usBRK.B.N（点号 + 交易所后缀）。普通代码没有 '-',这一句是空操作。
    base = out["code"].replace("-", ".")
    out["tx_kline"] = "us" + base + suffix
    out["secid"] = _US_SECID_FROM_SUFFIX.get(suffix.lstrip("."), "")
    return out, out["secid"]


EM_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
# 腾讯日K 的两个可用镜像。**不要用 web.ifzq.gtimg.cn** —— 那个域名被 WAF 拦成
# 501（app.py 量能模块的注释里已记录），去掉 `web.` 前缀的 ifzq.gtimg.cn 和
# proxy.finance.qq.com/ifzqgtimg 都正常（2026-09-26 实测 A股/港股/美股全通）。
TX_KLINE_HOSTS = ("https://ifzq.gtimg.cn/appstock/app/fqkline/get",
                  "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get")
SINA_CN_KLINE = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                "CN_MarketData.getKLineData")
SINA_US_KLINE = ("https://stock.finance.sina.com.cn/usstock/api/jsonp.php/"
                "var%20_data=/US_MinKService.getDailyK")
SOHU_KLINE = "https://q.stock.sohu.com/hisHq"

# 逐个源尝试的顺序。2026-09-26 在本机逐个实测过可达性，结论写在这里，
# 免得下次再从头试一遍：
#   腾讯 ifzq   —— 首选。一条符号打通 A/港/美（美股须带交易所后缀，见
#                    _resolve_us_suffix 的警告），两个镜像都可用
#   东财 push2his —— 一条 secid 打通 A/港/美，字段最干净；但本机 *.eastmoney.com
#                    整域连不上（RemoteDisconnected，sector.py 也有同样记录）
#   新浪 CN/US  —— 实测可用：A股 json_v2、美股 US_MinKService（返回全量历史，
#                    取末尾 days 根）。**新浪没有港股日K**（getDayK 已下线，
#                    CN_MarketData 对 hk00700 返回 null）
#   搜狐 hisHq  —— 实测 cn_ 前缀可用但会 503 限流，且不支持港股/美股，放最后
_KLINE_SOURCES = ("tencent", "eastmoney", "sina_cn", "sina_us", "sohu")


def _sina_jsonp(text: str):
    """新浪 JSONP 剥壳：/*<script>…</script>*/ var _data=([...]) → 列表。"""
    body = re.sub(r"^/\*.*?\*/", "", text.strip(), flags=re.S)
    i = body.find("(")
    j = body.rfind(")")
    if i == -1 or j <= i:
        return None
    return json.loads(body[i + 1:j])


def _kline_tencent(sym: dict, days: int) -> list[dict]:
    """腾讯日K（两个镜像依次试）。day bar 格式 [日期,开,收,高,低,量]，收是下标 2。

    返回里**可能带 cqr（前复权）修正**，所以收价可能与实时快照略有出入——对
    「算相关系数/比价」无影响（两侧都是复权价），页面上的实时价另走行情接口。
    """
    tx = sym.get("tx_kline")
    if not tx:
        return []
    for host in TX_KLINE_HOSTS:
        data = _get_json(host, params={"param": f"{tx},day,,,{days},qfq"}, timeout=15)
        bars = (data or {}).get("data", {}).get(tx, {})
        bars = bars.get("qfqday") or bars.get("day") or []
        out = [{"date": str(b[0]), "close": _f(b[2])}
               for b in bars if len(b) >= 3 and _f(b[2]) is not None]
        # <10 根基本是「符号写错/没带交易所后缀」——腾讯会返回 2011 年的陈旧
        # 2 根且不为空，能通过下游的 `if out: return out` 守卫，安静地算出一堆
        # 全错的相关性。这里直接当失败，让下一源接手。
        if len(out) >= 10:
            return out
    return []


def _kline_sina_cn(sym: dict, days: int) -> list[dict]:
    """新浪 A股日K。symbol 用 sh/sz/bj 前缀（与 resolve_symbol 的 tx_quote 同形）。"""
    symbol = sym.get("tx_quote") or ""
    if sym.get("market") != "a" or not symbol:
        return []
    data = _get_json(SINA_CN_KLINE, params={
        "symbol": symbol, "scale": 240, "ma": "no", "datalen": max(days, 30)},
        timeout=15)
    out = []
    for row in data or []:
        c = _f(row.get("close"))
        if row.get("day") and c is not None:
            out.append({"date": str(row["day"])[:10], "close": c})
    return out[-days:]


def _kline_sina_us(sym: dict, days: int) -> list[dict]:
    """新浪美股日K（返回全量历史，末尾 days 根）。"""
    if sym.get("market") != "us":
        return []
    text = _get(SINA_US_KLINE, params={"symbol": sym["code"], "___qn": 3}, timeout=20)
    rows = _sina_jsonp(text) or []
    out = []
    for row in rows:
        c = _f(row.get("c"))
        if row.get("d") and c is not None:
            out.append({"date": str(row["d"])[:10], "close": c})
    return out[-days:]


def _kline_sohu(sym: dict, days: int) -> list[dict]:
    """搜狐日K（只支持 A股 cn_<6位>；限流严，放最后）。"""
    if sym.get("market") != "a":
        return []
    code = sym["code"]
    text = _get(SOHU_KLINE, params={
        "code": "cn_" + code,
        "start": (datetime.now() - timedelta(days=days * 3 + 40)).strftime("%Y%m%d"),
        "end": datetime.now().strftime("%Y%m%d")}, timeout=15)
    try:
        hq = (json.loads(text) or {}).get("hq") or []
    except json.JSONDecodeError:
        return []
    out = []
    for bar in hq:
        # [日期,开,收,涨跌额,涨跌幅,最低,最高,成交量,成交额,换手率]
        if len(bar) >= 3:
            c = _f(bar[2])
            if c is not None:
                out.append({"date": str(bar[0])[:10], "close": c})
    return out[-days:]


def stock_daily_closes(sym: dict, days: int = 45) -> tuple[list[dict], str, str]:
    """关联股票的日收盘序列。返回 (序列, 用的源, 失败原因)。

    逐源降级尝试（见 _KLINE_SOURCES 的实测记录）。全失败时返回 ([], "", 原因)，
    原因会写进 sa_crypto_link_stats.error 并显示在页面上——「没数据」必须能
    区分「代码填错了」「源挂了」「这只票停牌了」三种情况。
    """
    sym, secid = _full_symbol(sym)
    errors: list[str] = []

    for name, fn in (("tencent", _kline_tencent), ("sina_cn", _kline_sina_cn),
                     ("sina_us", _kline_sina_us), ("sohu", _kline_sohu)):
        try:
            out = fn(sym, days)
            if out:
                return out, name, ""
            errors.append(f"{name}：无数据")
        except Exception as exc:
            errors.append(f"{name}：{type(exc).__name__}")

    if secid:
        try:
            data = _get_json(EM_KLINE_URL, params={
                "secid": secid, "klt": 101, "fqt": 1, "lmt": days,
                "fields1": "f1,f2,f3", "fields2": "f51,f53", "end": "20500101",
            }, timeout=15)
            out = []
            for k in (data or {}).get("data", {}).get("klines") or []:
                parts = str(k).split(",")
                if len(parts) >= 2 and _f(parts[1]) is not None:
                    out.append({"date": parts[0], "close": _f(parts[1])})
            if out:
                return out, "eastmoney", ""
            errors.append("东财：无数据")
        except Exception as exc:
            errors.append(f"东财：{type(exc).__name__}")

    mkt = sym.get("market")
    label = {"a": "A股", "hk": "港股", "us": "美股"}.get(mkt, mkt)
    return [], "", f"{label}日K 全部源失败（{'；'.join(errors)}）"


def _get(url: str, params: dict | None = None, timeout: int = 15) -> str:
    """GET 一个文本接口（新浪/搜狐返回的都不是纯 JSON，需要自己剥）。"""
    import requests
    sess = requests.Session()
    sess.trust_env = False
    resp = sess.get(url, params=params, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = resp.encoding or "utf-8"
    return resp.text


def _closes_map(rows: list[dict]) -> dict:
    return {r["date"]: r["close"] for r in rows if r.get("close") is not None}


def _crypto_daily(contract: str, days: int) -> list[dict]:
    """fetch_daily_closes 结果归一成 [{date:'YYYY-MM-DD'(UTC日), close}] 升序。

    ⚠️ 必须显式 tz=timezone.utc：本机是 UTC+8，裸 datetime.fromtimestamp(t) 会得到
    T-1，整条序列错一天——而相关系数照样算得出来，只是全错，不会有任何报错。
    """
    out = []
    for b in fetch_daily_closes(contract, days):
        if b.get("t") is None or b.get("close") is None:
            continue
        out.append({"date": datetime.fromtimestamp(int(b["t"]), tz=timezone.utc)
                    .strftime("%Y-%m-%d"), "close": float(b["close"])})
    out.sort(key=lambda x: x["date"])
    return out


def align_series(crypto_daily: list[dict], stock_closes: dict) -> list[dict]:
    """币圈 UTC 日K × 股票本地日收盘，按**同一日期**求交集配对（升序）。

    为什么同一日期对齐是对的：币圈日K 覆盖 00:00~24:00 UTC，而港股 09:30-16:00
    HKT = 01:30-08:00 UTC、A股 = 01:30-07:00 UTC、美股 = 13:30-20:00 UTC，
    三者都落在同一 UTC 日内。

    休市/停牌造成的缺口两侧一起跳过，**不插值**——插值会凭空造出相关性。
    """
    cmap = {b["date"]: b["close"] for b in crypto_daily if b.get("close") is not None}
    return [{"date": d, "crypto_close": cmap[d], "stock_close": stock_closes[d]}
            for d in sorted(set(cmap) & set(stock_closes))]


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def link_metrics(pairs: list[dict], min_overlap: int = 9) -> dict:
    """由已对齐的序列算 corr/beta/alpha/波动率/比价。纯函数，无 IO，可单测。

    ⚠️ 收益必须在**已对齐的 pairs 上**做差分 [i]/[i-1]，绝不能两侧各自差分再按
    日期 join：股票停牌那天币圈照常交易，两条序列长度不再相等且日期错位，
    corr 会静默算错（不报错、不断言、只是结果完全不对）。下面的 zip 因此
    天然等长——这是本函数唯一正确的差分位置。

    守卫：任一侧收益方差为 0 → insufficient（长期停牌或数据源返回常数，
    否则 beta=inf、corr=NaN）。
    """
    n = len(pairs)
    if n < min_overlap:
        return {"overlap_days": n, "n_obs": max(n - 1, 0), "status": "insufficient",
                "error": f"重叠交易日仅 {n} 天，少于 {min_overlap} 天，不计算相关系数"}
    rc = [pairs[i]["crypto_close"] / pairs[i - 1]["crypto_close"] - 1
          for i in range(1, n)]
    rs = [pairs[i]["stock_close"] / pairs[i - 1]["stock_close"] - 1
          for i in range(1, n)]
    m = len(rc)
    mc, ms = sum(rc) / m, sum(rs) / m
    cov = sum((a - mc) * (b - ms) for a, b in zip(rc, rs)) / (m - 1)
    vc = sum((a - mc) ** 2 for a in rc) / (m - 1)
    vs = sum((b - ms) ** 2 for b in rs) / (m - 1)
    if vc < 1e-12 or vs < 1e-12:
        return {"overlap_days": n, "n_obs": m, "status": "insufficient",
                "error": "某侧收益方差为 0（疑似长期停牌或数据源返回常数）"}
    ratios = [p["stock_close"] / p["crypto_close"] for p in pairs]
    last = pairs[-1]
    med = _median(ratios)
    return {
        "overlap_days": n, "n_obs": m, "status": "ok", "error": "",
        "corr": round(cov / (vc * vs) ** 0.5, 4),
        "beta": round(cov / vc, 4),
        "alpha_daily": round((ms - (cov / vc) * mc) * 100, 4),
        "vol_crypto": round(vc ** 0.5 * 100, 2),
        "vol_stock": round(vs ** 0.5 * 100, 2),
        "ratio": round(ratios[-1], 4),
        "ratio_ma": round(med, 4),
        "ratio_dev_pct": round((ratios[-1] / med - 1) * 100, 3) if med else None,
        "crypto_close": last["crypto_close"],
        "stock_close": last["stock_close"],
    }


def build_series(pairs: list[dict]) -> dict:
    """给前端画双线图用的序列（升序）。缺一边的日子留 null，前端断线不连过去。"""
    return {
        "dates": [p["date"] for p in pairs],
        "crypto": [p["crypto_close"] for p in pairs],
        "stock": [p["stock_close"] for p in pairs],
    }


# ---------------- 关联统计的落库 / 读取 / 重算 ----------------
#
# 读路径是 DISTINCT ON (contract) ... ORDER BY contract, stat_date DESC（每个合约
# 只取最新一天那一行），PK 是 (stat_date, contract)。一次重算 = 每个合约 upsert 一行。

_LINK_COLS = ("stat_date", "contract", "stock_code", "stock_symbol", "tx_symbol",
              "secid", "market", "currency", "overlap_days", "n_obs", "corr", "beta",
              "alpha_daily", "vol_crypto", "vol_stock", "ratio", "ratio_ma",
              "ratio_dev_pct", "crypto_close", "stock_close", "series", "status",
              "error")

_link_state = {"running": False, "last_run": None, "last_result": None}
_link_lock = threading.Lock()


def _upsert_link(deps, contract: str, sym: dict, secid: str, tx_sym: str,
                 stat_date, m: dict, series: dict) -> None:
    # stat_date / overlap_days / n_obs / series 都是 NOT NULL，而错误路径
    # （取不到 K 线）传进来的 m 里根本没有这些键，m.get() 返回 None。
    # **显式传 NULL 会覆盖列上的 DEFAULT**，于是整条 INSERT 抛 not-null
    # violation，把「数据源暂时挂了」变成「数据库报错刷屏」（2026-09-28 实发，
    # 先是 stat_date 报错，修掉后又轮到 overlap_days）。所以这里统一兜底。
    if not stat_date:
        stat_date = date.today().isoformat()
    n_int = lambda k: m.get(k) if m.get(k) is not None else 0
    n_num = lambda k: m.get(k)          # 可空数值列，保持 None
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO sa_crypto_link_stats ({",".join(_LINK_COLS)})
            VALUES ({",".join(["%s"] * len(_LINK_COLS))})
            ON CONFLICT (stat_date, contract) DO UPDATE SET
                stock_code=EXCLUDED.stock_code, stock_symbol=EXCLUDED.stock_symbol,
                tx_symbol=EXCLUDED.tx_symbol, secid=EXCLUDED.secid,
                market=EXCLUDED.market, currency=EXCLUDED.currency,
                overlap_days=EXCLUDED.overlap_days, n_obs=EXCLUDED.n_obs,
                corr=EXCLUDED.corr, beta=EXCLUDED.beta, alpha_daily=EXCLUDED.alpha_daily,
                vol_crypto=EXCLUDED.vol_crypto, vol_stock=EXCLUDED.vol_stock,
                ratio=EXCLUDED.ratio, ratio_ma=EXCLUDED.ratio_ma,
                ratio_dev_pct=EXCLUDED.ratio_dev_pct,
                crypto_close=EXCLUDED.crypto_close, stock_close=EXCLUDED.stock_close,
                series=EXCLUDED.series, status=EXCLUDED.status, error=EXCLUDED.error,
                computed_at=now()
            """,
            (stat_date, contract, sym.get("code", "") or "", tx_sym or "", tx_sym or "",
             secid or "", sym.get("market", "") or "", sym.get("currency", "") or "",
             n_int("overlap_days"), n_int("n_obs"),
             n_num("corr"), n_num("beta"),
             n_num("alpha_daily"), n_num("vol_crypto"), n_num("vol_stock"),
             n_num("ratio"), n_num("ratio_ma"), n_num("ratio_dev_pct"),
             n_num("crypto_close"), n_num("stock_close"),
             json.dumps(series or {}, ensure_ascii=False),
             m.get("status") or "ok", (m.get("error") or "")[:255]))


def compute_link(deps, contract: str, stock_code: str, conf: dict) -> dict:
    """单个合约 ↔ 股票的关联统计：取两侧日K → 对齐 → 算指标 → upsert。

    失败不抛：把原因写进 status/error 列，页面上能看到「为什么没数据」，
    否则一个配错的代码会永远显示空白、看不出是代码错了还是数据源挂了。
    """
    sym = resolve_symbol(stock_code)
    if not sym:
        return {"contract": contract, "stock_code": stock_code, "status": "error",
                "error": f"无法识别股票代码「{stock_code}」"}
    days = int(conf.get("link_lookback_days") or 45)
    min_overlap = int(conf.get("link_min_overlap") or 9)
    try:
        crypto_daily = _crypto_daily(contract, days)
        sym_full, secid = _full_symbol(sym)
        stock_rows, source, src_err = stock_daily_closes(sym_full, days)
    except Exception as exc:
        _upsert_link(deps, contract, sym, "", "", date.today().isoformat(),
                     {"status": "error", "error": f"取日K失败：{exc}"[:255]}, {})
        return {"contract": contract, "stock_code": stock_code, "status": "error",
                "error": str(exc)}
    if not crypto_daily or not stock_rows:
        pairs = []
        why = src_err or ""
        if not crypto_daily:
            why = (why + "；" if why else "") + "币圈日K 取不到"
        elif not stock_rows:
            why = why or "股票日K 取不到"
        m = {"status": "insufficient",
             "error": f"日K 取不到（币圈 {len(crypto_daily)} 根 / 股票 {len(stock_rows)} 根）"
                      + (f"：{why}" if why else "")}
    else:
        pairs = align_series(crypto_daily, _closes_map(stock_rows))
        m = link_metrics(pairs, min_overlap)
        m["stock_source"] = source
    stat_date = pairs[-1]["date"] if pairs else None
    series = build_series(pairs) if pairs else {}
    _upsert_link(deps, contract, sym, secid, sym_full.get("tx_kline") or "",
                 stat_date, m, series)
    return {"contract": contract, "stock_code": sym["code"], "market": sym["market"],
            "label": sym["label"], "status": m.get("status"),
            "error": m.get("error", ""), "corr": m.get("corr"), "beta": m.get("beta"),
            "overlap_days": m.get("overlap_days"), "ratio": m.get("ratio"),
            "ratio_dev_pct": m.get("ratio_dev_pct"), "stat_date": stat_date,
            "stock_source": m.get("stock_source", "")}


def refresh_links(deps, contracts: list[str] | None = None) -> dict:
    """重算关联统计。contracts 为空 = 白名单里所有配了股票的合约。

    串行 + 一把锁：每轮要发 2N 个 HTTP 请求（N≤几十），并发只会更容易被
    东财/腾讯限流，而且 429 之后整轮数据全废。锁非阻塞，已在跑就直接返回。
    """
    if not _link_lock.acquire(blocking=False):
        return {"skipped": True, "reason": "关联统计正在重算中"}
    _link_state["running"] = True
    try:
        with deps["get_conn"]() as conn, conn.cursor() as cur:
            cur.execute("SELECT contract, stock_code FROM sa_crypto_watch "
                        "WHERE enabled AND stock_code <> '' ORDER BY id")
            pairs = cur.fetchall()
        want = {c.upper() for c in contracts} if contracts else None
        targets = [(c, s) for c, s in pairs if not want or c in want and s]
        if not targets:
            return {"skipped": True, "reason": "没有配了关联股票的合约",
                    "items": [], "ts": _link_state["last_run"]}
        conf = load_crypto_conf()
        items = []
        for contract, stock_code in targets:
            try:
                items.append(compute_link(deps, contract, stock_code, conf))
            except Exception as exc:      # 单个失败不拖垮整轮
                print(f"[crypto] link {contract} 失败: {exc}", flush=True)
                items.append({"contract": contract, "stock_code": stock_code,
                              "status": "error", "error": str(exc)})
        ok = [i for i in items if i.get("status") == "ok"]
        _link_state["last_run"] = datetime.now().isoformat(timespec="seconds")
        result = {"items": items, "computed": len(items), "ok": len(ok),
                  "ts": _link_state["last_run"]}
        _link_state["last_result"] = result
        return result
    finally:
        _link_state["running"] = False
        _link_lock.release()


def link_status() -> dict:
    return {"running": _link_state["running"], "last_run": _link_state["last_run"],
            "last_result": _link_state["last_result"]}


def recent_links(deps) -> list[dict]:
    """每个合约最新一条**有效**关联统计（供页面表格直接读，不重算）。

    为什么要「有效」：重算失败时会写一条 status='error' 的行（记录原因，
    免得静默失败），而 PK 是 (stat_date, contract)，这条 error 行就成了该合约
    stat_date 最大的一行。若直接取最大行，页面/图表会变成空白，把上一次正常
    算出来的 corr、ratio 全遮住（2026-09-28 实发：K线源抽风，
    09-25 的 corr 0.8359 被 error 行盖掉）。
    所以：指标取最新一条 overlap_days>0 的行；状态/报错另外取最新一条，
    页面据此显示「数据是陈的 / 最新重算失败」。
    """
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (contract) stat_date, contract, stock_code, market,
                   currency, overlap_days, n_obs, corr, beta, alpha_daily,
                   vol_crypto, vol_stock, ratio, ratio_ma, ratio_dev_pct,
                   crypto_close, stock_close, status, error, computed_at
            FROM sa_crypto_link_stats
            WHERE overlap_days > 0
            ORDER BY contract, stat_date DESC""")
        rows = cur.fetchall()
        cur.execute("""SELECT DISTINCT ON (contract) contract, stat_date, status, error
                       FROM sa_crypto_link_stats
                       ORDER BY contract, stat_date DESC""")
        # 映射成 (status, error, 最新尝试日期)，与下面 st, err, latest_date 的解包对齐
        latest = {r[0]: (r[2], r[3], r[1]) for r in cur.fetchall()}
    out = []
    for r in rows:
        # SELECT 顺序：stat_date, contract, stock_code, market, currency, overlap_days,
        # n_obs, corr, beta, alpha_daily, vol_crypto, vol_stock, ratio, ratio_ma,
        # ratio_dev_pct, crypto_close, stock_close, status, error, computed_at
        st, err, latest_date = latest.get(r[1], (r[17], r[18], r[0]))
        item = {
            "contract": r[1], "stock_code": r[2], "market": r[3], "currency": r[4],
            "stat_date": r[0].isoformat() if r[0] else None,
            "overlap_days": r[5], "n_obs": r[6],
            "corr": _f(r[7]), "beta": _f(r[8]), "alpha_daily": _f(r[9]),
            "vol_crypto": _f(r[10]), "vol_stock": _f(r[11]),
            "ratio": _f(r[12]), "ratio_ma": _f(r[13]), "ratio_dev_pct": _f(r[14]),
            "crypto_close": _f(r[15]), "stock_close": _f(r[16]),
            "status": st, "error": err,
            "computed_at": r[19].isoformat(timespec="seconds") if r[19] else None,
        }
        # 最新一次重算失败但上面给的是更早的正常值 -> 明确标出来，别让人当成新鲜数据
        if st != "ok":
            item["stale"] = True
            item["latest_attempt"] = latest_date.isoformat() if latest_date else None
            item["data_stat_date"] = item["stat_date"]
        out.append(item)
    return out


def link_series(deps, contract: str) -> dict:
    """某合约最近一条**有效**记录的已对齐序列（画双线图用）。没有则空壳。

    与 recent_links 同理：只取 overlap_days>0 的行，否则重算失败留下的
    error 行（series 为 {}）会把图变成空白，而不是保留上一次的真实曲线。
    """
    contract = (contract or "").strip().upper()
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("""SELECT series, stat_date, stock_code, corr, beta, ratio,
                              ratio_ma, ratio_dev_pct, vol_crypto, vol_stock,
                              alpha_daily, n_obs, status, error
                       FROM sa_crypto_link_stats
                       WHERE contract=%s AND overlap_days > 0
                       ORDER BY stat_date DESC NULLS LAST LIMIT 1""", (contract,))
        r = cur.fetchone()
    if not r:
        return {"contract": contract, "dates": [], "crypto": [], "stock": []}
    # psycopg2 会把 JSONB 自动转成 dict；只有拿回原文（自定义游标/文本传输）时
    # 才是 str。两种都认，否则直接 json.loads(dict) 会抛 TypeError。
    raw = r[0] or {}
    s = raw if isinstance(raw, dict) else (json.loads(raw) if raw else {})
    return {"contract": contract, "dates": s.get("dates") or [],
            "crypto": s.get("crypto") or [], "stock": s.get("stock") or [],
            "stat_date": r[1].isoformat() if r[1] else None, "stock_code": r[2],
            "corr": _f(r[3]), "beta": _f(r[4]), "ratio": _f(r[5]),
            "ratio_ma": _f(r[6]), "ratio_dev_pct": _f(r[7]),
            "vol_crypto": _f(r[8]), "vol_stock": _f(r[9]),
            "alpha_daily": _f(r[10]), "n_obs": r[11],
            "status": r[12], "error": r[13]}


def _elapsed_since(last_iso: str) -> float:
    """距某个 ISO 时间戳过去了多少秒。

    ⚠️ 必须处理 naive/aware 混用（2026-09-26 实测踩过）：`last` 有两个来源，
    时区属性不一样——
      · 内存态 `_link_state["last_run"]` 由 `datetime.now()` 写出，是 **naive 本地时**
      · 回退读库的 `max(computed_at)` 来自 TIMESTAMPTZ 列，psycopg2 给出
        **aware**（本项目存的是 UTC）
    直接 `datetime.now() - fromisoformat(last)` 在第二种情况下抛
    TypeError: can't subtract offset-naive and offset-aware datetimes，
    而守护线程的 except 会把它吞掉 → 关联统计**永远不重算**，且日志里只有一行
    反复的 link loop error。统一按本地时归一：naive 视为本地时间。
    """
    now = datetime.now().astimezone()             # aware，本地时
    dt = datetime.fromisoformat(last_iso)
    if dt.tzinfo is None:
        dt = dt.astimezone()                       # naive 按本地时解释
    return (now - dt).total_seconds()


def links_due(deps) -> bool:
    """距上次重算是否已超过 link_refresh_hours（从没有过 = 立即该算一次）。"""
    conf = load_crypto_conf()
    hours = float(conf.get("link_refresh_hours") or 0)
    if hours <= 0:
        return False
    last = _link_state.get("last_run")
    if not last:
        # 进程重启后 _link_state 是空的：看库里最新一条 computed_at，
        # 否则每重启一次就重算一轮（一天重启十次 = 十轮 HTTP）。
        try:
            with deps["get_conn"]() as conn, conn.cursor() as cur:
                cur.execute("SELECT max(computed_at) FROM sa_crypto_link_stats")
                row = cur.fetchone()
            last = row[0].isoformat(timespec="seconds") if row and row[0] else None
        except Exception:
            last = None
        if not last:
            return True
        _link_state["last_run"] = last
    try:
        return _elapsed_since(last) >= hours * 3600
    except (ValueError, TypeError):
        return True        # 时间戳坏掉就当到期，宁可多算一轮


def _check_link_alerts(deps, rows: list[dict], conf: dict) -> list[str]:
    """比价偏离超阈值且不在冷却期 → 推一条合并微信。返回推送条数。

    比价 = 股票收盘 / 合约收盘。永续锚定现货时它是一条横线（港股那批约等于
    USD/HKD 汇率），突然偏离说明合约被溢价/折价，或股票除权跳空。
    """
    threshold = float(conf.get("link_alert_dev_pct") or 0)
    if threshold <= 0:
        return []
    cooldown = float(conf.get("link_alert_cooldown_hours") or 12) * 3600
    now = time.time()
    with _state_lock:
        state = _load_state()
        hits = []
        for r in rows:
            dev = r.get("ratio_dev_pct")
            if r.get("status") != "ok" or dev is None or abs(dev) < threshold:
                continue
            direction = "up" if dev > 0 else "down"
            last = state.get(f"link:{r['contract']}:{direction}")
            if last and now - float(last) < cooldown:
                continue
            state[f"link:{r['contract']}:{direction}"] = now
            hits.append((r, dev))
        if hits:
            _save_state(state)
    if not hits:
        return []
    lines = []
    for r, dev in hits:
        lines.append(f"- {r['contract']} ↔ {r['stock_code']}：比价 {r['ratio']:g}"
                     f"（中位 {r['ratio_ma']:g}，偏离 {dev:+.2f}%），"
                     f"合约收 {r['crypto_close']:g} / 股票收 {r['stock_close']:g}，"
                     f"相关 {r['corr']:.2f}、beta {r['beta']:.2f}")
    body = ("永续合约与锚定股票的日收盘比价明显偏离（阈值 "
            f"±{threshold:g}%）：\n" + "\n".join(lines)
            + "\n\n（比价=股票收盘/合约收盘；偏离常来自合约溢价或股票除权，"
               "仅提示，不构成投资建议。数据源 gate.io + 东财/腾讯）")
    deps.get("notify_fn")("🔗 币股比价偏离", body, event="crypto_link")
    return [f"{len(hits)} 条"]


def link_lines(deps) -> list[str]:
    """给盘前/盘后报告的关联段。失败或无数据静默返回空。"""
    try:
        rows = recent_links(deps)
    except Exception:
        return []
    label = {"a": "A股", "hk": "港股", "us": "美股"}
    lines = []
    for r in rows:
        if r.get("status") != "ok":
            continue
        corr, dev = r.get("corr"), r.get("ratio_dev_pct")
        lines.append(
            f"- {r['contract']} ↔ {r['stock_code']}（{label.get(r['market'], r['market'])}）："
            f"日涨跌相关 {corr:.2f}、beta {r['beta']:.2f}，"
            f"波动率 合约 {r['vol_crypto']:.1f}% / 股票 {r['vol_stock']:.1f}%，"
            f"比价 {r['ratio']:g}（中位 {r['ratio_ma']:g}"
            + (f"，偏离 {dev:+.2f}%" if dev is not None else "")
            + f"），样本 {r['n_obs']} 个重叠交易日"
            + ("，比价明显偏离，注意溢价/除权" if dev is not None and abs(dev) >= 5 else ""))
    return lines


# ---------------- 一轮抓取：取价 → 入库 → 异动判断 ----------------

def _save_quotes(deps, quotes: dict) -> None:
    if not quotes:
        return
    rows = [(q["contract"], q["last"], q["change_pct"], q["high_24h"], q["low_24h"],
             q["vol_quote"]) for q in quotes.values() if q.get("last") is not None]
    if not rows:
        return
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        # execute_values 而非 executemany：后者每行一次网络往返，这台远程共享
        # 云库单次往返约 65ms。670 行 executemany ≈ 44 秒，而这个函数是
        # **每 15 分钟跑一次**的后台线程，白白把连接占住 44 秒。
        # （2026-09-29 做挖新股时实测出来的，顺手修）
        execute_values(
            cur,
            "INSERT INTO sa_crypto_quotes (contract, last, change_pct, high_24h, low_24h, "
            "vol_quote) VALUES %s",
            rows, page_size=500)
        # 保留 30 天，防止长跑无限膨胀（每 15 分钟一插约 670 行/天）
        cur.execute("DELETE FROM sa_crypto_quotes WHERE ts < now() - interval '30 days'")


def _load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def _check_alerts(deps, quotes: dict, conf: dict) -> list[str]:
    """|24h 涨跌| 超阈值且不在冷却期 → 推一条合并微信，返回推送文案列表。"""
    threshold = float(conf.get("alert_threshold_pct") or 0)
    if threshold <= 0:
        return []
    cooldown = float(conf.get("alert_cooldown_hours") or 6) * 3600
    now = time.time()
    with _state_lock:
        state = _load_state()
        hits = []
        for name, q in quotes.items():
            pct = q.get("change_pct")
            if pct is None or abs(pct) < threshold:
                continue
            direction = "up" if pct > 0 else "down"
            last = state.get(f"{name}:{direction}")
            if last and now - float(last) < cooldown:
                continue
            state[f"{name}:{direction}"] = now
            hits.append((name, direction, pct, q))
        if hits:
            _save_state(state)
    if not hits:
        return []
    title = "🪙 币圈异动（股票永续 24h）"
    lines = []
    for name, _direction, pct, q in hits:
        arrow = "涨" if pct > 0 else "跌"
        lines.append(f"- {name}：{q['last']:g} USDT，24h {arrow} {pct:+.2f}%"
                     f"（高 {q.get('high_24h') or 0:g} / 低 {q.get('low_24h') or 0:g}，"
                     f"成交额 {(q.get('vol_quote') or 0) / 1e6:.1f}M USDT）")
    body = ("gate.io 股票永续 24h 异动（连续交易，与 A 股休市无关）：\n"
            + "\n".join(lines)
            + f"\n\n（阈值 ±{threshold:g}%，仅趋势参考，数据源 gate.io）")
    deps.get("notify_fn")("🪙 币圈异动（股票永续 24h）", body, event="crypto_move")
    return [title]


def run_once(deps) -> dict:
    """一轮完整流程：取白名单行情 → 入库 → 异动推送。返回本轮摘要。"""
    conf = load_crypto_conf()
    contracts = enabled_contracts(deps)
    if not contracts:
        return {"skipped": "白名单为空"}
    quotes = fetch_tickers(contracts)
    if not quotes:
        return {"error": "gate.io 未返回任何白名单合约行情"}
    _save_quotes(deps, quotes)
    pushed = []
    if conf.get("enabled", True) and conf.get("alert_threshold_pct"):
        try:
            pushed = _check_alerts(deps, quotes, conf)
        except Exception as exc:
            print(f"[crypto] alert check failed: {exc}", flush=True)
    return {"contracts": len(quotes), "pushed": len(pushed),
            "ts": datetime.now().isoformat(timespec="seconds")}


def crypto_lines(deps) -> list[str]:
    """给盘前简报/复盘的币圈 24h 段（趋势参考）。失败静默返回空。"""
    lines = []
    try:
        watch = [w for w in list_watch(deps) if w.get("last") is not None]
    except Exception:
        watch = []
    for w in watch:
        lines.append(f"- {w['name'] or w['contract']}（{w['contract']}）："
                     f"{w['last']:g} USDT，24h {w['change_pct']:+.2f}%"
                     f"，区间 {w.get('low_24h') or 0:g}~{w.get('high_24h') or 0:g}")
    try:
        for pair, label in SPOT_REFS:
            spot = fetch_spot([pair]).get(pair)
            if spot and spot.get("change_pct") is not None:
                lines.append(f"- {label} 现货：{spot['last']:g} USDT，"
                             f"24h {spot['change_pct']:+.2f}%")
    except Exception:
        pass
    return lines
