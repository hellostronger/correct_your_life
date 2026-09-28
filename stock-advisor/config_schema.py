"""config.yaml 的完整配置模式（config_schema.py）——「所有配置项都能在网页上改」的实现基础。

## 为什么要有这个文件

之前每个模块的网页设置是**手写**的：新闻页手写 4 个输入框、调度页手写币圈那几个…
漏掉是必然的（`fetch_timeout_minutes`、`alert_notify`、`paper.max_position_pct`、
`alerts.days`、`mp.base_url`…都曾经改不了只能手改 yaml）。

现在改成：模式（schema）里**声明**每个键的类型/范围/默认值/说明/是否敏感，
后端按模式校验并回写，前端按模式**动态生成**表单。好处：

1. 覆盖度由模式保证——加配置项只要在 SCHEMAS 加一行，前端自动出现输入框
2. 类型与范围在后端统一校验，不再散落在各个 PUT 接口里
3. 每个字段都能带 `help`，写清楚「0 是什么意思」这类不看源码就不知道的事
4. 敏感项（api_key / auth_code / mp.auth）统一标 `secret`，接口只回掩码

## 与其它配置写入路径的关系

本模块是**通用入口**（`/api/config/all`），与既有的专用入口并存：
- 通用：`GET/PUT /api/config/all` —— 这里，改任意键
- 专用：`/api/schedule`（时间点）、`/api/llm/config`、`/api/notify/config`、
  `/api/crypto/links` —— 为了那几个页面的顺手操作

两者写的是同一个 config.yaml，都走 conf_util.WRITE_LOCK，不会互相踩。
"""

from typing import Any

# 字段简写：
#   t=int/float/bool/str/list/time/password/url
#   d=默认值  lo/hi=取值范围（int/float）  req=是否必填
#   secret=True  接口只回掩码，PUT 时留空=不修改
#   adv=True     「高级」项，默认折叠（不常用但要能改）
#   choices=[..] 枚举

