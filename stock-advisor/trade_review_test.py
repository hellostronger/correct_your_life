# -*- coding: utf-8 -*-
"""trade_review 消息面判据的纯函数单测（不碰库、不联网）。

背景（2026-11-14）：用户发现「机构下调智谱评级」的新闻在库里、标了利空、
关联了 02513，但买卖评审（/api/trade-review）的 ctx 里根本没有新闻 ——
评审只看 K 线/趋势/资金，评级目标价这类先导信息进不了结论。这次把
消息面接进评审，本测试锁三件事：

1. 利空对买入是反对项（con），且标题要带出来（让人能核对是哪条利空）；
2. 利空对卖出是支持项（pro）——同一份消息，方向不同结论不同；
3. 分值有封顶：同一条降级被 5 家媒体转载，不许把一次利空放大成 5 倍。

运行：python trade_review_test.py
"""
import sys

import trade_review as TR
import news_context as NC


def _ctx(news=None, side="buy", **kw):
    """最小可评审 ctx：不给 K 线（趋势判据跳过），只测消息面这一环。"""
    c = {"code": "02513", "name": "智谱", "side": side, "today": "2026-11-14"}
    if news is not None:
        c["news"] = news
    c.update(kw)
    return c


def _neg(title="摩根大通下调智谱目标价至1200港元", n=1):
    return [{"title": title, "media": "智通财经", "sentiment": "neg",
             "publish_time": "2026-11-12T18:00:00"}] * n


def _pos(n=1):
    return [{"title": "智谱获大额政企订单", "media": "财联社",
             "sentiment": "pos", "publish_time": "2026-11-13T09:00:00"}] * n


def test_buy_with_neg_news_is_con():
    rv = TR.review(_ctx(news=_neg()))
    cons = [r for r in rv["reasons"] if r["kind"] == "con" and "利空" in r["text"]]
    assert cons, "买入遇利空必须出 con 理由，实际 reasons=%s" % rv["reasons"]
    t = cons[0]["text"]
    assert "摩根大通" in t, "标题要带出来供人核对：%s" % t
    assert abs(cons[0]["weight"] - NC.NEWS_NEG_W) < 1e-9


def test_sell_with_neg_news_is_pro():
    rv = TR.review(_ctx(news=_neg(), side="sell"))
    pros = [r for r in rv["reasons"] if r["kind"] == "pro" and "利空" in r["text"]]
    assert pros, "卖出遇利空必须出 pro 理由，实际 reasons=%s" % rv["reasons"]


def test_neg_weight_capped():
    # 5 家转载同一次降级：5×8=40 分必须封顶到 20
    rv = TR.review(_ctx(news=_neg(n=5)))
    cons = [r for r in rv["reasons"] if r["kind"] == "con" and "利空" in r["text"]]
    assert cons and cons[0]["weight"] <= NC.NEWS_NEG_CAP + 1e-9, \
        "利空分值没封顶：%s" % [(r["text"], r["weight"]) for r in cons]


def test_buy_with_pos_news_is_pro():
    rv = TR.review(_ctx(news=_pos()))
    pros = [r for r in rv["reasons"] if r["kind"] == "pro" and "利好" in r["text"]]
    assert pros, "买入遇利好必须出 pro 理由"


def test_empty_news_list_is_not_a_gap():
    # 空列表 = 确实没新闻；只有键不存在才算「消息面未纳入」
    rv = TR.review(_ctx(news=[]))
    assert rv["news"] == []
    assert not any("消息面" in g for g in rv["gaps"]), rv["gaps"]
    assert "近期消息面" not in rv["text"]  # 没新闻就不渲染这一节


def test_missing_news_key_is_a_gap():
    rv = TR.review(_ctx())  # 不传 news 键
    assert any("消息面" in g for g in rv["gaps"]), \
        "调用方忘了取新闻必须记 gaps，不许装作消息面平静：%s" % rv["gaps"]


def test_render_lists_headlines():
    rv = TR.review(_ctx(news=_neg() + _pos()))
    assert "近期消息面" in rv["text"]
    assert "利空" in rv["text"] and "摩根大通" in rv["text"]
    # 日期只保留到天：2026-11-12T18:00 → 2026-11-12
    assert "2026-11-12" in rv["text"]


def test_dirty_news_rows_dropped():
    dirty = _neg() + [{"title": "", "sentiment": "neg"},  # 空标题丢掉
                      "not-a-dict",                        # 非 dict 丢掉
                      {"title": "无标签新闻", "sentiment": ""}]  # 保留、不参与计分
    rv = TR.review(_ctx(news=dirty))
    assert len(rv["news"]) == 2  # 1 条利空 + 1 条无标签
    cons = [r for r in rv["reasons"] if r["kind"] == "con" and "利空" in r["text"]]
    assert cons and "1 条利空" in cons[0]["text"]


def test_duplicate_reprints_merged():
    # 同一次降级的 3 家转载：标题前缀不同但正文同一件事 → 合并 1 条，
    # 计分只按 1 条算（否则一次利空被放大成 3 倍）
    dups = [
        {"title": "汇丰下调智谱目标价至500港元", "media": "新浪财经",
         "sentiment": "neg", "publish_time": "2026-11-12T17:10:00"},
        {"title": "快讯：汇丰下调智谱目标价至500港元", "media": "智通财经",
         "sentiment": "neg", "publish_time": "2026-11-12T17:25:00"},
        {"title": "港股午间丨汇丰下调智谱目标价至500港元 维持持有评级", "media": "腾讯",
         "sentiment": "neg", "publish_time": "2026-11-12T18:00:00"},
    ]
    rv = TR.review(_ctx(news=dups))
    assert len(rv["news"]) == 1, "3 家转载应合并成 1 条，实际 %d" % len(rv["news"])
    assert rv["news"][0]["dup_count"] == 3
    assert rv["news"][0]["media"] == "新浪财经"  # 保留最早首发
    cons = [r for r in rv["reasons"] if r["kind"] == "con" and "利空" in r["text"]]
    assert cons and "1 条利空" in cons[0]["text"], \
        "合并后必须按 1 条计分：%s" % [r["text"] for r in cons]
    assert "×3 家" in rv["text"]


def test_similar_not_identical_still_merged():
    # 前 12 个实义字符相同即视为同一件事（转载标题大多加前缀）
    a = {"title": "摩根大通下调智谱目标价至1200港元", "media": "A",
         "sentiment": "neg", "publish_time": "2026-11-11T10:00:00"}
    b = {"title": "摩根大通下调智谱目标价至1200港元，维持增持评级", "media": "B",
         "sentiment": "neg", "publish_time": "2026-11-11T11:00:00"}
    rv = TR.review(_ctx(news=[a, b]))
    assert len(rv["news"]) == 1


def test_different_news_not_merged():
    a = {"title": "摩根大通下调智谱目标价至1200港元", "media": "A",
         "sentiment": "neg", "publish_time": "2026-11-11T10:00:00"}
    b = {"title": "汇丰维持智谱持有评级", "media": "B",
         "sentiment": "neg", "publish_time": "2026-11-12T10:00:00"}
    rv = TR.review(_ctx(news=[a, b]))
    assert len(rv["news"]) == 2, "不同事件不许误合并"


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
