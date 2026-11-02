# -*- coding: utf-8 -*-
"""抽样实测：LLM 判 B站动态情绪到底行不行。

背景
----
`social_signal.py` 模块头有一条实测结论：B站/公众号正文**按公司名可关联率 0%**
（讲板块概念、不讲公司全名），所以 direct 层基本白抓，真正有用的是 hot 层
（按互动量排的全市场热度）。现在要给这套社媒数据补一个**可量化**的情绪指标
（像 `ipo_market.sentiment()` 那样给出 band + multiplier），
判定方式用 LLM 而不是词表 —— 因为 B站是口语化、反讽、表情包的重灾区。

所以先抽样，拿数字说话，再决定要不要全量：
  1. **可用率**：多少条的正文够判（不是纯「哈哈哈」「转发抽奖」这类无信息量）
  2. **方向分布**：pos/neg/neutral 各占多少 —— neg 占比过高说明模型把
     「讨论风险」也算成看空，需要收紧定义
  3. **互动量加权后的偏移**：互动量天然偏向恐慌/愤怒内容，加权会把总分
     往负向推。这里同时打「等权」和「互动量加权」两套数字对比
  4. **单 UP 主主导度**：按 author 聚合后再平均 vs 直接平均，看某一两个
     UP 主是否主导了全局情绪（B站 UP 主的风格差异极大）

脚本只读不写（除了 --limit 的采样），不动 sa_bili_* 的现有数据。

用法
----
    python scripts/probe_bili_sentiment.py                # 默认 40 条
    python scripts/probe_bili_sentiment.py --limit 80 --hours 72
    python scripts/probe_bili_sentiment.py --dump 结果.json # 存逐条判定明细
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import get_conn                      # noqa: E402
from psycopg2.extras import RealDictCursor     # noqa: E402

DEFAULT_HOURS = 72
DEFAULT_LIMIT = 40
WORKERS = 3          # 网关并发别调高：实测 6 并发会打出 WinError10065


# ---- Prompt（与 news_fetcher 的 classify_sentiment 同一套产出契约）----
# 为什么不是直接复用词表函数：词表对「满仓了兄弟们冲」「完了芭比Q了」这种
# 口语完全无力，而 LLM 恰恰擅长这个。产出仍借用 pos/neg/neutral 三值，
# 是为了将来和 sa_news.sentiment 对齐渲染。
SYSTEM = """你是A股/港股散户社区的舆情标注员。给你一条 B站UP主动态，判断它对
**股市/个股**的情绪方向。只输出 JSON，不要任何其他文字。

输出格式：
{"sentiment":"pos"|"neg"|"neutral","topic":["题材词"],"reason":"20字内"}

判定规则（按优先级）：
1. sentiment 只描述**对股市的方向**，不是 UP 主本人的情绪。UP 主很激动但内容
   是中性的行情复盘 → neutral。
2. pos = 看多/看好/利好/赚钱效应（含晒盈亏为正是 pos，但「别人赚我没赚」
   这种抱怨算 neutral）。neg = 看空/利空/恐慌/亏钱。中性叙述 → neutral。
3. **反讽和表情包是 B站的常态**：「太棒了，又跌停了」是 neg 不是 pos。
4. 「满仓」「抄底」「梭哈」「冲」这类喊单且没有利空事实 → pos（弱信号，
   但方向明确）。
