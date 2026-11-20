"""聊天框「银证转账截图 → 待确认表 → 入账」离线测试（2026-11-19）。

红线：不烧钱（视觉模型全 mock）、不连库（save_message 与 get_conn 全 mock）、
不碰生产台账（DEFAULT_STORE 指向临时文件）。app.py 用 AST 抽取真实函数执行，
避免 import app 起整套 daemon（AGENTS.md 老坑）。
"""
from __future__ import annotations

import ast
import json
import pathlib
import re
import tempfile
import unittest.mock as mock
from types import SimpleNamespace

from fastapi import HTTPException
from pydantic import BaseModel, Field

import broker_cash
import web_chat

ROOT = pathlib.Path(__file__).resolve().parent
PASS = [0]


def ck(cond, label):
    if not cond:
        raise AssertionError("FAIL: " + label)
    PASS[0] += 1
    print("  ok  " + label)


# ---------------- 从 app.py 抽出真实端点代码 ----------------
def load_endpoints():
    src = (ROOT / "app.py").read_text(encoding="utf-8-sig")
    tree = ast.parse(src)
    wanted = {"_chat_transfer_prepare", "chat_transfer_commit"}
    body = [n for n in tree.body
            if isinstance(n, ast.ClassDef) and n.name == "TransferCommitIn"]
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in wanted:
            body.append(ast.FunctionDef(
                name=n.name, args=n.args, body=n.body, decorator_list=[],
                returns=n.returns, type_comment=n.type_comment))
    ns = {"BaseModel": BaseModel, "Field": Field, "HTTPException": HTTPException,
          "web_chat": web_chat, "broker_cash": broker_cash,
          "get_conn": lambda: None}
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])),
                 "<app.py extracted>", "exec"), ns)
    return ns["_chat_transfer_prepare"], ns["chat_transfer_commit"], ns["TransferCommitIn"]


PREPARE, COMMIT, CommitIn = load_endpoints()

# 模拟视觉模型对「转账记录」截图的输出：含负号、中文币种、月日、撤单、截断、美元
VISION_ROWS = [
    {"date": "2026-11-13 14:14:15", "side": "out", "amount": -15000.00,
     "currency": "人民币", "note": "证券转银行 14:14:15", "status": "成功"},
    {"date": "11-13", "side": "in", "amount": 137297.51,
     "currency": "CNY", "note": "银行转证券 13:36:03", "status": "成功"},
    {"date": "2026-11-13", "side": "out", "amount": 212625.27,
     "currency": "CNY", "note": "证券转银行 14:09:30", "status": "成功"},
    {"date": "2026-11-13", "side": "out", "amount": 123.0,
     "currency": "CNY", "note": "撤单行", "status": "撤单"},
    {"date": "", "side": "in", "amount": 0,
     "currency": "CNY", "note": "底部截断", "status": "看不清"},
    {"date": "2026-11-13", "side": "in", "amount": 100.0,
     "currency": "USD", "note": "美元页", "status": "成功"},
]
VISION_TEXT = "识别如下：\n```json\n" + json.dumps(VISION_ROWS, ensure_ascii=False) + "\n```"


