"""通知事件登记表（notify_events.py）——「哪些事件推哪些渠道」的唯一真源。

## 为什么要有这个文件

之前通知是**两级开关**，而且两级都不完整：

- 渠道级：`notify.wx.enabled` / `notify.email.enabled`（全局）
- 事件级：散落在各模块自己的 config 段里，名字还各不相同 ——
  `mp.notify_new`、`x.notify_new`、`sector.alert_notify`、`volume.notify`…

由此产生两个具体问题：

1. **公众号新文章会发邮件。** 它的守门条件是 `mp.notify_new`（写的是「新文章推
   微信」），但 `notifier.notify()` 是**按渠道广播**的：邮件一旦全局启用，
   每一类通知都会进邮箱。用户明确要求「公众号新文章不要发邮件」。
2. **开关藏得太深。** 事件级开关要么在模块段里（`volume.notify` 只有翻到
   ⚙️调度 页最下面的「🧩 完整配置」才看得到），要么压根没有（止盈止损、
   提款里程碑、模拟盘结算、盘前/盘后报告……全都没有独立开关，只能靠
   渠道总开关一刀切）。

## 这份登记表怎么解决

把「事件 → 渠道」显式化成一张矩阵，存成 `config.yaml` 的 `notify.events.<事件>.<渠道>`：

```yaml
notify:
  wx:   {enabled: true}
  email:{enabled: true, ...}
  events:
    mp_article: {wx: true, email: false}   # 公众号新文章：只推微信
    x_tweet:    {wx: true, email: true}
```

判定是**两级与**：`渠道总开关 AND 事件·渠道开关`。所以关掉任一都停，
而矩阵让「同一事件不同渠道分别开关」成为可能 —— 公众号新文章走微信、
盘后复盘走邮件这类需求，不用再改代码。

`notifier.notify(..., event=...)` 按这张表解析要发哪些渠道；
前端 🔔通知 页把这张矩阵直接画成复选框表格（见 index.html renderNotifyMatrix）。

## 约定

- **本模块零依赖**（只用内置库）。config_schema 要 import 它来生成字段、
  notifier 要 import 它来解析渠道，交叉 import 会踩到 news_fetcher 的循环。
- `EVENTS` 的每个 key 都是**稳定的对外契约**：写进 config.yaml 后就长期存在，
  改名等于丢用户配置。只增不删，改语义时同步改 label/help。
- `default` 是**该事件在两个渠道都开启时的默认路由**。首次读取 config.yaml
  时用它兜底；一旦用户显式改过某个事件·渠道，以 yaml 为准。
- `scanner` 标 True 表示这背后有个独立守护线程/扫描器（关掉它等于关功能，
  不只是关通知）；这类事件在页面上单独归到「⚙️ 功能开关」分组，因为它们
  同时是 `alerts.enabled` / `calendar.enabled` 那种模块级总开关的语义。
"""

from typing import Any

