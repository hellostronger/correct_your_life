# -*- coding: utf-8 -*-
"""抓真实 API 返回，存成 JSON 给 node 做渲染测试用。

用真数据而不是编的假数据：渲染代码里最容易错的地方恰恰是
「我以为字段叫 A 其实叫 B」和「某个字段是 null 我直接 .length 了」。
假数据会把这两类 bug 全放过去。

产物落在系统临时目录而不是仓库里 —— fixtures 是抓取时刻的快照，进 git
只会变成第二份会过期的东西。测试从临时目录读。
"""
import io
import json
import os
import tempfile
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8686"
OUT = Path(tempfile.gettempdir()) / "opencode" / "jq_fixtures.json"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"__http%d" % e.code: e.read().decode("utf-8", "replace")[:300]}


arts = get("/api/strategy-lib/articles?limit=100")
with_code = [a["post_id"] for a in (arts.get("items") or []) if a.get("has_code")]
pid = with_code[0] if with_code else "505366328b8be8ce53ef9575f22a65e0"

out = {
    "stats": get("/api/strategy-lib/stats"),
    "health": get("/api/sandbox/health"),
    "articles": arts,
    "digest": get("/api/strategy-digest/list?limit=100"),
    "sandbox_runs": get("/api/sandbox/runs?limit=50"),
    "sandbox_runs_failed": get("/api/sandbox/runs?limit=50&only_failed=true"),
    "backtest_runs": get("/api/backtest/runs?limit=50"),
    "article": get("/api/strategy-lib/article?post_id=" + pid),
    "precheck": post("/api/sandbox/precheck",
                     {"post_id": pid, "start": "2024-01-01", "end": "",
                      "limit_codes": 400}),
    "compare": get("/api/sandbox/compare?post_id=" + pid),
}
# 曲线单独抓：可能很长，单独存
runs = out["backtest_runs"].get("items") or []
if runs:
    out["curve"] = get("/api/backtest/curve?run_id=%s" % runs[0]["id"])
else:
    out["curve"] = {"run_id": 0, "curve": []}

OUT.parent.mkdir(parents=True, exist_ok=True)
io.open(str(OUT), "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False))
print("  抓完 -> %s" % OUT)
print("  post_id=%s" % pid)
for k, v in out.items():
    n = (len(v.get("items") or v.get("curve") or []) if isinstance(v, dict)
         else len(v) if isinstance(v, list) else 1)
    print("    %-22s %s" % (k, ("%d 项" % n) if n else "空"))
