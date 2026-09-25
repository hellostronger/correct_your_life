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
from datetime import datetime
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
}

# 种子白名单：gate.io 上成交额靠前的合约（2026-09-25 实测流动性排序）
# ZHIPU_USDT = 智谱（对应港股 02513，用户自选里有），7M USDT/24h，流动性够用
SEED_CONTRACTS = [
    ("CRCL_USDT", "Circle"),
    ("MSTR_USDT", "MicroStrategy"),
    ("NVDA_USDT", "英伟达"),
    ("ZHIPU_USDT", "智谱"),
    ("TSLA_USDT", "特斯拉"),
    ("COIN_USDT", "Coinbase"),
    ("AAPL_USDT", "苹果"),
    ("HOOD_USDT", "Robinhood"),
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
    with deps["get_conn"]() as conn, conn.cursor() as cur:
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
        for contract, name in SEED_CONTRACTS:
            cur.execute("INSERT INTO sa_crypto_watch (contract, name) VALUES (%s, %s) "
                        "ON CONFLICT (contract) DO NOTHING", (contract, name))


def list_watch(deps) -> list[dict]:
    """白名单（按创建顺序），带最新一条行情（没有则字段为 None）。"""
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, contract, name, enabled, created_at FROM sa_crypto_watch "
                    "ORDER BY id")
        rows = [dict(zip(("id", "contract", "name", "enabled", "created_at"), r))
                for r in cur.fetchall()]
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
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO sa_crypto_watch (contract, name) VALUES (%s, %s) "
                    "ON CONFLICT (contract) DO UPDATE SET enabled = TRUE RETURNING id",
                    (contract, name.strip() or contract.split("_")[0]))
        cid = cur.fetchone()[0]
    return {"id": cid, "contract": contract, "name": name.strip() or contract.split("_")[0]}


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


# ---------------- 一轮抓取：取价 → 入库 → 异动判断 ----------------

def _save_quotes(deps, quotes: dict) -> None:
    if not quotes:
        return
    rows = [(q["contract"], q["last"], q["change_pct"], q["high_24h"], q["low_24h"],
             q["vol_quote"]) for q in quotes.values() if q.get("last") is not None]
    if not rows:
        return
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO sa_crypto_quotes (contract, last, change_pct, high_24h, low_24h, "
            "vol_quote) VALUES (%s,%s,%s,%s,%s,%s)", rows)
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
    deps.get("notify_fn")("🪙 币圈异动（股票永续 24h）", body)
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
