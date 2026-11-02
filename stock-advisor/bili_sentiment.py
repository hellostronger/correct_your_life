# -*- coding: utf-8 -*-
"""B站动态情绪判定 → 可量化的市场情绪指标。

## 为什么加（2026-10-19）
`social_signal.py` 模块头有一条实测结论：社媒正文按公司名可关联率 **0%**
（讲板块概念、不讲公司全名），所以 direct 层基本白抓；真正有用的是 hot 层
（按互动量排的全市场热度，交给 LLM 自己做语义判断）。

但 hot 层只解决「给 LLM 看什么」，没解决「**这堆内容整体是什么情绪」」——
想要一个像 `ipo_market.sentiment()` 那样可量化、可回溯、能进报告的
band + multiplier，就得先把每条内容判出方向。

## 为什么用 LLM 而不是词表
`news_fetcher.classify_sentiment` 的词表对「满仓了兄弟们冲」「完了芭比Q了」
这种口语完全无力，而 B站是口语、反讽、表情包的重灾区。LLM 擅长这个。

## 抽样实测结论（scripts/probe_bili_sentiment.py，35 条 / 近 168h）
    pos       12   35.3%
    neutral   19   55.9%
    neg        3    8.8%
    policy_appeal  —— 单独一类，见下
    等权净分 +0.265 / 互动加权 +0.488（偏移 +0.223）

三条必须在实现里处理的结论：

1. **「呼吁救市」不能算方向。** 实测里「表达希望加大救市」被判 pos、
   「呼吁市场稳定」被判 neg —— 同一个动作因为措辞不同被分到相反两侧。
   这类内容反映的是**焦虑度**不是方向，所以单列 `policy_appeal`，
   不计入 direction，只作为焦虑度的旁证。

2. **neutral 不能进分母。** 56% 的 neutral 会把净分稀释到接近 0，五档 band
   永远落在"中性"。聚合口径只取 pos + neg。

3. **题材词必须从表里选。** 自由抽取 34 条只出 3 个词（正文太短，多为
   120 字内的观点句）—— 等于没这功能。改为给定题材表，宁可不填。

## ⚠️ 供给瓶颈（比算法重要）
`sa_bili_creators` 只有 2 个 UP 主，近 168 小时实际只有 1 个在产出
（另一个 C***学 自 2024-09 后无更新）。所以任何「全市场情绪」的说法在当前
供给水平下都不成立 —— 模块按 author 输出 per_author 作诊断，但不强行校正
（校正需要供给，不是算法能解决的）。

## 不读评论
`sa_bili_comments` 7414 条，但 `social_signal.py` 的 skip_concept 与
`stock_discovery.py:30` 两处实测结论都排除了评论（股吧闲聊噪音远大于信息量）。
注意：那条结论的验证场景是「按公司名关联」，直接搬到情绪判定上未必成立 ——
供给不足时评论是唯一的量级来源，值得单独抽样再评估。
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# ---------------- 配置 ----------------
# 为什么独立一段而不是塞进 bili：bili 段管的是**抓取**（周期/超时/UP主），
# 这里管的是**判定与聚合口径**，改一个是改采集行为、改一个是改指标定义，
# 混在一段里以后调参看不出在调什么。
DEFAULT_CONF = {
    "enabled": True,
    "hours": 48,             # 时间窗（与 social_signal 一致：社媒热度衰减快）
    "batch_limit": 60,       # 单轮最多判多少条未判动态（控 LLM 成本）
    "concurrency": 3,        # LLM 并发。实测 6 并发会打出 WinError10065
    "snippet": 600,          # 每条正文截断长度
    "topics": [],            # 题材表（list 或逗号分隔字符串）。空=不抽题材
    "include_appeal": False, # band 计算是否把 policy_appeal 计入焦虑度
    # 五档阈值。⚠️ 这是**拍的初值**，不是按 B站实测分布定的 ——
    # ipo_market 的阈值来自市场宽度的实测分布，这里没有同等样本量。
    # 等判定积累到几百条后用 recalibrate() 回头校准。
    "band": {"hot": 0.5, "warm": 0.2, "cold": -0.2, "freeze": -0.5},
    "multiplier": {"hot": 1.15, "warm": 1.05, "neutral": 1.0,
                   "cold": 0.9, "freeze": 0.75},
}

TABLE = "sa_bili_sentiment"


def load_conf(conf: dict | None = None) -> dict:
    """读 config.yaml 的 bili_sentiment 段；缺失用默认值。"""
    c = dict(DEFAULT_CONF)
    try:
        base = __file__.rsplit("\\", 1)[0] if "\\" in __file__ else "."
        text = (base + "/config.yaml") if base else "config.yaml"
        import pathlib
        p = pathlib.Path(__file__).resolve().parent / "config.yaml"
        text = p.read_text(encoding="utf-8") if p.exists() else ""
        if text:
            import yaml
            data = yaml.safe_load(text) or {}
            raw = dict(data.get("bili_sentiment") or {})
            for k, v in raw.items():
                if v is None:
                    continue
                if k in ("band", "multiplier") and isinstance(v, dict):
                    c[k] = dict(c[k])
                    c[k].update(v)
                else:
                    c[k] = v
    except Exception:
        pass
    for k, v in (conf or {}).items():
        if v is not None:
            c[k] = v
    # topics 允许写成逗号分隔字符串（网页表单存进来就是字符串）
    if isinstance(c.get("topics"), str):
        c["topics"] = [x.strip() for x in re.split(r"[,，、]", c["topics"])
                       if x.strip()]
    return c


# ---------------- Prompt ----------------
# 四分类而不是三分类：policy_appeal 是从抽样实测里挖出来的必要类别。
# 产出仍是 pos/neg/neutral 这套值，是为了和 sa_news.sentiment 对齐渲染。
SYSTEM = """你是A股/港股散户社区的舆情标注员。给你一条 B站UP主动态，判断它对
**股市/个股**的情绪方向。只输出 JSON，不要任何其他文字。

