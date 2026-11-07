# -*- coding: utf-8 -*-
"""fast 档候选模型探测（2026-11-08）：super-120b 已 404，按用户要求「往能力强切」。
测：存活 / 延迟 / JSON 可解析 / 判定正确性（一条真利好、一条纯情绪）。
跑法：python scripts/probe_fast_models.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["NO_PROXY"] = "*"

import llm_advisor as LA                                 # noqa: E402
import paper_trading as PT                               # noqa: E402

CANDIDATES = [
    "nvidia/nemotron-3-ultra-550b-a55b",        # 现standard/deep，最强
    "openai/gpt-oss-120b",                      # 120B 推理（10-14 曾 EOL，现又回列表）
    "z-ai/glm-5.3",                             # GLM 旗舰（慢但深）
    "nvidia/llama-3.1-nemotron-ultra-253b-v1",  # 253B
    "GLM-5.3-Flash",                            # GLM 快版（46~95s）
    "google/gemma-3-12b-it",                    # 小快灵（对照）
]

SYS = ("你是消息面核查员。判断股票突然大涨是否有实质性利好。实质性利好=公告级/权威媒体"
       "确认的具体事件；股价波动、情绪、自媒体猜测不算。证据不足→uncertain。"
       "只输出 JSON：{\"verdict\":\"positive|none|uncertain\",\"confidence\":0-10,"
       "\"reason\":\"...\",\"key_evidence\":[]}")

CASE_OK = ("股票：测试A（600001）。2026-12-31 单日+9%。近7天信息："
           "[12-30](公司公告) 与某央企签署 12 亿元供货合同，占去年营收 35%。"
           "[12-29](东财) 板块反弹讨论。请判定。")          # 真利好 → 应判 positive
CASE_NONE = ("股票：测试B（600002）。2026-12-31 单日+9%。近7天信息："
             "[12-30](股吧) 技术性反弹，超跌了。[12-29](微博) 情绪回暖。"
             "[12-28](东财) 板块轮动。无任何公告。请判定。")   # 纯情绪 → 应判 none

_conf0 = LA.load_llm_conf()          # 全量配置（含 api_key/base_url），只换 model
for m in CANDIDATES:
    res = {"model": m, "alive": False}
    for case, expect in ((CASE_OK, "positive"), (CASE_NONE, "none")):
        t0 = time.time()
        try:
            raw = LA.ask(SYS, case, conf={**_conf0, "model": m}, max_tokens=3000)
            dt = time.time() - t0
            j = PT._extract_json(raw) or {}
            v = j.get("verdict")
            res["alive"] = True
            res.setdefault("lat", []).append(round(dt, 1))
            res.setdefault("ok", []).append(v == expect)
            res.setdefault("verdicts", []).append(f"{v}/{j.get('confidence')}")
        except Exception as exc:                        # noqa: BLE001
            res.setdefault("errs", []).append(f"{str(exc)[:100]} ({time.time()-t0:.0f}s)")
            if time.time() - t0 < 5:
                break                                    # 瞬时失败（404/410）别再试第二条
    print(json.dumps(res, ensure_ascii=False), flush=True)
print("DONE", flush=True)
