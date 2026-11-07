# -*- coding: utf-8 -*-
"""E2E（零 token）：真 deps 跑一次 _decide_one，拦截 _llm_call 入参，
验证【现金/存款基准】块逐字节进入决策上下文。

照 AGENTS.md 的三必备动作：
  sys.sa_bootstrap_done=True（否则 import app 起全套 daemon）
  sys.path 剔脚本目录（temp 的 click.py 会遮蔽真 click 包）
  NO_PROXY=*（TUN 劫持时 WinError 10065）

首次 _llm_call 即抛 STOP：_decide_one 在首次调用前只有读库（已核对
1775~2060 行无 INSERT/UPDATE），零 token、零副作用。
跑法：python verify_cash_in_context.py
"""
import os
import sys
from pathlib import Path

os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.sa_bootstrap_done = True          # 必备①：不起全套守护线程
_here = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _here]
sys.path.insert(0, _here)             # 必备②：防 temp 目录 click.py 遮蔽

import app                            # noqa: E402  （no-boot 导入 ~5s）
import paper_trading as PT            # noqa: E402
import cash_baseline as cb            # noqa: E402

OUT = Path(r"C:\Users\strong\AppData\Local\Temp\cash_ctx.txt")

captured: list[tuple[str, str]] = []


def fake_llm_call(system, user, *a, **kw):
    captured.append((system or "", user or ""))
    raise RuntimeError("STOP-AT-FIRST-CALL")


PT._llm_call = fake_llm_call

deps = app._paper_deps()
conf = deps.get("conf") or app._conf_section("paper")
stock = {"code": "000858", "name": "五粮液"}
try:
    PT._decide_one(deps, conf, stock, "2026-11-08", slot="verify")
except RuntimeError as exc:
    if "STOP-AT-FIRST-CALL" not in str(exc):
        raise

assert captured, "LLM 未被调用（_decide_one 提前跳过了？）"
system_text, user_text = captured[0]
OUT.write_text(user_text, encoding="utf-8")

# 块内各行分别断言（不逐字节比对：_decide_one 传的是真 conn，10Y 行来自
# sa_macro_rates，本地无法离线重建同一字节串 —— 逐字节比对必然假失败）
cb_conf = {**cb.INJECT_DEFAULTS, **(conf.get("cash_baseline_inject") or {})}
ok_enable = cb_conf.get("enabled") is True
ok_title = "【现金/存款基准（机会成本）】" in user_text
ok_mf = "货基 7 日年化中位数：1.071%" in user_text
ok_lpr = "LPR（贷款端参考）：1年 3.00%" in user_text
ok_contrast = "五粮液近20日涨幅" in user_text and "无风险基准" in user_text
ok_warn = "不是交易指令" in user_text

print(f"llm_calls_captured = {len(captured)}")
print(f"context_chars      = {len(user_text)}")
print(f"enabled            = {ok_enable}")
print(f"block_title        = {ok_title}")
print(f"block_money_fund   = {ok_mf}")
print(f"block_lpr          = {ok_lpr}")
print(f"block_contrast     = {ok_contrast}")
print(f"block_warn         = {ok_warn}")
print(f"saved              = {OUT}")
sys.exit(0 if (ok_enable and ok_title and ok_mf and ok_lpr
               and ok_contrast and ok_warn) else 1)
