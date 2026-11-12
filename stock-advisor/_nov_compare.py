"""一次性分析：2026年11月「躺平不动」vs「实际操作」哪个赚得多。
数据：sa_trades 实盘流水 + 101 行情服务（10/30 收盘基准、11/11 收盘现价）。
不写库，纯只读。"""
import json, re, pathlib, urllib.parse, urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import psycopg2

SVC = "http://101.43.25.101:8080"
CUTOFF = "2026-10-31"     # 10/31 周六，10/30 为 10 月最后交易日
def load_db_conf():
    """与 app.py 一致：从仓库根 .env 读 DB_*，不在脚本里存密钥。"""
    conf = {"host": "127.0.0.1", "port": 5432, "user": "postgres",
            "password": "", "dbname": "postgres"}
    env = pathlib.Path(__file__).resolve().parent.parent / ".env"
    mapping = {"DB_HOST": "host", "DB_PORT": "port", "DB_USERNAME": "user",
               "DB_PASSWORD": "password", "DB_DATABASE": "dbname"}
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if "=" not in line or line.startswith("#"):
            continue
        k, _, v = line.partition("=")
        if k.strip() in mapping:
            key = mapping[k.strip()]
            conf[key] = int(v.strip()) if key == "port" else v.strip()
    return conf


CONF = load_db_conf()


def svc(path, params, timeout=30):
    url = SVC + path + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def sym(code):
    if re.fullmatch(r"\d{5}", code):
        return "hk" + code
    return ("sh" if code.startswith(("6", "9", "5")) else "sz") + code


def is_hk(code):
    return bool(re.fullmatch(r"\d{5}", code))


# ---------- 1. 实盘流水 ----------
conn = psycopg2.connect(connect_timeout=15, **CONF)
cur = conn.cursor()
cur.execute("""SELECT code, trade_date, side, shares, price, note
               FROM sa_trades ORDER BY trade_date, id""")
trades = [{"code": c, "date": d.isoformat(), "side": s,
           "shares": int(n), "price": float(p), "note": note}
          for c, d, s, n, p, note in cur.fetchall()]
cur.execute("SELECT code, name FROM sa_watchlist")
names = dict(cur.fetchall())
cur.close(); conn.close()

base_shares = defaultdict(int)
for t in trades:
    if t["date"] <= CUTOFF:
        base_shares[t["code"]] += t["shares"] * (1 if t["side"] == "buy" else -1)
base_shares = {c: n for c, n in base_shares.items() if n != 0}
nov = [t for t in trades if t["date"] > CUTOFF]
codes = sorted(set(base_shares) | {t["code"] for t in nov})

# ---------- 2. 行情：现价 + 10/30 收盘 ----------
qs = svc("/quote", {"symbols": ",".join(sym(c) for c in codes)}, timeout=40)["quotes"]
price_now, meta = {}, {}
for c in codes:
    q = qs.get(sym(c), {})
    price_now[c] = float(q["price"]) if q.get("price") else None
    meta[c] = {"prev_close": q.get("prev_close"), "updated": q.get("updated_at")}


def kline_of(code):
    secid = ("116." + code) if is_hk(code) else (("1." if code.startswith(("6", "9", "5")) else "0.") + code)
    try:
        ks = svc("/kline", {"secid": secid, "days": 60,
                            "fields": "f51,f53", "source": "auto"}, timeout=40)["klines"]
        rows = [(k.split(",")[0], float(k.split(",")[1])) for k in ks if "," in k]
    except Exception as e:
        return code, None, None, str(e)
    before = [(d, p) for d, p in rows if d <= "2026-10-30"]
    today = [p for d, p in rows if d == "2026-11-11"]
    return code, (before[-1] if before else None), (today[0] if today else None), None


with ThreadPoolExecutor(max_workers=8) as ex:
    kl = list(ex.map(kline_of, codes))
p1030, p1111_kline, kerr = {}, {}, {}
for c, b, tdy, err in kl:
    p1030[c] = b[1] if b else None
    p1111_kline[c] = tdy
    if err:
        kerr[c] = err

