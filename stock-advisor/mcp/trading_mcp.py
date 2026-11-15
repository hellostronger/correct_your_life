"""trading MCP server（stdio）—— nanobot 对话 agent 的只读交易工具。

红线：只读。不下单、不写任何库、不改配置、不 import app。
  - position_size   风险预算仓位计算（固定比例 / ATR / Kelly），算法 vendor 自
                    tradermonty/claude-trading-skills（MIT），见同目录 position_sizer.py。
                    纯计算，不联网。**输出是参考不是指令**。
  - stock_positions GET {SA_BASE}/api/holdings   真实持仓（代码/名称/股数/成本/现价/盈亏）
  - stock_watchlist GET {SA_BASE}/api/watchlist?with_quotes=1  自选+行情
数据只经本机 8686（主应用），再往上是 101 —— 符合「不要直连数据源」的硬规矩。

运行：venv python（mcp 包在 nanobot venv 里），由 nanobot serve 以 stdio 拉起：
    tools.mcpServers.trading = {command: <venv>python.exe, args: [本文件]}
"""

import json
import sys
from pathlib import Path

import requests
from mcp.server.fastmcp import FastMCP

SA_BASE = "http://127.0.0.1:8686"
_TIMEOUT = 8
_MAX_CHARS = 6000  # 塞进 LLM 上下文的截断上限，防大持仓表拖垮对话

# position_sizer.py 与本文件同目录（vendor 自 claude-trading-skills，纯标准库）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from position_sizer import SizingParameters, calculate_position, validate_parameters  # noqa: E402

mcp = FastMCP("trading")


def _get(path: str) -> dict | list:
    r = requests.get(SA_BASE + path, timeout=_TIMEOUT,
                     proxies={"http": None, "https": None})
    r.raise_for_status()
    return r.json()


def _cap(obj, limit: int = _MAX_CHARS) -> str:
    s = json.dumps(obj, ensure_ascii=False, default=str)
    if len(s) <= limit:
        return s
    return s[:limit] + f'…[截断，原 {len(s)} 字符]'


@mcp.tool()
def position_size(
    mode: str,
    account_size: float,
    entry_price: float = None,
    stop_price: float = None,
    risk_pct: float = None,
    atr: float = None,
    atr_multiplier: float = 2.0,
    win_rate: float = None,
    avg_win: float = None,
    avg_loss: float = None,
    max_position_pct: float = None,
) -> str:
    """按风险预算计算建议股数（只算数字，不是下单指令）。

    mode 三选一：
      - "fixed_fractional"：需要 entry_price + stop_price；risk_pct 默认 1（单笔风险占账户 %）。
      - "atr"：需要 entry_price + atr（ATR 波动值）；atr_multiplier 默认 2.0，止损距 = ATR×倍数。
      - "kelly"：需要 win_rate(0-1) / avg_win / avg_loss（历史胜率与盈亏比）。
    account_size = 总权益（¥）。max_position_pct 可选单票仓位上限 %。
    返回 JSON：建议股数 / 仓位金额 / 实际风险金额与占比 / 约束说明。
    """
    try:
        params = SizingParameters(
            account_size=account_size, entry_price=entry_price, stop_price=stop_price,
            risk_pct=risk_pct, atr=atr, atr_multiplier=atr_multiplier,
            win_rate=win_rate, avg_win=avg_win, avg_loss=avg_loss,
            max_position_pct=max_position_pct,
        )
        validate_parameters(params)
        result = calculate_position(params)
    except Exception as exc:  # 参数校验失败原样透传给模型
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
    result["note"] = "这是风险预算参考，不是下单指令；币种与 account_size 一致（¥）。"
    return _cap(result)


@mcp.tool()
def stock_positions() -> str:
    """查询真实持仓（只读）：代码/名称/股数/成本/现价/盈亏。用于回答「我亏得最多的是哪只」
    或为 position_size 提供 account_size（持仓页的总资产口径）。"""
    try:
        data = _get("/api/holdings")
    except Exception as exc:
        return json.dumps({"error": f"主应用 8686 不可达: {type(exc).__name__}: {exc}"},
                          ensure_ascii=False)
    return _cap(data)


@mcp.tool()
def stock_watchlist() -> str:
    """查询自选股列表与实时行情（只读），含现价/涨跌幅。用于取现价给 position_size。"""
    try:
        data = _get("/api/watchlist?with_quotes=1")
    except Exception as exc:
        return json.dumps({"error": f"主应用 8686 不可达: {type(exc).__name__}: {exc}"},
                          ensure_ascii=False)
    return _cap(data)


if __name__ == "__main__":
    mcp.run()
