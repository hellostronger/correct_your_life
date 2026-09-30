# -*- coding: utf-8 -*-
"""沙箱编排：把抓来的聚宽源码丢进 101 的容器里跑，回收结果落库。

整体流程
--------
    ① 从云库切一段行情（只切策略需要的那些票 + 基准）
    ② 静态扫源码（先抓明显的危险/缺失，别浪费一次容器往返）
    ③ 把 策略源码 + jq_api.py + sandbox_runner.py + data.csv 打包
    ④ scp 到 101，用 docker run 跑（--network=none + 资源上限 + 只读根）
    ⑤ 解析 stdout 里那行 #RESULT {json}
    ⑥ 落 sa_backtest_run / sa_backtest_daily，算超额

为什么必须真的用容器，而不是「在本地 python 里 import 一下跑」
------------------------------------------------------
抓来的源码是**完全不可信的**。社区里就有这种：

    def initialize(context):
        while True:      # 死循环
            pass
    # 或者
    b = bytearray(10**12)   # 内存炸弹

在本地进程里跑这些 = 我的机器卡死 / 磁盘写满。而且我无法保证
「以后自动跑的」时候不会遇到更恶意的样本。所以隔离不是可选项。

隔离强度（都实测过，不靠猜）
--------------------------
| 手段 | 挡什么 |
|---|---|
| --network=none | 数据外传、下载第二个 payload |
| --memory=512m --memory-swap=512m | 内存炸弹（超了直接 OOM kill） |
| --cpus=1 | 死循环烧满 CPU（配合墙钟超时） |
| --pids-limit=128 | fork 炸弹 |
| --read-only + tmpfs /tmp | 改系统、铺盘 |
| --ulimit fsize=1MB | 单文件写爆（tmpfs 也挡不住总量） |
| --cap-drop=ALL --security-opt=no-new-privileges | 提权 |
| 非 root（镜像里 uid 10005）| 逃出沙箱后也是 nobody |
| docker run --rm | 跑完不留痕 |
| 墙钟超时（本地计时）| 挂死 |

**它挡不住什么**（说清楚比含糊强）：容器共享宿主内核，所以
「内核 0-day 逃逸」这一类不在防护范围内。对「爬来的策略代码」这个
用途，真实风险是死循环 / 内存炸弹 / 写文件 / 联网，这四条都挡住了。
真要再强一档得上 gVisor 或 nsjail，但 101 只有 3G 内存、还跑着 9 个容器，
塞不下。

**先本地干跑，再上容器**
----------------------
很多失败（策略语法错、API 用错、路径写错）跟沙箱无关。本地跑一遍
能省掉一次 ssh 往返和一次镜像启动，实测也真是这样 —— 头三次失败全在
本地干跑阶段就暴露了，压根没进容器。
"""
from __future__ import annotations

import base64
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
RESULT_TAG = "#RESULT "
DOCKER_IMAGE = "jq-sandbox:py312"
DEFAULT_HOST = "root@101.43.25.101"

SANDBOX_DEFAULTS = {
    "memory": "512m",
    "memory_swap": "512m",
    "cpus": "1.0",
    "pids_limit": "128",
    "timeout_sec": 120,
    "tmpfs_mb": "64",
    "fsize_mb": "1",
}

FIELDS = ["open", "high", "low", "close", "volume"]


class SandboxError(Exception):
    pass


# ==========================================================================
# ① 切数据
# ==========================================================================

