"""雪球桥 —— 借一个真浏览器取雪球数据（2026-10-20 新增）。

## 为什么要有这个模块
雪球对**纯 HTTP 一律发 WAF 挑战页**（`<textarea id="renderData">{"_waf_…":…}`，
HTTP 200 但零内容），原先只能绕道 SearXNG 的 `site:xueqiu.com`。那条路有两个
治不了的毛病：

1. 召回**很脏**（实测一次 6 条里 5 条与目标公司无关），且日期是搜索引擎的
   `publishedDate`，不是发帖时间；
2. 只有**被搜索引擎索引过**的帖子才拿得到。实测「MiniMax 7 月解禁但团队自愿
   锁仓 12 个月」这类信息，eastmoney/baidu/searxng/ak_em 搜解禁**全部 0 命中**，
   而雪球站内搜索直接有。

真浏览器能过 WAF，所以走 Kimi WebBridge（daemon + Chrome 扩展）在页面里
`fetch` 雪球**内部 JSON 搜索 API**：同源、带 cookie、返回真实发帖时间。

## 雪球内部搜索 API（实测，2026-10-20）
```
GET /query/v1/symbol/search/status
    ?u=1&page=1&count=10&symbol=SZ000158&source=all&sort=time&type=11&q=解禁
```
- **必须从 xueqiu.com 页面里发**（相对 URL + `credentials:'include'`），
  换任何其他 origin 直接 `404 {"detail":"Not Found"}` —— 这就是为什么必须先
  navigate 一次（每个 session 只需一次，之后同一 tab 复用）。
- `q` 是**模糊**匹配，不是精确过滤 → 相关性闸门仍必须在调用方做。
- 返回 `list[]`：`id`、`text`/`description`（HTML）、`created_at`（**毫秒**）、
  `user.id`、`user.screen_name`。文章页 URL = `https://xueqiu.com/{user.id}/{id}`。
- 🔴 **`title` 不是标题**，是带 `<a href>` 的高亮片段
  （`$常山北明(SZ000158)$ …`）→ 标题必须从正文首行自己取。
- ⚠️ `total` 不可信：实测 `total=0` 但 `list` 有 5 条。**按 list 长度判有无**。
- ⚠️ 返回项**没有 `symbol` 字段**（实测 47 个键里无此键）→ 无法用它校验「这条确实
  属于该股」，相关性闸门只能继续靠**标题/正文里出现公司名或代码**。
- `symbol=` **只缩范围、不严格过滤**（实测 6 组 × 10 条 = 59 条，真提到该股的
  **45/59 = 76%**，比 SearXNG 的 ~17% 好 4 倍多，但**不是 100%**）：
  `SZ159321`+`ETF` 首条讲的是**另一只**黄金ETF、`00700`+`解禁` 混进老铺黄金，
  而最糟的 `SH600519`+`业绩` **0/10 全无关**（讲苹果市值和瑞士可持续大会）。
  ⇒ **闸门绝不能因为「用了 symbol 搜」就撤。**
- 🔴 但 `symbol=` 必须**格式正确否则静默 0 条**：非法值（`XYZ999`）和空串都返回
  0 条，同样的 `q` 换真代码有 10 条 —— 传错形式 = 该股**看起来没新闻**，比报错难发现。
  A股/ETF **带**市场前缀（`SZ000158`/`SH600519`/`SZ159321`，裸 `159321` 实测 0 条），
  **港股传裸 5 位码**（`00100` 有 10 条，`HK00100`/`HK0100` 实测全 0 条）。见 `symbol_of`。

## 出口 IP 决定成败（这是选主备顺序的唯一依据）
| 出口 | 实测（2026-10-20） |
|---|---|
| 本机（住宅 IP） | `http=200`、5 条真帖、**无验证码** |
| 101（机房 IP） | 白天同一套代码取到 10 条/0.45s；**密集探测后** 100% 跳 `check.xueqiu.com/captcha`，页面原话「检测到当前网络环境的**访问频率异常**」 |

⇒ 主 `http://127.0.0.1:10086`，备 `http://101.43.25.101/xq-bridge`（80 端口经
nginx 反代，10086 本身只绑服务器内部）。**机房 IP 更容易被按 IP 计的风控判定**，
所以批量与节奏要压住：一轮一次 navigate、evaluate 分批 ≤6 只、批内每条间隔 250ms。

⚠️ 被风控后**只能人工在被限的那个浏览器里过一次验证**
（`https://www.xueqiu.com/service/captcha`）。本模块**不做**任何自动解题/绕过，
检测到验证码页就如实报错（`reason="captcha"`）交给调用方降级到 SearXNG。

## WebBridge 协议（实测）
- `POST {base}/command`，体 `{"action","args","session"}`；回 `{"ok":true,"data":…}`
  或 `{"ok":false,"error":{"code","message"}}`
- `evaluate` 的入参名是 **`code`**（写成 `expression` 会回 `evaluate: code is required`），
  返回值在 **`data.value`**（`data.type` = string/number/…）。
- `GET {base}/status` 有 `extension_connected`。扩展掉线时 daemon 最多**等 22 秒**
  再报错（Chrome 重启后实测 ~16s 自动重连）→ 所以**报错要重试一次**再判死。
"""

