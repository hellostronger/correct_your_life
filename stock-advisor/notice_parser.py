"""notice_parser.py —— 「公告页 → 结构化数据」的通用抽取器（可复用）。

为什么要有这一层：项目里有好几处需要「从政府/交易所/央行的公告页里抠出结构化
数据」，最典型的是 `holiday_calendar.py` 的放假安排。这类页面有三个共同麻烦：

1. **正文混在页面噪音里**。gov.cn 那种页面剥完标签还有 127 行，其中只有 7 行是正文，
   剩下是导航/页脚/「登录 注册」。按行号或固定 class 取，一改版就废。
2. **格式每年微调**。放假通知的措辞（"放假调休" vs "放假"、"共3天" 有时省略、
   调休上班日有时写成顿号有时写成分号）年年变。写死正则 = 每年 11 月修一次 bug。
3. **HTTP 200 不等于有数据**。反爬页会返回 200 + 一段 JS 挑战页。

所以：**抓正文 → 喂 LLM 出 JSON → 自校验 → 记 provenance**。LLM 不可用/超时时
回落调用方给的确定性兜底（`fallback=`），并在结果里标明 `used`，绝不假装成功。

复用示例（未来的两会日程、央行公开市场操作日历、LME/港交所假期）：
    r = parse_notice(url,
                     system_prompt="你从公告里抽取…",
                     schema_hint='{"items":[{"date":"YYYY-MM-DD"}]}',
                     fallback=lambda text: my_regex(text))
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

# 本机有系统代理（Windows WinINET），不设会让所有 requests 报 ProxyError。
# 与 app.py:31 同一个理由。
os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

import requests  # noqa: E402

DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
DEFAULT_TIMEOUT = 25


class NoticeError(RuntimeError):
    """抓取或抽取失败。调用方应据此回落，不要吞掉当成功。"""


@dataclass
class ParseResult:
    data: Any
    used: str                      # 'llm' | 'fallback'
    url: str = ""
    fetched_at: str = ""
    text_len: int = 0
    notes: list[str] = field(default_factory=list)
    model: str = ""


# --------------------------------------------------------------------------
# ① 抓正文
# --------------------------------------------------------------------------

_DROP_TAGS = ("script", "style", "noscript", "iframe")
# 页面噪音关键词：命中就整行丢（导航/页脚/版权）
_NOISE = re.compile(
    r"(登录|注册|打印|分享|字号|网站声明|联系我们|网站纠错|版权所有|ICP|"
    r"公网安备|主办单位|运行维护|客户端|小程序|微博|微信|国务院部门网站|"
    r"地方政府网站|驻港澳|驻外机构|链接：|导航)"
)


def html_to_lines(html: str, *, keep_noise: bool = False) -> list[str]:
    """HTML → 有意义的文本行列表（按文档顺序）。

    做法：先整块删掉 script/style，再把块级标签结尾换成换行（保住段落边界），
    最后按行清洗 + 丢弃噪音行。**不依赖任何 class/id** —— 那是页面改版就废的写法。
    """
    t = html
    for tag in _DROP_TAGS:
        t = re.sub(rf"<{tag}\b.*?</{tag}>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"</(p|div|li|tr|h[1-6]|section|article|td)>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&ldquo;", "「"), ("&rdquo;", "」"), ("&quot;", '"'), ("&#39;", "'")):
        t = t.replace(a, b)
    t = re.sub(r"[ \t　]+", " ", t)
    out = []
    for raw in t.split("\n"):
        ln = raw.strip()
        if len(ln) < 2:
            continue
        if not keep_noise and _NOISE.search(ln) and len(ln) < 60:
            continue
        out.append(ln)
    # 去连续重复（页面常把同一段渲染两遍，如移动端/桌面端双份）
    dedup: list[str] = []
    for ln in out:
        if not dedup or dedup[-1] != ln:
            dedup.append(ln)
    return dedup


def fetch_page_text(url: str, *, timeout: int = DEFAULT_TIMEOUT,
                    min_chars: int = 120,
                    must_contain: str | list[str] | None = None) -> tuple[str, str]:
    """抓页面并返回 (正文文本, 实际 URL)。

    `must_contain` 是**反 200-with-garbage** 的闸门：抓完必须在正文里找到这些
    关键词，否则抛 NoticeError（反爬挑战页也是 200，但正文里没有「放假」）。
    """
    try:
        r = requests.get(url, headers={"User-Agent": DEFAULT_UA}, timeout=timeout)
    except Exception as exc:                      # 连接类错误要说清是哪一类
        raise NoticeError(f"抓取失败 {type(exc).__name__}: {str(exc)[:160]}") from exc
    if r.status_code != 200:
        raise NoticeError(f"HTTP {r.status_code}（{url}）")
    # 中文站大多是 utf-8，但偶尔声明错；apparent_encoding 兜一下
    if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
        r.encoding = r.apparent_encoding or "utf-8"
    lines = html_to_lines(r.text)
    text = "\n".join(lines)
    if len(text) < min_chars:
        raise NoticeError(f"正文过短({len(text)}字)，疑似挑战页/空页：{url}")
    keys = [must_contain] if isinstance(must_contain, str) else list(must_contain or [])
    missing = [k for k in keys if k not in text]
    if missing:
        raise NoticeError(f"正文缺少关键词 {missing}，疑似反爬/非公告页：{url}")
    return text, r.url


# --------------------------------------------------------------------------
# ② LLM 抽取
# --------------------------------------------------------------------------

_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.S)


def extract_json(raw: str) -> Any:
    """从 LLM 回复里抠出 JSON。

    模型常犯的三种毛病都要兜：整体包在 ```json 里、前后加一段解释、
    中文全角引号。抠不出来就抛，让调用方回落 —— 宁可回落也别塞半截数据进去。
    """
    s = (raw or "").strip()
    m = _JSON_RE.search(s)
    if m:
        s = m.group(1)
    else:
        # 裸 JSON：取第一个 { 到最后一个 } 之间
        i, j = s.find("{"), s.rfind("}")
        if i == -1 or j <= i:
            i, j = s.find("["), s.rfind("]")
        if i == -1 or j <= i:
            raise NoticeError(f"LLM 回复里没有 JSON：{s[:160]}")
        s = s[i:j + 1]
    s = s.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    try:
        return json.loads(s)
    except json.JSONDecodeError as exc:
        raise NoticeError(f"JSON 解析失败：{exc}；原文片段 {s[:160]}") from exc


def parse_with_llm(text: str, *, system_prompt: str, schema_hint: str,
                   conf: dict | None = None, max_tokens: int = 3072,
                   temperature_note: str = "") -> tuple[Any, str]:
    """把正文喂 LLM 要结构化 JSON。返回 (data, model)。

    走项目自己的 llm_advisor.ask（同一套配置与降级链），不额外引依赖。
    刻意不开 thinking：这是纯抽取任务，且 base_url 非空时代理网关不支持 thinking
    参数（见 llm_advisor.ask 的说明）。
    """
    import llm_advisor
    sys_p = (
        system_prompt
        + "\n\n【输出格式】只输出一个 JSON 对象，不要任何解释文字、不要 markdown 代码块。"
        + "\n结构必须符合：" + schema_hint
        + ("\n" + temperature_note if temperature_note else "")
    )
    user = "以下是公告正文（已去网页噪音），请抽取：\n\n" + text
    raw = llm_advisor.ask(sys_p, user, conf=conf, max_tokens=max_tokens, thinking=False)
    return extract_json(raw), str((conf or llm_advisor.load_llm_conf()).get("model") or "")


# --------------------------------------------------------------------------
# ③ 编排：LLM 优先 + 确定性兜底
# --------------------------------------------------------------------------

def parse_notice(url: str, *, system_prompt: str, schema_hint: str,
                 fallback: Callable[[str], Any] | None = None,
                 must_contain: str | list[str] | None = None,
                 llm_conf: dict | None = None, timeout: int = DEFAULT_TIMEOUT,
                 max_tokens: int = 3072) -> ParseResult:
    """一条龙：抓 → LLM 抽 →（失败）回落。

    任何一步失败都会记进 notes 并回落；只有两条路都失败才抛 NoticeError。
    """
    notes: list[str] = []
    text, final_url = "", ""
    try:
        text, final_url = fetch_page_text(url, timeout=timeout, must_contain=must_contain)
    except NoticeError as exc:
        if fallback is None:
            raise
        notes.append(f"抓取失败，改用兜底：{exc}")

    fetched_at = time.strftime("%Y-%m-%dT%H:%M:%S")

    if text:
        try:
            data, model = parse_with_llm(text, system_prompt=system_prompt,
                                         schema_hint=schema_hint, conf=llm_conf,
                                         max_tokens=max_tokens)
            return ParseResult(data=data, used="llm", url=final_url or url,
                               fetched_at=fetched_at, text_len=len(text),
                               notes=notes, model=model)
        except Exception as exc:
            notes.append(f"LLM 抽取失败（{type(exc).__name__}: {str(exc)[:160]}），回落兜底")

    if fallback is None:
        raise NoticeError("；".join(notes) or "无兜底可用")
    return ParseResult(data=fallback(text), used="fallback", url=final_url or url,
                       fetched_at=fetched_at, text_len=len(text), notes=notes)
