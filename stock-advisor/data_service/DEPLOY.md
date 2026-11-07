# 部署与恢复手册

> 本仓库根目录**就是** `data_service` 包的内容。部署目录 `/opt/sa-data-service`
> 是它的平铺拷贝（没有 `.git`），容器里以 `python -m uvicorn data_service.api:app` 启动。
> 因此克隆下来的目录名可以随意，`run_tests.py` 会显式把包注册成 `data_service`。

## 当前部署（2026-11-08，1.0.6）

| 项 | 值 |
|---|---|
| 机器 | `101.43.25.101`（腾讯云上海） |
| 目录 | `/opt/sa-data-service`（平铺拷贝） |
| 镜像 | `sa-data-service:1.0.6`（image id `40510eaaf6b4`，叠层自 1.0.5） |
| 本次变更 | rates.py 新增 `/cash-baseline`（LPR + 货基 7 日年化）；顺带把
aggregate/market/sources 同步到仓库最新（101 此前一直跑 10-08 旧版：缺
预算跳过上报、缺 /global-index）。5 个旧文件已留 `.bak.20261108`。 |
| 容器 | `sa-data-service`，`healthy` |
| 监听 | `0.0.0.0:8080`（安全组只放行 8080；不要改回只绑回环） |
| 入口 | `http://101.43.25.101:8080` |
| 本机配置 | `stock-advisor/config.yaml` → `sector.data_service_url` |

## 从零构建

```bash
cd /opt/sa-data-service          # 或任意克隆目录
docker build -t sa-data-service:1.0.2 .
docker compose up -d
```

`Dockerfile` 的构建上下文就是本目录（`COPY requirements.txt` + `COPY . ./data_service`），
依赖包含 `akshare / levistock / adata`，首次构建需要联网（aliyun 镜像源）。

## 叠层构建（101 磁盘紧张时用）

**不要**在 101 上全量重建：`/` 只剩 ~2G，而重装 pandas/numpy/akshare 会撑爆。
改完 `.py` 后用 `Dockerfile.overlay`，只增加几十 KB 的一层：

```bash
# 1) scp 覆盖改动文件（先备份原文件）
# 2) 构建
docker build -f Dockerfile.overlay -t sa-data-service:1.0.3 .

# 3) 四道校验，缺一道都可能「看着部署成功了」
docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}' | grep sa-data-service
docker run --rm --entrypoint sha256sum sa-data-service:1.0.3 /app/data_service/*.py
docker run --rm -i --entrypoint python3 sa-data-service:1.0.3 - <<'PY'
import ast
for p in ("/app/data_service/api.py", "/app/data_service/market.py"):
    with open(p, "rb") as f:
        ast.parse(f.read())
    print("OK", p)
PY

# 4) 切标签 + 重启（sed 必须用通配，写死旧标签会静默空操作）
sed -i 's|image: sa-data-service:.*|image: sa-data-service:1.0.3|' docker-compose.yml
grep -n "image:" docker-compose.yml
docker compose up -d             # 不要加 --build
```

⚠️ `docker run ... python3 - <<PY` 必须带 `-i`，否则 stdin 被关掉，Python 读 EOF，
什么都不打印也不报错。

## 部署后验收（外部真请求）

```bash
curl -s http://101.43.25.101:8080/health
curl -s "http://101.43.25.101:8080/kline?secid=1.600519&days=5&fields=f51,f52,f53,f54,f55,f56,f57"
curl -s "http://101.43.25.101:8080/quote?symbols=sh600519,hk00100,whHKDCNY,usAAPL,hkHSI"
curl -s "http://101.43.25.101:8080/intraday?code=600519&period=m5&count=320"
```

回滚：`/opt/sa-data-service/` 下有 `api.py.bak.*`、`__init__.py.bak.*`、
`docker-compose.yml.bak.*`，旧镜像 `sa-data-service:1.0.1` 也还在。

## 行情代理端点（1.0.2 新增）

背景：本机直连东财 `push2his` 被 RST、腾讯旧 K 线接口 501，导致生产日 K 为空、
`trend_state()` 只能返回 `flat`。按用户要求，**本机不再直连任何行情数据源**，
所有取数都经本服务。

### `GET /kline`