# 事件元信息表。字段：
#   label   页面上的中文名（也是推送标题的语义来源）
#   group   分组，前端按它分块渲染
#   default {渠道: 该事件的默认路由}
#   help    提示文案（写清楚这个事件什么时候会响）
#   source  触发点（代码位置），便于日后排查
EVENTS: dict[str, dict[str, Any]] = {
    # ---------------- 内容抓取 ----------------
    "mp_article": {
        "label": "📰 公众号新文章",
        "group": "内容抓取",
        "default": {"wx": True, "email": False},
        "help": "公众号抓到新文章时推一条。**默认只推微信** —— 新文量大且时效性强，"
                "进邮箱只会变成没人看的垃圾堆；要归档可以在下面勾上邮件。",
        "source": "wechat_mp.run_once()",
    },
    "x_tweet": {
        "label": "🐦 X 新推文",
        "group": "内容抓取",
        "default": {"wx": True, "email": True},
        "help": "监听的人发了新推文时推一条。",
        "source": "x_monitor.run_once()",
    },

    # ---------------- 每日报告 ----------------
    "premarket_report": {
        "label": "🌅 盘前简报",
        "group": "每日报告",
        "default": {"wx": True, "email": True},
        "help": "交易日盘前生成的 Claude 简报摘要。",
        "source": "daily_reports.generate_premarket_brief()",
    },
    "postmarket_report": {
        "label": "📋 盘后复盘",
        "group": "每日报告",
        "default": {"wx": True, "email": True},
        "help": "交易日盘后生成的 Claude 复盘摘要。",
        "source": "daily_reports.generate_postmarket_review()",
    },

    # ---------------- 交易提醒 ----------------
    "take_profit": {
        "label": "🎯 止盈/止损触发",
        "group": "交易提醒",
        "default": {"wx": True, "email": True},
        "help": "逐笔买入绑定的止盈/止损策略条件达成时提醒。**盘中触发，越快越好**，"
                "别关微信。",
        "source": "app.check_strategies_once()",
    },
    "position_strategy": {
        "label": "🎯 持仓策略触发",
        "group": "交易提醒",
        "default": {"wx": True, "email": True},
        "help": "整仓级策略（回撤止盈、时间止盈等）条件达成。",
        "source": "app.check_position_strategies_once()",
    },
    "withdrawal": {
        "label": "🏧 提款计划里程碑",
        "group": "交易提醒",
        "default": {"wx": True, "email": True},
        "help": "提款计划达标 / 剩 10·5·1 个交易日 / 逾期。每个里程碑只推一次。",
        "source": "app.check_withdrawal_once()",
    },

    # ---------------- 行情异动 ----------------
    "market_volume": {
        "label": "📊 大盘量能翻转",
        "group": "行情异动",
        "default": {"wx": True, "email": False},
        "help": "三大指数量比出现放量/缩量翻转时提醒（连续两次采样确认，"
                "每天最多 4 条、同方向 45 分钟冷却）。盘中噪音，**默认只推微信**。",
        "source": "app._market_volume_loop()",
    },
    "sector_intraday": {
        "label": "⚡ 板块盘中异动",
        "group": "行情异动",
        "default": {"wx": True, "email": False},
        "help": "盘中板块急拉 / 涨停骤增预警（交易时段每 5 分钟采样）。"
                "盘中噪音，**默认只推微信**。",
        "source": "sector._intraday_loop()",
    },
    "sector_rotation": {
        "label": "🧭 板块轮动预警",
        "group": "行情异动",
        "default": {"wx": True, "email": True},
        "help": "盘后快照发现新主线候选 / 涨停聚集时提醒。",
        "source": "sector._sector_auto_loop()",
    },
    "sector_collect_failed": {
        "label": "🔌 板块采集失败",
        "group": "行情异动",
        "default": {"wx": True, "email": True},
        "help": "板块快照/盘中采样连续失败时提醒。**这个必须开着** ——"
                "2026-09-21 起采集静默失败 9 天，页面照常显示旧数据没人发现。",
        "source": "sector._intraday_loop() / _sector_auto_loop()",
    },
    "crypto_move": {
        "label": "🪙 币圈 24h 异动",
        "group": "行情异动",
        "default": {"wx": True, "email": False},
        "help": "gate.io 股票永续 24h 涨跌超阈值（同向有冷却）。7×24 小时连续交易，"
                "**默认只推微信**。",
        "source": "crypto_watch.check_alerts()",
    },
    "crypto_link": {
        "label": "🔗 币股比价偏离",
        "group": "行情异动",
        "default": {"wx": True, "email": False},
        "help": "永续合约与锚定股票的日收盘比价偏离中位超过阈值。**默认只推微信**。",
        "source": "crypto_watch.link_alerts()",
    },
    "discover": {
        "label": "🔭 挖到高置信新标的",
        "group": "行情异动",
        "default": {"wx": True, "email": False},
        "help": "全市场扫描命中高置信候选并自动加进自选股时提醒。**默认只推微信**。",
        "source": "app._discover_loop()",
    },

    # ---------------- 事件 / 日历 ----------------
    "alerts_event": {
        "label": "⚠️ 解禁/增发/减持",
        "group": "事件 / 日历",
        "default": {"wx": True, "email": True},
        "help": "自选股未来的限售解禁 / 增发新股上市，以及减持预披露与已发生的"
                "董监高减持。同一事件只推一次。",
        "source": "alerts.check_alerts_once()",
    },
    "calendar": {
        "label": "📅 财经日历",
        "group": "事件 / 日历",
        "default": {"wx": True, "email": True},
        "help": "非农 / 交割日 / 四巫 / LPR 本地推算，美国中期选举、两会开幕，"
                "以及手动补录的 FOMC / CPI。",
        "source": "econ_calendar.check_calendar_once()",
    },
    "macro_rate": {
        "label": "📉 美债收益率异动",
        "group": "事件 / 日历",
        "default": {"wx": True, "email": False},
        "help": "美债 10Y 单日变动超阈值（默认 5bp）时提醒，并附期限利差方向。"
                "**默认只推微信**。",
        "source": "macro_rates.check_alert()",
    },
    "ipo_listing": {
        "label": "🆕 新股上市",
        "group": "事件 / 日历",
        "default": {"wx": True, "email": False},
        "help": "A/H 股新股上市日前一天提醒（含发行价、顶格申购需配市值）。"
                "**默认只推微信**。",
        "source": "ipo_calendar.listing_alerts()",
    },
    "ipo_quota": {
        "label": "🎫 打新额度提醒",
        "group": "事件 / 日历",
        "default": {"wx": True, "email": False},
        "help": "按「T-2 日前 **20 个交易日日均市值** ÷ 5000」算配号数，"
                "并在**该补仓的时间点**提醒（提前量是倒推的，不是固定天数）。"
                "补仓只占 20 日窗口的 1/20，所以等 T-2 才动手日均爬不上去。"
                "**默认只推微信**。",
        "source": "ipo_quota_alert.build_alerts()",
    },
    "market_holiday": {
        "label": "🌴 休市/调休提醒",
        "group": "事件 / 日历",
        "default": {"wx": True, "email": False},
        "help": "法定长假前最后一个交易日提醒「明日休市 N 天」，"
                "以及调休上班的周末提醒。**默认只推微信**。",
        "source": "holiday_calendar 巡检",
    },

    # ---------------- 模拟盘 ----------------
    "paper_settle": {
        "label": "🧪 模拟交易结算",
        "group": "模拟盘",
        "default": {"wx": True, "email": False},
        "help": "收盘后结算（平仓 + 复盘反思）结果。**默认只推微信**，内容较长。",
        "source": "paper_trading.settle_and_reflect()",
    },
    "paper_goal": {
        "label": "🎯 模拟盘目标达标/到期",
        "group": "模拟盘",
        "default": {"wx": True, "email": True},
        "help": "模拟盘某个目标周期结束时推送最终收益与时间进度对比。",
        "source": "paper_goal.evaluate_active()",
    },
    "paper_strategy": {
        "label": "🧪 策略对照触发",
        "group": "模拟盘",
        "default": {"wx": True, "email": False},
        "help": "影子策略扫描触发卖出时提醒（用于对比「LLM 卖早了还是卖晚了」）。"
                "触发频繁，**默认只推微信**。",
        "source": "app._paper_strategy_scan()",
    },
    "paper_stoploss_failed": {
        "label": "🔴 模拟盘止损失败",
        "group": "模拟盘",
        # 微信 + 邮件都开：这是**风控静默失效**，属于「必须看到」的一类。
        # 2026-10-08 加 —— 那天 3 只持仓跌破止损线却没卖（异常回滚了成交），
        # 系统只是「看起来今天没止损」，没人第一时间发现。
        "default": {"wx": True, "email": True},
        "help": "持仓**已跌破止损线**但因异常未能卖出（成交被回滚，"
                "风险敞口仍在）。同一只票同一原因当天只推一次。",
        "source": "paper_trading.check_stop_loss()",
    },
}