def run():
    tmp = pathlib.Path(tempfile.gettempdir()) / "chat_transfer_test_store.json"
    tmp.unlink(missing_ok=True)
    up = pathlib.Path(tempfile.gettempdir()) / "chat_transfer_test_uploads"
    up.mkdir(exist_ok=True)
    (up / "shot.png").write_bytes(b"fake-image-bytes")
    # 预置一条已录入流水，用于查重（2026-11-20 时间戳升级：真实台账的 date
    # 已带时刻 —— 查重两侧都带时刻时精确到秒，同日不同时刻不再误判重复）
    broker_cash.add_flow("2026-11-13 14:14:15", "out", 15000.0, "CNY",
                         "已存在的出金", path=tmp)

    print("\n== 1. 纯函数：意图识别 ==")
    for t in ("录入银证转账记录", "把这张转账记录导入", "补录一下出金",
              "帮我把银证转账记到台账", "把转账流水录进去"):
        ck(web_chat.is_transfer_intent(t), "命中 " + t)
    for t in ("帮我看看这张转账截图", "这张图是什么", "录入交易记录",
              "转账记录", "出入金怎么算", ""):
        ck(not web_chat.is_transfer_intent(t), "不误触发 " + repr(t))

    print("\n== 2. 纯函数：JSON 解析容错 ==")
    for text, n in (
        ('[{"date":"2026-11-13","side":"out","amount":1}]', 1),
        (VISION_TEXT, 6),
        ("[{'date':'11-13','side':'in','amount':2.5,}]", 1),
        ("没有表格", 0), ("", 0), ("[]", 0),
    ):
        ck(len(web_chat.parse_transfer_json(text)) == n,
           "解析 %r → %d 行" % (text[:26], n))

    print("\n== 3. 纯函数：归一化与查重 ==")
    existing = broker_cash.list_flows(path=tmp)
    rows = web_chat.normalize_transfer_rows(
        VISION_ROWS, default_year=2026, existing=existing)
    ck(len(rows) == 6, "6 行全保留，不静默丢行")
    ck(rows[0]["date"] == "2026-11-13 14:14:15" and rows[0]["amount"] == 15000.0
       and rows[0]["currency"] == "CNY", "date 带时刻/负号取正/人民币映射")
    ck(rows[0]["duplicate"] and "疑似已录入" in rows[0]["issues"], "重复行被标记（同日同时刻同额）")
    ck(rows[1]["date"] == "2026-11-13 13:36:03" and rows[1]["side"] == "in",
       "月日补年 + 备注时刻提取 + 方向")
    ck(rows[3]["skip"], "撤单行标跳过")
    ck(rows[4]["skip"] and "日期缺失或看不清" in rows[4]["issues"], "截断行标跳过")
    ck(rows[5]["currency"] == "USD"
       and any("币种不支持" in x for x in rows[5]["issues"]), "USD 标红且不被静默改成 CNY")

    print("\n== 4. 纯函数：commit 前复核 ==")
    acc, rej = web_chat.validate_transfer_rows(VISION_ROWS, existing=existing)
    ck(len(acc) == 2, "只放行 2 行干净数据（实际 %d）" % len(acc))
    ck(len(rej) == 4, "拦下重复/撤单/截断/美元 4 行")
    ck(all("why" in x and x["why"] for x in rej), "每行都有拒绝原因")
    acc_f, rej_f = web_chat.validate_transfer_rows(
        [dict(VISION_ROWS[0], force=True)], existing=existing)
    ck(len(acc_f) == 1 and not rej_f, "force=True 放行同额同日的真实第二笔")

    print("\n== 5. 视觉调用参数（不真发请求） ==")
    seen = {}

    class Resp:
        status_code = 200
        text = "ok"

        def json(self):
            return {"choices": [{"message": {"content": "[{\"date\":\"2026-11-13\","
                                                        "\"side\":\"in\",\"amount\":1}]"}}],
                    "usage": {"total_tokens": 100}}

    def fake_post(url, json=None, headers=None, timeout=None, proxies=None):
        seen.update(url=url, body=json)
        return Resp()

    conf = web_chat.load_conf()
    with mock.patch("requests.post", side_effect=fake_post):
        out, model = web_chat.describe_image(
            b"x", conf, system_prompt="PROBE-PROMPT", max_chars=9999,
            max_tokens=4000)
    ck(seen["body"]["max_tokens"] == 4000, "结构化调用放大 max_tokens")
    ck(seen["body"]["messages"][0]["content"][0]["text"] == "PROBE-PROMPT",
       "自定义 prompt 生效")
    ck(out.startswith("[{") and model, "返回未截断的 JSON 文本")
    with mock.patch("requests.post", side_effect=fake_post):
        out2, _ = web_chat.describe_image(b"x", conf)
    ck(len(out2) <= len(out), "不传参时仍按 img_note_chars 截断（旧行为不变）")

    print("\n== 6. /api/chat/send 意图分支：只解析不写库 ==")
    body = SimpleNamespace(sid="web:abcdef12", text="录入银证转账记录",
                           img_name="shot.png")
    saved = []
    with mock.patch.object(broker_cash, "DEFAULT_STORE", tmp), \
         mock.patch.object(web_chat, "UPLOAD_DIR", up), \
         mock.patch.object(web_chat, "describe_image",
                           return_value=(VISION_TEXT, "mock-vision")), \
         mock.patch.object(web_chat, "save_message",
                           side_effect=lambda *a, **k: saved.append(a)):
        before = len(broker_cash.list_flows())
        r = PREPARE(body, body.text, conf, {"get_conn": lambda: None})
        after = len(broker_cash.list_flows())
    ck(r["mode"] == "transfer_confirm", "返回待确认模式")
    ck(len(r["rows"]) == 6 and r["model"] == "mock-vision", "6 行候选 + 模型名")
    ck(before == after == 1, "**台账条数没变**（prepare 绝不写库）")
    ck(len(saved) == 2, "落库 user + assistant 两行提示")
    ck("没有写入任何数据" in r["hint"], "提示语明确未入库")
    ck(r["user_message"]["role"] == "user"
       and r["assistant_message"]["role"] == "assistant", "双行返回兼容原前端")

    print("\n== 7. /api/chat/transfer-commit：逐条校验 + 逐条反馈 ==")
    rr = r["rows"]
    payload = [rr[0], rr[1], rr[2], rr[3], rr[4], rr[5], rr[1]]
    with mock.patch.object(broker_cash, "DEFAULT_STORE", tmp), \
         mock.patch.object(web_chat, "save_message", lambda *a, **k: None):
        out = COMMIT(CommitIn(sid="web:abcdef12", rows=payload))
    ck(len(out["added"]) == 2, "成功写入 2 笔（实际 %d）" % len(out["added"]))
    ck(len(out["rejected"]) == 4, "复核拦下 4 笔")
    ck(len(out["failed"]) == 1 and "本批次内重复" in out["failed"][0]["why"],
       "批次内同额重复被拦，防连点")
    ck(out["ok"] and "已录入 2 笔" in out["message"], "结果文案正确")
    final = broker_cash.list_flows(path=tmp)
    ck(len(final) == 3, "台账最终 3 条（1 原有 + 2 新增）")
    ck(all(f["currency"] in ("CNY", "HKD") for f in final), "没有写进不支持的 USD")
    ck(all(f["amount"] > 0 for f in final), "金额全为正数")

    print("\n== 8. force 放行 + 空提交 + 非法日期 ==")
    with mock.patch.object(broker_cash, "DEFAULT_STORE", tmp), \
         mock.patch.object(web_chat, "save_message", lambda *a, **k: None):
        o2 = COMMIT(CommitIn(rows=[dict(rr[0], force=True)]))
        ck(len(o2["added"]) == 1, "force 真写入第二笔同额流水")
        try:
            COMMIT(CommitIn(rows=[]))
            raise AssertionError("空提交应 400")
        except HTTPException as e:
            ck(e.status_code == 400, "空提交 400")
        bad = COMMIT(CommitIn(rows=[dict(rr[1], date="2026-02-30",
                                         force=True)]))
        ck(len(bad["rejected"]) == 1 and not bad["added"], "非法日期被复核拦下")
    ck(len(broker_cash.list_flows(path=tmp)) == 4, "最终 4 条，非法行未落盘")

    print("\n== 9. 静态接线 ==")
    app_src = (ROOT / "app.py").read_text(encoding="utf-8-sig")
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    ck('@app.post("/api/chat/transfer-commit")' in app_src, "commit 路由存在")
    ck("if body.img_name and web_chat.is_transfer_intent(text):" in app_src,
       "chat_send 有意图前置分支")
    ck("return _chat_transfer_prepare(" in app_src, "前置分支指向 prepare")
    for fn in ("renderChatTransferConfirm", "chatTransferCollect",
               "chatTransferCount", "chatTransferRemove", "chatTransferCancel",
               "chatTransferCommit", "chatTransferStatus"):
        ck(re.search(r"(async\s+)?function\s+" + fn + r"\s*\(", html) is not None,
           "前端函数 " + fn)
    ck("r.mode === 'transfer_confirm'" in html, "chatSend 识别确认模式")
    ck("/api/chat/transfer-commit" in html, "前端真的调用 commit 端点")
    ck("银证转账截图" in html, "聊天欢迎语有用法提示")

    tmp.unlink(missing_ok=True)
    print("\n== 全部 %d 条断言通过；生产台账未被触碰 ==" % PASS[0])


if __name__ == "__main__":
    run()