输出格式：
{"sentiment":"pos"|"neg"|"neutral"|"policy_appeal","topic":["题材词"],"reason":"20字内"}

判定规则（按优先级，先满足的生效）：
1. **policy_appeal（政策建言类）**：内容是**呼吁/建议/建言**救市、稳市、
   出政策、加强监管 —— 而不是在陈述自己的持仓观点或看多看跌。
   典型：「建议加大救市力度」「应该出手稳市场」「呼吁保护中小投资者」。
   这类反映的是**焦虑与期待**，不是多空方向，所以单独归类。
   注意区分：「国家队出手了，利好」是在陈述利好 → pos；
             「希望国家队出手」是在呼吁 → policy_appeal。
2. sentiment 只描述**对股市的方向**，不是 UP 主本人的情绪。UP 主很激动但
   内容是中性的行情复盘 → neutral。
3. pos = 看多/看好/利好/赚钱效应（晒盈亏为正是 pos；但「别人赚我没赚」
   这种抱怨 → neutral）。neg = 看空/利空/恐慌/亏钱。中性叙述 → neutral。
4. **反讽和表情包是 B站常态**：「太棒了，又跌停了」是 neg 不是 pos。
5. 「满仓」「抄底」「梭哈」「冲」这类喊单且没有利空事实 → pos。
6. 无法判断（抽奖/闲聊/与股市无关/纯表情/夸UP主本人）→ neutral。
7. topic 只能从给定题材表里选，**一个都不匹配就填空数组**，
   不要自己造词、不要填公司全名。"""


def build_user(row: dict, topics: list, snippet: int = 600) -> str:
    title = (row.get("title") or "").strip()
    text = (row.get("text") or "").strip()
    body = f"{title}：{text}" if title and text else (title or text)
    body = " ".join(body.split())[:snippet]
    st = row.get("stats") or {}
    lines = [f"UP主：{row.get('author_name') or row.get('uid') or '?'}",
             f"互动：赞{int(st.get('likes') or 0)} "
             f"评{int(st.get('comments') or 0)} "
             f"转{int(st.get('forwards') or 0)}",
             f"正文：\n{body or '（空）'}"]
    if topics:
        lines.append(f"题材表（只能从这些里选）：{'、'.join(topics)}")
    return "\n".join(lines)


# ---------------- 取数 ----------------

def _conn_ctx():
    """延迟导入 app.get_conn：本模块被 app 导入，顶层 import app 会成环。
    这与 bili_monitor / sector 的做法一致。"""
    from app import get_conn
    return get_conn()


def fetch_pending(cur, hours: int, limit: int) -> list[dict]:
    """取未判定（或判定失败）的动态。只取 dynamics，不取 comments —— 见模块头。"""
    cur.execute(
        """
        SELECT d.dynamic_id, d.uid, d.author_name, d.title, d.text,
               d.pub_ts, d.stats
        FROM sa_bili_dynamics d
        LEFT JOIN sa_bili_sentiment s ON s.dynamic_id = d.dynamic_id
        WHERE d.pub_ts >= now() - interval '%s hours'
          AND (s.dynamic_id IS NULL OR s.sentiment = '')
        ORDER BY d.pub_ts DESC
        LIMIT %s
        """,
        (hours, limit),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------------- 判定 ----------------

def parse_judge(raw: str) -> dict:
    """LLM 返回 → {sentiment, topic, reason}。解析失败 available=False。"""
    m = re.search(r"\{.*?\}", raw or "", re.S)
    if not m:
        return {"available": False, "why": "无 JSON"}
    try:
        d = json.loads(m.group(0))
    except Exception as exc:
        return {"available": False, "why": f"JSON 解析失败: {exc}"}
    s = str(d.get("sentiment") or "").lower()
    if s not in ("pos", "neg", "neutral", "policy_appeal"):
        return {"available": False, "why": f"非法 sentiment 值: {d.get('sentiment')!r}"}
    topic = d.get("topic") or []
    if not isinstance(topic, list):
        topic = []
    return {"available": True, "sentiment": s,
            "topic": [str(x) for x in topic if str(x).strip()],
            "reason": str(d.get("reason") or "")[:60]}


def judge_one(row: dict, conf: dict, llm) -> dict:
    out = {"dynamic_id": row["dynamic_id"],
           "author": row.get("author_name") or "",
           "stats": row.get("stats") or {}}
    try:
        raw = llm.ask(SYSTEM, build_user(row, conf["topics"], conf["snippet"]),
                      thinking=False, task="fast", max_tokens=400)
    except Exception as exc:
        # LLM 失败不 raise：一条判不了不该让整轮停下。落 'unknown' 由下一轮补判，
        # 不落 '' —— '' 是「还没判」的标记，会被下一轮反复重取。
        out.update(available=False, sentiment="unknown",
                   why=f"{type(exc).__name__}: {str(exc)[:160]}")
        return out
    out.update(parse_judge(raw))
    return out


def run_once(conf: dict | None = None, hours: int | None = None) -> dict:
    """判一轮未判定的动态并落库。返回统计。"""
    c = load_conf(conf)
    if not c.get("enabled"):
        return {"enabled": False}
    hours = int(hours if hours is not None else c["hours"])
    limit = int(c["batch_limit"])

    ensure_table()
    with _conn_ctx() as conn:
        with conn.cursor() as cur:
            rows = fetch_pending(cur, hours, limit)
    if not rows:
        return {"pending": 0, "judged": 0, "failed": 0}

    import llm_advisor as llm
    lconf = llm.load_llm_conf()
    if not (lconf or {}).get("enabled") or not (lconf or {}).get("api_key"):
        return {"available": False, "why": "llm.enabled=false 或未配 api_key"}

    t0 = time.time()
    results: list[dict] = []
    workers = max(1, int(c["concurrency"]))
    if workers > 1 and len(rows) > 4:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(lambda r: judge_one(r, c, llm), rows))
    else:
        results = [judge_one(r, c, llm) for r in rows]

    model = llm.model_for("fast", lconf)
    with _conn_ctx() as conn, conn.cursor() as cur:
        for j in results:
            cur.execute(
                """
                INSERT INTO sa_bili_sentiment
                    (dynamic_id, sentiment, topic, reason, model, raw)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (dynamic_id) DO UPDATE SET
                    sentiment = EXCLUDED.sentiment,
                    topic = EXCLUDED.topic,
                    reason = EXCLUDED.reason,
                    model = EXCLUDED.model,
                    raw = EXCLUDED.raw,
                    judged_at = now()
                """,
                (j["dynamic_id"], j.get("sentiment", "unknown"),
                 json.dumps(j.get("topic") or [], ensure_ascii=False),
                 j.get("reason") or j.get("why") or "",
                 model, str(j.get("raw") or "")[:2000]),
            )
    return {"pending": len(rows), "judged": sum(
                1 for j in results if j.get("sentiment") in
                ("pos", "neg", "neutral", "policy_appeal")),
            "failed": sum(1 for j in results
                          if j.get("sentiment") in ("unknown", "")),
            "seconds": round(time.time() - t0, 1), "model": model}


# ---------------- 表 ----------------
# TODO(2026-10-19): 这段 DDL 没进 app.py 的 init_db —— 本模块要能被脚本单独
# 调用，自包含比遵循「DDL 都在 init_db」的约定更重要。若以后并入 init_db，
# 记得两处幂等、不要各写一份。
def ensure_table() -> None:
    with _conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sa_bili_sentiment (
                dynamic_id VARCHAR(128) PRIMARY KEY
                    REFERENCES sa_bili_dynamics(dynamic_id) ON DELETE CASCADE,
                sentiment  VARCHAR(16) NOT NULL DEFAULT '',
                topic      JSONB NOT NULL DEFAULT '[]'::jsonb,
                reason     VARCHAR(255) NOT NULL DEFAULT '',
                model      VARCHAR(128) NOT NULL DEFAULT '',
                raw        TEXT NOT NULL DEFAULT '',
                judged_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """)
        # 判定失败时 sentiment='unknown'。加部分索引只为统计失败率，量小不值当全索引。
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_sa_bili_senti_s "
            "ON sa_bili_sentiment (sentiment)")