def build_slice_csv(get_conn, codes: list, start: str, end: str,
                    out_path: Path, benchmark: str = "") -> dict:
    """从云库切一段日线写成宽表 CSV（列名 `code.field`，runner 靠它 pivot 回去）。

    只切策略**声明要用的**那些票 —— 沙箱里没有数据库，给多少传多少。
    不知道策略要哪些票时，调用方先用 `referenced_codes` 静态抠。
    """
    codes = sorted({str(c) for c in codes if c})
    if benchmark and benchmark not in codes:
        codes.append(benchmark)
    if not codes:
        raise SandboxError("没有要回测的标的（codes 为空）")
    if len(codes) > 400:
        raise SandboxError(
            "一次最多切 400 只（当前 %d）—— 数据切片会太大，"
            "而且真用到几百只的多半是宽基轮动，那种应该用指数 ETF 代替"
            % len(codes))
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT code, trade_date, open, high, low, close, volume
               FROM sa_market_kline
               WHERE code = ANY(%s) AND trade_date BETWEEN %s AND %s
               ORDER BY code, trade_date""",
            (codes, start, end))
        rows = cur.fetchall()
    if not rows:
        raise SandboxError(
            "%s ~ %s 在 sa_market_kline 里没有数据，先补数据再回测" % (start, end))
    by_code = {}
    for r in rows:
        by_code.setdefault(r[0], []).append(r)
    out = io.StringIO()
    out.write("date," + ",".join("%s.%s" % (c, f)
                                 for c in sorted(by_code)
                                 for f in FIELDS) + "\n")
    days = sorted({r[1] for r in rows})
    idx = {c: {r[1]: r for r in v} for c, v in by_code.items()}
    for d in days:
        cells = [str(d)]
        for c in sorted(by_code):
            r = idx[c].get(d)
            for j in range(len(FIELDS)):
                # psycopg2 把 NUMERIC 列取成 decimal.Decimal，不是 float 也不是
                # str，直接 join 会报
                # "sequence item 1: expected str instance, decimal.Decimal found"
                v = r[2 + j] if r else None
                cells.append("" if v is None else str(v))
        out.write(",".join(cells) + "\n")
    out_path.write_text(out.getvalue(), encoding="utf-8")
    return {"codes": sorted(by_code), "days": len(days), "rows": len(rows),
            "path": str(out_path), "bytes": out_path.stat().st_size}


# ==========================================================================
# ② 静态扫
# ==========================================================================

BANNED_IMPORTS = {
    "subprocess", "socket", "shutil", "ctypes", "multiprocessing",
    "urllib", "urllib2", "http", "ftplib", "smtplib", "telnetlib",
    "paramiko", "requests", "aiohttp", "pty", "signal", "resource",
    "sysconfig", "imp", "importlib",
}

BANNED_PATTERNS = [
    (r"\bos\.system\s*\(", "os.system", "high"),
    (r"\bos\.popen\s*\(", "os.popen", "high"),
    (r"\beval\s*\(", "eval", "high"),
    (r"\bexec\s*\(", "exec", "high"),
    (r"\b__import__\s*\(", "__import__", "high"),
    (r"\bopen\s*\(\s*['\"]/", "open 绝对路径", "high"),
    (r"\bwhile\s+True\s*:", "while True（可能死循环）", "medium"),
    (r"bytearray\s*\(\s*10\s*\*\*", "bytearray(10**N) 内存炸弹", "high"),
]


def static_scan(code: str) -> dict:
    """静态扫源码。**只报告，不拦** —— 拦了会误杀（社区策略里偶尔真的
    只是拿 eval 处理字符串），报告给人看更合适。

    但 `import socket/subprocess` 标 high：在策略代码里没有正当用途。
    """
    import re
    hits = []
    for name in sorted(BANNED_IMPORTS):
        if re.search(r"^\s*(?:import\s+%s\b|from\s+%s\b)" % (name, name),
                     code, re.M):
            hits.append({"level": "high", "kind": "import", "detail": name})
    for pat, name, level in BANNED_PATTERNS:
        for m in re.finditer(pat, code):
            hits.append({"level": level, "kind": "pattern", "detail": name,
                         "line": code[:m.start()].count("\n") + 1})
    stub = len(re.findall(r"^\s*(?:\.\.\.|pass)\s*$", code, re.M))
    funcs = re.findall(r"^\s*def\s+(\w+)", code, re.M)
    return {"risks": hits, "n_lines": len(code.split("\n")),
            "n_funcs": len(funcs), "n_stub_markers": stub,
            "looks_redacted": stub >= 2, "funcs": funcs[:60],
            "has_initialize": "initialize" in funcs}


def referenced_codes(code: str, known=None) -> list:
    """从源码里静态抠出可能用到的标的代码。

    两种来源：
      ① 硬编码的 6 位代码（'600519' / '000001.XSHG' / 'sh600519'）
      ② get_all_securities() 之类的**全市场**调用 -> 记一个特殊标记
    第②种是宽基/小市值类策略的常态，那种一次要几千只，切不动 ——
    所以返回 `__ALL_MARKET__` 标记，调用方要么换 ETF 代替，要么明确
    告诉用户「这个策略需要先补全市场数据」。
    """
    import re
    found = set()
    for m in re.finditer(r"['\"](\d{6})(?:\.(?:XSHG|XSHE|SH|SZ))?['\"]", code):
        found.add(m.group(1))
    for m in re.finditer(r"['\"](?:sh|sz)(\d{6})['\"]", code, re.I):
        found.add(m.group(1))
    if re.search(r"get_all_securities|get_index_stocks|get_industry_stocks",
                 code):
        found.add("__ALL_MARKET__")
    return sorted(found)


# ==========================================================================
# ③ 跑
# ==========================================================================

def _pack(workdir: Path, code: str, start: str, end: str, cash: float,
          benchmark: str, in_container: bool = True) -> str:
    for name in ("jq_api.py", "sandbox_runner.py"):
        src = BASE_DIR / name
        if not src.exists():
            raise SandboxError("找不到 %s" % src)
        (workdir / name).write_text(src.read_text(encoding="utf-8"),
                                    encoding="utf-8")
    (workdir / "strategy.py").write_text(code, encoding="utf-8")
    # 容器里工作目录挂在 /work；本地干跑要用临时目录的绝对路径。
    # 第一版两个路径都写死成 /work/...，本地干跑连着两个
    # FileNotFoundError（先 data.csv 后 strategy.py）。
    work = "/work" if in_container else str(workdir)
    return json.dumps({"data": "%s/data.csv" % work,
                       "strategy": "%s/strategy.py" % work,
                       "start": str(start), "end": str(end),
                       "cash": cash, "benchmark": benchmark or ""},
                      ensure_ascii=False)


def _stage(code: str, data_csv: Path, start: str, end: str, cash: float,
           benchmark: str, in_container: bool) -> tuple:
    """准备工作目录并返回 (临时目录对象, cfg)。临时目录必须由调用方持有。"""
    td = tempfile.TemporaryDirectory(prefix="jqsb_")
    w = Path(td.name)
    shutil.copyfile(str(data_csv), str(w / "data.csv"))
    cfg = _pack(w, code, start, end, cash, benchmark, in_container)
    return td, w, cfg


def run_local(code: str, data_csv: Path, start: str, end: str, cash: float,
              benchmark: str = "", timeout: int = 120) -> dict:
    """在本机跑一遍（**无隔离**）。只用于快速失败，正式结果必须过容器。"""
    td, w, cfg = _stage(code, data_csv, start, end, cash, benchmark, False)
    try:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONPATH"] = str(BASE_DIR)
        # 注意：**可执行文件不能 shell-quote**。shlex.quote 在 POSIX 上加单引号
        # 没问题，Windows 上会变成 'C:\...\python.exe'，CreateProcess 找不到，
        # 报 [WinError 2]。要引号的是参数，不是 exe。
        p = subprocess.run(
            [sys.executable, "-u", str(BASE_DIR / "sandbox_runner.py"), cfg],
            capture_output=True, timeout=timeout, cwd=str(BASE_DIR), env=env)
        return _parse(p.stdout.decode("utf-8", "replace"),
                      p.stderr.decode("utf-8", "replace"), p.returncode)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "本地干跑超时（>%ds）" % timeout,
                "killed_by": "wall-clock"}
    finally:
        td.cleanup()


def docker_cmd(cfg: str, host_workdir: str, limits: dict = None) -> list:
    """组装 docker run 命令。参数含义见模块 docstring 的隔离表。"""
    L = dict(SANDBOX_DEFAULTS)
    L.update(limits or {})
    return [
        "docker", "run", "--rm",
        "--network=none",
        "--memory=%s" % L["memory"],
        "--memory-swap=%s" % L["memory_swap"],
        "--cpus=%s" % L["cpus"],
        "--pids-limit=%s" % L["pids_limit"],
        "--read-only",
        "--tmpfs", "/tmp:rw,size=%sm,mode=1777" % L["tmpfs_mb"],
        "--ulimit", "nofile=256:256",
        "--ulimit", "fsize=%s000:1048576" % L["fsize_mb"],
        "--cap-drop=ALL",
        "--security-opt", "no-new-privileges",
        "-v", "%s:/work:ro" % host_workdir,
        "-w", "/work",
        DOCKER_IMAGE, cfg,
    ]


def run_in_docker(code: str, data_csv: Path, start: str, end: str,
                  cash: float, benchmark: str = "", host: str = DEFAULT_HOST,
                  ssh_key: str = None, timeout: int = None,
                  limits: dict = None) -> dict:
    """把工作目录传到远端，docker run，再把结果拿回来。"""
    L = dict(SANDBOX_DEFAULTS)
    L.update(limits or {})
    tmo = int(timeout or L["timeout_sec"])
    ssh_key = ssh_key or str(Path.home() / ".ssh" / "sa_deploy_ed25519")
    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    remote_dir = "/tmp/jqsb_%s" % stamp
    ssh = ["ssh", "-i", ssh_key, "-o", "BatchMode=yes",
           "-o", "ConnectTimeout=20", host]

    td, w, cfg = _stage(code, data_csv, start, end, cash, benchmark, True)
    try:
        b64 = base64.b64encode(_tar_dir(w)).decode("ascii")
        prep = ("mkdir -p %s && cd %s && echo %s | base64 -d | tar xzf -"
                % (remote_dir, remote_dir, b64))
        p = subprocess.run(ssh + [prep], capture_output=True, timeout=300)
        if p.returncode != 0:
            raise SandboxError("上传工作目录失败：%s"
                               % p.stderr.decode("utf-8", "replace")[:300])
        cmd = " ".join(shlex.quote(x) for x in docker_cmd(cfg, remote_dir, limits))
        try:
            p = subprocess.run(ssh + ["cd %s && timeout %d %s"
                                      % (remote_dir, tmo, cmd)],
                               capture_output=True, timeout=tmo + 120)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "沙箱超时（>%ds）" % tmo,
                    "killed_by": "wall-clock", "truncated": True}
        return _parse(p.stdout.decode("utf-8", "replace"),
                      p.stderr.decode("utf-8", "replace"), p.returncode)
    finally:
        subprocess.run(ssh + ["rm -rf %s" % remote_dir], capture_output=True)
        td.cleanup()


def _tar_dir(w: Path) -> bytes:
    """打成 tar.gz 的字节流（不落盘）。"""
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for f in sorted(w.iterdir()):
            if f.is_file():
                tf.add(str(f), arcname=f.name)
    return buf.getvalue()


def _parse(stdout: str, stderr: str, rc: int) -> dict:
    """从 stdout 里取那行 #RESULT。

    策略自己会 print（而且通常 print 得很多），所以 stdout 前面全是噪音是
    常态 —— 只认这个前缀，且**从后往前找**（策略可能在收尾时又 print）。
    这个协议是我在 sandbox_runner.py 里定的，两边必须对齐。
    """
    line = None
    for l in reversed(stdout.splitlines()):
        if l.startswith(RESULT_TAG):
            line = l[len(RESULT_TAG):]
            break
    if not line:
        return {"ok": False,
                "error": "沙箱没有输出 %s 行" % RESULT_TAG.strip(),
                "returncode": rc, "stdout_tail": stdout[-3000:],
                "stderr_tail": stderr[-2000:]}
    try:
        res = json.loads(line)
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": "结果行不是合法 JSON：%s" % exc,
                "raw": line[:2000], "returncode": rc}
    res["returncode"] = rc
    if stderr.strip():
        res["stderr_tail"] = stderr[-1500:]
    return res


def sandbox_health(host: str = DEFAULT_HOST,
                    ssh_key: str = None) -> dict:
    """查沙箱那边通不通：镜像在不在、docker 能不能用、磁盘余量。

    跑之前先调它 —— 「容器不存在」和「策略跑失败」是两回事，
    不先分清的话每次都要人去翻远端日志。
    """
    ssh_key = ssh_key or str(Path.home() / ".ssh" / "sa_deploy_ed25519")
    script = (
        "docker images %s --format '{{{{.Repository}}}}:{{{{.Tag}}}} "
        "{{{{.Size}}}}' ; echo '---'; "
        "df -h / | tail -1 | awk '{print $4}'; echo '---'; "
        "free -m | awk '/Mem:/{print $7}'" % DOCKER_IMAGE)
    p = subprocess.run(["ssh", "-i", ssh_key, "-o", "BatchMode=yes",
                        "-o", "ConnectTimeout=20", host, script],
                       capture_output=True, timeout=60)
    if p.returncode != 0:
        return {"ok": False,
                "error": p.stderr.decode("utf-8", "replace")[:300]}
    lines = p.stdout.decode("utf-8", "replace").strip().split("\n")
    img = lines[0].strip() if lines and lines[0].strip() else ""
    disk = lines[2].strip() if len(lines) > 2 else ""
    mem = lines[4].strip() if len(lines) > 4 else ""
    return {"ok": bool(img), "image": img, "disk_free": disk,
            "mem_available_mb": mem,
            "hint": "" if img else
            "镜像 %s 不存在，先在 101 上构建（见 /opt/jq-sandbox/Dockerfile）"
            % DOCKER_IMAGE}


# ==========================================================================
# ④ 落库
# ==========================================================================

def save_run(get_conn, result: dict, name: str, start: str, end: str,
             universe: list, params: dict, init_cash: float,
             benchmark: str = "", strategy_id: int = None,
             bench_return: float = None, article_id: str = None) -> int:
    """把沙箱结果写进 sa_backtest_run / sa_backtest_daily。

    和本地 backtest.run() 用同一套表，这样前端「回测」页不用区分
    「本地引擎跑的」和「沙箱跑的聚宽策略」—— 都是同一种东西。
    """
    m = result.get("metrics") or {}
    ts = result.get("trade_stats") or {}
    daily = result.get("daily") or []
    err = ""
    if not result.get("ok"):
        err = ((result.get("error") or "") + "\n"
               + (result.get("traceback") or ""))[:4000]

    with get_conn() as conn:
        with conn.cursor() as cur:
            if strategy_id is None and article_id:
                cur.execute(
                    """INSERT INTO sa_strategy_def
                       (name, article_id, runnable, code, params, universe, note)
                       VALUES (%s,%s,'idea','',%s,%s,%s)
                       RETURNING id""",
                    (name[:160], article_id,
                     json.dumps(params, ensure_ascii=False),
                     json.dumps(universe, ensure_ascii=False),
                     "由沙箱回测自动登记"))
                r = cur.fetchone()
                strategy_id = r[0] if r else None
            cur.execute(
                """INSERT INTO sa_backtest_run
                   (strategy_id, name, start_date, end_date, universe, params,
                    init_cash, total_return, annual_return, max_drawdown,
                    sharpe, win_rate, trade_count, benchmark, bench_return,
                    excess, error, elapsed_ms)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   RETURNING id""",
                (strategy_id, name[:160], start, end,
                 json.dumps(universe, ensure_ascii=False),
                 json.dumps(params, ensure_ascii=False), init_cash,
                 _pct(m.get("total_return")), _pct(m.get("annual_return")),
                 _pct(m.get("max_drawdown")), m.get("sharpe"),
                 _pct(ts.get("win_rate")), ts.get("n_trades"),
                 (benchmark or "")[:16], _pct(bench_return),
                 _pct((m.get("annual_return") or 0) - (bench_return or 0)
                      if bench_return is not None else None),
                 err, int((result.get("elapsed") or 0) * 1000)))
            run_id = cur.fetchone()[0]
            if daily:
                import numpy as np
                eq = np.array([d["equity"] for d in daily], dtype=float)
                peak = np.maximum.accumulate(eq) if len(eq) else eq
                dd = (eq / peak - 1.0) if len(eq) else eq
                rows = [(run_id, d["date"], d["equity"], d["cash"],
                         d["market_value"],
                         round(float(dd[i]) * 100, 4) if len(eq) else 0.0,
                         json.dumps(d.get("holdings") or [],
                                    ensure_ascii=False))
                        for i, d in enumerate(daily)]
                cur.executemany(
                    """INSERT INTO sa_backtest_daily
                       (run_id, trade_date, equity, cash, position_value,
                        drawdown, holdings)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (run_id, trade_date) DO NOTHING""", rows)
        conn.commit()
    return run_id


def _pct(v):
    """小数 -> 百分数（表里存 %，口径跟本地回测一致）。"""
    if v is None:
        return None
    try:
        return round(float(v) * 100.0, 4)
    except (TypeError, ValueError):
        return None
