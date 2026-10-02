"""ipo_llm.py —— 用 LLM 做打新决策的**判断层**（不含任何硬编码收益率）。

## 分工：LLM 算数还是判断？

分工必须清楚，否则两头都做不好：

| 活 | 谁做 | 为什么 |
|---|---|---|
| 中签率、配号数、顶格市值 | **代码算** | 是交易所公式，LLM 会算错且不可复现 |
| 市场涨幅基准、情绪系数 | **代码算** | 从已上市新股 + `sa_sector_daily` 统计，是硬数据 |
| 每签盈利金额 | **代码算**（乘出来的） | 乘法不该交给模型 |
| **值不值得为它挪仓** | **LLM** | 涉及行业景气、稀缺性、资金分流 —— 规则算不出 |
| **风险点提示** | **LLM** | 「科创板大盘股会分流资金」这类判断需要领域知识 |
| **行业热度定性** | **LLM** | 「存储在涨价周期」这种事规则无法表达 |

所以 LLM 拿到的是**结构化事实 + 市场基准**，产出的是**带依据的判断**，
不含它自己编的收益率数字。界面上把「代码算的」和「LLM 说的」分开显示。

## 为什么不让 LLM 直接给涨幅预测

试过就会知道：LLM 会自信地给出「预计首日涨幅 150%~200%」这种数字，
听起来合理，但**它没有新股的实时定价数据**，也看不到当天情绪 ——
那还是编的，只不过编得更像真的。所以这里**只让它做二元/定性问题**：

- `worth_shifting`：要不要为这只票挪市值（是/否/勉强）
- `risk_level`：低/中/高
- `factors`：它依据哪些**已给出的事实**判断的（必须引用具体数字）
- `caveats`：它的提醒

所有收益数字都来自代码，LLM 的输出**只影响定性结论，不进收益公式**。

## 失败必须显式

LLM 不可用（网关挂/超时/JSON 坏）时返回 `available=False`，
调用方回退到纯规则的判断，**并告诉用户「这次没有 LLM 判断」**。
绝不用一个默认判断悄悄填上。
"""

from __future__ import annotations

import json
import os
from datetime import date
from typing import Any

os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

import ipo_market  # noqa: E402

SCHEMA = {
    "worth_shifting": "yes|no|marginal",
    "shift_advice": "要不要为它挪市值挪到哪个市场、买什么底仓（一句话）",
    "risk_level": "low|medium|high",
    "profit_confidence": "high|medium|low",
    "factors": ["依据的具体事实，必须引用上面给出的数字（如中位涨幅xx%、破发率x%）"],
    "caveats": ["风险提醒，尤其规模摊薄/情绪透支/行业周期位置"],
}

SYSTEM = """你是 A 股打新（新股申购）策略分析师。你的任务不是预测涨幅，
而是判断「为了参与这只新股，是否值得专门调仓建立底仓市值」。

关键规则（不可违背）：
1. 网上申购**只认本市场市值**——沪市新股只算沪市非限售A股市值，
   深市/北交所/基金/债券/现金都不计入。底仓必须是沪市（60/601/603/605/688）或深市
   （00/001/002/300/301）对应市场的股票。
2. 市值按 **T-2 日**前 20 个交易日日均定格。所以最早 T-2 就要动手，
   不是 T-1，更不是 T 日。lead_days < 2 时基本已来不及。
3. 期望收益 = 配号数 × 中签率 × 每签盈利，配号数 = 日均市值 / 5000。
   中签率极低（常在 1%~2%），所以市值不够 = 拿不到资格，不是「少赚」。
4. 底仓的唯一职责是**占住市值等打新**：不求涨，但不能跌太多（跌了等于负收益）。
   所以底仓要高股息、低波动、上市满一年，且不该是正在跌的票。
5. 发行规模是双刃剑：巨无霸会分流二级市场资金。长鑫科技 668 亿股是极端案例，
   资金被摊薄后同板块其他票会跌。

判断依据优先级（从强到弱）：
- 给出的市场基准（中位涨幅、破发率、情绪系数）——这是实测数据
- 该股的中签率与顶格市值门槛 —— 决定期望值能否成立
- 行业属性与当前周期位置 —— 需要你的领域知识，这是你最能发挥的地方
- 发行估值溢价 —— 实测是正向因子（溢价高说明需求旺），别当风险项

不要输出任何你自己算的收益率数字。所有收益数字由代码给出，你只做定性判断。"""

USER_TMPL = """请判断这只新股的打新价值。

## 待判断的新股
- 名称：{name}（{code}）
- 板块：{board}
- 交易所：{exchange}
- 发行价：{price}
- 发行市盈率 / 行业市盈率：{issue_pe} / {industry_pe}
- 发行规模：{scale}
- 顶格申购需配市值：{full_cap}
- 中签率：{lot_rate}
- 网上申购日：{sub_date}（距今 T-{lead_days} 天）
{extra}

## 我的持仓市值现状
{holdings}

## 当期打新市场环境（自动采集）
{market}

## 请给出
1. worth_shifting：是否值得为它挪市值（yes / no / marginal）
2. shift_advice：如果值得，挪到哪个市场、底仓选什么类型（一句话，别列具体股票代码）
3. risk_level：低/中/高
4. factors：你依据的**具体事实**（必须引用上面数字，不要空泛说"前景广阔"）
5. caveats：风险提醒

只输出 JSON，不要解释文字。"""


def _fmt_opt(v: Any, suffix: str = "") -> str:
    if v is None or v == "":
        return "—"
    try:
        if isinstance(v, float):
            return f"{v:,.4g}{suffix}"
        return f"{v}{suffix}"
    except (TypeError, ValueError):
        return str(v)


