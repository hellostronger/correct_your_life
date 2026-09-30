# -*- coding: utf-8 -*-
"""json 容错解析器的回归测试。

单独放一个文件而不是塞进临时脚本，因为这套修复规则是被**真实的坏输出**
逼出来的，每加一条规则都必须确认没把之前修好的形态弄坏 ——
用 write 工具写（PowerShell heredoc 会被引号转义咬）。

跑法：python scripts/test_json_repair.py
"""
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import strategy_extract as SX  # noqa: E402

# (名称, 输入, 期望结果)  期望为 None 表示只要求能解析
CASES = [
    ("真合法 JSON", '{"a": 1}', {"a": 1}),
    ("单引号", "{'a': 'x', 'n': 1}", {"a": "x", "n": 1}),
    ("键也用单引号", "{'steps': [{'what': 'X'}]}",
     {"steps": [{"what": "X"}]}),
    ("尾逗号", '{"a": [1,2,3,], "b": {"c": 1,},}',
     {"a": [1, 2, 3], "b": {"c": 1}}),
    ("Python 字面量", '{"a": True, "b": False, "c": None}',
     {"a": True, "b": False, "c": None}),
    ("字符串内裸换行", '{"a": "第一行\n第二行"}', {"a": "第一行\n第二行"}),
    ("代码围栏", "好的：\n```json\n{\"a\": 1}\n```\n完毕", {"a": 1}),
    ("包了一层数组", '[{"a": 1, "b": "x"}]', {"a": 1, "b": "x"}),
    ("裸文本夹在引号片段之间", '{"e": \'前半\'；\'后半\'}',
     {"e": "前半 ；后半"}),
    ("三段夹杂", '{"e": \'A\'；\'B\'；\'C\'}', {"e": "A ；B ；C"}),
    ("句尾多一个引号", "{\"e\": '内容'\'}", {"e": "内容"}),
    # 实测：开引号 ' 收尾 " —— 类型不一致
    ("开闭引号类型不一致", '{"e": \'A\'；\'B\'。"}', {"e": "A ；B 。"}),
    ("字符串里有花括号", '{"b": "含}括号与\\"引号\\""}',
     {"b": "含}括号与\"引号\""}),
    ("中文弯引号", '{"a": "他说‘好’然后走"}', {"a": "他说‘好’然后走"}),
    ("嵌套单引号数组", '{"a": [\'x\', \'y\']}', {"a": ["x", "y"]}),
    ("字符串里的 None 不能被替换", '{"a": "值为 None 和 True"}',
     {"a": "值为 None 和 True"}),
    ("多余逗号+单引号+尾逗号", "{'a': [1, 2,], 'b': None,}",
     {"a": [1, 2], "b": None}),
]


def run() -> int:
    bad = 0
    for label, text, want in CASES:
        try:
            got = SX._extract_json(text)
        except Exception as exc:            # noqa: BLE001
            bad += 1
            print("  [解析失败] %-22s %s" % (label, str(exc)[:70]))
            continue
        if want is None:
            print("  [OK] %-22s -> %s" % (label, json.dumps(got, ensure_ascii=False)[:56]))
            continue
        if got != want:
            bad += 1
            print("  [结果不符] %-20s 得到 %s 期望 %s"
                  % (label, json.dumps(got, ensure_ascii=False)[:44],
                     json.dumps(want, ensure_ascii=False)[:44]))
        else:
            print("  [OK] %-22s" % label)

    # 真实样本：把当初让整条链路失败的原文再跑一遍
    sample = Path(__file__).resolve().parent.parent.parent / "clade" / "llm_raw.txt"
    sample = Path(r"C:\Users\Strong\AppData\Local\Temp\opencode\llm_raw.txt")
    if sample.exists():
        raw = sample.read_text(encoding="utf-8")
        try:
            json.loads(raw)
            print("\n  [真实样本] 这次 json.loads 直接就过了（模型表现变了）")
        except (TypeError, ValueError):
            pass
        try:
            d = SX._extract_json(raw)
            n = SX.normalize(d)
            print("\n  [真实样本] 修复成功：%d 键 / %d 步 / %d 分 / 完整度 %d"
                  % (len(d), len(n["steps"]), n["portable_score"],
                     SX.completeness(n)))
        except Exception as exc:            # noqa: BLE001
            bad += 1
            print("\n  [真实样本] 仍失败：%s" % str(exc)[:140])

    print("\n  %d/%d 通过" % (len(CASES) + 1 - bad, len(CASES) + 1))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(run())