# ---------------- 聚合 ----------------

def aggregate(cur, hours: int = 48, conf: dict | None = None) -> dict:
    """把判定结果聚合成一个情绪指标。输出结构与 ipo_market.sentiment() 同构，
    方便将来给打新/盘前报告复用同一套渲染。"""
    c = load_conf(conf)
    cur.execute(
        """
        SELECT s.sentiment,
               coalesce((d.stats::jsonb ->> 'likes')::bigint, 0)
             + coalesce((d.stats::jsonb ->> 'comments')::bigint, 0)
             + coalesce((d.stats::jsonb ->> 'forwards')::bigint, 0) AS eng,
               d.author_name, coalesce(s.topic, '[]'::jsonb) AS topic
        FROM sa_bili_sentiment s
        JOIN sa_bili_dynamics d ON d.dynamic_id = s.dynamic_id
        WHERE d.pub_ts >= now() - interval '%s hours'
          AND s.sentiment IN ('pos','neg','neutral','policy_appeal')
        """,
        (hours,))
    rows = cur.fetchall()
    if not rows:
        return {"available": False, "why": "近 %d 小时无判定结果" % hours}

    cnt = {"pos": 0, "neg": 0, "neutral": 0, "policy_appeal": 0}
    eng = {"pos": 0, "neg": 0, "policy_appeal": 0}
    by_author: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # [pos, neg]
    topic_cnt: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for s, e, author, topic in rows:
        cnt[s] = cnt.get(s, 0) + 1
        if s in eng:
            eng[s] += int(e or 0)
        if s in ("pos", "neg"):
            by_author[author or "?"][0 if s == "pos" else 1] += 1
            for t in (topic or []):
                if s == "pos":
                    topic_cnt[t][0] += 1
                else:
                    topic_cnt[t][1] += 1

    # 口径：只用 pos+neg 归一化。neutral 进分母会把分数稀释到永远落在"中性"
    # （实测 56% neutral），policy_appeal 不是方向，只能当焦虑旁证。
    directional = cnt["pos"] + cnt["neg"]
    if directional == 0:
        out = {"available": False, "why": "无 pos/neg 方向性内容"}
    else:
        net = (cnt["pos"] - cnt["neg"]) / directional
        out = {"available": True, "net": round(net, 3)}

    out.update({
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hours": hours,
        "counts": cnt,
        "directional": directional,
        "total": len(rows),
        "eng_weighted_net": (
            round((eng["pos"] - eng["neg"]) / (eng["pos"] + eng["neg"]), 3)
            if (eng["pos"] + eng["neg"]) else None),
        "appeal_ratio": round(cnt["policy_appeal"] / len(rows), 3),
        "per_author": {a: {"pos": p, "neg": g}
                       for a, (p, g) in sorted(
                           by_author.items(), key=lambda kv: -(kv[1][0] + kv[1][1]))},
        "author_n": len(by_author),
        "topics": [{"topic": t, "pos": p, "neg": g}
                   for t, (p, g) in sorted(topic_cnt.items(),
                                           key=lambda kv: -(kv[1][0] + kv[1][1]))[:10]],
    })

    if not out["available"]:
        return out

    # 五档 + multiplier。阈值来自配置（初值是拍的，见 DEFAULT_CONF 注释）。
    b = c["band"]
    n = out["net"]
    if n >= float(b["hot"]):
        out["band"], out["multiplier"] = "亢奋", float(c["multiplier"]["hot"])
    elif n >= float(b["warm"]):
        out["band"], out["multiplier"] = "偏暖", float(c["multiplier"]["warm"])
    elif n > float(b["cold"]):
        out["band"], out["multiplier"] = "中性", float(c["multiplier"]["neutral"])
    elif n > float(b["freeze"]):
        out["band"], out["multiplier"] = "偏冷", float(c["multiplier"]["cold"])
    else:
        out["band"], out["multiplier"] = "低迷", float(c["multiplier"]["freeze"])
    out["low_confidence"] = directional < 8 or out["author_n"] < 2
    return out


