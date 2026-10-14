# -*- coding: utf-8 -*-
"""hk-supply 服务（跑在 101）：港股解禁期解析。

为什么单独一个服务（而不是并进 data_service）
--------------------------------------------
`sa-data-service` 的职责边界很清楚：**板块/行业/概念行情聚合**
（ths + kph + sina + ztpool 四个源），产出结构化行情快照。
港股解禁是另一类东西：数据源是 HKEXnews 的 **PDF**，要过 MinerU 解析 +
LLM 抽取，产出**法律条款**而非行情。
塞进去会让它的定位变模糊，而且解析一份要 40 秒，会把 `/boards` 堵住。
所以独立容器、独立端口 8090。

⚠️ 本服务**不要 import app**（本机 app 的 import 要 80 秒且会起守护线程）。
依赖只有 fastapi / uvicorn / requests —— 全部已存在于基座镜像
sa-data-service:1.0.1，**零新依赖**。

为什么必须放在 101（2026-10-12 实测）
------------------------------------
本机到 hkexnews 连续 8 次全部失败（WinError 10054 连接被重置 +
TLS 握手超时，api.ipify.org 同样不通 —— 本机被 wmsxwd 的 TUN 劫持全流量）。
101 侧：prefix.do 200、PDF 200 且 1049004 字节完整。**出口稳定性差一个量级**。

端点
----
GET /health   —— 不发外部请求，查配置与 token 是否就位
GET /scan?codes=02513&max_docs=1 —— 真正的解析（慢：有缓存）
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, HTTPException, Query        # noqa: E402
import requests                                         # noqa: E402

import hk_supply_scan as SCAN                           # noqa: E402
import hk_supply as HS                                  # noqa: E402

TOKEN = os.environ.get("MINERU_API_TOKEN", "").strip()
# ⚠️ 默认值不能用 127.0.0.1 —— 那是**容器自己**的回环，不是宿主机上的网关。
#    172.17.0.1 是 docker0 的宿主侧地址（Linux 默认可达）。
LLM_BASE = os.environ.get("LLM_BASE_URL", "http://172.17.0.1:3000").rstrip("/")
LLM_KEY = os.environ.get("LLM_API_KEY", "").strip()
LLM_MODEL = os.environ.get("LLM_MODEL", "z-ai/glm-5.3-flash")
MAX_DOCS = int(os.environ.get("MAX_DOCS", "3"))

app = FastAPI(title="hk-supply", version="1.0.0")


class _NoLLM(RuntimeError):
    pass


def ask(system: str, user: str, max_tokens: int = 3000) -> str:
    """调自建 LLM 网关。

    ⚠️ 用 requests 直连、**不装 anthropic SDK** —— 基座镜像里没有那个包，
       为一次调用装 SDK 不划算（且 101 磁盘只剩 1.9G/96%）。
    ⚠️ base_url 指向网关时，走的是 **Anthropic 协议** `/v1/messages`
       （本机 llm_advisor 就是这么调的，实测 /v1/messages 返回 401 而非 404，
       说明路径存在、只是缺 key）。
    """
    if not LLM_KEY:
        raise _NoLLM("未配置 LLM_API_KEY")
    payload = {
        "model": LLM_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user",
                      "content": system + "\n\n" + user}],
    }
    r = requests.post(LLM_BASE + "/v1/messages", json=payload,
                      headers={"Content-Type": "application/json",
                               "x-api-key": LLM_KEY,
                               "Authorization": "Bearer " + LLM_KEY,
                               "anthropic-version": "2023-06-01"},
                      timeout=300)
    if r.status_code != 200:
        raise RuntimeError("LLM 网关 HTTP %s: %s" % (r.status_code, r.text[:200]))
    d = r.json()
    # Anthropic 协议：content 是 [{type:text, text:...}]
    parts = []
    for c in (d.get("content") or []):
        if isinstance(c, dict) and c.get("type") == "text":
            parts.append(c.get("text") or "")
    txt = "".join(parts).strip()
    if not txt:
        raise RuntimeError("LLM 返回空 content: %s" % json.dumps(d)[:200])
    return txt


# ---------------- 后台任务 + 进度（2026-10-13 用户要求进度条）----------------
# 为什么不用「同步请求 + 前端转圈」：解析要 3~6 分钟，
# 前端除了转圈什么都不知道，用户反复报「一直没结果不知道什么情况」。
# 改成 POST /scan 立刻返回 job_id，GET /job/{id} 轮询 —— 前端能显示
# 「正在解析 3/24 份公告（02513）」这种**真实进度**。
#
# ⚠️ 任务表是**进程内内存**的（重启即丢）。这是刻意的：任务本身是幂等的
#    （缓存已经落盘），重启后重跑即可，不需要持久化。
#    真正跨重启的进度信息在 data/hk_supply_cache 里。
JOBS: dict = {}
JOBS_MAX = 20          # 防止无限堆积


def _new_job(codes, max_docs, refresh):
    import uuid
    jid = uuid.uuid4().hex[:12]
    # 超量就丢最老的（已完成优先丢）
    if len(JOBS) >= JOBS_MAX:
        for k in [k for k, v in JOBS.items() if v.get("state") != "running"]:
            JOBS.pop(k, None)
            break
    JOBS[jid] = {
        "job_id": jid, "state": "running", "done": 0, "total": 0,
        "stage": "start", "code": "", "name": "", "status": "排队中",
        "items": [], "error": None, "started": time.time(), "finished": None,
        "codes": codes, "max_docs": max_docs, "refresh": refresh,
    }
    return jid


def _run_job(jid):
    j = JOBS.get(jid)
    if not j:
        return

    def _p(stage, done, total, code, name, status):
        # 后台线程与请求线程共享 JOBS，用锁保护一下避免读到半更新的 dict
        cur = JOBS.get(jid)
        if not cur:
            return
        cur.update({"stage": stage, "done": done, "total": total,
                    "code": code or "", "name": name or "",
                    "status": status or ""})

    try:
        items = SCAN.analyze_many(j["codes"], token=TOKEN, ask_fn=ask,
                                  max_docs=j["max_docs"],
                                  progress_fn=_p,
                                  # ⚠️ refresh 必须**透传**：之前 _new_job 存了它、
                                  #   analyze_many 却没这个参数，于是 refresh=true
                                  #   完全无效（实测 5 秒就返回「done」，
                                  #   因为全走的缓存）。典型的「存了但没用」。
                                  refresh=bool(j.get("refresh")))
        j["items"] = items
        j["summary"] = SCAN.summarize(items)
        j["state"] = "done"
    except Exception as exc:                       # noqa: BLE001
        j["state"] = "error"
        j["error"] = "分析异常：%s" % str(exc)[:300]
    finally:
        j["finished"] = time.time()


@app.post("/scan")
def scan_start(codes: str = Query(..., description="逗号分隔的港股代码，**必填**"),
              max_docs: int = Query(0),
              refresh: bool = Query(False)):
    """**立即返回** job_id，不阻塞。进度用 GET /job/{job_id} 轮询。"""
    if not TOKEN:
        raise HTTPException(200, "未配置 MINERU_API_TOKEN")
    want = [c.strip() for c in codes.split(",") if c.strip()]
    if not want:
        return {"error": "必须用 ?codes= 指定港股代码（支持任意港股）",
                "hint": "本服务对任意港股代码通用，不内置任何持仓清单"}
    jid = _new_job(want, int(max_docs or MAX_DOCS), refresh)
    threading.Thread(target=_run_job, args=(jid,), daemon=True).start()
    return {"job_id": jid, "state": "running", "total_codes": len(want),
            "poll": "/job/%s" % jid, "boundary": _boundary()}


@app.get("/job/{jid}")
def job_status(jid: str):
    """轮询进度。返回 done/total/stage/current + 最终结果。"""
    j = JOBS.get(jid)
    if not j:
        return {"error": "job %s 不存在（服务可能已重启）" % jid, "state": "gone"}
    out = {
        "job_id": jid, "state": j["state"], "stage": j["stage"],
        "done": j["done"], "total": j["total"], "code": j["code"],
        "name": j["name"], "status": j["status"], "error": j["error"],
        "boundary": _boundary(),
        "elapsed_sec": round((j.get("finished") or time.time()) - j["started"], 1),
    }
    if j["state"] in ("done", "error"):
        out["items"] = j.get("items") or []
        out["summary"] = j.get("summary") or ""
    return out


@app.get("/health")
def health():
    """不外发请求。**只报状态**，不发一次探测就能知道的东西不做。"""
    return {
        "ok": bool(TOKEN and LLM_KEY),
        "mineru_token": bool(TOKEN),
        "llm_key": bool(LLM_KEY),
        "llm_model": LLM_MODEL,
        "llm_base": LLM_BASE,
        "max_docs": MAX_DOCS,
        "retention_days": HS.HKEX_RETENTION_DAYS,
        "reason": "" if (TOKEN and LLM_KEY) else
                  "缺 " + ("MINERU_API_TOKEN" if not TOKEN else "LLM_API_KEY"),
    }


# ⚠️ 刻意**没有** WATCHLIST / HK_CODES 默认清单。理由见 scan_start 里那段注释。


@app.get("/scan")
def scan(codes: str = Query(..., description="逗号分隔的港股代码，**必填**，支持任意港股"),
         max_docs: int = Query(0),
         refresh: bool = Query(False, description="忽略缓存重新解析")):
    """**同步**解析（兼容旧调用/脚本）。慢：有缓存时数十秒，无缓存每份约 40 秒。

    ⚠️ 页面**不走**这条（用户 2026-10-13 要求进度条）——
       走 `POST /scan` + `GET /job/{id}`，才能显示真实进度。
       保留它是因为调试脚本可能还在用 GET。

    **codes 必填且不给默认值**，理由见 scan_start 里那段注释。
    不返回「没有结果」——那是把
    「没配要查什么」显示成「查了没有」，属于最坏的那种假成功。
    """
    if not TOKEN:
        return {"items": [], "error": "未配置 MINERU_API_TOKEN",
                "boundary": _boundary()}
    want = [c.strip() for c in codes.split(",") if c.strip()]
    if not want:
        # ⚠️ **不提供任何默认代码清单**（2026-10-12 用户指出并纠正）。
        #   我第一版把当时持仓的 8 只港股写进 `.env` 的 HK_CODES 当默认值，
        #   那等于把「谁持有」这个**会变的状态固化进部署配置** ——
        #   加一只票就得改 .env + 重新部署，而且从代码/配置里完全看不出
        #   服务「支持哪些股票」，会让人误以为只支持这 8 只。
        #   正确分层：**持仓由调用方（本机 app.py 查 sa_watchlist）传进来**，
        #   本服务对任意港股代码都一视同仁。
        return {"items": [], "error": "必须用 ?codes= 指定港股代码（逗号分隔，"
                                      "支持任意港股，如 codes=02513,00700）",
                "boundary": _boundary(), "hint": "本服务对任意港股代码通用，"
                                                "不内置任何持仓清单"}
    docs = int(max_docs or MAX_DOCS)

    def _one(c):
        try:
            return SCAN.analyze_one(c, "", TOKEN, ask_fn=ask, max_docs=docs,
                                    refresh=refresh)
        except Exception as exc:                       # noqa: BLE001
            return {"code": c, "name": "", "ok": False,
                    "error": "分析异常：%s" % str(exc)[:200],
                    "ipo_lockup": [], "issuer_pledge": [], "announcements": [],
                    "trace": traceback.format_exc()[-400:]}

    t0 = time.time()
    items = [_one(c) for c in want] if want else []
    return {
        "items": items,
        "elapsed_sec": round(time.time() - t0, 1),
        "error": None,
        "boundary": _boundary(),
        "summary": SCAN.summarize(items),
    }


def _boundary() -> str:
    return ("HKEXnews 标题搜索只保留近 %d 天，更早的公告查不到"
            % HS.HKEX_RETENTION_DAYS)