5. 无法判断（抽奖/闲聊/与股市无关/纯表情）→ neutral，topic 给空数组。
6. topic 只填**题材/板块/概念**词（如 算力、AI、创新药、PCB、机器人），
   不要填公司全名。"""


def build_user(row: dict, snippet: int = 600) -> str:
    title = (row.get("title") or "").strip()
    text = (row.get("text") or "").strip()
    body = f"{title}：{text}" if title and text else (title or text)
    body = " ".join(body.split())[:snippet]
    st = row.get("stats") or {}
    likes = int(st.get("likes") or 0)
    comments = int(st.get("comments") or 0)
    fwd = int(st.get("forwards") or 0)
    return (f"UP主：{row.get('author_name') or row.get('uid') or '?'}\n"
            f"发布时间：{row.get('pub_ts')}\n"
            f"互动：赞{likes} 评{comments} 转{fwd}\n"
            f"正文：\n{body}")


def fetch_rows(hours: int, limit: int) -> list[dict]:
    """只取 dynamics —— 不取 comments。两处实测结论都排除了评论：
    social_signal.py 的 skip_concept、stock_discovery.py:30 的权重注释。"""
    with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT dynamic_id, uid, author_name, title, text, pub_ts, stats
            FROM sa_bili_dynamics
            WHERE pub_ts >= now() - interval '%s hours'
            ORDER BY pub_ts DESC
            LIMIT %s
            """,
            (hours, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def parse_judge(raw: str) -> dict:
    """LLM 返回 -> {sentiment, topic, reason}。解析失败返回 available=False。"""
    import re as _r
    m = _r.search(r"\{.*?\}", raw or "", _r.S)
    if not m:
        return {"available": False, "why": "无 JSON"}
    try:
        d = json.loads(m.group(0))
    except Exception as exc:
        return {"available": False, "why": f"JSON 解析失败: {exc}"}
    s = str(d.get("sentiment") or "").lower()
    if s not in ("pos", "neg", "neutral"):
        return {"available": False, "why": f"非法 sentiment 值: {d.get('sentiment')!r}"}
    topic = d.get("topic") or []
    if not isinstance(topic, list):
        topic = []
    return {"available": True, "sentiment": s,
            "topic": [str(x) for x in topic if str(x).strip()],
            "reason": str(d.get("reason") or "")[:60]}


def judge_one(row: dict, llm) -> dict:
    user = build_user(row)
    out = {"dynamic_id": row["dynamic_id"], "author": row.get("author_name") or "",
           "stats": row.get("stats") or {}}
    try:
        raw = llm.ask(SYSTEM, user, thinking=False, task="fast",
                      max_tokens=400)
    except Exception as exc:
        out.update(available=False, why=f"{type(exc).__name__}: {str(exc)[:160]}")
        return out
    out.update(parse_judge(raw))
    out["raw"] = raw
    return out


def eng(row_or_judge) -> int:
    st = row_or_judge.get("stats") or {}
    return (int(st.get("likes") or 0) + int(st.get("comments") or 0)
            + int(st.get("forwards") or 0))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=DEFAULT_HOURS)
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ap.add_argument("--dump", default="", help="逐条判定明细输出 JSON 路径")
    args = ap.parse_args()

    import llm_advisor as llm
    conf = llm.load_llm_conf()
    if not conf.get("enabled") or not conf.get("api_key"):
        print("llm.enabled=false 或未配 api_key，无法抽样", flush=True)
        return 2
    print("模型档位：fast -> %s" % (llm.model_for("fast", conf) or "(沿用默认)"),
          flush=True)

    rows = fetch_rows(args.hours, args.limit)
    print("近 %d 小时 sa_bili_dynamics：取样 %d 条" % (args.hours, len(rows)),
          flush=True)
    if not rows:
        return 1

    t0 = time.time()
    judges: list[dict] = []
    if WORKERS > 1 and len(rows) > 4:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for i, j in enumerate(ex.map(lambda r: judge_one(r, llm), rows), 1):
                judges.append(j)
                print("  %d/%d %s" % (i, len(rows), j.get("sentiment") or "?"),
                      flush=True)
    else:
        for i, r in enumerate(rows, 1):
            j = judge_one(r, llm)
            judges.append(j)
            print("  %d/%d %s" % (i, len(rows), j.get("sentiment") or "?"),
                  flush=True)
    took = time.time() - t0

    ok = [j for j in judges if j.get("available")]
    bad = [j for j in judges if not j.get("available")]

    print("\n" + "=" * 72)
    print("LLM 判定实测：%d 条，成功 %d，失败 %d，耗时 %.0fs（%.1fs/条）"
          % (len(judges), len(ok), len(bad), took, took / max(1, len(judges))))
    if bad:
        why = Counter(j.get("why", "?")[:40] for j in bad)
        print("失败原因：", dict(why.most_common(5)))

    if not ok:
        return 1

    n = len(ok)
    cnt = Counter(j["sentiment"] for j in ok)
    print("\n方向分布（等权）：")
    for k in ("pos", "neutral", "neg"):
        print("  %-8s %3d  %5.1f%%" % (k, cnt.get(k, 0), cnt.get(k, 0) / n * 100))

    # 归一化净情绪分 -1~+1：neg 权重 1、pos 权重 1，neutral 不计
    net = (cnt.get("pos", 0) - cnt.get("neg", 0)) / n
    print("  净情绪分（等权）     %+.3f" % net)

    # 互动量加权：恐慌/愤怒内容天然高互动，这里是偏移的量度
    w_pos = sum(eng(j) for j in ok if j["sentiment"] == "pos")
    w_neg = sum(eng(j) for j in ok if j["sentiment"] == "neg")
    w_tot = w_pos + w_neg
    if w_tot:
        print("  净情绪分（互动加权） %+.3f"
              "   ← 偏移 %+.3f"
              % ((w_pos - w_neg) / w_tot, (w_pos - w_neg) / w_tot - net))
        print("  pos 互动合计 %d / neg 互动合计 %d" % (w_pos, w_neg))
    zero_eng = sum(1 for j in ok if eng(j) == 0)
    print("  互动量为 0 的判定：%d 条（%.0f%%）—— 这类对加权无贡献"
          % (zero_eng, zero_eng / n * 100))

    # 单 UP 主主导度：按 author 平均后再比，与直接平均对照
    by_author: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # pos, neg
    for j in ok:
        a = j.get("author") or "?"
        if j["sentiment"] == "pos":
            by_author[a][0] += 1
        elif j["sentiment"] == "neg":
            by_author[a][1] += 1
    if by_author:
        per_author = [(p - g) / (p + g) for p, g in by_author.values() if p + g]
        if per_author:
            aa = sum(per_author) / len(per_author)
            print("\n按 UP 主聚合后净情绪 %+.3f（%d 个 UP 主，%s）"
                  % (aa, len(by_author),
                     "结论：需按 author 聚合防个别UP主主导"
                     if abs(aa - net) > 0.15 else "结论：个别UP主未主导，可按条平均"))
        multi = [a for a, (p, g) in by_author.items() if p + g >= 3]
        if multi:
            print("  判定≥3条的UP主：%s" % ", ".join(
                "%s(%dpos/%dneg)" % (a, *by_author[a]) for a in multi[:6]))

    # 题材维度：按 topic 词统计 pos-neg，看 LLM 抽的题材词能不能分出方向
    topic_cnt: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for j in ok:
        for t in set(j.get("topic") or []):
            if j["sentiment"] == "pos":
                topic_cnt[t][0] += 1
            elif j["sentiment"] == "neg":
                topic_cnt[t][1] += 1
    if topic_cnt:
        print("\n题材方向（出现≥2次）：")
        for t, (p, g) in sorted(topic_cnt.items(),
                                key=lambda kv: -(kv[1][0] + kv[1][1]))[:12]:
            if p + g < 2:
                continue
            print("  %-12s pos %d / neg %d  → %s"
                  % (t, p, g, "看多" if p > g else ("看空" if g > p else "分歧")))

    if args.dump:
        p = Path(args.dump)
        p.write_text(json.dumps(
            {"meta": {"hours": args.hours, "limit": args.limit,
                      "model": llm.model_for("fast", conf),
                      "judged_at": time.strftime("%Y-%m-%d %H:%M:%S")},
             "judges": judges},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print("\n明细已存 %s" % p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