# ---------------- 报告渲染 ----------------

def digest(cur, hours: int = 48, conf: dict | None = None) -> str:
    """给 LLM/报告看的几行文字。无数据返回空串。"""
    st = aggregate(cur, hours, conf)
    if not st.get("available"):
        return ""
    lines = ["### B站情绪（近 %d 小时，判定 %d 条 / 有方向 %d 条，%d 个UP主）"
             % (st["hours"], st["total"], st["directional"], st["author_n"]),
             "- 净情绪 **%+.3f** → **%s**（情绪系数 %.2f）"
             % (st["net"], st["band"], st["multiplier"])]
    c = st["counts"]
    lines.append("- pos %d / neg %d / neutral %d / 政策建言 %d（占比 %.0f%%）"
                 % (c["pos"], c["neg"], c["neutral"], c["policy_appeal"],
                    st["appeal_ratio"] * 100))
    if st.get("eng_weighted_net") is not None:
        lines.append("- 互动量加权净情绪 %+.3f（与等权差 %+.3f）"
                     % (st["eng_weighted_net"], st["eng_weighted_net"] - st["net"]))
    if st.get("topics"):
        t = st["topics"][0]
        lines.append("- 题材首名：%s（pos %d / neg %d）"
                     % (t["topic"], t["pos"], t["neg"]))
    if st.get("low_confidence"):
        lines.append("- ⚠️ 方向性内容 <8 条或 UP 主 <2 个，统计意义有限（不构成全市场情绪）")
    return "\n".join(lines)