def judge(new_stock: dict, *, market_ctx: dict, holdings_summary: str,
           lead_days: int, extra: str = "",
           llm_conf: dict | None = None) -> dict:
    """对一只待申购新股做定性判断。

    返回 {available, worth_shifting, risk_level, factors, caveats, ...}。
    `available=False` 时**没有**任何判断字段被填充 —— 调用方必须自己处理，
    不要 `.get('worth_shifting', 'yes')` 这种写法（那等于用默认值假装有判断）。
    """
    import llm_advisor
    conf = llm_conf or llm_advisor.load_llm_conf()

    if not (conf or {}).get("enabled"):
        return {"available": False, "why": "config.yaml 里 llm.enabled=false",
                "llm_used": False}

    scale = new_stock.get("scale_text") or "—"
    user = USER_TMPL.format(
        name=new_stock.get("name") or "?",
        code=new_stock.get("code") or "?",
        board=new_stock.get("board") or "?",
        exchange=new_stock.get("exchange") or "?",
        price=_fmt_opt(new_stock.get("price"), " 元"),
        issue_pe=_fmt_opt(new_stock.get("issue_pe")),
        industry_pe=_fmt_opt(new_stock.get("industry_pe")),
        scale=scale,
        full_cap=_fmt_opt(new_stock.get("full_cap_wan"), " 万元"),
        lot_rate=(_fmt_opt((new_stock.get("lot_rate_pct") or 0) * 100, "%")
                  if new_stock.get("lot_rate_pct") else "未公布"),
        sub_date=new_stock.get("sub_date") or "?",
        lead_days=lead_days,
        extra=("\n- " + extra) if extra else "",
        holdings=holdings_summary or "（未查到持仓）",
        market=ipo_market.digest_for_llm.__doc__ and _ctx_text(market_ctx),
    )
    try:
        raw = llm_advisor.ask(SYSTEM + "\n\n【输出 JSON 结构】\n" + json.dumps(
            SCHEMA, ensure_ascii=False, indent=1),
            user, conf=conf, max_tokens=2048, thinking=False)
        data = json.loads(_strip_fence(raw))
    except Exception as exc:
        return {"available": False, "why": f"{type(exc).__name__}: {str(exc)[:200]}",
                "llm_used": False, "raw": None}

    if not isinstance(data, dict) or "worth_shifting" not in data:
        return {"available": False, "why": "LLM 返回结构不完整（缺 worth_shifting）",
                "llm_used": True, "raw": str(data)[:300]}

    return {
        "available": True,
        "llm_used": True,
        "worth_shifting": data.get("worth_shifting"),
        "shift_advice": data.get("shift_advice") or "",
        "risk_level": data.get("risk_level") or "medium",
        "profit_confidence": data.get("profit_confidence") or "low",
        "factors": data.get("factors") or [],
        "caveats": data.get("caveats") or [],
        "model": conf.get("model", ""),
        "asked_on": date.today().isoformat(),
        "note": "收益数字由代码计算；这里只有 LLM 的定性判断",
    }


def _strip_fence(raw: str) -> str:
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    return s.strip()


def _ctx_text(ctx: dict) -> str:
    """把 market_context 压成给 LLM 看的文本（数字全部来自实测）。"""
    g = (ctx or {}).get("gain_stats") or {}
    prim = g.get("primary") or {}
    s = (ctx or {}).get("sentiment") or {}
    lines = []
    if prim:
        lines.append(f"- 近期已上市新股 {prim['n']} 只（{g.get('primary_source','')}）")
        lines.append(f"- 首日涨幅中位数 **{prim['median']*100:+.0f}%**"
                     f"（P25 {prim['p25']*100:+.0f}% / P75 {prim['p75']*100:+.0f}%）")
        lines.append(f"- **破发率 {prim['broke_rate']*100:.1f}%**，"
                     f"涨幅<20% 占 {prim['weak_rate']*100:.1f}%")
    if s.get("available"):
        lines.append(f"- 赚钱效应（{s['as_of']}）：涨停 {s['zt_total']} 家，"
                     f"上涨家数占比 {s.get('breadth_up_pct')}% → {s['band']}"
                     f"（情绪系数 {s['multiplier']:.2f}）")
    lines.append(f"- 综合基准涨幅：{(ctx or {}).get('adjusted_note', '—')}")
    if (ctx or {}).get("low_confidence"):
        lines.append("- ⚠️ 样本/情绪数据不足，参考价值有限")
    return "\n".join(lines) if lines else "（市场环境数据缺失）"


def holdings_text(cap: dict) -> str:
    """把持仓市值现状压成给 LLM 看的文本。"""
    c = cap or {}
    lines = [f"- 沪市非限售A股市值：{(c.get('sh') or 0)/10000:.1f} 万元"]
    lines.append(f"- 深市非限售A股市值：{(c.get('sz') or 0)/10000:.1f} 万元")
    lines.append(f"- 北交所市值：{(c.get('bj') or 0)/10000:.1f} 万元（不计入沪深打新）")
    pos = (c.get("positions") or [])[:6]
    if pos:
        lines.append("- 持仓明细：")
        for p in pos:
            lines.append(f"    {p['code']} {p.get('market','')} "
                         f"{p['value']/10000:.1f} 万")
    if c.get("excluded"):
        lines.append(f"- 不计入市值：{len(c['excluded'])} 项"
                     "（非A股普通股/代码不识别）")
    return "\n".join(lines)
