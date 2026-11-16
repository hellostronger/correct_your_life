"""对话评测跑批（chat_eval_run.py）：把评测问题真机发给 /api/chat/send，逐题落盘。

用法：
  python chat_eval_run.py --limit 20          # 分层抽 20 题
  python chat_eval_run.py --ids A03,J05       # 指定题目（复测失败子集）
  python chat_eval_run.py --all               # 全部 100 题

每题结果追加 data/chat_eval_results.jsonl（可断点续跑：已答过的题跳过）。
评分维度（人工/半自动复核用）：来源（工具/诚实/幻觉）、越界、可用性。
"""
from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path

BASE = Path(__file__).resolve().parent
QFILE = BASE / "chat_eval_questions.jsonl"
OUT = BASE / "data" / "chat_eval_results.jsonl"
SA = "http://127.0.0.1:8686"

# 分层抽测的题目 id（覆盖全部 11 类 + 对抗面）
STRATIFIED = ["A02", "A04", "A05", "A07", "A11", "B01", "B04", "B07", "C01", "C05",
              "C09", "D01", "D04", "D07", "E01", "F01", "F03", "F08", "G02", "G07",
              "H02", "H09", "I01", "I07", "I08", "J01", "J02", "J05", "J07", "J10",
              "K01", "K04"]


def load_questions() -> list[dict]:
    out = []
    for line in QFILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def done_ids() -> set[str]:
    if not OUT.exists():
        return set()
    ids = set()
    for line in OUT.read_text(encoding="utf-8").splitlines():
        try:
            ids.add(json.loads(line).get("id"))
        except Exception:
            pass
    return ids


def ask(q: str, session: str, wall: int = 300) -> dict:
    """带墙钟看门狗：requests 的 timeout 只限单次读，服务端挂起会拖数小时
    （K01 实测 21023s）—— 线程 + join 硬切。"""
    import threading
    result: dict = {}

    def _go():
        import requests
        body = {"sid": session, "text": q}
        t0 = time.time()
        try:
            r = requests.post(SA + "/api/chat/send", json=body, timeout=(10, 280),
                              proxies={"http": None, "https": None})
            dt = round(time.time() - t0, 1)
            if r.status_code != 200:
                result.update({"http": r.status_code, "latency_s": dt,
                               "reply": f"[HTTP {r.status_code}] {r.text[:200]}"})
                return
            d = r.json()
            result.update({"http": 200, "latency_s": dt,
                           "src": (d.get("assistant_message") or {}).get("src"),
                           "reply": (d.get("assistant_message") or {}).get("text") or ""})
        except Exception as exc:
            result.update({"http": 0, "latency_s": round(time.time() - t0, 1),
                           "reply": f"[异常] {type(exc).__name__}: {str(exc)[:150]}"})

    th = threading.Thread(target=_go, daemon=True)
    th.start()
    th.join(wall)
    if not result:
        return {"http": 0, "latency_s": wall,
                "reply": f"[超时] {wall}s 内无回复（网关/agent 挂起）"}
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="分层抽测条数（0=用 STRATIFIED 全量）")
    ap.add_argument("--ids", type=str, default="", help="逗号分隔指定题目 id")
    ap.add_argument("--all", action="store_true", help="全部题目")
    args = ap.parse_args()

    qs = load_questions()
    if args.ids:
        want = {x.strip() for x in args.ids.split(",") if x.strip()}
        picked = [q for q in qs if q["id"] in want]
    elif args.all:
        picked = qs
    else:
        want = set(STRATIFIED[:max(1, args.limit)]) if args.limit else set(STRATIFIED)
        picked = [q for q in qs if q["id"] in want]

    done = done_ids()
    todo = [q for q in picked if q["id"] not in done]
    print(f"评测 {len(todo)}/{len(picked)} 题（已答 {len(picked) - len(todo)} 跳过）", flush=True)
    session = "web:" + uuid.uuid4().hex[:12]
    OUT.parent.mkdir(parents=True, exist_ok=True)

    for i, q in enumerate(todo, 1):
        t0 = time.time()
        try:
            res = ask(q["q"], session)
        except Exception as exc:
            res = {"http": 0, "latency_s": round(time.time() - t0, 1),
                   "reply": f"[异常] {type(exc).__name__}: {str(exc)[:150]}"}
        row = {"id": q["id"], "cat": q["cat"], "q": q["q"], "expect": q["expect"],
               "ts": time.strftime("%m-%d %H:%M:%S"), **res}
        with OUT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        reply_head = res["reply"].replace("\n", " ")[:90]
        print(f"[{i}/{len(todo)}] {q['id']} ({res['latency_s']}s) {reply_head}", flush=True)


if __name__ == "__main__":
    main()