# ---------------- 阈值校准 ----------------

def recalibrate(hours: int = 720) -> dict:
    """按近期判定的实际分布给出五档阈值建议。

    为什么需要：DEFAULT_CONF 里的阈值是拍的初值（没有 B站实测分布支撑），
    ipo_market 的阈值来自市场宽度实测分布。等积累到几百条判定后用这个校准。
    """
    ensure_table()
    with _conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.sentiment,
                   coalesce((d.stats::jsonb ->> 'likes')::bigint, 0)
                 + coalesce((d.stats::jsonb ->> 'comments')::bigint, 0)
                 + coalesce((d.stats::jsonb ->> 'forwards')::bigint, 0) AS eng
            FROM sa_bili_sentiment s
            JOIN sa_bili_dynamics d ON d.dynamic_id = s.dynamic_id
            WHERE d.pub_ts >= now() - interval '%s hours'
              AND s.sentiment IN ('pos','neg')
            """,
            (hours,))
        rows = cur.fetchall()
    if len(rows) < 30:
        return {"available": False, "why": "近 %d 小时只有 %d 条方向性判定，"
                                           "样本不足 30，不建议校准" % (hours, len(rows))}
    nets = [(1 if s == "pos" else -1) * (int(e or 0) + 1) for s, e in rows]
    nets.sort()

    def q(p: float) -> float:
        return round(nets[min(len(nets) - 1, int(len(nets) * p))], 3)
    return {"available": True, "n": len(rows),
            "suggested": {"hot": q(0.85), "warm": q(0.65),
                          "cold": q(0.35), "freeze": q(0.15)},
            "note": "按互动量加权净分的实际分位数给建议，需人工确认后写进 config.yaml"}


# ---------------- 供 app.py 调用的便捷入口 ----------------

def _auto_loop():
    """按 bili_sentiment 的时间窗周期性补判新动态。没有自己的周期配置 ——
    复用 bili.interval_minutes 的节奏判定一次就够，不需要更勤。"""
    from app import get_conn
    while True:
        try:
            conf = load_conf()
            if conf.get("enabled"):
                st = run_once()
                if st.get("pending"):
                    print("[bili_sentiment] 判定 %d 条：%s"
                          % (st["pending"], st), flush=True)
            else:
                break
        except Exception as exc:
            print("[bili_sentiment] loop error: %s" % exc, flush=True)
        time.sleep(3600)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        print(json.dumps(run_once(), ensure_ascii=False))
    elif cmd == "recalibrate":
        print(json.dumps(recalibrate(), ensure_ascii=False))
    else:
        with _conn_ctx() as _c, _c.cursor() as _cur:
            print(digest(_cur))
