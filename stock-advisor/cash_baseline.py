# -*- coding: utf-8 -*-
"""现金/存款机会成本基准 —— 决策上下文的「无风险 baseline」注入块。

2026-11-08 新增（用户要求）。背景：决策上下文里现金只是余额数字，LLM 看到
「近20日涨37%」就倾向追 —— 没有任何地方告诉它「不买股票、持有存款/货基也能
稳赚 X%」。用户原话：没有存款利率作为参考，容易冲动买股，存款 baseline
严重影响判断。

**用户裁决（勿重开）**：
- 基准构成 = 完整现金基准：定存挂牌（主锚）+ 货基 7 日年化 + 中美 10Y + LPR；
- **纯信息注入**，不加系统侧硬闸（「LLM 是交易员」一脉，与策略注入同一裁决）。

数据链路（2026-11-08 用户定：**所有取数统一在 101 data_service**，本机不直连）：
- LPR / 货基 → 101 `/cash-baseline`（data_service/rates.py，akshare 双源，
  服务端缓存 6h）；本模块再落一层磁盘缓存（data/cash_baseline.json）。
- 中/美 10Y → 直接读 `sa_macro_rates` 表（macro_rates.latest，零网络）。
- **存款挂牌利率没有可靠在线源**（东财 RPT_ECONOMY_DEPOSIT_RATE 是央行基准，
  2015-10-24 起冻结在 1.5%，远高于现行挂牌 ~0.95%，注入会误导）→ 由
  config.yaml `cash_baseline.deposit_1y/deposit_3y/deposit_updated` 手工维护，
  超过 90 天没更新会在注入文本里标注「请核对本行」。

注入块语义（写进文本，不许省）：
- 「低于基准＝承担波动换不来补偿」+「空仓持有现金/货基同样是一种理性选择」；
- 纯信息口径：不是交易指令；
- 无数据返回 ""（上下文逐字节不变）。
"""
from __future__ import annotations

import json
import math
import threading
from datetime import date, datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
CACHE_FILE = BASE_DIR / "data" / "cash_baseline.json"

#: 决策注入的默认参数（调用方合并 paper.cash_baseline_inject 后再传入）。
INJECT_DEFAULTS = {"enabled": True, "max_chars": 1200}

DEFAULTS = {
    # 同步开关与节奏：存款利率是「月级」慢变量，默认一周刷一次足够。
    "enabled": True,
    "sync_interval_hours": 168,
    # 存款挂牌利率（config 手工维护）。默认值 = 2026-11 大行挂牌
    # （工行 1Y 0.95% / 3Y 1.25%，实测口径见 AGENTS.md），用户应按自己
    # 实际存款银行更新 —— 没有在线源，改这里。
    "deposit_1y": 0.95,
    "deposit_3y": 1.25,
    "deposit_updated": "2026-11-08",
}

_SYNC_LOCK = threading.Lock()


def load_conf() -> dict:
    """读 config.yaml 顶层 `cash_baseline:` 段，补齐默认（每轮现读，改配置即生效）。

    嵌套必须**深合并**（wechat_mp.load_mp_conf 同类教训：浅 update 会把只写了
    一个键的段顶成默认值）。"""
    conf = dict(DEFAULTS)
    try:
        import yaml
        data = yaml.safe_load((BASE_DIR / "config.yaml").read_text(encoding="utf-8")) or {}
        for k, v in (data.get("cash_baseline") or {}).items():
            if v is None:
                continue
            conf[k] = v
    except FileNotFoundError:
        pass
    except Exception as exc:      # noqa: BLE001
        print(f"[cash_baseline] 读取配置失败（用默认值）: {exc}", flush=True)
    return conf


