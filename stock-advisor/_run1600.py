# -*- coding: utf-8 -*-
"""万得微盘股复刻：1600 只截面重跑。

400 只那轮 +51.66%，而同策略实测 400/800/1600 → +16.95%/+87.72%/+128.57%，
所以必须重跑才能知道这个数字到底多少。precheck 估 424MB > 512MB 上限说不下，
local 不吃容器限制，先用它出数；容器那轮再按需覆盖 limits。
"""
import json, time, sys
sys.stdout.reconfigure(encoding="utf-8")
import app as A

PID = "505366328b8be8ce53ef9575f22a65e0"
OUT = "_run1600.json"

try:
    d = json.load(open(OUT, encoding="utf-8"))
except Exception:
    d = {}
if d.get("done"):
    print("[skip] 已有结果"); sys.exit(0)

t0 = time.time()
print("===== 万得微盘股复刻 1600 只 local 开始", flush=True)
rec = {"done": False, "codes": 1600, "where": "local"}
d = rec
try:
    r = A.sandbox_run(A.SandboxRunIn(post_id=PID, where="local", save=True,
                                      limit_codes=1600, cash=1_000_000,
                                      timeout=1800))
    res = r.get("result") or {}
    rec.update({"done": True, "ok": r.get("ok"), "run_id": r.get("run_id"),
                "slice": r.get("slice"), "metrics": res.get("metrics"),
                "trade_stats": res.get("trade_stats"),
                "n_rejected": res.get("n_rejected"),
                "n_callback_errors": res.get("n_callback_errors"),
                "warnings": (res.get("warnings") or [])[:10],
                "error": (res.get("error") or "")[:1500],
                "traceback": (res.get("traceback") or "")[-2500:],
                "elapsed": res.get("elapsed"),
                "wall": round(time.time() - t0, 1)})
    json.dump(rec, open(OUT, "w", encoding="utf-8"), ensure_ascii=False,
              indent=1, default=str)
    print("  ok=%s 用时%.1fs run_id=%s" % (r.get("ok"), time.time()-t0,
                                          r.get("run_id")), flush=True)
    print("  metrics:", json.dumps(rec.get("metrics"), ensure_ascii=False), flush=True)
    print("  trade_stats:", json.dumps(rec.get("trade_stats"), ensure_ascii=False), flush=True)
    if rec["error"]:
        print("  error:", rec["error"][:400], flush=True)
except Exception as exc:
    import traceback
    rec.update({"done": True, "ok": False,
                "error": "%s: %s" % (type(exc).__name__, exc),
                "traceback": traceback.format_exc()[-2000:],
                "wall": round(time.time() - t0, 1)})
    json.dump(rec, open(OUT, "w", encoding="utf-8"), ensure_ascii=False,
              indent=1, default=str)
    print("  EXC:", rec["error"][:400], flush=True)
