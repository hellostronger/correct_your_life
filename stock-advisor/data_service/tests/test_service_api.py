"""对运行中的 data_service 做端到端 HTTP 测试。"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"
sys.stdout.reconfigure(encoding="utf-8")

from data_service.tests.asserts import FAIL, PASS, check, report

BASE = "http://127.0.0.1:8901"


def call(path, method="GET", timeout=180):
    # 关键：路径里的中文板块名必须百分号编码，否则 urllib 直接报错
    enc = urllib.parse.quote(path, safe="/?&=%")
    req = urllib.request.Request(BASE + enc, method=method)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            return r.status, json.loads(body), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:200], time.time() - t0
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}", time.time() - t0


print("=" * 78)
print("1. 根 + /health")
print("=" * 78)
st, j, ms = call("/")
check("GET / = 200", st == 200, note=f"({ms:.0f}ms)")
st, h, ms = call("/health")
check("GET /health = 200", st == 200, note=f"({ms:.0f}ms)")
if st == 200:
    print(f"      version={h.get('version')}  cached={h.get('cached')}")
    for s in h.get("sources", []):
        print(f"      源 {s['name']:<7} healthy={s['healthy']} "
              f"fail={s['consec_fail']} calls={s['calls']} "
              f"err={s.get('last_error','')[:50]}")
    print(f"      请求量: {json.dumps(h.get('meter', {}), ensure_ascii=False)}")

print()
print("=" * 78)
print("2. GET /boards —— 核心端点（真打源）")
print("=" * 78)
st, b, ms = call("/boards", timeout=300)
check("GET /boards = 200", st == 200, note=f"({ms:.0f}ms)")
if st != 200:
    print("  响应:", str(b)[:400])
else:
    print(f"      板块总数={b['total_boards']}  trade_date={b['trade_date']}")
    print(f"      用到的源={b['sources_used']}  degraded={b['degraded']}")
    for n in b.get("notes", []):
        print(f"      note: {n}")
    for f in b.get("sources_failed", []):
        print(f"      FAILED: {f}")
    cc = b.get("cross_check") or {}
    if cc:
        print(f"      跨厂商校验（指标={cc.get('metric')}）:")
        for pr in cc.get("pairs", []):
            print(f"        {' vs '.join(pr['pair'])}: 共同 {pr['common']} 个  "
                  f"涨跌幅差 中位 {pr['median_pp']}pp / p90 {pr['p90_pp']}pp / "
                  f"max {pr['max_pp']}pp  <=1pp 占比 {pr['within_1pp_pct']}%")
    else:
        print("      （本次无跨厂商校验记录）")
    bt = b["boards"]
    check("有板块数据", b["total_boards"] > 0, note=f"= {b['total_boards']}")
    if bt:
        from collections import Counter
        kinds = Counter(x["kind"] for x in bt)
        srcs = Counter(x["source"] for x in bt)
        print(f"      类别分布: {dict(kinds)}")
        print(f"      来源分布: {dict(srcs)}")
        check("含行业板块", kinds.get("industry", 0) > 0)
        check("评分已算好", all("score" in x for x in bt))
        check("按分降序", all(bt[i]["score"] >= bt[i + 1]["score"]
                              for i in range(len(bt) - 1)))
        filled = sum(1 for x in bt if x.get("filled_by"))
        print(f"      被 kph 补齐字段的板块: {filled}")
        print()
        print(f"      {'#':<4}{'板块':<16}{'总分':>7}{'涨幅%':>8}{'净流入亿':>10}"
              f"{'源':>7}{'类别':>9}  分项")
        for i, x in enumerate(bt[:10], 1):
            p = x.get("score_parts", {})
            print(f"      {i:<4}{x['name'][:15]:<16}{x['score']:>7}"
                  f"{(x['pct'] if x['pct'] is not None else 0):>8.2f}"
                  f"{(x['main_inflow'] if x['main_inflow'] is not None else 0):>10.2f}"
                  f"{x['source']:>7}{x['kind']:>9}  {p}")
        print()
        print("      净流出 Top5:")
        for x in sorted([y for y in bt if y["main_inflow"] is not None],
                        key=lambda y: y["main_inflow"])[:5]:
            print(f"        {x['name'][:15]:<16} 涨{x['pct']:>7}%  "
                  f"净流入 {x['main_inflow']:>9.2f}亿  涨/跌 "
                  f"{x.get('up_count')}/{x.get('down_count')}")

print()
print("=" * 78)
print("3. GET /zt-pool")
print("=" * 78)
st, z, ms = call("/zt-pool")
check("GET /zt-pool = 200", st == 200, note=f"({ms:.0f}ms)")
if st == 200:
    print(f"      qdate={z.get('qdate')} 涨停={z.get('total')} "
          f"最高连板={z.get('max_lb')} 炸板={z.get('sum_zbc')} "
          f"涉及板块={len(z.get('by_board', {}))}")

print()
print("=" * 78)
print("4. GET /history/{板块}")
print("=" * 78)
st, hh, ms = call("/history/生物制品?days=20", timeout=120)
check("GET /history = 200", st == 200, note=f"({ms:.0f}ms)")
if st == 200:
    print(f"      {hh['name']} {hh['days']} 根日线")
    for r in hh["rows"][-3:]:
        print(f"        {r['date']} close={r['close']}")

print()
print("=" * 78)
print("5. GET /boards/881142/constituents（行业成分股，应通）")
print("=" * 78)
st, cc2, ms = call("/boards/881142/constituents?limit=5", timeout=120)
check("行业成分股 = 200", st == 200, note=f"({ms:.0f}ms)")
if st == 200:
    print(f"      {cc2['code']} 共 {cc2['count']} 只, 返回前 {len(cc2['items'])}")
    for it in cc2["items"][:5]:
        print(f"        {it['code']} {it['name']}")
else:
    print("      ", str(cc2)[:200])

print()
print("=" * 78)
print("6. GET /boards/308941/constituents（概念 3xxxxx，应给可读错误）")
print("=" * 78)
st, e3, ms = call("/boards/308941/constituents", timeout=120)
check("概念 3xxxxx 返回 404(不是静默 0 行)", st == 404, note=f"(实际 {st})")
print(f"      错误信息: {str(e3)[:200]}")

print()
print("=" * 78)
print("7. POST /collect 强制重采")
print("=" * 78)
st, fc, ms = call("/collect", method="POST", timeout=300)
check("POST /collect = 200", st == 200, note=f"({ms:.0f}ms)")
if st == 200:
    print(f"      板块={fc['total_boards']} degraded={fc['degraded']}")
    for t in fc.get("top10", [])[:5]:
        print(f"        {t['name'][:16]:<18} {t['score']}")

print()
print("=" * 78)
print("8. 降级路径：关掉 kph 只用同花顺")
print("=" * 78)
st, only, ms = call("/boards?include_kph=false&include_concept=true&refresh=true",
                    timeout=300)
check("仅同花顺 = 200", st == 200, note=f"({ms:.0f}ms)")
if st == 200:
    print(f"      板块={only['total_boards']} 源={only['sources_used']} "
          f"degraded={only['degraded']}")
    from collections import Counter
    print(f"      类别: {dict(Counter(x['kind'] for x in only['boards']))}")

print()
print("=" * 78)
print("9. 最省模式：只要行业")
print("=" * 78)
st, cheap, ms = call("/boards?include_concept=false&include_kph=false&refresh=true",
                     timeout=200)
check("仅行业 = 200", st == 200, note=f"({ms:.0f}ms)")
if st == 200:
    print(f"      板块={cheap['total_boards']} 用时={ms:.0f}ms")

sys.exit(report())