from __future__ import annotations

import json
import re
import threading
import time

import requests

DEFAULTS = {
    "enabled": True,
    "base_url": "http://127.0.0.1:10086",
    "fallback_url": "http://101.43.25.101/xq-bridge",
    "session": "xq",
    "seed_url": "https://xueqiu.com/",
    "count": 10,
    "batch_size": 6,
    # 🔴 pace_ms / cache_ttl 是**风控预算**，不是性能参数（2026-10-20 实测：
    # 按 250ms + 30 分钟 TTL 跑，43 只 × 2 词 = 86 组/轮、一天 ~4000 组，
    # 101 的机房 IP 当天下午就被弹到 `check.xueqiu.com/captcha`）。
    # 解法不是绕过验证（不做），是**少问**：解禁/锁仓这类信息的变化单位是
    # 「天」，不是「分钟」，所以 TTL 放到 3 小时、每批之间停 0.8 秒。
    "pace_ms": 800,
    "timeout": 90,
    "retries": 1,
    "cache_ttl": 10800,
}

# 一轮抓取里同一 (symbol, 关键词) 只问一次；TTL 到期自动重新问。
_CACHE: dict[str, tuple[float, list[dict]]] = {}
_LOCK = threading.Lock()
# 每个 base+session 只需 navigate 一次；失败或换端点后要重置。
_READY: set[str] = set()


class BridgeError(Exception):
    """带**可分类原因**的失败 —— 调用方据此决定降级还是重试。

    reason 只有四种，都是实测出来的形态，不能混：
      bridge_down   连不上（端口没开 / 服务没起 / 网络断）
      captcha       雪球风控（需人工验证，重试无用，只会加重）
      waf           回到 WAF 挑战页（浏览器没带上通行 cookie）
      upstream      其他一切（daemon 报错、解析失败、扩展未就绪）
    """

    def __init__(self, message: str, reason: str = "upstream") -> None:
        super().__init__(message)
        self.reason = reason


def conf_of(conf: dict | None) -> dict:
    """把 `news.channels.xueqiu.bridge` 深合并到 DEFAULTS 上。

    深合并是必须的：config.yaml 里只想改 `enabled` 时，浅 update 会把
    batch_size/timeout 全顶成缺失（与 `wechat_mp.load_mp_conf` 同一个坑）。
    """
    ch = ((conf or {}).get("channels") or {}).get("xueqiu") or {}
    raw = ch.get("bridge") or {}
    out = dict(DEFAULTS)
    for k, v in raw.items():
        if v is None:
            continue
        out[k] = v
    return out


def symbol_of(code: str) -> str:
    """A股/ETF/港股代码 → 雪球搜索 API 的 `symbol=` 值。

    🔴 **港股要传裸 5 位码，不能带 `HK` 前缀**（2026-10-20 实测 6 种写法）：
    `HK00100` / `HK0100` / `HK00700` / `HK02513` **全部 0 条**，
    而 `00100` 返回 10 条、`00700` 9 条、`02513` 10 条。
    A股/ETF 相反，**必须带市场前缀**：`SZ000158` / `SH600519` 各 10 条。

    `symbol=` 是**真过滤**不是摆设（`XYZ999` 与空串都返回 0 条，而同样的 `q` 换
    真代码有 10 条）—— 所以传错形式=该股静默 0 命中，比报错更难发现。

    识别口径与 `paper_trading.market_of` 对齐（5 位纯数字 = 港股），**没有逐票映射**。
    """
    c = re.sub(r"[^0-9A-Za-z]", "", str(code or ""))
    if c[:2].lower() == "hk":
        return c[2:]                      # 00100 而不是 HK00100
    if c[:2].lower() in ("sh", "sz", "bj"):
        return c[:2].upper() + c[2:]
    if c.isdigit() and len(c) == 5:
        return c                          # 港股原样
    if c.isdigit() and len(c) == 6:
        if c[0] in "48":
            return "BJ" + c
        return ("SH" if c[0] in "569" else "SZ") + c
    return "SZ" + c


