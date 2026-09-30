"""数据源适配层：每个源一个类，统一输出 normalize 后的板块列表。

设计要点（都来自 2026-09-30/10-01 的实测，不是猜的）
--------------------------------------------------
1. **双厂商**：同花顺(ths) 为主、开盘红(kph) 为副。两者无任何关系，
   单一厂商被掐时另一个还能活。原实现只有东财一家，被掐即全盘停摆。
2. **请求量记账与降频**：原实现 576 请求/日 打一个 host 被 WAF 掐死。
   这里按 host 记账，超过日预算自动降频，并在 /health 里暴露用量。
3. **缓存**：板块名↔代码映射、板块指数历史都是稳定的，不该每轮重拉。
   概念名列表单次要 41 个请求，必须缓存。
4. **不静默失败**：每个源都记 last_error / last_ok / 连续失败数，
   降级时通过 degraded 字段告诉调用方"这份数据是降级的"。
"""
from __future__ import annotations

import collections
import os
import threading
import time
import traceback
from datetime import date, datetime, timedelta

from . import normalize as N

# 同花顺三家子域的本机实测日预算。576/日 打在单一 host 上会被烧死，
# 这里保守设成 200/日/子域，留足余量。
#
# 但**一次采集就要 55 个请求**（概念名表 41 + 行业概览 3 + 概念资金流 9
# + 行业名 1 + 概念资金流子域 9 ≈ 63），所以 200 的预算只够 3 轮采集。
# 盘中 5 分钟一轮的话一天 48 轮，预算会在第 3 轮耗尽。
# 这是**刻意的**：预算耗尽后自动降级到新浪（57 请求/轮），而不是像原实现
# 那样无限打同花顺直到被永久封禁。要提高盘中频率就得先调高预算。
HOST_DAILY_BUDGET = int(os.environ.get("SA_HOST_DAILY_BUDGET", "200"))