SCHEMAS: dict[str, dict[str, Any]] = {
    # ---------------- 新闻 ----------------
    "news": {
        "label": "📰 新闻抓取",
        "help": "多渠道免费抓取。每渠道可单独开关并设请求间隔（防反爬）。",
        "fields": [
            {"key": "fetch_interval_minutes", "t": "int", "d": 30, "lo": 0, "hi": 1440,
             "label": "抓取周期(分钟)", "help": "0 = 关闭自动抓取"},
            {"key": "items_per_query", "t": "int", "d": 10, "lo": 1, "hi": 50,
             "label": "每查询取条数", "help": "每只股票每个渠道取多少条"},
            {"key": "keywords_extra", "t": "list", "d": [],
             "label": "全局追加搜索词", "help": "逗号分隔，对所有自选股生效"},
            {"key": "channels.eastmoney.enabled", "t": "bool", "d": True,
             "label": "东财 启用", "help": "个股公告 + 资讯搜索，最快最稳"},
            {"key": "channels.eastmoney.min_interval", "t": "float", "d": 1.0,
             "lo": 0, "hi": 60, "label": "东财 请求间隔(秒)"},
            {"key": "channels.baidu.enabled", "t": "bool", "d": True,
             "label": "百度 启用", "help": "百度新闻垂直搜索，反爬敏感"},
            {"key": "channels.baidu.min_interval", "t": "float", "d": 3.0,
             "lo": 0, "hi": 60, "label": "百度 请求间隔(秒)"},
            {"key": "channels.sina.enabled", "t": "bool", "d": True,
             "label": "新浪 启用"},
            {"key": "channels.sina.min_interval", "t": "float", "d": 2.0,
             "lo": 0, "hi": 60, "label": "新浪 请求间隔(秒)"},
            {"key": "channels.duckduckgo.enabled", "t": "bool", "d": True,
             "label": "DDG 启用", "help": "兜底，多为新闻聚合页"},
            {"key": "channels.duckduckgo.min_interval", "t": "float", "d": 5.0,
             "lo": 0, "hi": 60, "label": "DDG 请求间隔(秒)"},
            {"key": "channels.duckduckgo.timeout", "t": "int", "d": 30, "lo": 5, "hi": 120,
             "label": "DDG 超时(秒)"},
        ],
    },
    # ---------------- 社媒抓取 ----------------
    "bili": {
        "label": "📺 B站动态",
        "help": "调 MediaCrawler 抓 UP 主动态+评论。",
        "fields": [
            {"key": "enabled", "t": "bool", "d": True, "label": "启用"},
            {"key": "interval_minutes", "t": "int", "d": 30, "lo": 0, "hi": 1440,
             "label": "抓取周期(分钟)", "help": "0 = 关闭"},
            {"key": "fetch_timeout_minutes", "t": "int", "d": 10, "lo": 1, "hi": 120,
             "label": "单轮爬取超时(分钟)", "adv": True,
             "help": "到点会杀掉整棵进程树；盘上已写的数据仍会入库"},
        ],
    },
    "wb": {
        "label": "🔥 微博",
        "help": "调 MediaCrawler 抓博主微博。微博风控较严，周期别设太短。",
        "fields": [
            {"key": "enabled", "t": "bool", "d": True, "label": "启用"},
            {"key": "interval_minutes", "t": "int", "d": 60, "lo": 0, "hi": 1440,
             "label": "抓取周期(分钟)", "help": "0 = 关闭；风控严，建议 ≥60"},
            {"key": "fetch_timeout_minutes", "t": "int", "d": 15, "lo": 1, "hi": 120,
             "label": "单轮爬取超时(分钟)", "adv": True},
        ],
    },
    # ---------------- 公众号 ----------------
    "mp": {
        "label": "📰 公众号（拉 WeRSS 的 RSS）",
        "help": "本项目**不爬**微信，只拉上游 WeRSS 服务的 RSS 产物。服务独立部署，"
                "见仓库 mp-service/ 目录。",
        "fields": [
            {"key": "enabled", "t": "bool", "d": True, "label": "启用"},
            {"key": "interval_minutes", "t": "int", "d": 30, "lo": 0, "hi": 1440,
             "label": "拉取周期(分钟)", "help": "0 = 关闭"},
            {"key": "notify_new", "t": "bool", "d": True, "label": "新文章推微信"},
            {"key": "base_url", "t": "url", "d": "", "label": "WeRSS 服务地址",
             "help": "远程实例填 http://<服务器IP>:8001。配一次，网页添加订阅源时自动带入"},
            {"key": "auth", "t": "password", "d": "", "secret": True,
             "label": "Authorization 头",
             "help": "远程加了反代鉴权时填，原样放进请求头：Basic <base64> / Bearer <token>"},
            {"key": "use_fresh", "t": "bool", "d": False, "label": "走 /fresh 强制上游更新",
             "help": "远程共享实例建议关：走 WeRSS 自己的缓存，不反复催它爬"
                     "（上游提示「添加订阅频率过高容易被封控」）。逐个订阅源可覆盖此项"},
            {"key": "max_items", "t": "int", "d": 50, "lo": 1, "hi": 100,
             "label": "每源每轮取条数", "adv": True},
            {"key": "keep_days", "t": "int", "d": 120, "lo": 1, "hi": 3650,
             "label": "文章保留天数", "adv": True},
            {"key": "timeout", "t": "int", "d": 25, "lo": 3, "hi": 300,
             "label": "HTTP 超时(秒)", "adv": True},
        ],
    },
    # ---------------- X ----------------
    "x": {
        "label": "🐦 X(Twitter) 指定用户发言",
        "help": "用 twscrape（需 pip install \"twscrape[curl]\"）。Nitter/snscrape/Twint "
                "均已失效。账号 cookie 在「🐦 X」页粘贴。",
        "fields": [
            {"key": "enabled", "t": "bool", "d": True, "label": "启用"},
            {"key": "interval_minutes", "t": "int", "d": 30, "lo": 0, "hi": 1440,
             "label": "抓取周期(分钟)", "help": "0 = 关闭；未配 cookie 时本项自动跳过"},
            {"key": "notify_new", "t": "bool", "d": True, "label": "新推文推微信"},
            {"key": "include_retweets", "t": "bool", "d": False, "label": "收录转发",
             "help": "默认关，噪音大"},
            {"key": "max_tweets", "t": "int", "d": 40, "lo": 1, "hi": 200,
             "label": "每人每轮取条数", "adv": True},
            {"key": "keep_days", "t": "int", "d": 90, "lo": 1, "hi": 3650,
             "label": "推文保留天数", "adv": True},
            {"key": "wait_timeout", "t": "int", "d": 25, "lo": 1, "hi": 300,
             "label": "等空闲账号秒数", "adv": True,
             "help": "账号池全被限流时最多等多久"},
            {"key": "max_text", "t": "int", "d": 2000, "lo": 100, "hi": 20000,
             "label": "正文入库截断长度", "adv": True},
        ],
    },
    # ---------------- 币圈 ----------------
    "crypto": {
        "label": "🪙 币圈 24h（gate.io 股票永续）",
        "help": "欧易 OKX 本机直连不通，此为平替源。白名单在「自选行情」页加。",
        "fields": [
            {"key": "enabled", "t": "bool", "d": True, "label": "启用"},
            {"key": "interval_minutes", "t": "int", "d": 15, "lo": 0, "hi": 1440,
             "label": "抓价周期(分钟)", "help": "0 = 关闭"},
            {"key": "alert_threshold_pct", "t": "float", "d": 3, "lo": 0, "hi": 100,
             "label": "24h 异动阈值(%)", "help": "超此值推微信"},
            {"key": "alert_cooldown_hours", "t": "int", "d": 6, "lo": 1, "hi": 720,
             "label": "同向推送冷却(小时)"},
            {"key": "link_enabled", "t": "bool", "d": True,
             "label": "计算「币↔股」关联"},
            {"key": "link_refresh_hours", "t": "int", "d": 6, "lo": 0, "hi": 720,
             "label": "关联重算周期(小时)", "help": "0 = 关闭；与抓价周期解耦"},
            {"key": "link_lookback_days", "t": "int", "d": 45, "lo": 10, "hi": 500,
             "label": "关联回看天数", "adv": True,
             "help": "两侧各取多少根日K。样本受合约上市时长限制，短合约取不满"},
            {"key": "link_min_overlap", "t": "int", "d": 9, "lo": 3, "hi": 200,
             "label": "最少重叠交易日", "adv": True,
             "help": "低于此值不算相关系数（保证收益样本 ≥ 8）"},
            {"key": "link_alert_dev_pct", "t": "float", "d": 5, "lo": 0, "hi": 100,
             "label": "比价偏离告警(%)", "help": "0 = 关闭。偏离中位比价超此值推微信"},
            {"key": "link_alert_cooldown_hours", "t": "int", "d": 12, "lo": 1, "hi": 720,
             "label": "比价告警冷却(小时)", "adv": True},
        ],
    },
    # ---------------- 板块 ----------------
    "sector": {
        "label": "🔄 板块轮动",
        "help": "东财口径全市场板块（行业+概念+地域）。",
        "fields": [
            {"key": "enabled", "t": "bool", "d": True, "label": "启用"},
            {"key": "interval_minutes", "t": "int", "d": 5, "lo": 0, "hi": 1440,
             "label": "盘中采样周期(分钟)", "help": "0 = 关闭盘中监控"},
            {"key": "alert_notify", "t": "bool", "d": True,
             "label": "盘中异动推微信",
             "help": "急拉/涨停骤增预警。盘后轮动预警不受此开关控制"},
        ],
    },
    # ---------------- 告警 / 日历 / 量能 / 提款 ----------------
    "alerts": {
        "label": "⚠️ 解禁/增发告警",
        "help": "A 股口径（港股不适用），数据源东财。",
        "fields": [
            {"key": "enabled", "t": "bool", "d": True, "label": "启用"},
            {"key": "days", "t": "int", "d": 14, "lo": 1, "hi": 90,
             "label": "提前天数", "help": "窗口内的新事件推微信，同事件只推一次"},
        ],
    },
    "calendar": {
        "label": "📅 财经日历提醒",
        "help": "非农/交割/四巫/LPR 本地推算，FOMC/CPI 需手动补录。",
        "fields": [
            {"key": "enabled", "t": "bool", "d": True, "label": "启用"},
        ],
    },
    "volume": {
        "label": "📊 盘中大盘量能",
        "help": "三大指数量比（行情 f49）采样监控。",
        "fields": [
            {"key": "enabled", "t": "bool", "d": True, "label": "启用"},
            {"key": "interval_minutes", "t": "int", "d": 5, "lo": 0, "hi": 1440,
             "label": "采样周期(分钟)", "help": "0 = 关闭"},
            {"key": "notify", "t": "bool", "d": True,
             "label": "放量/缩量翻转推微信",
             "help": "连续两次采样确认才推；每天最多 4 条、同方向 45 分钟冷却"},
        ],
    },
    "withdrawal": {
        "label": "🏧 提款计划检查",
        "fields": [
            {"key": "enabled", "t": "bool", "d": True, "label": "启用",
             "help": "达标/临期里程碑推微信"},
        ],
    },
    # ---------------- 模拟交易 ----------------
    "paper": {
        "label": "🧪 模拟交易",
        "help": "盘中**按固定节奏反复判断**（默认每 30 分钟一轮）→ 免 LLM 止损 → LLM 重新决策 "
                "→ 收盘后 5 交易日结算 → 反思沉淀。**会产生 LLM 费用**：每轮每只票 2 次调用，"
                "频率×自选股数就是每天的调用量。只想看止损就开「仅判持仓」。"
                "模拟成交遵守真实交易规则：A 股 T+1（当天买的当天不能卖）、港股与货币/债券/"
                "黄金/跨境 ETF 为 T+0、科创板买入最少 200 股（主板 100 股整手）。",
        "fields": [
            {"key": "enabled", "t": "bool", "d": True, "label": "启用",
             "help": "总开关。关掉后后台线程轮内直接跳过"},
            {"key": "interval_minutes", "t": "int", "d": 30, "lo": 1, "hi": 240,
             "label": "盘中判断间隔(分钟)",
             "help": "每隔多久判断一次。30=半点判断一次；5 分钟会很频繁（费用线性上涨），"
                     "且自选股多时单轮可能跑不完上一次"},
            {"key": "start_time", "t": "time", "d": "09:35", "label": "盘中开始判断",
             "help": "早于开盘 5 分钟，避开开盘剧烈波动那几分钟的假信号"},
            {"key": "end_time", "t": "time", "d": "14:40", "label": "盘中最后判断",
             "help": "留 20 分钟尾盘不再开新仓；收盘后的平仓交给结算流程"},
            {"key": "latest_trade_time", "t": "time", "d": "15:55",
             "label": "轮次最后允许成交时刻",
             "help": "粗闸，防止整个判断轮次在傍晚白跑。真正的闸门是**每只票自己的交易"
                     "时段**（A 股 15:00 收市、港股 16:00 收市），这个值只是上限，"
                     "取 15:55 是为了不挡掉港股 15:05~16:00 的正常交易。结算复盘不受此限"},
            {"key": "quote_max_age_min", "t": "int", "d": 0, "lo": 0, "hi": 120,
             "label": "行情快照最长时间(分钟)", "adv": True,
             "help": "0 = 不额外限制。只认「快照时间不是今天就拒绝成交」这条硬规则。"
                     "想更严可以设 15：盘中快照超过 15 分钟没更新也拒绝成交"},
            {"key": "stop_loss_max_pct", "t": "float", "d": 5, "lo": 1, "hi": 30,
             "label": "止损宽度上限(%)",
             "help": "LLM 定的止损线不得比这更宽。取的是「LLM 值」与「本值」的较大者，"
                     "所以只会把过宽的止损收紧，不会把过窄的放宽。防止它给个 -15% "
                     "这种形同虚设的止损"},
            {"key": "judge_holdings_only", "t": "bool", "d": False,
             "label": "每轮只判已持仓",
             "help": "开启后每轮只重新判断持仓股（最省，止损+止盈照常）；"
                     "关闭则每轮把全部自选股重判一遍（会发现新买点，但费用高）"},
            {"key": "tplus0_extra", "t": "str", "d": "", "label": "强制 T+0（覆盖）",
             "help": "逗号分隔，匹配代码或名称子串。自动判定：港股=T+0；"
                     "货币/债券/黄金/商品/跨境(QDII) ETF 与 LOF=T+0；其余 A 股=T+1。"
                     "判错了在这里强制纠正。例：518880,511880"},
            {"key": "tplus1_extra", "t": "str", "d": "", "label": "强制 T+1（覆盖）",
             "help": "同上，优先级低于 T+0 覆盖。例：某只跨境 ETF 想按 T+1 处理"},
            {"key": "fees.a_commission_rate", "t": "float", "d": 0.00025,
             "lo": 0, "hi": 0.01, "step": 0.00005, "label": "佣金费率", "adv": True,
             "help": "A 股/场内基金佣金费率，万2.5 = 0.00025。双向收取"},
            {"key": "fees.a_commission_min", "t": "float", "d": 5, "lo": 0, "hi": 50,
             "label": "佣金最低(元)", "adv": True,
             "help": "单笔最低佣金。这条对小额高频影响最大：5 元对 2000 元的单子"
                     "就是 0.25% 成本，相当于凭空多赚 0.25%"},
            {"key": "fees.a_stamp_duty", "t": "float", "d": 0.0005, "lo": 0, "hi": 0.01,
             "step": 0.0001, "label": "印花税费率", "adv": True,
             "help": "A 股印花税 0.05%，**仅卖出**收（ETF/LOF 免）"},
            {"key": "fees.transfer_fee", "t": "float", "d": 0.00001, "lo": 0, "hi": 0.001,
             "step": 0.00001, "label": "过户费率", "adv": True, "help": "0.001%，双向"},
            {"key": "fees.hk_commission_rate", "t": "float", "d": 0.0025, "lo": 0, "hi": 0.02,
             "step": 0.0005, "label": "港股佣金费率", "adv": True, "help": "默认万2.5"},
            {"key": "fees.hk_commission_min", "t": "float", "d": 5, "lo": 0, "hi": 100,
             "label": "港股佣金最低(元)", "adv": True},
            {"key": "fees.hk_stamp_duty", "t": "float", "d": 0.001, "lo": 0, "hi": 0.01,
             "step": 0.0005, "label": "港股印花税", "adv": True,
             "help": "0.1%，**买卖双向都收**（与 A 股只收卖出不同）"},
            {"key": "fees.hk_levy", "t": "float", "d": 0.000027, "lo": 0, "hi": 0.001,
             "step": 0.000001, "label": "港股交易征费", "adv": True},
            {"key": "fees.hk_tx_fee", "t": "float", "d": 0.0000565, "lo": 0, "hi": 0.001,
             "step": 0.000005, "label": "港股交易费", "adv": True},
            {"key": "fees.hk_ccass", "t": "float", "d": 0.00002, "lo": 0, "hi": 0.001,
             "step": 0.00001, "label": "港股结算费率", "adv": True},
            {"key": "fees.hk_settle_min", "t": "float", "d": 2, "lo": 0, "hi": 50,
             "label": "港股结算费最低(元)", "adv": True},
            {"key": "fees.hk_settle_max", "t": "float", "d": 100, "lo": 0, "hi": 500,
             "label": "港股结算费最高(元)", "adv": True},
            {"key": "initial_cash", "t": "float", "d": 100000, "lo": 1000, "hi": 1e9,
             "label": "初始虚拟资金(元)"},
            {"key": "holding_days", "t": "int", "d": 5, "lo": 1, "hi": 60,
             "label": "持有几个交易日平仓"},
            {"key": "max_position_pct", "t": "float", "d": 25, "lo": 1, "hi": 100,
             "label": "单票市值上限(%)", "help": "占总资产比例"},
            {"key": "max_positions", "t": "int", "d": 0, "lo": 0, "hi": 50,
             "label": "同时持仓只数上限",
             "help": "0 = 不限。设成 N 后，**持仓股票数不会超过 N 只**：满了就不再开新仓"
                     "（新标的直接不给 LLM 判，省调用费），但卖出不受限制 —— "
                     "所以要先卖出一只才能买入新的。加仓已持仓的票不算新名额"},
            {"key": "sleep_seconds", "t": "int", "d": 3, "lo": 0, "hi": 60,
             "label": "逐股决策间隔(秒)", "adv": True, "help": "防 LLM 网关限流"},
        ],
    },
    # ---------------- LLM ----------------
    "llm": {
        "label": "🤖 Claude API",
        "help": "用于 AI 提款建议、每日报告、模拟交易决策。留空则读环境变量 "
                "ANTHROPIC_API_KEY。",
        "fields": [
            {"key": "enabled", "t": "bool", "d": True, "label": "启用"},
            {"key": "api_key", "t": "password", "d": "", "secret": True,
             "label": "API key", "help": "sk-ant-… 存本机 config.yaml，注意保管"},
            {"key": "base_url", "t": "url", "d": "", "label": "base_url",
             "help": "留空 = Anthropic 官方端点；用代理/网关在此填"},
            {"key": "model", "t": "str", "d": "claude-opus-5", "label": "模型名"},
            {"key": "auto_advice", "t": "bool", "d": True,
             "label": "盘后自动补一条提款建议",
             "help": "每日每计划至多一次"},
        ],
    },
    # ---------------- 通知 ----------------
    "notify": {
        "label": "🔔 通知渠道",
        "help": "微信走腾讯官方 iLink Bot（凭据存云库，不在 yaml 里）。",
        "fields": [
            {"key": "wx.enabled", "t": "bool", "d": True, "label": "微信 启用"},
            {"key": "email.enabled", "t": "bool", "d": False, "label": "邮件 启用"},
            {"key": "email.smtp_host", "t": "str", "d": "",
             "label": "SMTP 服务器", "help": "留空按发件邮箱后缀自动推断"
                                                "（qq→smtp.qq.com:465、163→smtp.163.com:465）"},
            {"key": "email.smtp_port", "t": "int", "d": 0, "lo": 0, "hi": 65535,
             "label": "SMTP 端口", "help": "0 = 用预设"},
            {"key": "email.from_addr", "t": "str", "d": "", "label": "发件邮箱"},
            {"key": "email.to_addr", "t": "str", "d": "", "label": "接收邮箱"},
            {"key": "email.auth_code", "t": "password", "d": "", "secret": True,
             "label": "邮箱授权码",
             "help": "QQ/163 需在邮箱设置里开 SMTP 后生成，**不是登录密码**"},
        ],
    },
    # ---------------- 定时任务时间点 ----------------
    "schedule": {
        "label": "⏰ 定时任务时间点",
        "help": "本机时间，周一~周五（节假日不判）。改完 1 分钟内生效，无需重启。",
        "fields": [
            {"key": "premarket_report.enabled", "t": "bool", "d": True, "label": "盘前简报 启用"},
            {"key": "premarket_report.time", "t": "time", "d": "08:23", "label": "盘前简报 时间"},
            {"key": "postmarket_report.enabled", "t": "bool", "d": True, "label": "盘后复盘 启用"},
            {"key": "postmarket_report.time", "t": "time", "d": "15:57", "label": "盘后复盘 时间"},
            {"key": "withdrawal.check_time", "t": "time", "d": "15:10", "label": "提款检查"},
            {"key": "paper.decide_time", "t": "time", "d": "15:35", "label": "模拟·兜底补跑",
             "help": "盘中轮次（频率在「🧪 模拟交易」里配）之外的保险：过了这个点若当天一轮都"
                     "没跑过，补判一次，避免服务中途才启动导致整天被跳过"},
            {"key": "paper.settle_time", "t": "time", "d": "16:10", "label": "模拟·结算",
             "help": "收盘后结算+复盘。错开盘中轮次，给当日日K落库留时间"},
            {"key": "sector.snapshot_time", "t": "time", "d": "15:10", "label": "板块快照"},
            {"key": "alerts.check_time", "t": "time", "d": "08:30", "label": "事件告警扫描"},
            {"key": "calendar.start_time", "t": "time", "d": "08:00", "label": "日历提醒 窗口起"},
            {"key": "calendar.end_time", "t": "time", "d": "12:00", "label": "日历提醒 窗口止"},
        ],
    },
}


