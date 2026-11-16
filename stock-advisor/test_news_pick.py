# -*- coding: utf-8 -*-
"""pick_news 相关性分层（2026-11-15 用户反馈「重复的消息占用了预算」）。

实测背景：智谱(02513) 最终 10 条进决策的新闻里 8 条是「广发策略：…」
「北京15日起正式供热」「周大生加盟收入骤降」这类**标题互不重复**、但与
该股无关的全市场要闻 —— 标题去重对它们无能为力，必须按「标题是否含
股票名/搜索词 token」分层限流。

跑法：python test_news_pick.py（纯逻辑，不碰库不联网）
"""
import news_context as NC


def _row(title, senti="neg", t="2026-11-15 09:00"):
    return {"title": title, "sentiment": senti, "publish_time": t,
            "media": "测试源"}


def test_macro_capped_when_tokens_given():
    """8 条宏观 + 2 条公司级 → 公司级排前，宏观至多 MACRO_QUOTA 条。"""
    general = [
        _row("北京15日起正式供热", "", "2026-11-15T09:34"),
        _row("广发策略：打铁还需自身硬", "neg", "2026-11-15T10:00"),
        _row("国金策略：最黑暗处最光明", "neg", "2026-11-15T09:40"),
        _row("东吴策略：美联储加息启示", "neg", "2026-11-15T09:20"),
        _row("冲刺四季度，券商密集开会！", "neg", "2026-11-15T09:12"),
        _row("周大生加盟收入骤降67%", "", "2026-11-15T08:26"),
        _row("创新药医保目录解读", "pos", "2026-11-15T06:37"),
        _row("广东农产品品牌方案发布", "", "2026-11-15T06:49"),
        _row("智谱发布新一代推理模型", "pos", "2026-11-14T15:00"),
        _row("摩根大通上调智谱评级", "pos", "2026-11-13T10:00"),
    ]
    out = NC.pick_news(general, [], tokens=["智谱"])
    assert len(out) <= NC.NEWS_QUOTA
    macros = [x for x in out if x.get("rel") == "macro"]
    hits = [x for x in out if x.get("rel") == "hit"]
    # 宏观至多 MACRO_QUOTA
    assert len(macros) <= NC.MACRO_QUOTA, macros
    # 公司级在前
    assert all("智谱" in x["title"] for x in out[:len(hits)])
    # 两条公司级都在（评级词那条也命中 token）
    assert len(hits) == 2, hits
    # rel 标记完备
    assert all(x.get("rel") in ("hit", "macro") for x in out)


def test_tokens_empty_keeps_old_behavior():
    """tokens 为空（取不到配置）→ 不分层，全量按名额截断（旧行为不劣化）。"""
    general = [_row("宏观文%d" % i) for i in range(20)]
    out = NC.pick_news(general, [])
    assert len(out) == NC.NEWS_QUOTA
    # 没有分层时全部视为 hit
    assert all(x.get("rel") == "hit" for x in out)


def test_rating_still_first():
    """评级定向新闻仍排最前（回归：分层不能动评级优先）。"""
    rating = [_row("汇丰：智谱研发成本攀升利润率承压", "neg", "2026-11-12T09:43")]
    general = [_row("智谱股价今日大涨", "pos", "2026-11-15T10:00")] + \
        [_row("宏观文%d" % i) for i in range(10)]
    out = NC.pick_news(general, rating, tokens=["智谱"])
    assert "汇丰" in out[0]["title"]
    assert out[0].get("rel") == "hit"


def test_dedup_still_works_with_layering():
    """评级层与通用层捞到同一条（标题前12字相同）仍合并，dup_count 累计。"""
    same = "汇丰：智谱研发成本攀升兼利润率承压，料收支平衡推迟"
    rating = [_row(same, "neg", "2026-11-12T09:43")]
    general = [_row(same, "neg", "2026-11-12T10:30")]  # 转载，更晚
    out = NC.pick_news(general, rating, tokens=["智谱"])
    assert len(out) == 1
    assert out[0]["dup_count"] == 2
    assert out[0]["publish_time"] == "2026-11-12T09:43"  # 保留最早首发


def test_macro_not_exceed_total_quota():
    """宏观补位受总名额约束：quota=2 时最多 2 条。"""
    general = [_row("公司级新闻", "pos", "2026-11-15T10:00")] + \
        [_row("宏观文%d" % i) for i in range(10)]
    out = NC.pick_news(general, [], quota=2, tokens=["公司级新闻"])
    assert len(out) <= 2
    assert out[0]["title"] == "公司级新闻"


