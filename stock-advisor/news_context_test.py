# -*- coding: utf-8 -*-
"""news_context —— 模拟盘与实盘评审共用消息面大脑的单测（纯函数）。

背景（2026-11-14 用户裁决）：「模拟盘与实盘买卖评审应该除了产出操作
不一样，其他应该一样，这个大脑应该是一样的，融合两个的大脑消息面
判据，不要再搞特殊了」。本测试锁住三件事：

1. 一份大脑两种呈现自洽：face_rows（实盘评审用）与 face_summary
   （模拟盘喂 LLM 用）数的是同一批利空/利好、用的是同一组权重常量；
2. pick_news：评级定向捞的必进名单，通用新闻里的评级词命中的也优先，
   去重后总名额不超 quota；
3. 与 trade_review 的接线：TR._news_rows/TR._news_reasons 委托本模块后
   行为不回退（用重复转载场景回归）。

运行：python news_context_test.py
"""
import sys

import news_context as NC


def _neg(title="汇丰下调智谱目标价至500港元", when="2026-11-12T17:10"):
    return {"title": title, "media": "新浪财经", "sentiment": "neg",
            "publish_time": when}


def _pos(title="智谱获大额政企订单", when="2026-11-13T09:00"):
    return {"title": title, "media": "财联社", "sentiment": "pos",
            "publish_time": when}


# ---------------- 去重 ----------------

def test_title_key_strips_prefix():
    assert NC.title_key("快讯：汇丰下调智谱目标价至500港元") == \
        NC.title_key("汇丰下调智谱目标价至500港元")
    assert NC.title_key("港股午间丨汇丰下调智谱目标价 维持持有") == \
        NC.title_key("汇丰下调智谱目标价 维持持有")


def test_dedup_keeps_earliest_and_counts():
    rows = [
        _neg(when="2026-11-12T18:00"),
        _neg(when="2026-11-12T17:10"),
        {"title": "快讯：汇丰下调智谱目标价至500港元", "media": "智通财经",
         "sentiment": "neg", "publish_time": "2026-11-12T17:25"},
    ]
    out = NC.dedup_news(rows)
    assert len(out) == 1
    assert out[0]["dup_count"] == 3
    assert out[0]["publish_time"] == "2026-11-12T17:10"  # 最早首发


def test_dedup_preserves_extra_fields():
    rows = [{"title": "标题甲", "sentiment": "neg", "url": "http://x",
             "code": "02513", "related": ["02513"]}]
    out = NC.dedup_news(rows)
    assert out[0]["url"] == "http://x" and out[0]["code"] == "02513"


def test_dedup_mixed_time_types():
    # DB 出来是 datetime、/api/news 出来是字符串 —— 混合不许 TypeError
    import datetime as _dt
    rows = [{"title": "标题甲", "sentiment": "neg",
             "publish_time": _dt.datetime(2026, 11, 12, 18, 0)},
            {"title": "标题甲", "sentiment": "neg",
             "publish_time": "2026-11-12T17:10"}]
    out = NC.dedup_news(rows)
    assert len(out) == 1 and out[0]["publish_time"] == "2026-11-12T17:10"


# ---------------- 名额分配 ----------------

def test_pick_news_rating_rows_first():
    general = [{"title": "行业新闻%d" % i, "sentiment": "",
                "publish_time": "2026-11-13T1%d:00" % i} for i in range(12)]
    rating = [_neg()]
    out = NC.pick_news(general, rating, quota=10)
    assert out and out[0]["title"].startswith("汇丰下调")
    assert len(out) == 10


def test_pick_news_general_rating_kw_promoted():
    # 通用列表里命中评级词的，即使不在 rating 参数里也要优先
    general = [{"title": "行业新闻%d" % i, "sentiment": "",
                "publish_time": "2026-11-13T1%d:00" % i} for i in range(12)]
    general.append({"title": "摩根大通下调智谱目标价", "sentiment": "neg",
                    "publish_time": "2026-11-11T10:00"})
    out = NC.pick_news(general, None, quota=10)
    assert out[0]["title"] == "摩根大通下调智谱目标价"


def test_pick_news_dedups_overlap():
    # rating 捞到的与通用列表里的是同一条（不同前缀转载）→ 去重
    rating = [_neg(when="2026-11-12T17:10")]
    general = [{"title": "快讯：汇丰下调智谱目标价至500港元", "sentiment": "neg",
                "publish_time": "2026-11-12T17:25"}]
    out = NC.pick_news(general, rating)
    assert len(out) == 1 and out[0]["dup_count"] == 2


# ---------------- 判据：一份大脑两种呈现 ----------------

def test_face_rows_buy_and_sell():
    rows = NC.dedup_news([_neg(), _pos()])
    buy = NC.face_rows(rows, "buy")
    sell = NC.face_rows(rows, "sell")
    buy_con = [x for x in buy if x["kind"] == "con"]
    sell_pro = [x for x in sell if x["kind"] == "pro"]
    assert buy_con and sell_pro, "利空必须对买入是 con、对卖出是 pro"
    assert buy_con[0]["weight"] == sell_pro[0]["weight"] == NC.NEWS_NEG_W


def test_face_rows_capped():
    rows = NC.dedup_news([_neg("利空甲"), _neg("利空乙"), _neg("利空丙"),
                          _neg("利空丁")])
    out = NC.face_rows(rows, "buy")
    con = [x for x in out if x["kind"] == "con"]
    assert con[0]["weight"] <= NC.NEWS_NEG_CAP


def test_face_summary_same_counts_as_face_rows():
    """一份大脑：face_summary 告诉 LLM 的条数/权重必须与 face_rows 判据
    完全一致 —— 两边数的是同一批行、同一组常量，不许各数各的。"""
    rows = NC.dedup_news([_neg(), _neg("另一条利空"), _pos()])
    summary = NC.face_summary(rows)
    assert "利空 2 条" in summary and "利好 1 条" in summary
    assert ("每条对买入判 -%.0f 分" % NC.NEWS_NEG_W) in summary
    assert ("合计封顶 ∓%.0f" % NC.NEWS_NEG_CAP) in summary
    assert "汇丰下调智谱目标价" in summary  # 标题要带出来
    buy = NC.face_rows(rows, "buy")
    con = [x for x in buy if x["kind"] == "con"][0]
    assert "2 条利空" in con["text"], "face_rows 与 face_summary 条数必须一致"


def test_face_summary_empty():
    assert NC.face_summary([]) == ""
    assert NC.face_summary([{"title": "无标签", "sentiment": ""}]) == ""


def test_dup_headline_marked():
    rows = NC.dedup_news([_neg(when="2026-11-12T17:10"),
                          _neg(when="2026-11-12T17:25")])
    summary = NC.face_summary(rows)
    assert "×2家" in summary


# ---------------- 与 trade_review 的接线 ----------------

def test_trade_review_delegates_to_news_context():
    import trade_review as TR
    rows = TR._news_rows([_neg(), _neg(when="2026-11-12T17:25")])
    assert len(rows) == 1 and rows[0]["dup_count"] == 2
    reasons = []
    TR._news_reasons(reasons, "buy", rows)
    con = [r for r in reasons if r["kind"] == "con"]
    assert con and con[0]["weight"] == NC.NEWS_NEG_W
    assert "1 条利空" in con[0]["text"], "合并后按 1 条计分"


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS %s" % fn.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL %s: %s" % (fn.__name__, e))
    print("%d/%d passed" % (len(fns) - failed, len(fns)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
