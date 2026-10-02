"""ipo_discover.py —— 新股「早期信号」探测（方式3：公告源）。

为什么排期源不够
--------------
`ipo_calendar` 抓的是「已经定下哪天申购」的排期（akshare stock_xgsglb_em，
4033 行，最新申购日 2026-10-19）。但排期**披露得很晚** —— 通常只提前
几个工作日。而打新额度规则是「T-2 日前 20 个交易日日均市值」，
补仓要 20+ 个交易日才爬得满（见 ipo_quota.days_needed_to_reach）。
**等排期出来再补仓，这一轮必然来不及。**

所以需要比排期**早得多**的信号。本模块抓三个层次（都实测过）：

    层 1  IPO 辅导工作进展报告     -> 最早（受理前几个月到 1-2 年）
         来源 ak.stock_notice_report，实测 2407 条
    层 2  招股说明书 / 注册批复   -> 中期（申购前 1-2 个月）
         来源 巨潮全文检索 API，实测 94100 条命中
    层 3  上市委审议通过          -> 晚期（申购前 2-4 周）
         同一 API，实测 146 条

关键实测结论（2026-10-01）
-------------------------
1. **`ak.stock_ipo_summary_cninfo()` 不可用于发现**：它要传 `symbol`
   （单只股票），不给就返回那一只的历史 IPO 数据（实测传 600030 返回
   2002 年中金的一行）。它是「查单只 IPO 档案」，不是「列全市场新股」。
2. **`ak.stock_new_gh_cninfo()` 已崩**：`ValueError: Length mismatch:
   Expected axis has 0 elements` —— 上游返回空但代码还在 rename 列。
3. **巨潮 API 可直接用**（不需要 token）：
   POST https://www.cninfo.com.cn/new/hisAnnouncement/query
   必需 header：Referer + X-Requested-With: XMLHttpRequest
   实测 '首次公开发行' 94100 条 / '招股说明书' 7825 / '注册批复' 2952
   / '上市委审议' 146，延迟 187-556ms。
4. **`ak.stock_notice_report()` 含「辅导工作进展报告」** —— 这是唯一
   能拿到「还没过会」公司的通道，实测第一条就是
   「关于浙江大农实业股份有限公司首次公开发行股票并上市辅导情况报告」。
5. **东财 search-api 不可用**：HTTP 400（参数格式已变）。

去重与噪声
----------
巨潮全文检索对「首次公开发行」会返回大量**已上市公司**的历史公告
（募投项目结项、参股公司上市等），噪声极高。所以：

- 必须按**公告类型**过滤，不是按关键词命中就收
- 提取股票代码后，只保留「未在 sa_watchlist、且不在已知已上市列表」的
- 同一公司只保留最新的一条（进展报告是重复的）
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "data" / "ipo_discover_state.json"

# ---------------- 配置 ----------------

# 巨潮全文检索关键词 -> (层次, 保留条数, 公告类型白名单)
# 层次：early(辅导/受理) / mid(招股书/注册) / late(上市委/发行)
#
# ⚠️ 巨潮的 searchkey 是**字面量**匹配，不支持正则。实测传
# "同意.*首次公开发行.*注册批复" 返回 0 条（而单搜"注册批复"有 2952 条）。
# 所以关键词必须是能独立命中的短语，复杂的过滤交给 patterns 白名单做。
CNINFO_QUERIES: list[tuple[str, str, int, tuple[str, ...]]] = [
    # (searchkey, 层次, 保留条数, 公告类型白名单)
    #
    # ⚠️ 关键词选错会让整个模块白做。实测 2026-10-01 每个关键词的
    # 「总命中 → 过噪 → 早期信号」密度（pageSize=30，取 4 个时间窗验证）：
    #
    #   关键词                      总命中  过噪  早期  早期占比  结论
    #   招股说明书                     7825    23    23     100%   ★主源
    #   首次公开发行股票申请            1038    27    17      63%   ★★金矿
    #   上市委                         365     9     9     100%   ★
    #   受理                          1101     5     5     100%   ★（要严过滤）
    #   过会                          1624    12     1       8%   ✗ 噪声大
    #   注册批复                      2952     6     1      17%   ✗ 29/30 是再融资
    #   提交注册                        79     0     0       0%   ✗ 全是「提交注册」空泛
    #
    # 「首次公开发行股票申请」是**申购前 1-2 个月**最集中的早期信号
    # （受理→问询→过会→注册都在这个短语下），最初漏了它导致
    # fresh 恒为 0、误以为这个源没有提前发现能力。
    #
    # 层次 early **不靠巨潮搜「辅导」** —— 实测 2026-10-01：搜「上市辅导」
    # 「辅导备案」各有 30 条，但含 IPO 强信号的 **0 条**（巨潮只收录
    # 已受理/已过会公司的公告，辅导期搜不到）。辅导期唯一可用通道是
    # akshare 的 stock_notice_report（见 fetch_ipo_notices，默认关闭）。
    #
    # 「受理」这个关键词要严过滤：它会捞到**药品/医疗器械上市许可受理**
    # （实测海思科「创新药新适应症上市许可申请受理通知书」、
    #  翰宇药业「司美格鲁肽注射液上市申请获得受理」），与 IPO 无关。
    ("首次公开发行股票申请", "mid", 200,
     (r"首次公开发行股票", r"首次公开发行.{0,8}(申请|受理|问询|过会|注册)")),
    ("招股说明书", "mid", 200,
     # 必须同时含「首次公开发行」才算 A 股 IPO 招股书。
     # 实测：不加这个限制，「招股说明书」返回的 30 条里 12 条是
     # **H 股**（彤程新材/罗博特科/景旺电子/星环科技…都是「关于刊发
     # H股招股说明书、H股发行价格区间及H股香港公开发售」），
     # 那是港股，跟 A 股打新额度完全无关。
     (r"招股(说明书|意向书).*(首次公开发行|股票上市|北交所上市)"
      r"|首次公开发行.*招股(说明书|意向书)",)),
    ("上市委", "late", 100,
     (r"上市委", r"审议结果", r"符合发行条件", r"上市条件",
      r"会议公告", r"审议意见")),
    ("受理", "early", 100,
     # 只认股票发行受理，排除药品/器械/专利/诉讼受理
     (r"首次公开发行|股票发行|公开发行股票|北交所",)),
    ("注册批复", "mid", 100, (r"注册批复",)),
    ("提交注册", "mid", 100, (r"提交注册",)),
    # late 层（申购前后）留作交叉验证，不产生 fresh
    ("首次公开发行股票发行公告", "late", 150,
     (r"发行公告", r"网上路演", r"申购", r"提示公告", r"上市公告书",
      r"初步询价", r"配售结果", r"中签结果")),
    ("网上申购", "late", 150,
     (r"申购", r"中签率", r"发行公告", r"路演")),
]

# ---------------- 实测结论（2026-10-01，写在这里免得下一个人重查）----------------
#
# ## 一句话：这个源**不能**提供「排期外提前发现」。`fresh` 恒为 0。
# 试过 8 个巨潮关键词 × 4 个时间窗 × 加不加 IPO 辅导层，全部为 0。
#
# ## 完整探测结果（默认 8 关键词，7.1s）
#
#     candidates 54
#     ├ listed     38   已上市（很多是「中签率公告」—— 申购早结束了）
#     ├ scheduled  16   已在 ipo_calendar 排期里（打新额度提醒已覆盖）
#     ├ indirect   12   参股/控股子公司上市（要打新的不是母公司）
#     ├ post       38   申购后才发的公告，仅归档
#     └ ★fresh      0   未上市 + 未排期 + 申购前 + 直接信号  ← 想要的
#        dropped_non_code 29 / dropped_stale_ipo_stage 27（见下）
#
# ## 为什么 fresh 必然是 0 —— 三层原因，每层都实测过
#
# ### 1. 公告时间轴和排期表高度重叠
# IPO 从受理到申购，交易所/券商必然先披露排期；巨潮的招股书、发行公告、
# 中签率公告都落在排期前后。所以「公告能看到的时刻」排期表也能看到 →
# **零增量**。这是结构性的，不是关键词选得不好。
#
# ### 2. 北交所新股没有「排期外待打新」的存量
# 我曾把 920238 长鹰硬科当成「还在排队」：2026-07-13 出招股书，
# 推断 10-01 还没申购。实际 `sa_stock_roster` 里
# `list_date=2026-07-24` —— **从招股书到上市只隔 11 天**
# （920176 维琪科技 07-27、920079 乔路通 07-22 同理）。
#
# ### 3. 唯一理论上的早期通道（IPO 辅导）返回的是 4 年前冻结快照
# `ak.stock_notice_report()` 实测 56 条「辅导/IPO」公告：
#   - **日期全是 `2022-05-11`**（1604 天前），不是活数据
#   - 未辅导公司的「代码」是**辅导备案号**（A21479 / A16087 / A12031 /
#     A17225 / A21186 / A21619 / A22052），不是 A 股代码
# 不加过滤时它贡献 4 个假 `fresh`（拿备案号查名册必然落空 → 判成
# 「未上市未排期」）。加了 `_is_a_share_code` + `_age_ok` 后贡献**归零**。
# 它另一个问题是极不稳定：同一份代码连跑三次 8.0s / 21.3s / 45s+挂死，
# 已用子进程隔离 + 默认关闭。
#
# ## 那它还有什么用（这才是保留它的理由）
#
# 1. **交叉验证排期表**：16 个 `scheduled` 是公告层**独立**抓到的，
#    与排期表对上了 → 排期表在这 16 只上没漏。反向查漏能找到
#    「排期有但公告层没抓到」的：实测 3 只（920071 金钛股份 /
#    920269 杰锋动力 / 920289 华汇智能，都是北交所票）。
# 2. **`indirect` 12 个对持仓有意义**：参股/控股子公司上市，母公司通常
#    确认一次性投资收益（万润股份、北陆药业、信德新材、汇川技术…）。
# 3. **`post` 38 个可归档**：「中签率公告」含新股中签率，能反推打新收益率。
#
# ## 关键词选错的代价（实测各关键词的早期信号密度，pageSize=30）
#
#   关键词                      总命中  过噪  早期  早期占比  结论
#   招股说明书                     7825    23    23     100%   ★主源
#   首次公开发行股票申请            1038    27    17      63%   ★★金矿
#   上市委                         365     9     9     100%   ★
#   受理                          1101     5     5     100%   ★（要严过滤）
#   过会                          1624    12     1       8%   ✗ 噪声大
#   注册批复                      2952     6     1      17%   ✗ 29/30 是再融资
#   提交注册                        79     0     0       0%   ✗
#   网上申购/发行公告/提示公告       ~2000   ~80   ~0      ~0%   仅 post 校验
#
# 「首次公开发行股票申请」最初漏了 —— 它是**申购前 1-2 个月**最集中的
# 早期信号（受理→问询→过会→注册都在这个短语下）。漏了它会让
# `pre_far` 从 17 掉到 0，误以为「巨潮没有早期信号」。

CNINFO_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Referer": ("https://www.cninfo.com.cn/new/commonUrl"
                "?url=disclosure/list/notice"),
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded",
}

# ⚠️ 必须是 **RLock**，不能用 Lock。
# `discover()` 里 `with _lock:` 内部又 `with _lock:` 写缓存，
# 而 threading.Lock 非重入 -> 直接自死锁（进程无输出、无异常、永久挂住）。
# 这个 bug 让我误判成「akshare 卡住 / IP 被限流」，白排查了很久。
_lock = threading.RLock()
_cache: dict = {"at": 0.0, "candidates": []}
CACHE_TTL = 6 * 3600          # 公告类信息变化慢，缓存 6 小时

# 明显不是「本轮 A 股新股发行」的公告 —— 用来降噪。
#
# ⚠️ 这层噪声比想象的大得多（实测 2026-10-01，逐条统计）：
#   "注册批复"  30 条 → **29 条是已上市公司再融资**（向特定对象发行 /
#                       发行股份购买资产），1 条新股
#                → 另外还有公司债（哈投股份/首创证券/中信证券/
#                   黔源电力/信达证券）和**医疗器械注册**
#                   （宜安科技「镁骨内固定螺钉注册申请补正」——
#                   「注册」二字被"注册批复"关键词误命中）
#   "招股说明书" 30 条 → **12 条是 H 股**（港股，与 A 股打新无关）
#   "上市委"     30 条 → 9 条是「参股/控股子公司」上市，不是本主体上市
#   "网上申购"   30 条 → 噪声最低，基本都是真 A 股新股
# 所以关键词层面的噪声必须靠「必须含首次公开发行」+ 下面的 NOISE 双保险。
NOISE = re.compile(
    r"募投项目结项|节余募集资金|永久补充流动资金|注销.*募集资金专户"
    # 再融资 / 并购（不是首次公开发行）
    r"|向特定对象发行|向不特定对象发行|非公开发行|定向增发"
    r"|发行股份及支付现金购买资产|发行股份购买资产|募集配套资金"
    r"|重大资产重组|资产重组|吸收合并"
    # 债券（不是股票）
    r"|公司债券|可转换公司债券|可转债|永续次级债|中期票据|短期融资券"
    r"|债券发行|债券上市|ABS|资产支持证券"
    # H 股 / 港股 / 美股（不是 A 股 IPO）
    r"|H股|港股|香港公开发售|香港联合交易所|境外上市|纳斯达克|纽交所"
    # 参股/控股公司上市（不是本主体）
    r"|参股公司|参股基金|控股子公司|参股投资项目"
    # 中介机构文件（同一 IPO 会出十几份，不是新信号）
    r"|法律意见书|审计报告|上市保荐书|发行保荐书|保荐机构|尽职调查报告"
    r"|专项核查|核查意见|发行与承销|责任保险"
    # 非股票注册的「注册」（药品/器械/专利/核准）
    r"|注册申请|补正资料|医疗器械|药品注册|临床试验"
    # 常规定期报告
    r"|年度报告|半年度报告|季度报告|业绩预告|业绩快报"
    # 募集资金/账户管理
    r"|募集资金专户|募集资金存放|使用募集资金|置换预先投入"
)

# 强信号：标题里这些词出现时，**即使命中上面某些噪声也保留**。
# 「首次公开发行股票并在XX板上市」是铁证 —— 实测 IPO 公告全都有这句。
# 注意 STRONG 的优先级高于 NOISE，所以**别把再融资措辞写进 STRONG**。
STRONG = re.compile(
    r"首次公开发行股票并在.{0,8}(主板|创业板|科创板|北交所|上交所|深交所|上市)"
    r"|首次公开发行股票.{0,12}(网上申购|发行公告|招股说明书|提示公告|发行)"
    r"|首次公开发行股票并上市"
    r"|向不特定合格投资者公开发行股票.{0,10}(北交所|上市|招股)"
    r"|上市公告书"
)


# ---------------- 巨潮全文检索 ----------------

def cninfo_search(searchkey: str, *, page_size: int = 30, page: int = 1,
                  column: str = "szse", sedate: str = "") -> list[dict]:
    """巨潮公告全文检索。实测 187-556ms，不需要 token。

    ⚠️ 必须带 Referer + X-Requested-With，否则返回空。
    ⚠️ `sedate` 格式 `YYYY-MM-DD~YYYY-MM-DD`；传空 = 只返回最新一批
    （实测 pageSize=30 封顶，不管传 50 还是 200 都只给 30 条）。
    ⚠️ `column` 实测 `szse` 和 `sse` 返回**完全相同**的结果（逐条比对
    一致），所以不用为沪深各查一遍 —— 省一半请求。
    """
    import requests
    try:
        r = requests.post(
            CNINFO_URL,
            data={"pageNum": page, "pageSize": page_size,
                  "column": column, "tabName": "fulltext",
                  "searchkey": searchkey, "seDate": sedate,
                  "sortName": "", "sortType": "", "isHLtitle": "true"},
            headers=CNINFO_HEADERS, timeout=25)
        r.raise_for_status()
        return r.json().get("announcements") or []
    except Exception as exc:                              # noqa: BLE001
        print(f"[ipo_discover] 巨潮检索 '{searchkey}'"
              f"{'@' + sedate if sedate else ''} 失败: {exc}", flush=True)
        return []


def _strip_em(s: str) -> str:
    """巨潮标题里的 <em> 高亮标签要去掉。"""
    return re.sub(r"</?em>", "", str(s or "")).strip()


def _ann_date(a: dict) -> str:
    ts = a.get("announcementTime")
    try:
        return datetime.fromtimestamp(int(ts) / 1000).strftime("%Y-%m-%d")
    except Exception:
        return ""


# ---------------- 层 1：akshare 的 IPO 辅导公告 ----------------

def fetch_ipo_notices(limit: int = 200, timeout: int = 40) -> list[dict]:
    """`ak.stock_notice_report()` 里的 IPO 辅导类公告（层1）。

    ⚠️ **它返回的是 2022-05-11 的冻结快照，不是活数据**，实测：
      - 56 条辅导/IPO 公告的日期全部是 `2022-05-11`（1604 天前）
      - 未辅导公司的「代码」是**辅导备案号** A21479/A16087/A12031/A17225
        （不是 A 股 6 位代码）—— 拿去查名册必然落空，于是被误判成
        「未上市未排期的新发现」，实测产生 4 个假 fresh
      所以 `discover()` 里会用 `_is_a_share_code` + `_age_ok` 把这批全丢掉
      （实测 dropped_non_code 29 / dropped_stale_ipo_stage 27），
      它的 fresh 贡献为 0。

    ⚠️ 这个函数还**极不稳定**：同一份代码连跑三次分别是
      8.0s -> 成功 2407 行 / 21.3s -> 成功 56 条 / 45s+ -> 直接挂死，
      线程杀不掉（akshare 内部持锁，后续 akshare 调用一起挂）。
    所以用**子进程**隔离：挂死就整个丢掉，不影响主进程。
    """
    import subprocess
    import sys
    import warnings

    script = (
        "import os,sys,json,warnings;"
        "os.environ['NO_PROXY']='*';os.environ['no_proxy']='*';"
        "warnings.filterwarnings('ignore');"
        "import akshare as ak;"
        "df=ak.stock_notice_report();"
        "rows=[];"
        "it=df.itertuples() if hasattr(df,'itertuples') else [];"
        "cols=list(df.columns);"
        "[rows.append({c: ('' if getattr(r,c,None) is None else str(getattr(r,c)))"
        " for c in cols}) for r in it];"
        "sys.stdout.write(json.dumps({'rows':rows}, ensure_ascii=False, default=str))"
    )
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        p = subprocess.run([sys.executable, "-c", script], capture_output=True,
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        print(f"[ipo_discover] stock_notice_report 超时 {timeout}s（子进程已杀），"
              f"跳过「辅导期」这一层；层2/3 的巨潮不受影响", flush=True)
        return []
    except Exception as exc:                              # noqa: BLE001
        print(f"[ipo_discover] stock_notice_report 子进程失败: {exc}", flush=True)
        return []
    if p.returncode != 0:
        print(f"[ipo_discover] stock_notice_report 返回码 {p.returncode}，"
              f"跳过这一层", flush=True)
        return []
    try:
        rows = json.loads(p.stdout.decode("utf-8", "replace") or "{}").get("rows") or []
    except Exception as exc:                              # noqa: BLE001
        print(f"[ipo_discover] 解析 stock_notice_report 输出失败: {exc}", flush=True)
        return []
    if not rows:
        return []
    out = []
    for r in rows:
        title = str(r.get("公告标题") or "")
        atype = str(r.get("公告类型") or "")
        if not re.search(r"辅导|上市辅导|IPO", title + atype, re.I):
            continue
        d = str(r.get("公告日期") or "")[:10]
        out.append({
            "code": str(r.get("代码") or "").strip(),
            "name": str(r.get("名称") or "").strip(),
            "title": title,
            "ann_type": atype,
            "ann_date": d,
            "url": str(r.get("网址") or ""),
            "stage": "early",
            "source": "akshare.stock_notice_report",
        })
        if len(out) >= limit:
            break
    return out


# ---------------- 层 2/3：巨潮检索 ----------------

def fetch_cninfo_stages(history_months: int = 0) -> list[dict]:
    """跑 CNINFO_QUERIES 里的全部关键词，按公告类型白名单过滤。

    `history_months` > 0 时**额外**扫历史时间窗（按月分片往前推）。
    为什么要扫历史（实测 2026-10-01 才发现）：
      巨潮 `seDate` 传空 = 只返回**最新 30 条** ≈ 最近 1-2 个月。
      而 A 股现在从「受理」到「申购」常常隔 6-12 个月 ——
      实测 920238 长鹰硬科招股书 2026-07-13，到 10-01 已 2.5 个月
      **还没申购**（920176 维琪科技、920079 乔路通同样在排队）。
      只看最新窗口，这三只**根本不会出现**，`fresh` 恒为 0，
      我一度据此误判「这个源没有提前发现能力」。
    """
    out: list[dict] = []
    for searchkey, stage, page_size, patterns in CNINFO_QUERIES:
        anns = cninfo_search(searchkey, page_size=page_size)
        got = _filter_anns(anns, patterns, stage, searchkey)
        out += got
        if not anns:
            print(f"[ipo_discover] 巨潮 '{searchkey}' -> 0 条", flush=True)
        time.sleep(0.4)

    if history_months:
        seen = {(c.get("code"), c.get("title")) for c in out}
        today = date.today()
        for back in range(1, history_months + 1):
            y, m = today.year, today.month - back
            while m <= 0:
                m += 12
                y -= 1
            eom = (date(y + (m == 12), 1 if m == 12 else m + 1,
                        1) - timedelta(days=1))
            sedate = f"{date(y, m, 1).isoformat()}~{eom.isoformat()}"
            for searchkey, stage, page_size, patterns in CNINFO_QUERIES:
                anns = cninfo_search(searchkey, page_size=page_size,
                                     sedate=sedate)
                if not anns:
                    continue
                for c in _filter_anns(anns, patterns, stage, searchkey,
                                      quiet=True):
                    if (c.get("code"), c.get("title")) not in seen:
                        seen.add((c.get("code"), c.get("title")))
                        out.append(c)
                time.sleep(0.3)
            print(f"[ipo_discover] 历史窗口 {sedate} 累计 {len(out)} 条",
                  flush=True)
    return out


def _filter_anns(anns: list[dict], patterns: tuple[str, ...], stage: str,
                 searchkey: str = "", *, quiet: bool = False) -> list[dict]:
    out: list[dict] = []
    for a in anns:
        title = _strip_em(a.get("announcementTitle"))
        # 强信号豁免：IPO 公告常同时含「募集资金专户」等词
        if NOISE.search(title) and not STRONG.search(title):
            continue
        if not any(re.search(p, title) for p in patterns):
            continue
        code = str(a.get("secCode") or a.get("secCodeId") or "").strip()
        code = code.split(".")[0]
        if len(code) != 6 or not code.isdigit():
            continue
        out.append({
            "code": code,
            "name": _strip_em(a.get("secName")),
            "title": title,
            "ann_type": stage,
            "ann_date": _ann_date(a),
            "url": f"https://static.cninfo.com.cn/{a.get('adjunctUrl')}",
            "stage": stage,
            "source": "cninfo.fulltext",
        })
    if not quiet:
        print(f"[ipo_discover] 巨潮 '{searchkey}' -> 命中 {len(anns)}，"
              f"过滤后保留 {len(out)}", flush=True)
    return out


# ---------------- 合并与去重 ----------------

# ---------------- 信号时间轴：这条公告发出来时，申购还没发生吗 ----------------
#
# ⚠️ 这是决定「有用没用」的关键（实测 2026-10-01）：
# 「网上申购情况及中签率公告」这类信号最容易抓到（30 条里 30 条都是它），
# 但它是**申购当天/之后**发的 —— 看到它时申购已经结束，
# 对「要不要为了这次打新补市值」**零价值**。
# 真正有价值的是申购**之前**的信号，排序：
#
#   pre_far   过会/注册       申购前 1-2 个月   ← 补仓还来得及
#   pre_book  招股书/意向书   申购前 2-4 周
#   pre_near  发行/询价/路演  申购前 1-3 天     ← 来不及了，但可加自选
#   post      中签率/中签/上市 申购之后        ← 无价值，仅归档
#
# 所以 `fresh` 还要再过一道 `signal_kind in (pre_far, pre_book)` 过滤 ——
# 实测 40 个 fresh 里 33 个是 post，滤完只剩 7 个真信号。
SIGNAL_KIND: list[tuple[str, str]] = [
    # (正则, 时间轴档位) —— 顺序敏感：post 放前面，因为它最具体
    (r"中签率|中签结果|网上申购情况|配售结果|缴款|上市公告书"
     r"|上市交易|网上路演公告", "post"),
    (r"初步询价|询价公告|定价公告|发行公告|提示公告|招股意向"
     r"|网上申购公告", "pre_near"),
    (r"注册批复|同意注册|提交注册|注册生效", "pre_far"),
    (r"招股说明书|招股意向书|发行与承销方案|发行方案", "pre_book"),
    (r"上市委|审议结果|会议决议|符合发行条件|符合上市条件"
     r"|受理|问询|辅导", "pre_far"),
]

# 这几档才算「对打新额度决策有用」
USEFUL_KINDS = ("pre_far", "pre_book")

# 辅导层的额外门槛（实测 2026-10-01）
# ------------------------------
# `ak.stock_notice_report()` 返回的**不是活数据，是 2022-05-11 的冻结快照**：
#   - 全部 56 条的日期都是 `2022-05-11`（1604 天前）
#   - 未辅导公司的「代码」是 **辅导备案号**（A21479 / A16087 / A12031 /
#     A17225 / A21186 / A21619 / A22052），根本不是 A 股代码 ——
#     拿它去查名册永远查不到，于是被误判成「fresh 提前发现」
# 所以辅导层必须同时满足：① 6 位数字真实 A 股代码 ② 日期在 MAX_AGE_DAYS 内。
# 加这两条后辅导层的 fresh 贡献**归零**（实测 4 -> 0），
# 也就是「IPO 辅导」这条唯一理论上的早期通道实际也不可用。
MAX_AGE_DAYS = 180


def _age_ok(d: str | None) -> bool:
    n = _days_ago(d)
    return n is not None and n <= MAX_AGE_DAYS


def _is_a_share_code(code: str) -> bool:
    """A 股/北交所 6 位数字代码。排除辅导备案号（A21479）和港股等。"""
    c = str(code or "").strip()
    return len(c) == 6 and c.isdigit()


def signal_kind(title: str) -> str:
    """按公告类型判时间轴档位（见 SIGNAL_KIND 注释）。"""
    for pat, kind in SIGNAL_KIND:
        if re.search(pat, title or ""):
            return kind
    return "unknown"


# 间接信号：标题里的 IPO 主体**不是上市公司本身**，而是它的子公司/参股公司。
# 实测 2026-10-01：8 个「有用」的 fresh 信号里 **8 个全是这类** ——
#   宝钢股份→宝武碳业  用友网络→子公司  宗申动力→参股子公司
#   万润股份→控股子公司  中色股份→参股公司  上峰水泥→参股公司
#   宗申… *ST宇顺→参股基金投资的公司
# 这类信号对**打新额度决策毫无意义**（要打新的是子公司，代码还不是 A 股代码），
# 但对**持仓关联**有意义：参股公司上市，母公司通常有一次性投资收益。
# 所以单列 `indirect`，不进 `fresh`。
INDIRECT = re.compile(
    r"控股子公司|参股公司|参股基金|所属子公司|控股企业|参股投资项目"
    r"|股权投资业务参股"
)


def is_indirect(title: str) -> bool:
    return bool(INDIRECT.search(title or ""))


def _days_ago(d: str | None) -> int | None:
    """公告日距今天数。日期格式非法时返回 None 而不是抛异常
    —— 巨潮偶尔会给出 `2026-9-3` 这种非零填充日期。"""
    if not d:
        return None
    try:
        return (date.today() - date.fromisoformat(str(d)[:10])).days
    except ValueError:
        return None


def _classify(codes: list[str]) -> dict[str, str]:
    """给每个 code 打上三档身份。

    ⚠️ 原来的判据是「在 sa_stock_roster 里吗」，结果 **`new` 永远是 0**
    （实测 61/61 全命中）—— 因为 roster 是**全市场名册**，连还没上市的
    次新股都在里面（实测含 list_date=2026-10-09 的未来上市票）。
    用它判「是不是新发现」等于用「是不是股票」判「是不是新股票」。

    改成三档：
      listed     已上市（roster.list_date <= 今天）
                 -> 对打新无用，但可用于「新上市」跟踪
      scheduled  还没上市、但已经在 ipo_calendar 排期里
                 -> 打新额度提醒已覆盖，不用重复通知
      fresh      还没上市、也不在排期里  ★这才是「提前发现」
    """
    out: dict[str, str] = {}
    if not codes:
        return out
    try:
        from ipo_quota import _query
        today = date.today().isoformat()
        # ⚠️ 三个都是实测踩过的坑：
        #  1. `= ANY(%s)` + `(codes,)`；写成 `IN (%s)` 会报
        #     `IndexError: tuple index out of range`（psycopg2 解析不了
        #     「一个 list 参数对应多个占位符」）。`IN %s` 也不行，
        #     psycopg2 不会帮你展开成 ARRAY。
        #  2. args 必须是 **tuple of one list**，直接传 list 会报
        #     `TypeError: not all arguments converted during string formatting`
        #  3. `list_date` 是 date 列，必须显式 `::text` 比较，
        #     传 ISO 字符串会报 `operator does not exist: date = text`
        for (code, ld) in _query(
                "SELECT code, list_date::text FROM sa_stock_roster "
                "WHERE code = ANY(%s)", (codes,)):
            c = str(code).strip()
            if ld and ld <= today:
                out[c] = "listed"
            else:
                out.setdefault(c, "pre_listing")
        # 排期表（ipo_calendar 有本地缓存，不走网络）
        sched: set[str] = set()
        try:
            import ipo_calendar
            for it in ipo_calendar._load().get("items") or []:
                if it.get("code"):
                    sched.add(str(it["code"]).strip())
        except Exception as exc:                          # noqa: BLE001
            print(f"[ipo_discover] 读排期缓存失败: {exc}", flush=True)
        for c in codes:
            if c in sched:
                out[c] = "scheduled"
            elif out.get(c) != "listed":
                out.setdefault(c, "fresh")
        return out
    except Exception as exc:                              # noqa: BLE001
        print(f"[ipo_discover] 读已知名单失败（可能重复报告）: {exc}", flush=True)
        # 查不到就退回「全部视为 fresh」——宁可多报也不漏报
        return {c: "fresh" for c in codes}


def discover(*, use_cache: bool = True, mark_new: bool = True,
             include_ipo_stage: bool = False,
             history_months: int = 0) -> dict:
    """跑一轮新股早期信号探测。

    返回 {candidates, new, stats, skipped}

    `include_ipo_stage`（默认**关**）是否跑层1（akshare IPO 辅导公告）。
      开了也没用：实测它返回 2022-05-11 的冻结快照、未辅导公司的「代码」
      是辅导备案号，过滤后 fresh 贡献为 0（详见 fetch_ipo_notices 注释）。
      保留参数只是因为它是唯一「理论上的早期通道」，将来若该接口恢复活数据
      可以直接启用。

    `candidates` 按「code + 最早信号日」去重，一只票只保留最早的信号
    （越早越有价值）。

    `history_months`（默认 **0**，实测没增量但要多花 14s）往前额外扫几个
    自然月的公告。开它是因为巨潮默认只给最新 30 条（≈1-2 个月）；
    但实测 `history_months=4` 只把候选 54→57（+3 全是老公告），
    `fresh` 依然为 0 —— 因为北交所新股从招股书到上市只隔 11 天，
    没有「排期外待打新」的存量。留作参数供将来复核。

    ⚠️ 一句话结论（写在模块顶部有完整版）：**这个源 `fresh` 恒为 0，
    不能用来「提前发现新股」**。它的真实用途是交叉验证排期表 +
    抓参股子公司上市信号。
    """
    # ⚠️ 取缓存 / 写缓存各自单独加锁，**不要把网络 I/O 包在锁里**。
    # 一轮探测实测 7.3s（默认）/ 21s（history_months=4）/ 40s（含辅导层），
    # 包在锁里会把所有并发调用方一起堵住。并发重复抓取代价只是几次多余请求。
    with _lock:
        cache_fresh = ((time.time() - _cache["at"]) < CACHE_TTL)
        cached = list(_cache["candidates"]) if (use_cache and cache_fresh) else None

    if cached:
        cands, from_cache = cached, True
    else:
        from_cache = False
        cands = fetch_cninfo_stages(history_months=history_months)
        if include_ipo_stage:
            cands += fetch_ipo_notices()
        with _lock:
            _cache["candidates"] = cands
            _cache["at"] = time.time()

    # 一只票只留最早信号（日期最小；无日期排最后）
    dropped_stale = dropped_code = 0
    best: dict[str, dict] = {}
    for c in cands:
        code = c.get("code") or ""
        if not code:
            continue
        # 辅导备案号（A21479）不是股票代码，拿它查名册必然落空 -> 误判 fresh
        if not _is_a_share_code(code):
            dropped_code += 1
            continue
        # 4 年前的快照不是「早期信号」，是历史归档
        if c.get("source") == "akshare.stock_notice_report" and not _age_ok(
                c.get("ann_date")):
            dropped_stale += 1
            continue
        cur = best.get(code)
        if cur is None:
            best[code] = c
            continue
        cd = c.get("ann_date") or "9999"
        pd = cur.get("ann_date") or "9999"
        if cd < pd:
            # 换成本条，但保留已知的更早日期
            c["first_seen"] = pd if pd < "9999" else cur.get("ann_date")
            best[code] = c
        elif not cur.get("first_seen"):
            cur["first_seen"] = pd

    for code, c in best.items():
        c["days_ago"] = _days_ago(c.get("ann_date"))
        c["name"] = _strip_em(c.get("name"))       # 巨潮 secName 也带 <em>
        c["signal"] = signal_kind(c.get("title"))
        c["useful"] = c["signal"] in USEFUL_KINDS
        c["indirect"] = is_indirect(c.get("title"))
    cls = _classify(sorted(best))                 # 一次性批量，别逐个查
    for code, c in best.items():
        c["identity"] = cls.get(code, "fresh")
        # 兼容旧字段：identity 不是 fresh 就等价于「已知」
        c["already_known"] = c["identity"] != "fresh"

    all_c = sorted(best.values(),
                   key=lambda x: (x.get("ann_date") or "9999"))
    # 「提前发现」= 还没上市 + 排期表里还没有 + 公告发在申购之前
    # + 不是「参股/控股子公司上市」这种间接信号
    # ⚠️ 后三个条件缺一不可，实测逐条砍掉的：
    #   只判前两条 -> 40 个 fresh，其中 **32 个是「中签率公告」**
    #     （看到它时申购早已结束，对补市值零价值）
    #   再排除间接 -> 剩下 8 个，**又全是「控股/参股子公司上市」**
    #     （要打新的是子公司，不是母公司）
    fresh = [c for c in all_c
             if c["identity"] == "fresh" and c["useful"] and not c["indirect"]]
    indirect = [c for c in all_c if c["useful"] and c["indirect"]]
    post = [c for c in all_c if not c["useful"]]
    scheduled = [c for c in all_c if c["identity"] == "scheduled"]

    if mark_new and fresh:
        _mark_discovered(fresh)

    by_stage: dict[str, int] = {}
    for c in all_c:
        by_stage[c["stage"]] = by_stage.get(c["stage"], 0) + 1
    by_id: dict[str, int] = {}
    for c in all_c:
        by_id[c["identity"]] = by_id.get(c["identity"], 0) + 1
    by_signal: dict[str, int] = {}
    for c in all_c:
        by_signal[c["signal"]] = by_signal.get(c["signal"], 0) + 1
    return {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "from_cache": from_cache,
        "include_ipo_stage": include_ipo_stage,
        "skipped": ([] if include_ipo_stage else
                    ["early(辅导期)：ak.stock_notice_report 极不稳定"
                     "（实测 8s/21s/45s+挂死），默认不跑"]),
        "stats": {"candidates": len(all_c),
                  "fresh": len(fresh),          # ★提前发现（真·可打新）
                  "scheduled": len(scheduled),  # 已进排期，额度提醒会覆盖
                  "indirect": len(indirect),    # 参股/控股子公司上市
                  "post": len(post),            # 申购后才发的，仅归档
                  "by_stage": by_stage,
                  "by_identity": by_id, "by_signal": by_signal,
                  "dropped_non_code": dropped_code,
                  "dropped_stale_ipo_stage": dropped_stale},
        "candidates": all_c,
        "fresh": fresh,
        "scheduled": scheduled,
        "indirect": indirect,
        "post": post,
        # 旧字段名保留（= fresh），避免调用方 KeyError
        "new": fresh,
    }


# ---------------- 新发现打标（避免每天重复推）----------------

def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _mark_discovered(items: list[dict]) -> None:
    st = _load_state()
    today = date.today().isoformat()
    for it in items:
        rec = st.setdefault(it["code"], {})
        if rec.get("first_discovered") == today:
            continue
        rec["first_discovered"] = rec.get("first_discovered") or today
        rec.setdefault("notified_bands", [])
        rec["last_stage"] = it["stage"]
        rec["last_signal"] = it.get("signal")
        rec["name"] = it.get("name")
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except Exception as exc:                              # noqa: BLE001
        print(f"[ipo_discover] 状态保存失败: {exc}", flush=True)


def mark_notified(code: str, band: str) -> bool:
    """标记某只票某个阶段已通知过。返回 True 表示这次是首次通知。"""
    st = _load_state()
    rec = st.setdefault(code, {})
    bands = rec.setdefault("notified_bands", [])
    if band in bands:
        return False
    bands.append(band)
    rec["last_notified_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except Exception as exc:                              # noqa: BLE001
        print(f"[ipo_discover] 状态保存失败: {exc}", flush=True)
    return True


# ---------------- 微信文案 ----------------
#
# ⚠️ 分类**必须按 `signal`（时间轴）而不是 `stage`**。
# 实测：12 个 indirect 里 12 个 stage 都是 `late`，按 stage 排会把
# 「参股公司过会（还有 6-12 个月）」和「网上路演（只剩 1 天）」显示成
# 同一种「🔴 临近发行」，误导性极强。
# 所以这里只用 signal，并单独处理 indirect。

SIGNAL_CN = {
    "pre_far":  "🟠 已受理/过会（申购前 1-2 月）",
    "pre_book": "🟡 已出招股书（申购前 2-4 周）",
    "pre_near": "🔴 临近申购（1-3 天）",
    "post":     "⚪ 已申购完",
    "unknown":  "❔ 类型未识别",
}
SIGNAL_TIP = {
    "pre_far": "额度按 20 个交易日日均市值算，**现在补仓还来得及爬满**。",
    "pre_book": "申购前 2-4 周。20 日窗口还没跑满，这一轮可能只拿到部分额度。",
    "pre_near": "申购前 1-3 天 —— 补市值已来不及，只建议加自选股观察。",
    "post": "已过申购日，仅归档。",
}


def format_wx(items: list[dict]) -> str:
    if not items:
        return ""
    lines = [f"🔎 新股信号（{len(items)} 只）", ""]
    for it in items[:20]:
        nm = it.get("name") or "—"
        code = it.get("code") or "—"
        sig = it.get("signal", "unknown")
        tag = "（间接：参股/控股子公司）" if it.get("indirect") else ""
        d = it.get("ann_date") or "?"
        days = it.get("days_ago")
        lines.append(f"{SIGNAL_CN.get(sig, sig)} {nm}({code}){tag}")
        lines.append(f"   {d}"
                     + (f"（{days} 天前）" if days is not None else ""))
        title = (it.get("title") or "")[:56]
        if title:
            lines.append(f"   {title}")
        lines.append(f"   💡 {SIGNAL_TIP.get(sig, '')}")
        lines.append("")
    lines.append("来源：巨潮公告全文检索（cninfo.com.cn）。")
    lines.append("⚠️ 实测结论：这个源与 ipo_calendar 排期**高度重叠、零增量**，"
                 "「未上市且不在排期」的信号恒为 0。它的用途是 "
                 "①交叉验证排期表 ②参股/控股子公司上市（母公司有一次性投资收益）。")
    return "\n".join(lines)


if __name__ == "__main__":
    print(json.dumps(discover(use_cache=False), ensure_ascii=False, indent=2))