class _HostMeter:
    """按 host 记账 + 降频。超预算就跳过本轮，不去打。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: collections.Counter = collections.Counter()
        self._day: str = date.today().isoformat()

    def _roll(self) -> None:
        today = date.today().isoformat()
        if today != self._day:
            self._day = today
            self._counts.clear()

    def allow(self, host: str, n: int = 1) -> bool:
        with self._lock:
            self._roll()
            if self._counts[host] + n > HOST_DAILY_BUDGET:
                return False
            self._counts[host] += n
            return True

    def snapshot(self) -> dict:
        with self._lock:
            self._roll()
            return {"day": self._day,
                    "budget_per_host": HOST_DAILY_BUDGET,
                    "used": dict(self._counts)}


METER = _HostMeter()


class SourceError(RuntimeError):
    pass


class _BaseSource:
    name = "base"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.last_ok: str | None = None
        self.last_error: str = ""
        self.consec_fail: int = 0
        self.calls: int = 0

    def _ok(self) -> None:
        with self._lock:
            self.last_ok = datetime.now().isoformat(timespec="seconds")
            self.last_error = ""
            self.consec_fail = 0
            self.calls += 1

    def _fail(self, exc: BaseException) -> None:
        with self._lock:
            self.consec_fail += 1
            self.last_error = f"{type(exc).__name__}: {str(exc)[:180]}"
            self.last_ok = None if not self.last_ok else self.last_ok
            self.calls += 1

    def health(self) -> dict:
        with self._lock:
            return {"name": self.name, "healthy": self.consec_fail == 0,
                    "consec_fail": self.consec_fail,
                    "last_ok": self.last_ok, "last_error": self.last_error,
                    "calls": self.calls}


class ThsSource(_BaseSource):
    """同花顺（经 akshare）。主力源。

    实测请求量：
        stock_board_industry_summary_ths  3 请求 / 0.6s  -> 90 行业，8 字段全
        stock_fund_flow_concept            9 请求 / 1.1s  -> 387 概念
        stock_board_industry_name_ths      1 请求          -> 行业 code（缓存）
        stock_board_concept_name_ths      41 请求          -> 概念 code（缓存）
        stock_board_industry_index_ths     1-2 请求        -> 行业指数历史（缓存）
    """

    name = "ths"

    def __init__(self) -> None:
        super().__init__()
        self._code_ind: dict[str, str] = {}
        self._code_con: dict[str, str] = {}
        self._code_ts: float = 0.0
        # 行业 code 相对稳定，缓存 12 小时足够
        self._CODE_TTL = 12 * 3600

    def _ak(self):
        import warnings
        warnings.filterwarnings("ignore")
        import akshare as ak
        return ak

    def _refresh_codes(self, force: bool = False) -> None:
        if not force and self._code_ind and (time.time() - self._code_ts) < self._CODE_TTL:
            return
        ak = self._ak()
        t0 = time.time()
        # 行业名 1 请求
        if not METER.allow("q.10jqka.com.cn", 1):
            raise SourceError("q.10jqka 预算不足，跳过行业 code 刷新")
        ind = ak.stock_board_industry_name_ths()
        self._code_ind = {str(n).strip(): str(c)
                          for n, c in zip(ind["name"], ind["code"])}
        # 概念名列表单次 41 个请求（实测），缓存 24 小时。
        # 预算不够就只用行业 code —— 概念 code 缺失只是拿不到 code，
        # 行情本身不受影响，比整轮失败好。
        if force or not self._code_con:
            if METER.allow("q.10jqka.com.cn", 41):
                con = ak.stock_board_concept_name_ths()
                self._code_con = {str(n).strip(): str(c)
                                  for n, c in zip(con["name"], con["code"])}
            else:
                print("[data_service] 概念 code 表预算不足（需 41），"
                      "本轮概念无 code", flush=True)
        self._code_ts = time.time()
        print(f"[data_service] ths code map refreshed: "
              f"{len(self._code_ind)} 行业 / {len(self._code_con)} 概念 "
              f"({time.time()-t0:.1f}s)", flush=True)

    def fetch(self, include_concept: bool = True) -> list[dict]:
        """返回统一结构的板块列表。

        预算检查放在**每一步之前**而不是一次给足。原因：akshare 的
        `stock_board_concept_name_ths` 单次 41 个请求，如果只按「4 个」记账，
        实际会打出 164 个（实测 meter 里 q.10jqka 一天 164）—— 这正是
        原实现 576 请求/日被烧死的机制。逐步检查才真正限得住。
        """
        ak = self._ak()
        try:
            self._refresh_codes()          # 内部自行记账
            out: list[dict] = []

            # 行业：8 字段最全
            if METER.allow("q.10jqka.com.cn", 3):
                for _, r in ak.stock_board_industry_summary_ths().iterrows():
                    nm = str(r["板块"]).strip()
                    out.append(N.from_ths_industry(r.to_dict(),
                                                   self._code_ind.get(nm)))
            else:
                print("[data_service] q.10jqka 预算不足，跳过行业", flush=True)

            # 概念：只有涨跌幅/资金流/领涨股
            if include_concept:
                if METER.allow("data.10jqka.com.cn", 9):
                    for _, r in ak.stock_fund_flow_concept().iterrows():
                        nm = str(r["行业"]).strip()
                        out.append(N.from_ths_concept(r.to_dict(),
                                                     self._code_con.get(nm)))
                else:
                    print("[data_service] data.10jqka 预算不足，跳过概念",
                          flush=True)

            if not out:
                raise SourceError("同花顺：预算不足，一个板块都没拿到")
            self._ok()
            return out
        except SourceError as exc:
            self._fail(exc)
            raise
        except BaseException as exc:                      # noqa: BLE001
            self._fail(exc)
            raise SourceError(f"同花顺源失败: {exc}") from exc

    def index_history(self, board_name: str, days: int = 30) -> list[dict]:
        """板块指数历史日线 —— 动量分的数据来源，不必自己攒快照。"""
        if not METER.allow("d.10jqka.com.cn", 2):
            raise SourceError("d.10jqka.com.cn 预算用尽")
        ak = self._ak()
        try:
            end = date.today()
            df = ak.stock_board_industry_index_ths(
                symbol=board_name,
                start_date=(end - timedelta(days=days * 2 + 10)).strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"))
            rows = []
            for _, r in df.iterrows():
                rows.append({
                    "date": str(r["日期"])[:10],
                    "open": N.num(r.get("开盘价")),
                    "high": N.num(r.get("最高价")),
                    "low": N.num(r.get("最低价")),
                    "close": N.num(r.get("收盘价")),
                    "volume": N.num(r.get("成交量")),
                    "amount": N.num(r.get("成交额")),
                })
            return rows
        except BaseException as exc:                      # noqa: BLE001
            print(f"[data_service] ths index_history({board_name}) 失败: {exc}",
                  flush=True)
            return []


class KphSource(_BaseSource):
    """开盘红/财联社（经 levistock）。第二厂商。

    实测：sector_ranking_kph(date, zs_type, fetch_all)
        zs_type=4 行业 104    zs_type=6 地域 42    zs_type=7 题材 259
        合计 405，一次 fetch_all 约 0.15-0.7s
    字段比同花顺多：turnover_rate / buy_amount / sell_amount / market_cap
    坑：net_inflow 与 net_inflow_5d 名字对调（已在 normalize 修正）
    坑：PyPI 元数据无 License，仅供个人研究，勿 vendor 进仓库
    """

    name = "kph"
    ZS = {"4": N.KIND_INDUSTRY, "6": N.KIND_REGION, "7": N.KIND_THEME}

    def fetch(self) -> list[dict]:
        if not METER.allow("kaipanhong", 6):
            raise SourceError("开盘红今日预算用尽")
        try:
            import levistock as ls
        except ImportError as exc:
            raise SourceError(f"levistock 未安装: {exc}") from exc

        # 休市回溯：实测 2026-10-01（国庆）三个类别全为 0，2026-09-30 有 405 条。
        # 直接用 date.today() 会在休市日拿到空数据并误判成"源挂了"。
        last = None
        for back in range(0, 10):
            d = (date.today() - timedelta(days=back)).strftime("%Y-%m-%d")
            total = 0
            for zs in self.ZS:
                try:
                    total += len(ls.sector_ranking_kph(
                        date=d, zs_type=zs, fetch_all=True) or [])
                except BaseException as exc:              # noqa: BLE001
                    print(f"[data_service] kph {d} zs={zs} 失败: {exc}", flush=True)
            if total:
                last = d
                break
            time.sleep(0.4)
        if last is None:
            raise SourceError("回溯 10 天都没有数据（可能是长假或源挂了）")
        if last != date.today().strftime("%Y-%m-%d"):
            print(f"[data_service] kph 今日休市，改用最近交易日 {last}", flush=True)

        out: list[dict] = []
        for zs, kind in self.ZS.items():
            try:
                rows = ls.sector_ranking_kph(date=last, zs_type=zs, fetch_all=True)
            except BaseException as exc:                  # noqa: BLE001
                print(f"[data_service] kph {last} zs_type={zs} 失败: {exc}", flush=True)
                continue
            for r in rows or []:
                b = N.from_kph(r, kind)
                b["quote_ts"] = last
                out.append(b)
        if not out:
            raise SourceError(f"开盘红 {last} 三个类别都返回空")
        self._ok()
        return out


class ZtPoolSource(_BaseSource):
    """涨停池（东财 push2ex，未被封）。含连板数 lbc / 炸板 zbc。

    坑：url 必须显式传 date，否则返回 200 + 空数组。
    坑：假日传 date 会返回上一交易日集合 —— 所以要按返回的 qdate 自纠。
    """

    name = "ztpool"
    URL = "https://push2ex.eastmoney.com/getTopicZTPool"
    UT = "fa5fd1943c7b386f172d6893dbfba10b"

    def fetch(self) -> dict:
        # ---- 档 1：push2ex 原生池（有连板高度，信息最全）----
        if METER.allow("push2ex.eastmoney.com", 2):
            r1 = self._from_push2ex()
            if r1:
                r1["tier"] = "push2ex"
                self._ok()
                return r1
        # ---- 档 2：同花顺行业榜反推 ----
        r2 = self._from_ths_rankings()
        if r2:
            self._ok()
            return r2
        raise SourceError("涨停池：push2ex 已封，同花顺行业榜也取不到")

    def _from_push2ex(self) -> dict | None:
        import requests
        for back in range(0, 6):
            d = (date.today() - timedelta(days=back)).strftime("%Y%m%d")
            try:
                r = requests.get(self.URL, params={"d": d, "ut": self.UT},
                                 headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                                 timeout=15)
                j = r.json()
            except BaseException:                         # noqa: BLE001
                return None
            data = j.get("data") or {}
            pool = data.get("pool") or []
            if pool:
                s = self._to_summary(pool, data.get("qdate") or d)
                s["coverage"] = {"stocks_seen": len(pool), "full_market": True}
                return s
        return None

    def _from_ths_rankings(self) -> dict | None:
        """push2ex 挂掉后的降级：用同花顺行业榜反推涨停家数。

        实测 `stock_rank_cxsl_ths`(680 行) / `stock_rank_cxfl_ths`(180 行)
        这两个 ranking **同时带 `所属行业` 和 `涨跌幅`**，把涨幅触及涨停阈值的
        按行业计数即可。覆盖是部分的（这两榜本身是缩量/放量筛选，不是全市场），
        所以 coverage 里如实写明覆盖多少只，不冒充全量。
        """
        import warnings
        warnings.filterwarnings("ignore")
        import akshare as ak
        if not METER.allow("q.10jqka.com.cn", 4):
            return None
        by_board: dict[str, int] = collections.Counter()
        seen: set[str] = set()
        for fn_name in ("stock_rank_cxsl_ths", "stock_rank_cxfl_ths"):
            f = getattr(ak, fn_name, None)
            if f is None:
                continue
            try:
                df = f()
            except BaseException as exc:                  # noqa: BLE001
                print(f"[data_service] {fn_name} 失败: {exc}", flush=True)
                continue
            if not len(df) or "所属行业" not in df.columns:
                continue
            pct_col = next((c for c in ("涨跌幅", "阶段涨跌幅")
                            if c in df.columns), None)
            if pct_col is None:
                continue
            for _, r in df.iterrows():
                code = str(r.get("股票代码") or "").strip()
                if not code or code in seen:
                    continue
                pct = N.num(r.get(pct_col))
                if pct is None:
                    continue
                seen.add(code)
                if pct >= self._limit_pct(code, r.get("股票简称")):
                    ind = str(r.get("所属行业") or "").strip()
                    if ind:
                        by_board[ind] += 1
        if not by_board:
            return None
        return {
            "qdate": date.today().isoformat(),
            "total": sum(by_board.values()),
            "max_lb": None,          # 这档拿不到连板高度，如实留空
            "sum_zbc": None,
            "by_board": dict(by_board),
            "tier": "ths_rankings",
            "coverage": {"stocks_seen": len(seen), "full_market": False,
                         "note": "由同花顺缩量/放量行业榜反推，只覆盖榜内股票，"
                                 "非全市场；连板高度与炸板数不可得"},
        }

    @staticmethod
    def _limit_pct(code: str, name: str) -> float:
        """涨停阈值（%）。按板块和是否 ST 区分。"""
        c = str(code)
        nm = str(name or "")
        if "ST" in nm.upper():
            return 4.8
        if c.startswith(("300", "301", "688", "689")):
            return 19.5
        if c.startswith(("8", "4")):              # 北交所
            return 29.5
        return 9.8

    @staticmethod
    def _to_summary(pool: list, qdate: str) -> dict:
        by_board: dict[str, int] = collections.Counter()
        max_lb = 0
        sum_zbc = 0
        for row in pool:
            hybk = str(row.get("hybk") or "").strip()
            lbc = N.int_or_none(row.get("lbc")) or 1
            zbc = N.int_or_none(row.get("zbc")) or 0
            if hybk:
                by_board[hybk] += 1
            max_lb = max(max_lb, lbc)
            sum_zbc += zbc
        return {"qdate": str(qdate), "total": len(pool),
                "max_lb": max_lb, "sum_zbc": sum_zbc,
                "by_board": dict(by_board)}


class SinaSource(_BaseSource):
    """新浪 MoneyFlow.ssl_bkzj_bk。第三厂商，1 个请求拿 383 个板块。

    实测：fenlei 0=行业(48) 1=概念(181) 2=证监会行业(154)，单次 num=1000 拿全，
    约 130-160ms，无翻页。非东财，未被 WAF 掐。

    定位是**辅助源**而非主源，理由（都是实测的）：
    - avg_changeratio 是比率且只 2 位小数 -> 涨跌幅精度 ±0.5pp，不够做主源
    - 申万口径，与同花顺 90 个行业只重叠 4 个
    - 没有绝对主力净流入（ratioamount 是比率）
    但 netamount（净流入）是绝对额且可靠，适合补同花顺缺资金流的板块。
    """

    name = "sina"
    URL = ("http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "MoneyFlow.ssl_bkzj_bk")
    FENLEI = {"0": N.KIND_INDUSTRY, "1": N.KIND_CONCEPT, "2": N.KIND_INDUSTRY}

    def fetch(self) -> list[dict]:
        if not METER.allow("vip.stock.finance.sina.com.cn", 3):
            raise SourceError("新浪今日预算用尽")
        import requests
        out: list[dict] = []
        for fenlei, kind in self.FENLEI.items():
            try:
                r = requests.get(self.URL,
                                 params={"page": 1, "num": 1000,
                                         "sort": "netamount", "asc": 0,
                                         "fenlei": fenlei},
                                 headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                                 timeout=25)
                rows = r.json()
            except BaseException as exc:                  # noqa: BLE001
                print(f"[data_service] sina fenlei={fenlei} 失败: {exc}", flush=True)
                continue
            for row in rows or []:
                out.append(N.from_sina(row, kind))
        if not out:
            raise SourceError("新浪三个分类都返回空")
        self._ok()
        return out


class BaostockSource(_BaseSource):
    """baostock 日 K 兜底（板块不行，但日K 14 字段含换手率）。"""

    name = "baostock"

    def daily(self, code: str, start: str, end: str) -> list[dict]:
        import baostock as bs
        lg = bs.login()
        if lg.error_code != "0":
            raise SourceError(f"baostock 登录失败: {lg.error_msg}")
        try:
            sym = ("sh." if code[0] in "56" else
                   ("sz." if code[0] in "03" else "bj.")) + code
            rs = bs.query_history_k_data_plus(
                sym, "date,code,open,high,low,close,volume,amount,turn,pctChg",
                start_date=start, end_date=end, frequency="d", adjustflag="2")
            rows = []
            while (rs.error_code == "0") and rs.next():
                r = rs.get_row_data()
                rows.append({
                    "trade_date": r[0], "code": r[1],
                    "open": N.num(r[2]), "high": N.num(r[3]),
                    "low": N.num(r[4]), "close": N.num(r[5]),
                    "volume": N.num(r[6]), "amount": N.num(r[7]),
                    "turnover_rate": N.num(r[8]), "pct": N.num(r[9]),
                    "source": "baostock",
                })
            return rows
        finally:
            bs.logout()


THS = ThsSource()
KPH = KphSource()
SINA = SinaSource()
ZT = ZtPoolSource()
BAOSTOCK = BaostockSource()
