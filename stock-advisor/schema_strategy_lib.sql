-- 量化策略库：原始文章（聚宽社区等）落库 + 防重复爬取
-- 设计要点（2026-09-30）：
--   1) 「防重复爬取」靠 **url_hash 唯一键**，不是靠「先查再插」——
--      那是竞态（两个进程同时插会都成功）。唯一键让重复插入直接失败。
--   2) **content_md 与 content_text 分离**：原文 markdown 存一份（保真），
--      剥掉 HTML/图片/裸 URL 后的纯文本另存一份，专供 LLM 抽取用。
--      直接把带 `![](https://...)` 的原文喂 LLM 是浪费 token 也污染输出。
--   3) **crawl_state 与 fetch_time 解耦**：状态机用枚举字符串（人可读、
--      便于 SQL 里筛「卡在哪一步」），时间戳单独存（用于算退避间隔）。
--   4) 每篇文章的原文与抽取结果**分表存**（sa_strategy_article / sa_strategy_digest），
--      这样重新抽取不会动原文，重爬原文也不会丢抽取结果。

CREATE TABLE IF NOT EXISTS sa_crawl_queue (
    -- ---------------- 防重复爬取的核心 ----------------
    -- URL 规范化后的 sha1。同一个 URL 无论从列表页、搜索还是推荐哪条路来，
    -- 撞到同一个 hash 就不会再爬第二遍。
    url_hash      CHAR(40) PRIMARY KEY,
    url           TEXT        NOT NULL,
    site          VARCHAR(32) NOT NULL DEFAULT '',
    -- 列表页给的排序依据（热度/最新），用来挑「先爬哪个」
    rank_hint     INTEGER     NOT NULL DEFAULT 0,
    title_hint    VARCHAR(255) NOT NULL DEFAULT '',
    -- pending / fetching / fetched / failed / skipped
    crawl_state   VARCHAR(16) NOT NULL DEFAULT 'pending',
    fetch_time    TIMESTAMPTZ,           -- 最近一次尝试（成功或失败都记）
    next_retry_at TIMESTAMPTZ,           -- 退避：失败后多久再试
    retry_count   INTEGER     NOT NULL DEFAULT 0,
    last_error    TEXT        NOT NULL DEFAULT '',
    note          TEXT        NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sa_crawl_state
    ON sa_crawl_queue (crawl_state, rank_hint DESC);
CREATE INDEX IF NOT EXISTS idx_sa_crawl_next
    ON sa_crawl_queue (crawl_state, next_retry_at);
-- 同一篇文章可能被多个入口发现（列表页 + 详情页相关推荐）。
-- 这个索引用于「这条我已经见过了」的快速判断。
CREATE INDEX IF NOT EXISTS idx_sa_crawl_url ON sa_crawl_queue (url);

CREATE TABLE IF NOT EXISTS sa_strategy_article (
    post_id       VARCHAR(64) PRIMARY KEY,   -- 站点内的稳定 ID（聚宽=uniqueKey）
    -- 站点每次请求重新签发的那个 id（聚宽的 postId）。**不能做主键** ——
    -- 实测同一篇帖两次请求拿到的 postId 完全不同。留着只为了出问题时
    -- 能对照原始响应看。
    src_post_id   VARCHAR(64) NOT NULL DEFAULT '',
    site          VARCHAR(32) NOT NULL DEFAULT 'joinquant',
    url_hash      CHAR(40) NOT NULL REFERENCES sa_crawl_queue(url_hash) ON DELETE CASCADE,
    url           TEXT        NOT NULL,

    title         VARCHAR(512) NOT NULL DEFAULT '',
    author        VARCHAR(128) NOT NULL DEFAULT '',
    author_id     VARCHAR(64)  NOT NULL DEFAULT '',

    -- ---- 正文三件套 ----
    -- 原文 markdown：保真存档，不动它
    content_md    TEXT        NOT NULL DEFAULT '',
    -- 剥掉 HTML/图片/裸 URL/代码块后的纯文本，**专供 LLM 抽取**
    content_text  TEXT        NOT NULL DEFAULT '',
    -- 纯文本的 sha1：判断「内容是否变了」。没变就不必重新抽取（省 LLM 调用）
    content_hash  CHAR(40)    NOT NULL DEFAULT '',

    tags          JSONB       NOT NULL DEFAULT '[]'::jsonb,
    -- ---- 站点自带的指标 ----
    view_count    INTEGER     NOT NULL DEFAULT 0,
    like_count    INTEGER     NOT NULL DEFAULT 0,
    reply_count   INTEGER     NOT NULL DEFAULT 0,
    collect_count INTEGER     NOT NULL DEFAULT 0,
    clone_count   INTEGER     NOT NULL DEFAULT 0,   -- 被克隆次数 = 实用度代理指标
    published_at  TIMESTAMPTZ,
    updated_at_s  TIMESTAMPTZ,           -- 站点的最后修改时间
    last_active_at TIMESTAMPTZ,          -- 最后有人回复的时间

    is_strategy   BOOLEAN     NOT NULL DEFAULT FALSE,  -- 是否「策略类」文章
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 文章可能被编辑过；变了就置 false 以便重新抽取
    needs_reextract BOOLEAN   NOT NULL DEFAULT FALSE,
    UNIQUE (site, post_id)
);
CREATE INDEX IF NOT EXISTS idx_sa_article_hash ON sa_strategy_article (content_hash);
CREATE INDEX IF NOT EXISTS idx_sa_article_strategy
    ON sa_strategy_article (is_strategy, published_at DESC);

-- ------------------------------------------------------------------
-- LLM 抽取的结构化结果（与原文分表：重新抽取不丢原文，重爬原文不丢抽取）
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sa_strategy_digest (
    article_id    VARCHAR(64) PRIMARY KEY
                  REFERENCES sa_strategy_article(post_id) ON DELETE CASCADE,

    -- ---- 一句话概括 ----
    title_zh      VARCHAR(512) NOT NULL DEFAULT '',   -- LLM 归纳的策略名
    summary       TEXT        NOT NULL DEFAULT '',   -- 2~4 句讲清它干什么
    strategy_type VARCHAR(32)  NOT NULL DEFAULT '',   -- 多因子/择时/轮动/网格/打板…
    -- ---- 实现步骤（LLM 拆出来的可执行步骤，按顺序）----
    steps         JSONB       NOT NULL DEFAULT '[]'::jsonb,
    -- ---- 标的与参数 ----
    universe      JSONB       NOT NULL DEFAULT '[]'::jsonb,  -- 选股范围/池子
    params        JSONB       NOT NULL DEFAULT '{}'::jsonb,  -- 周期/阈值/权重/仓位
    -- ---- 作者自述的运行效果（**关键：标注这是自述还是推断**）----
    perf_claimed  JSONB       NOT NULL DEFAULT '{}'::jsonb,  -- 年化/回撤/胜率/夏普
    perf_verified VARCHAR(16) NOT NULL DEFAULT '',  -- claimed / verified / none
    backtest_period TEXT      NOT NULL DEFAULT '',  -- 回测区间
    -- ---- 适用场景 ----
    applicable    JSONB       NOT NULL DEFAULT '[]'::jsonb,  -- 适用市场/市况/资金体量
    unsuitable    JSONB       NOT NULL DEFAULT '[]'::jsonb,  -- 明确不适用的场景
    risk_notes    JSONB       NOT NULL DEFAULT '[]'::jsonb,  -- 风险与坑
    dependencies  JSONB       NOT NULL DEFAULT '[]'::jsonb,  -- 依赖的数据/库/环境
    -- ---- 可落地性评估（我们自己打的分，不是文章自述）----
    -- 能不能搬进本系统的模拟盘：1~5
    portable_score SMALLINT   NOT NULL DEFAULT 0,
    portable_why  TEXT        NOT NULL DEFAULT '',
    -- ---- 抽取过程的元信息（可追溯：哪次抽取、用的什么模型、多少 token）----
    -- uncertainty / needs_research / research_hint 这三列是**抽取器自己的
    -- 产出，不是文章的**。它们回答的是「这份抽取可信吗、还缺什么要补查」。
    -- 没有它们，被脱敏的帖子抽出来的「实现步骤」看起来和完整源码抽出来的
    -- 一模一样，我就分不出哪个能拿去跑、哪个只是照着空壳编出来的。
    uncertainty   TEXT        NOT NULL DEFAULT '',
    needs_research BOOLEAN    NOT NULL DEFAULT FALSE,
    research_hint TEXT        NOT NULL DEFAULT '',
    extract_model VARCHAR(128) NOT NULL DEFAULT '',
    extract_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    extract_cost  JSONB       NOT NULL DEFAULT '{}'::jsonb,
    -- 抽取质量自评：有几个必填字段没抽到
    completeness  SMALLINT    NOT NULL DEFAULT 0,
    raw_response  TEXT        NOT NULL DEFAULT ''
);

-- ------------------------------------------------------------------
-- 策略源码（2026-09-30）
-- ------------------------------------------------------------------
-- 为什么单独一张表而不是塞进 sa_strategy_article 的 JSONB 字段：
-- 源码动辄几百上千行，塞 JSONB 之后每次列表查询都要读它；而且源码需要
-- 「按语言/行数检索」和「重新解析」，独立表更合适。
--
-- **origin 一定要记**：实测社区帖里源码有两个来源 ——
--   body  = 原文正文里的 ``` 代码块
--   reply = 评论区里的（你提的那点，不少帖子源码只在评论区）
-- 两者混在一起会分不清代码到底可不可信（作者贴的 vs 别人贴的）。
CREATE TABLE IF NOT EXISTS sa_strategy_source (
    post_id       VARCHAR(64) PRIMARY KEY
                  REFERENCES sa_strategy_article(post_id) ON DELETE CASCADE,
    lang          VARCHAR(16)  NOT NULL DEFAULT '',
    code          TEXT         NOT NULL DEFAULT '',
    lines         INTEGER      NOT NULL DEFAULT 0,
    -- body / reply
    origin        VARCHAR(8)   NOT NULL DEFAULT 'body',
    origin_author VARCHAR(128) NOT NULL DEFAULT '',   -- reply 时是谁贴的
    origin_reply_id VARCHAR(64) NOT NULL DEFAULT '',
    -- **脱敏标记**：社区里不少「复现年化XXX%」的帖，代码块里核心函数体
    -- 是 `...`（Python Ellipsis，语法合法但逻辑是空的）—— 作者防抄袭。
    -- 实测《实测复现年化526%的低吸连阳首板策略》整篇就是这么写的。
    -- 照样入库（变量名和选股思路有参考价值），但必须打标记，否则后面
    -- LLM 抽「实现步骤」时会照着空壳脑补出一套不存在的策略。
    redacted      BOOLEAN     NOT NULL DEFAULT FALSE,
    stub_sites    INTEGER     NOT NULL DEFAULT 0,
    stub_reasons  JSONB       NOT NULL DEFAULT '[]'::jsonb,
    -- 能不能直接跑（这是**一等公民字段**，不是等真跑挂了才知道）。
    -- 实测 5 篇有源码的策略，结论很扎心：
    --   2 篇语法正确能跑
    --   1 篇正文有 5 个代码块，拼接后第 4 块是个只有 return 的函数片段
    --   1 篇原文里是「小于号 被 HTML 转义后与等号之间多一个空格」
    --   1 篇整个代码块其实是中文说明，被误判成了代码
    -- 不把这件事量化出来，用户就只能一份份试跑才知道哪些能用。
    -- 取值：ok / syntax_error / fragment / empty
    syntax_state  VARCHAR(16)  NOT NULL DEFAULT 'ok',
    syntax_ok     BOOLEAN     NOT NULL DEFAULT FALSE,
    syntax_detail TEXT        NOT NULL DEFAULT '',
    -- 抽取时的告警（拼接后跑不了、中文说明被排除等）
    warnings      JSONB       NOT NULL DEFAULT '[]'::jsonb,
    -- **逐块的结构化记录**。为什么必须有：`code` 是把正文里所有代码块
    -- 拼起来的一份完整源码（作者常把一个策略拆成 initialize / 因子 /
    -- 选股 / 调仓 几段贴 —— 实测《多因子LightGBM选股策略》有 9 块，
    -- 单块都只有 12 行，只留一块等于把选股逻辑整段丢掉）。
    -- 拼接后看不出边界，回填二次抽取、单独喂 LLM、定位某一块都靠它。
    blocks         JSONB       NOT NULL DEFAULT '[]'::jsonb,
    n_blocks       INTEGER     NOT NULL DEFAULT 1,
    -- 其它非策略的代码块（回测日志/SQL/HTML），留着但不参与抽取
    other_blocks  JSONB        NOT NULL DEFAULT '[]'::jsonb,
    extracted_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sa_src_lang ON sa_strategy_source (lang, lines DESC);

-- ------------------------------------------------------------------
-- 演进用的 ALTER（2026-09-30）
-- ------------------------------------------------------------------
-- **为什么需要这一段**：上面全是 CREATE TABLE IF NOT EXISTS，而
-- IF NOT EXISTS 只判断「表在不在」，**不会给已存在的表补新列**。
-- 我加 redacted/stub_sites 时就踩了：表早就建好了，DDL 跑一遍「全部通过」，
-- 但运行时 psycopg2 报 column "redacted" does not exist。
-- 这跟之前 init_db 里 DROP TABLE IF EXISTS 顶崩启动是同一类问题：
-- 「幂等的 DDL」不等于「能演进的 DDL」。
--
-- 所以规矩是：**加列 = 在这里加一条 ALTER ... ADD COLUMN IF NOT EXISTS**，
-- 跟 CREATE 写在同一个文件里，保证 check_ddl 跑一遍就知道库对不对。
ALTER TABLE sa_strategy_source
    ADD COLUMN IF NOT EXISTS redacted    BOOLEAN     NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS stub_sites  INTEGER     NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS stub_reasons JSONB     NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS blocks      JSONB       NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS n_blocks    INTEGER     NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS syntax_state VARCHAR(16) NOT NULL DEFAULT 'ok',
    ADD COLUMN IF NOT EXISTS syntax_ok    BOOLEAN     NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS syntax_detail TEXT       NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS warnings     JSONB       NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE sa_strategy_digest
    ADD COLUMN IF NOT EXISTS uncertainty    TEXT     NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS needs_research BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS research_hint  TEXT     NOT NULL DEFAULT '';
ALTER TABLE sa_strategy_article
    ADD COLUMN IF NOT EXISTS src_post_id VARCHAR(64) NOT NULL DEFAULT '';

-- ------------------------------------------------------------------
-- 抽取任务：把待抽取的文章排成队，逐条跑，成功/失败都记
-- （否则失败的文章会每轮重试，浪费 LLM 调用）
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sa_strategy_extract_queue (
    article_id  VARCHAR(64) PRIMARY KEY
                REFERENCES sa_strategy_article(post_id) ON DELETE CASCADE,
    state       VARCHAR(16) NOT NULL DEFAULT 'pending',  -- pending/done/failed/skip
    try_count   INTEGER     NOT NULL DEFAULT 0,
    next_at     TIMESTAMPTZ,             -- 退避用
    last_error  TEXT        NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sa_eq_state ON sa_strategy_extract_queue (state, next_at);

-- ------------------------------------------------------------------
-- 评论区（2026-09-30）
-- ------------------------------------------------------------------
-- 为什么单独一张表而不是塞进 article 的 JSONB：
-- **评论区的信息密度往往比正文高**。实测「聚宽新手指南」正文 825 字没代码，
-- 但下面 7268 条回复里全是实操问答；不少「源码在哪」的答案是作者自己在
-- 评论里贴的。第一版我把 replies 拼成个列表就扔了（拼完没落库），
-- 等于把最该看的那部分丢了。
--
-- has_code 单独记一列：抽取时优先在评论区找代码块，但要能筛出
-- 「哪条评论是贴代码的」给 LLM 当重点，而不是把 7000 条全塞进去。
CREATE TABLE IF NOT EXISTS sa_strategy_reply (
    reply_id     VARCHAR(64) PRIMARY KEY,
    article_id   VARCHAR(64) NOT NULL
                 REFERENCES sa_strategy_article(post_id) ON DELETE CASCADE,
    author       VARCHAR(128) NOT NULL DEFAULT '',
    content      TEXT        NOT NULL DEFAULT '',
    content_len  INTEGER     NOT NULL DEFAULT 0,
    has_code     BOOLEAN     NOT NULL DEFAULT FALSE,
    n_code_blocks INTEGER    NOT NULL DEFAULT 0,
    is_author    BOOLEAN     NOT NULL DEFAULT FALSE,  -- 作者自己回的，权重更高
    backtest_id  VARCHAR(64) NOT NULL DEFAULT '',
    backtest_name TEXT       NOT NULL DEFAULT '',
    add_time     TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sa_reply_article ON sa_strategy_reply (article_id);
CREATE INDEX IF NOT EXISTS idx_sa_reply_code ON sa_strategy_reply (article_id, has_code);

-- ------------------------------------------------------------------
-- 本地行情数据（供本地回测/验证用，2026-09-30）
-- ------------------------------------------------------------------
-- 为什么必须落库：一个 3 年回测、5 只标的、调 20 组参数 = 300 次取数，
-- 直接打行情接口必被风控（东财 push2 我今天已被封过一次，6 个镜像主机全挂）。
-- UNIQUE(code, trade_date) 同时解决「防重复拉取」：K 线只追加，
-- 重复拉到同一天只是 ON CONFLICT 幂等覆盖，不会产生重复行。
CREATE TABLE IF NOT EXISTS sa_market_kline (
    code         VARCHAR(8)  NOT NULL,
    trade_date   DATE        NOT NULL,
    open         NUMERIC(14,4),
    high         NUMERIC(14,4),
    low          NUMERIC(14,4),
    close        NUMERIC(14,4),
    volume       NUMERIC(20,2),   -- 手
    amount       NUMERIC(20,2),   -- 元；腾讯源没有，留 NULL 不填 0
    pct          NUMERIC(8,4),    -- 当日涨跌幅 %
    turnover_rate NUMERIC(8,4),   -- 换手率 %；腾讯源没有
    source       VARCHAR(8) NOT NULL DEFAULT '',
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_sa_kline_date ON sa_market_kline (trade_date);

-- ------------------------------------------------------------------
-- 策略定义（从文章抽出来的，或自己写的）
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sa_strategy_def (
    id             BIGSERIAL PRIMARY KEY,
    name           VARCHAR(160) NOT NULL,
    strategy_type  VARCHAR(32)  NOT NULL DEFAULT '',  -- 多因子/择时/轮动/网格/打板…
    -- 来源（可空：自己写的不挂在任何文章上）
    article_id     VARCHAR(64) REFERENCES sa_strategy_article(post_id) ON DELETE SET NULL,
    -- 可执行形态：'rules'（规则化，能进模拟盘/策略对照）
    --           'backtest'（回测脚本，需 backtest.py 支持）
    --           'idea'（只有思路，暂不可跑）
    runnable       VARCHAR(16)  NOT NULL DEFAULT 'idea',
    -- 规则化策略的参数（直接可映射到 sa_strategies 的 6 种 kind）
    params         JSONB        NOT NULL DEFAULT '{}'::jsonb,
    universe       JSONB        NOT NULL DEFAULT '[]'::jsonb,
    -- 回测脚本（Python，runnable='backtest' 时用）
    code           TEXT         NOT NULL DEFAULT '',
    -- 我们自己打的分：能否搬进本系统模拟盘 1~5
    portable_score SMALLINT     NOT NULL DEFAULT 0,
    note           TEXT         NOT NULL DEFAULT '',
    enabled        BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sa_sd_type ON sa_strategy_def (strategy_type, enabled);

-- ------------------------------------------------------------------
-- 回测运行 + 逐日净值（「验证图」的数据源）
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sa_backtest_run (
    id           BIGSERIAL PRIMARY KEY,
    strategy_id  BIGINT REFERENCES sa_strategy_def(id) ON DELETE CASCADE,
    name         VARCHAR(160) NOT NULL DEFAULT '',
    -- 回测口径（记清楚，否则结果没法比）
    start_date   DATE NOT NULL,
    end_date     DATE NOT NULL,
    universe     JSONB NOT NULL DEFAULT '[]'::jsonb,
    params       JSONB NOT NULL DEFAULT '{}'::jsonb,
    init_cash    NUMERIC(14,2) NOT NULL DEFAULT 1000000,
    -- 汇总指标
    total_return NUMERIC(12,4),   -- 区间总收益 %
    annual_return NUMERIC(12,4),  -- 年化 %
    max_drawdown NUMERIC(12,4),   -- 最大回撤 %（正数表示回撤幅度）
    sharpe       NUMERIC(8,4),
    win_rate     NUMERIC(8,4),    -- 胜率 %
    trade_count  INTEGER,
    turnover     NUMERIC(12,4),   -- 换手率
    benchmark    VARCHAR(16) NOT NULL DEFAULT '',  -- 基准代码
    bench_return NUMERIC(12,4),   -- 同期基准收益 %
    excess       NUMERIC(12,4),   -- 超额 = annual_return - bench_return
    error        TEXT NOT NULL DEFAULT '',
    elapsed_ms   INTEGER NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sa_bt_strategy ON sa_backtest_run (strategy_id, created_at DESC);

-- 逐日净值/回撤/持仓 —— 前端的「验证图」直接画这个，不从文章爬图
CREATE TABLE IF NOT EXISTS sa_backtest_daily (
    run_id     BIGINT NOT NULL REFERENCES sa_backtest_run(id) ON DELETE CASCADE,
    trade_date DATE NOT NULL,
    equity     NUMERIC(16,4) NOT NULL,   -- 总资产
    cash       NUMERIC(16,4) NOT NULL,   -- 现金
    position_value NUMERIC(16,4) NOT NULL,
    drawdown   NUMERIC(8,4) NOT NULL DEFAULT 0,   -- 当前回撤 %
    holdings   JSONB NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (run_id, trade_date)
);

-- 抓取与抽取的日志，便于排查「为什么这篇没被处理」
CREATE TABLE IF NOT EXISTS sa_crawl_log (
    id         BIGSERIAL PRIMARY KEY,
    url_hash   CHAR(40) NOT NULL,
    level      VARCHAR(8) NOT NULL DEFAULT 'info',  -- info/warn/error
    stage      VARCHAR(32) NOT NULL DEFAULT '',     -- list/detail/extract/recursion
    message    TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sa_crawl_log_hash
    ON sa_crawl_log (url_hash, created_at DESC);