def test_face_rows_counts_only_after_curation():
    """端到端口径：分层后进 face_rows 的利空条数不含被限流的宏观文。"""
    general = [_row("智谱遭下调目标价", "neg", "2026-11-14T10:00")] + \
        [_row("宏观利空文%d" % i, "neg", "2026-11-15T09:00") for i in range(8)]
    out = NC.pick_news(general, [], tokens=["智谱"])
    negs = [x for x in out if x.get("sentiment") == "neg" and x.get("rel") == "hit"]
    assert len(negs) == 1
    rows = NC.face_rows(out, "buy")
    assert rows and "1 条利空" in rows[0]["text"]


def test_semantic_dedup_merges_rewrite():
    """语义层：换标题转载（title_key 不同）但向量余弦 ≥ 0.90 → 合并。

    模拟「汇丰看空智谱」vs「智谱遭外资行下调目标价」——前缀规则抓不到，
    靠向量合并。用 4 维小向量构造余弦 0.99。
    """
    a = _row("汇丰：智谱研发成本攀升利润率承压", "neg", "2026-11-12T09:43")
    a["title_vec"] = [1.0, 0.0, 0.0, 0.0]
    b = _row("智谱遭外资行下调目标价评级", "neg", "2026-11-12T11:00")
    b["title_vec"] = [0.99, 0.1, 0.0, 0.0]  # cos ≈ 0.995
    c = _row("智谱发布新一代推理模型", "pos", "2026-11-15T10:00")
    c["title_vec"] = [0.0, 0.0, 1.0, 0.0]
    out = NC.dedup_news([b, a, c])  # 注意顺序：b 在前，a 更早 → a 当首发
    assert len(out) == 2, out
    first = next(x for x in out if "汇丰" in x["title"])
    assert first["dup_count"] == 2
    assert "智谱遭外资行" in first.get("dup_titles", [])
    assert first["publish_time"] == "2026-11-12T09:43"
    # 向量是内部字段，必须剥离
    assert all("title_vec" not in x for x in out)


def test_semantic_threshold_respected():
    """余弦 0.85 < 0.90 → 不合并（阈值不许误伤相近但不同的事件）。"""
    a = _row("智谱发布新模型A", "pos", "2026-11-15T09:00")
    a["title_vec"] = [1.0, 0.0, 0.0, 0.0]
    b = _row("智谱发布新模型B", "pos", "2026-11-15T10:00")
    b["title_vec"] = [0.85, 0.53, 0.0, 0.0]  # cos ≈ 0.85
    out = NC.dedup_news([a, b])
    assert len(out) == 2


def test_semantic_missing_vec_falls_back():
    """部分行有向量、部分没有：没有向量的行只走前缀规则，不报错。"""
    a = _row("汇丰：智谱研发成本攀升利润率承压", "neg", "2026-11-12T09:43")
    a["title_vec"] = [1.0, 0.0]
    b = _row("智谱遭外资行下调目标价", "neg", "2026-11-12T11:00")  # 无向量
    c = _row("完全无关的新闻", "pos", "2026-11-15T10:00")
    c["title_vec"] = [0.0, 1.0]
    out = NC.dedup_news([a, b, c])
    # a/b 向量比对不可得 → 不合并（保守方向：宁可漏合不可错合）
    assert len(out) == 3


def test_cosine_basic():
    """_cos 的正确性与退化输入（长度不等/空/零向量）。"""
    import math
    assert abs(NC._cos([1, 0], [0, 1])) < 1e-9
    assert abs(NC._cos([1, 0], [1, 0]) - 1.0) < 1e-9
    assert NC._cos([], []) == 0.0
    assert NC._cos([1], [1, 2]) == 0.0
    assert NC._cos([0.0, 0.0], [1.0, 1.0]) == 0.0
    v = [math.cos(t) for t in (0.1, 0.5, 0.9)]
    w = [math.sin(t) for t in (0.1, 0.5, 0.9)]
    assert -1e-9 < NC._cos(v, w) < 0.1


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS %s" % fn.__name__)
        except AssertionError as exc:
            failed += 1
            print("FAIL %s: %r" % (fn.__name__, exc))
    print("%d/%d passed" % (len(fns) - failed, len(fns)))
    raise SystemExit(1 if failed else 0)