def _num(v) -> float | None:
    """数值闸门：NaN/inf/非数一律 None（AGENTS.md 的 NaN 坑）。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# ---------------------------------------------------------------------------
# 磁盘缓存（data/cash_baseline.json，tmp+replace 原子写）
# ---------------------------------------------------------------------------

def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:      # noqa: BLE001
        print(f"[cash_baseline] 缓存读取失败（当不存在处理）: {exc}", flush=True)
        return {}


def _write_cache(payload: dict) -> bool:
    tmp = CACHE_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(CACHE_FILE)
        return True
    except OSError as exc:
        # 缓存写失败绝不能静默（hk_supply 那次的教训：跑了 25 分钟、0 个缓存
        # 文件，界面看起来像「没实现缓存」）。
        print(f"[cash_baseline] 缓存写入失败 {CACHE_FILE}: {exc} —— "
              "本进程内仍可用，重启后需重新同步", flush=True)
        return False


def sync_due(conf: dict | None = None, now: datetime | None = None) -> bool:
    """距上次成功同步是否已超过 sync_interval_hours。"""
    conf = conf or load_conf()
    cache = _load_cache()
    synced_at = cache.get("synced_at")
    if not synced_at:
        return True
    try:
        last = datetime.fromisoformat(str(synced_at))
    except ValueError:
        return True
    now = now or datetime.now()
    hours = max(1, int(conf.get("sync_interval_hours") or DEFAULTS["sync_interval_hours"]))
    return (now - last).total_seconds() >= hours * 3600


def _sync_impl(conf: dict, force: bool) -> dict:
    """sync 的本体。调用方必须已持有 _SYNC_LOCK（sync 抢锁；sync_async 由
    工作线程持锁进来），本函数绝不自己抢/放锁。"""
    cache = _load_cache()
    if not force and not sync_due(conf):
        return {"ok": True, "skipped": True, "synced_at": cache.get("synced_at")}
    import market_data
    snap = market_data.service_get("/cash-baseline", {}, timeout=120)
    payload = {
        "synced_at": datetime.now().isoformat(timespec="seconds"),
        "lpr": snap.get("lpr"),
        "money_fund": snap.get("money_fund"),
        "errors": snap.get("errors") or {},
        "notes": snap.get("notes") or [],
    }
    if not payload["lpr"] and not payload["money_fund"]:
        # 101 两源全挂（还给了旧快照或 503）——保留旧缓存，别拿空数据覆盖。
        old = cache.get("lpr") or cache.get("money_fund")
        if old:
            print(f"[cash_baseline] 101 两源均无数据，沿用旧缓存 "
                  f"({cache.get('synced_at')})", flush=True)
            return {"ok": False, "kept_old": True,
                    "error": str(payload["errors"])[:300]}
        return {"ok": False, "error": "101 /cash-baseline 无数据: "
                + str(payload["errors"])[:300]}
    _write_cache(payload)
    return {"ok": True, "synced_at": payload["synced_at"],
            "lpr": payload["lpr"], "money_fund": payload["money_fund"],
            "errors": payload["errors"]}


def sync(conf: dict | None = None, force: bool = False) -> dict:
    """从 101 /cash-baseline 拉一次快照并落盘。失败沿用旧缓存不清空。

    单次就是一条 HTTP（101 侧有 6h 缓存），同步执行即可；拿不到锁返回 busy。
    """
    conf = conf or load_conf()
    if not _SYNC_LOCK.acquire(blocking=False):
        return {"ok": False, "busy": True}
    try:
        return _sync_impl(conf, force)
    except Exception as exc:      # noqa: BLE001
        print(f"[cash_baseline] 同步失败（沿用旧缓存）: {exc}", flush=True)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
    finally:
        _SYNC_LOCK.release()


def sync_async(conf: dict | None = None, force: bool = False) -> dict:
    """后台线程跑 sync（手动触发入口用）；拿不到锁立即返回 busy 不排队。"""
    import threading as _t
    got = _SYNC_LOCK.acquire(blocking=False)
    if not got:
        return {"started": False, "busy": True}
    conf = conf or load_conf()

    def _run():
        try:
            _sync_impl(conf, force)   # 锁已由本线程持有，_sync_impl 不重抢
        except Exception as exc:      # noqa: BLE001
            print(f"[cash_baseline] 异步同步失败: {exc}", flush=True)
        finally:
            _SYNC_LOCK.release()

    _t.Thread(target=_run, daemon=True, name="sa_cash_baseline_sync").start()
    return {"started": True}


def status() -> dict:
    """页面/端点用的快照：缓存 + 配置摘要（不触发网络）。"""
    conf = load_conf()
    cache = _load_cache()
    return {
        "synced_at": cache.get("synced_at"),
        "lpr": cache.get("lpr"),
        "money_fund": cache.get("money_fund"),
        "notes": cache.get("notes") or [],
        "errors": cache.get("errors") or {},
        "deposit": {"1y": _num(conf.get("deposit_1y")),
                    "3y": _num(conf.get("deposit_3y")),
                    "updated": conf.get("deposit_updated")},
        "sync_interval_hours": conf.get("sync_interval_hours"),
        "due": sync_due(conf),
    }


# ---------------------------------------------------------------------------
# 决策注入
# ---------------------------------------------------------------------------

def _pct(v, nd=2) -> str | None:
    f = _num(v)
    return f"{f:.{nd}f}%" if f is not None else None


def _fmt_line(prefix: str, parts: list[str]) -> str | None:
    """把非空片段拼成一行；全空返回 None（该行整个省略，不留『：』空行）。"""
    seg = "｜".join(p for p in parts if p)
    return f"· {prefix}：{seg}" if seg else None


def build_injection(conn=None, code: str = "", name: str = "",
                    recent_ret20: float | None = None,
                    conf: dict | None = None,
                    max_chars: int | None = None) -> str:
    """「现金/存款基准（机会成本）」注入块。无数据返回 ""。

    ⚠️ conn 为 None 时不读 sa_macro_rates（页面/离线测试场景），10Y 行省略。
    enabled 闸门在调用方（_decide_one），本函数不判。
    """
    cap = max(200, int(max_chars or INJECT_DEFAULTS["max_chars"]))
    lines: list[str] = []

    # 存款挂牌（config 手工维护，主锚）
    cb = load_conf()
    dep_parts = []
    v1, v3 = _num(cb.get("deposit_1y")), _num(cb.get("deposit_3y"))
    if v1 is not None:
        dep_parts.append(f"1年期 {v1:.2f}%")
    if v3 is not None:
        dep_parts.append(f"3年期 {v3:.2f}%")
    if dep_parts:
        stale_note = ""
        try:
            upd = date.fromisoformat(str(cb.get("deposit_updated") or ""))
            days = (date.today() - upd).days
            stale_note = f"，更新于 {upd.isoformat()}"
            if days > 90:
                stale_note += f"（已 {days} 天未更新，请核对本行挂牌价）"
        except ValueError:
            stale_note = "，更新日期未配置"
        lines.append("· 1年期定存挂牌：" + "｜".join(dep_parts)
                     + f"（config 手工维护{stale_note}；以本行实际挂牌为准）")

    cache = _load_cache()
    mf, lpr = cache.get("money_fund") or {}, cache.get("lpr") or {}
    if mf.get("median") is not None:
        lines.append(f"· 货基 7 日年化中位数：{_pct(mf['median'], 3)} "
                     f"（{mf.get('count', '?')} 只，截至 {mf.get('date', '?')}）")
    lpr_parts = []
    if lpr.get("lpr1y") is not None:
        lpr_parts.append(f"1年 {_pct(lpr['lpr1y'])}")
    if lpr.get("lpr5y") is not None:
        lpr_parts.append(f"5年 {_pct(lpr['lpr5y'])}")
    row = _fmt_line("LPR（贷款端参考）", lpr_parts)
    if row:
        if lpr.get("date"):
            row += f"（截至 {lpr['date']}）"
        lines.append(row)

    # 中/美 10Y：直接读 sa_macro_rates（sa_macro 线程每日同步，零网络）
    if conn is not None:
        try:
            import macro_rates as _mr
            r = _mr.latest(conn) or {}
            cn, us = _pct(r.get("cn10y")), _pct(r.get("us10y"))
            if cn or us:
                seg = "｜".join(x for x in (
                    f"中国 {cn}" if cn else None,
                    f"美国 {us}" if us else None) if x)
                lines.append(f"· 10Y 国债：{seg}（截至 {r.get('stat_date', '?')}）")
        except Exception as exc:  # noqa: BLE001
            print(f"[cash_baseline] 10Y 读取跳过: {exc}", flush=True)

    # 逐票对照行：把「无风险基准」和这只票的短期涨幅摆在一起
    r20 = _num(recent_ret20)
    if r20 is not None:
        tag = name or code or "本股"
        lines.append(
            f"· 对照：{tag}近20日涨幅 {r20:+.1f}%。短期涨幅不是年化收益的依据——"
            "判断买入/持有前，先估未来一年预期收益是否显著高于上述无风险基准；"
            "低于基准＝承担波动换不来补偿；空仓持有现金/货基同样是一种理性选择。")
    elif lines:
        lines.append("· 对照：判断买入/持有前，先估未来一年预期收益是否显著高于"
                     "上述无风险基准；低于基准＝承担波动换不来补偿；空仓持有"
                     "现金/货基同样是一种理性选择。")

    if not lines:
        return ""
    lines.append("⚠ 本块只提供无风险基准与机会成本口径，不是交易指令。")
    text = "【现金/存款基准（机会成本）】\n" + "\n".join(lines)
    if len(text) > cap:
        text = text[:cap] + f"\n[现金基准块超长已截断，全文 {len(text)} 字符]"
    return text