| 参数 | 说明 |
|---|---|
| `secid` | 必填，东财口径：`1.600519` / `0.000158` / `116.02513` / `100.HSI`（前缀 1~3 位） |
| `days` | 1~2000，默认 60 |
| `fields` | 默认 `f51,...,f57`；可加 `f58..f61`（振幅/涨跌幅/涨跌额/换手率） |
| `source` | `auto`（默认）/ `em` / `tx` |
| `beg` / `end` | `YYYY-MM-DD`，闭区间过滤 |

返回 `{secid, source, count, klines:[CSV...], notes:[]}`；`ValueError` → 400，
上游全失败 → 502。

**源策略**：`auto` 先东财 `push2his`，失败退腾讯 `newfqkline`（`param={symbol},day,,,{n},qfq`）。
`source` 字段标明实际来源。

> 2026-10-22 实测：**东财在 101 上同样不可达**，四个市场（A / ETF / 科创 / 港股）
> 全部走 `source=tx`、各 61 根。腾讯这条备源是现在日 K 能工作的真正原因，
> 不要因为「本地腾讯旧接口 501」就以为腾讯整体不可用 —— 旧接口与新接口不是一回事。

### `GET /quote`

`symbols` 逗号分隔（≤300），腾讯符号风格：`sh600519` / `sz000158` / `hk00100` /
`hkHSI` / `usAAPL` / `whHKDCNY`。港股个股走新浪（字段更全），其余走腾讯。
返回 `{quotes:{symbol:{name,price,prev_close,open,high,low,volume,amount,
change,change_pct,ratio,market_cap,market,...}}, notes:[]}`。

### `GET /intraday`

`code`（裸代码，自动判市场）/ `period=m5|m30|m60` / `count`（只影响 mkline 请求条数）。
返回与本地旧 `market_data.fetch_intraday` 同形状：
`{code,date,is_hk,bars,points,vwap,vwap_is_true,source,has_ohlc}`。
港股没有 mkline，bars 由分时点合成（`source=tx_trend`、`has_ohlc=false`）。

### 成交量/成交额单位（算 VWAP 错 100 倍就来源于此）

| 标的 | 源给的单位 | 归一化成 f56 |
|---|---|---|
| A 股 / ETF | 手 | 原样 |
| 科创板（688/689） | **股** | ÷100 |
| 港股 | **股** | 原样 |

`f57`（成交额）统一成元：源给万元 → ×1e4；缺失时用 `量 × 收盘价` 估算
（A 股按手×100，港股/科创板按股），**必须保证是数值** ——
调用方 `paper_trading.fetch_kline_ohlcv` 会直接 `float(p[6])`。

## 参数与识别

- `secid` 前缀是 **1~3 位**（东财 A 股是 0/1，港股 116，指数 100），不要写成固定 3 位；
- `tx_to_secid("hkHSI")` 必须得到 `100.HSI`，**只对前缀小写化，代码部分保留大小写**；
- 腾讯日 K 的三层结构是 `data.<symbol>.qfqday`（或 `day`），分时是
  `data.<symbol>.data.{date,data:[...]}` —— 后者是 **dict 不是 list**，写错会静默 0 条。

## 已知漂移（2026-10-22）

本地 `sources.py` / `aggregate.py` 比 101 上部署的版本**新**：本地多了
「同花顺被每日预算挡掉时把 `degraded` 置真并写明原因」的修复
（此前概念板块整轮消失时 `degraded=False`、`healthy=true`，板块数从 971 静默掉到 697）。
1.0.2 的叠层构建**没有**带上这两个文件，所以线上仍是 10-08 旧版。
下次重建（建议标 `1.0.3`）时 `Dockerfile.overlay` 会一并覆盖它们。

## 测试

```bash
python run_tests.py        # 离线：selftest + 预算 + 多源归一 + 行情代理
```

需要真实网络的单独跑（打 101:8080，会消耗采集预算）：

```bash
python tests/test_sector_integration.py
python tests/test_service_api.py
```

行情代理的离线测试（`tests/test_market_proxy.py`，54 项）**完全不联网**，
所有 HTTP 都用 Fake 替换，覆盖符号映射、单位换算、首根无前收、
港股分时合成、VWAP 单位、>300 符号拒绝等。