def _session() -> requests.Session:
    """trust_env=False —— 本机有坏的系统代理（AGENTS.md：ProxyError 老坑），
    而 127.0.0.1 走代理必然连不上；101 那侧也已经靠 NO_PROXY 例外。"""
    s = requests.Session()
    s.trust_env = False
    return s


_SESS = _session()


def _post(base: str, payload: dict, timeout: float) -> dict:
    try:
        r = _SESS.post(base.rstrip("/") + "/command", json=payload,
                       timeout=timeout)
    except Exception as exc:                                   # noqa: BLE001
        raise BridgeError(f"桥连不上 {base}: {type(exc).__name__}: {exc}",
                          "bridge_down") from exc
    if r.status_code != 200:
        raise BridgeError(f"桥 HTTP {r.status_code}: {r.text[:160]}", "upstream")
    try:
        d = r.json()
    except Exception as exc:                                   # noqa: BLE001
        raise BridgeError(f"桥返回不是 JSON: {r.text[:160]}", "upstream") from exc
    if not d.get("ok"):
        err = d.get("error") or {}
        raise BridgeError(f"{err.get('code')} {err.get('message')}"[:300],
                          "upstream")
    return d.get("data") or {}


def status(base: str, timeout: float = 10.0) -> dict:
    try:
        r = _SESS.get(base.rstrip("/") + "/status", timeout=timeout)
        return r.json() if r.status_code == 200 else {}
    except Exception:                                          # noqa: BLE001
        return {}


def _navigate(base: str, conf: dict) -> None:
    url = conf.get("seed_url") or DEFAULTS["seed_url"]
    _post(base, {"action": "navigate",
                 "args": {"url": url},          # 不给 newTab → 复用同一个 tab
                 "session": conf["session"]}, conf["timeout"])


def ensure_page(base: str, conf: dict) -> None:
    """每个 session 只做一次 navigate（其余请求都在这一个 tab 里发）。"""
    key = f"{base}|{conf['session']}"
    if key in _READY:
        return
    _navigate(base, conf)
    with _LOCK:
        _READY.add(key)


def reset_page(base: str = "", session: str = "") -> None:
    """换端点/报错后清掉「已 navigate」标记，下一次会重新导航。"""
    with _LOCK:
        if base or session:
            _READY.discard(f"{base}|{session}")
        else:
            _READY.clear()


_JS = """(async () => {
  const jobs = %s;
  const pace = %s;
  const out = [];
  for (const j of jobs) {
    const one = { sym: j.sym, q: j.q };
    one.href = location.href;
    try {
      const u = '/query/v1/symbol/search/status?u=1&page=1&count=' + (j.count || 10) +
        '&symbol=' + j.sym + '&source=all&sort=time&type=11&q=' +
        encodeURIComponent(j.q);
      const r = await fetch(u, { credentials: 'include' });
      const t = await r.text();
      let p = null;
      try { p = JSON.parse(t); } catch (e) {}
      one.http = r.status;
      one.waf = (!p) && t.indexOf('_waf') >= 0;
      one.total = p ? (p.total || 0) : -1;
      one.list = (p && p.list) ? p.list.map(x => ({
        id: x.id,
        uid: (x.user && x.user.id) || '',
        name: (x.user && x.user.screen_name) || '',
        text: String(x.text || x.description || '').replace(/<[^>]+>/g, ' '),
        ts: x.created_at
      })) : [];
    } catch (e) { one.error = String(e).slice(0, 160); }
    out.push(one);
    await new Promise(s => setTimeout(s, pace));
  }
  return JSON.stringify(out);
})()"""


def _raw_search(base: str, jobs: list[dict], conf: dict) -> list[dict]:
    code = _JS % (json.dumps(jobs, ensure_ascii=False), int(conf["pace_ms"]))
    data = _post(base, {"action": "evaluate", "args": {"code": code},
                        "session": conf["session"]}, conf["timeout"])
    val = data.get("value")
    if not isinstance(val, str):
        raise BridgeError(f"evaluate 没返回字符串（type={data.get('type')}）",
                          "upstream")
    rows = json.loads(val)
    for one in rows:
        href = str(one.get("href") or "")
        if "check.xueqiu.com" in href or "captcha" in href:
            raise BridgeError("雪球风控：页面被跳到验证页，需人工过一次", "captcha")
        if one.get("http") == 404:
            raise BridgeError("搜索 API 404（页面不在 xueqiu.com 源上，或接口已改）",
                              "upstream")
        if one.get("waf"):
            raise BridgeError("回到 WAF 挑战页（浏览器未带上通行 cookie）", "waf")
    return rows