try:
    fxq = svc("/quote", {"symbols": "whHKDCNY"}, timeout=20)["quotes"]
    fx = float(fxq["whHKDCNY"]["price"])
except Exception:
    fx = 0.851
print("HKD->CNY 汇率:", fx)
print("行情条数:", len(codes), "缺现价:", [c for c in codes if not price_now[c]],
      "缺10/30:", [c for c in codes if p1030[c] is None], "K线错误:", kerr)
print("11/11 K线收盘 vs 实时收盘差异 >2% 的:",
      [(c, p1111_kline[c], price_now[c]) for c in codes
       if p1111_kline[c] and price_now[c] and abs(p1111_kline[c]/price_now[c]-1) > 0.02])

# ---------- 3. 两种情景 ----------
missing = [c for c in codes if not price_now[c] or p1030[c] is None]
if missing:
    print("!! 价格缺失，结果不完整:", missing)

now_shares = dict(base_shares)
cash_cny, cash_hkd = 0.0, 0.0
contrib = defaultdict(float)          # 每笔交易相对躺平的贡献（本币）
for t in nov:
    c, n, p = t["code"], t["shares"], t["price"]
    pn = price_now.get(c)
    if not pn:
        continue
    if t["side"] == "buy":
        now_shares[c] = now_shares.get(c, 0) + n
        contrib[c] += n * (pn - p)
        if is_hk(c):
            cash_hkd -= n * p
        else:
            cash_cny -= n * p
    else:
        now_shares[c] = now_shares.get(c, 0) - n
        contrib[c] += n * (p - pn)
        if is_hk(c):
            cash_hkd += n * p
        else:
            cash_cny += n * p

# 一致性校验：回放出的期末持股应等于 /api/holdings
import urllib.request as _u
api = json.load(_u.urlopen("http://127.0.0.1:8686/api/holdings", timeout=20))
api_shares = {x["code"]: x["net_shares"] for x in api}
diff = [(c, now_shares.get(c, 0), api_shares.get(c, 0)) for c in codes
        if now_shares.get(c, 0) != api_shares.get(c, 0)]
print("持股回放校验(应为空):", diff)

# 分币种汇总
def bucket(codeset):
    return "HKD" if is_hk(list(codeset)[0]) else "CNY"

rows = []
for c in codes:
    s0, sn, pn, p0 = base_shares.get(c, 0), now_shares.get(c, 0), price_now.get(c), p1030.get(c)
    if not pn or p0 is None:
        continue
    v0 = s0 * p0
    flat_end = s0 * pn
    # 现金流（本币）只计入该股 11 月交易
    cf = sum((t["shares"] * t["price"] * (1 if t["side"] == "sell" else -1))
             for t in nov if t["code"] == c)
    real_end = sn * pn + cf
    rows.append({"code": c, "name": names.get(c, c), "ccy": "HKD" if is_hk(c) else "CNY",
                 "s0": s0, "sn": sn, "p0": p0, "pn": pn,
                 "v0": v0, "flat_end": flat_end, "real_end": real_end,
                 "flat_gain": flat_end - v0, "real_gain": real_end - v0,
                 "delta": real_end - flat_end, "contrib": contrib.get(c, 0.0),
                 "fx": fx if is_hk(c) else 1.0})

def s(key, ccy=None):
    """key 求和；ccy='CNY' 时把港股按汇率折成人民币，ccy=None 为原币(不混合)。"""
    return sum(r[key] * (r["fx"] if ccy == "CNY" else 1.0)
               for r in rows if ccy is None or True)

print("\n================ 汇总（原币分列） ================")
for ccy in ("CNY", "HKD"):
    sub = [r for r in rows if r["ccy"] == ccy]
    print(f"[{ccy}] 期初市值={sum(r['v0'] for r in sub):,.0f}  "
          f"躺平期末={sum(r['flat_end'] for r in sub):,.0f}  "
          f"实际期末(持仓+现金)={sum(r['real_end'] for r in sub):,.0f}")
    print(f"       躺平11月收益={sum(r['flat_gain'] for r in sub):+,.0f}  "
          f"实际11月收益={sum(r['real_gain'] for r in sub):+,.0f}  "
          f"差额(实际-躺平)={sum(r['delta'] for r in sub):+,.0f}")