# 渠道名（与 notify.<渠道> 段一一对应）
CHANNELS = ("wx", "email")

# 前端分组顺序（省得按字母排）
GROUPS = ["内容抓取", "每日报告", "交易提醒", "行情异动", "事件 / 日历", "模拟盘"]


def event_default(event: str) -> dict:
    """该事件的默认路由 {渠道: bool}。未知事件退回「全开」。

    未知事件返回全开而不是全关：`notifier.notify()` 是对外的通用入口
    （`python notifier.py send` / `POST /api/notify/send`），手工发的消息
    不该因为「没登记过」就被静默丢掉。
    """
    meta = EVENTS.get(event) or {}
    return {ch: bool((meta.get("default") or {}).get(ch, True)) for ch in CHANNELS}


def channels_json() -> list[dict]:
    """给前端动态渲染矩阵用的分组结构。"""
    out: dict[str, list[dict]] = {g: [] for g in GROUPS}
    for key, meta in EVENTS.items():
        row = {
            "key": key,
            "label": meta["label"],
            "group": meta["group"],
            "help": meta.get("help", ""),
            "source": meta.get("source", ""),
            "default": event_default(key),
        }
        out.setdefault(meta["group"], []).append(row)
    return [{"group": g, "rows": rows} for g, rows in out.items() if rows]