def field_index() -> dict[tuple[str, str], dict]:
    """(段, 键) -> 字段定义。校验时用。"""
    return {(sec, f["key"]): f
            for sec, meta in SCHEMAS.items() for f in meta["fields"]}


def defaults() -> dict:
    """模式给出的默认值（与各模块 DEFAULT_* 一起构成回退链的第一环）。"""
    out: dict = {}
    for sec, meta in SCHEMAS.items():
        d: dict = {}
        for f in meta["fields"]:
            cur = d
            parts = f["key"].split(".")
            for p in parts[:-1]:
                cur = cur.setdefault(p, {})
            cur[parts[-1]] = f["d"]
        out[sec] = d
    return out


def coerce(sec: str, key: str, raw, field: dict):
    """按字段定义把页面传来的值转成正确类型；不合法抛 ValueError（→ API 400）。

    secret 字段传空串/None = **不修改**（避免前端把掩码当新值写回去，把真值弄丢）。
    返回 (value, skip) —— skip=True 表示这一项不用写。
    """
    t = field.get("t", "str")
    if field.get("secret") and (raw is None or str(raw).strip() == ""
                                or str(raw).strip().startswith("••")):
        return None, True
    if t == "bool":
        if isinstance(raw, bool):
            return raw, False
        return str(raw).strip().lower() in ("1", "true", "yes", "on"), False
    if t in ("int", "float"):
        try:
            val = int(raw) if t == "int" else float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{sec}.{key} 需要{'整数' if t == 'int' else '数字'}，"
                             f"收到 {raw!r}")
        lo, hi = field.get("lo"), field.get("hi")
        if lo is not None and val < lo:
            raise ValueError(f"{sec}.{key} 不能小于 {lo}")
        if hi is not None and val > hi:
            raise ValueError(f"{sec}.{key} 不能大于 {hi}")
        return val, False
    if t == "list":
        if isinstance(raw, list):
            items = [str(x).strip() for x in raw if str(x).strip()]
        else:
            items = [x.strip() for x in str(raw).split(",") if x.strip()]
        return items, False
    return ("" if raw is None else str(raw)), False


def schema_json() -> list[dict]:
    """给前端的模式（去掉内部字段）。"""
    out = []
    for sec, meta in SCHEMAS.items():
        out.append({
            "section": sec,
            "label": meta.get("label", sec),
            "help": meta.get("help", ""),
            "fields": [{
                "key": f["key"], "type": f.get("t", "str"),
                "label": f.get("label", f["key"]),
                "help": f.get("help", ""),
                "default": f.get("d"), "min": f.get("lo"), "max": f.get("hi"),
                "secret": bool(f.get("secret")), "adv": bool(f.get("adv")),
            } for f in meta["fields"]],
        })
    return out