# 现金流与买卖贡献分解（折算 CNY）
buy_amt = {"CNY": 0.0, "HKD": 0.0}
sell_amt = {"CNY": 0.0, "HKD": 0.0}
buy_contrib = sell_contrib = 0.0
for t in nov:
    c, n, p = t["code"], t["shares"], t["price"]
    k = "HKD" if is_hk(c) else "CNY"
    w = fx if is_hk(c) else 1.0
    pn = price_now.get(c) or 0
    if t["side"] == "buy":
        buy_amt[k] += n * p
        buy_contrib += n * (pn - p) * w
    else:
        sell_amt[k] += n * p
        sell_contrib += n * (p - pn) * w
print("\n================ 11月现金流（原币） ================")
for k in ("CNY", "HKD"):
    print(f"[{k}] 买入 {buy_amt[k]:,.0f}  卖出 {sell_amt[k]:,.0f}  "
          f"净现金 {sell_amt[k]-buy_amt[k]:+,.0f}")
print(f"[折算CNY] 买入贡献(买对/买错) {buy_contrib:+,.0f}   "
      f"卖出贡献(卖对/卖飞) {sell_contrib:+,.0f}   合计 {buy_contrib+sell_contrib:+,.0f}")
print("  K线11/11收盘 vs 实时收盘:",
      [(c, p1111_kline[c], price_now[c]) for c in codes if p1111_kline[c]])

tot_v0, tot_flat, tot_real = s("v0", "CNY"), s("flat_end", "CNY"), s("real_end", "CNY")
print("\n================ 折算人民币合计 ================")
print(f"10/30 期初市值  ¥{tot_v0:,.0f}")
print(f"躺平不动 期末   ¥{tot_flat:,.0f}   11月收益 ¥{tot_flat-tot_v0:+,.0f}  ({(tot_flat/tot_v0-1)*100:+.2f}%)")
print(f"实际操作 期末   ¥{tot_real:,.0f}   11月收益 ¥{tot_real-tot_v0:+,.0f}  ({(tot_real/tot_v0-1)*100:+.2f}%)")
print(f">>> 差额 实际操作 - 躺平 = ¥{tot_real-tot_flat:+,.0f}")

print("\n================ 11月交易明细（按对差额贡献排序，CNY） ================")
for c, v in sorted(contrib.items(), key=lambda kv: -kv[1] * (fx if is_hk(kv[0]) else 1)):
    ts = [t for t in nov if t["code"] == c]
    detail = " ".join(f"{t['date'][5:]}{'买' if t['side']=='buy' else '卖'}{t['shares']}@{t['price']:g}" for t in ts)
    print(f"{c} {names.get(c,c):<12} 现价{price_now[c]:>8g}  贡献 {v*(fx if is_hk(c) else 1):>+12,.0f}  | {detail}")

print("\n================ 躺平收益主要来源（CNY） ================")
for r in sorted(rows, key=lambda r: -abs(r["flat_gain"] * r["fx"]))[:12]:
    print(f"{r['code']} {r['name']:<12} {r['s0']:>7}股  {r['p0']:>8g}->{r['pn']:<8g} "
          f"躺平{r['flat_gain']*r['fx']:>+12,.0f}  实际{r['real_gain']*r['fx']:>+12,.0f}  差{r['delta']*r['fx']:>+12,.0f}")

# 输出 JSON 供后续使用
out = {"fx": fx, "asof": "2026-11-11 收盘", "base_date": "2026-10-30",
       "totals_cny": {"v0": tot_v0, "flat_end": tot_flat, "real_end": tot_real,
                      "flat_gain": tot_flat-tot_v0, "real_gain": tot_real-tot_v0,
                      "delta": tot_real-tot_flat},
       "rows": rows,
       "nov_trades": nov}
with open("D:/correct_your_life/stock-advisor/data/_nov_compare_result.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1, default=str)
print("\n[saved] data/_nov_compare_result.json")