def search(pairs: list[tuple[str, str]], conf: dict | None = None,
           count: int | None = None) -> dict[tuple[str, str], list[dict]]:
    """批量查 (code, keyword) → 原始帖子列表。

    返回 `{(code, kw): [ {url,title,text,publish_time,sym,q}, … ]}`。
    一次 HTTP 拿一批（批内 250ms 间隔），命中缓存的直接复用。
    """
    conf = conf_of(conf)
    if not conf["enabled"] or not pairs:
        return {}
    n = int(count or conf["count"])
    out: dict[tuple[str, str], list[dict]] = {}
    todo: list[tuple[tuple[str, str], dict]] = []
    now = time.time()
    ttl = float(conf["cache_ttl"])
    for code, kw in pairs:
        sym = symbol_of(code)
        key = f"{sym}|{kw}"
        with _LOCK:
            hit = _CACHE.get(key)
        if hit and now - hit[0] < ttl:
            out[(code, kw)] = hit[1]
            continue
        todo.append(((code, kw), {"sym": sym, "q": kw, "count": n}))
    if not todo:
        return out

    bases = [str(conf["base_url"]).strip()]
    if conf.get("fallback_url"):
        bases.append(str(conf["fallback_url"]).strip())
    last: Exception | None = None
    for base in bases:
        if not base:
            continue
        for attempt in range(int(conf["retries"]) + 1):
            try:
                ensure_page(base, conf)
                rows = _run_batches(base, todo, conf)
                for (code, kw), row in zip([t[0] for t in todo], rows):
                    items = _to_items(code, row)
                    out[(code, kw)] = items
                    sym = symbol_of(code)
                    with _LOCK:
                        _CACHE[f"{sym}|{kw}"] = (now, items)
                return out
            except BridgeError as exc:
                last = exc
                reset_page(base, conf["session"])
                if exc.reason == "captcha":
                    break                       # 重试只会加重风控
                if exc.reason == "bridge_down":
                    break                       # 换下一个端点
                if attempt < int(conf["retries"]):
                    time.sleep(2.0)             # 扩展重连实测 ~16s，但多为瞬时
            except Exception as exc:            # noqa: BLE001
                last = BridgeError(f"{type(exc).__name__}: {exc}", "upstream")
                reset_page(base, conf["session"])
        if isinstance(last, BridgeError) and last.reason == "captcha":
            raise last
    raise last or BridgeError("所有桥端点都不可用", "bridge_down")


def _run_batches(base: str, todo: list, conf: dict) -> list[dict]:
    size = max(1, int(conf["batch_size"]))
    rows: list[dict] = []
    for i in range(0, len(todo), size):
        chunk = todo[i:i + size]
        rows.extend(_raw_search(base, [j for _k, j in chunk], conf))
    return rows


_TITLE_RE = re.compile(r"^\s*\$[^$]{1,40}\(([0-9A-Z]{5,8})\)\$")


def _to_items(code: str, row: dict) -> list[dict]:
    """原始行 → 条目。**只整理事实，不做相关性判断**（闸门留在 news_fetcher，
    两条路径（桥 / SearXNG）要共用同一套判据，不能各判一遍）。"""
    items: list[dict] = []
    for x in (row.get("list") or []):
        uid, sid = str(x.get("uid") or ""), str(x.get("id") or "")
        if not uid or not sid:
            continue
        text = re.sub(r"\s+", " ", str(x.get("text") or "")).strip()
        if not text:
            continue
        # title 是搜索引擎式的高亮片段，不可用 → 正文首句当标题。
        m = _TITLE_RE.match(text)
        body = text[m.end():].strip() if m else text
        title = (body[:80] + "…") if len(body) > 80 else body
        ts = x.get("ts")
        pub = ""
        try:
            pub = time.strftime("%Y-%m-%dT%H:%M:%S",
                                 time.localtime(int(ts) / 1000.0))
        except (TypeError, ValueError):
            pub = ""
        items.append({
            "url": f"https://xueqiu.com/{uid}/{sid}",
            "title": title,
            "text": body[:2000],
            "publish_time": pub,          # 毫秒时间戳 = 真实发帖时间
            "author": str(x.get("name") or ""),
            "sym": str(row.get("sym") or symbol_of(code)),
            "q": str(row.get("q") or ""),
            "source": "bridge",
        })
    return items
