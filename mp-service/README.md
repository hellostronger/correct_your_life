# 微信公众号监控服务（WeRSS）· 独立部署

把「爬公众号」单独放到一台远程机器上跑，本仓库的 `stock-advisor` 只当客户端拉它的
RSS 产物。两边**不共享配置、不共享数据、可以各自升级**。

---

## 为什么值得单独拆出去

微信**没有任何官方接口**能读别人公众号的文章（官方 API 只管自己运营的号）。所以必须
有一个常驻服务持有登录态去爬。它天然是「服务端」角色：

- 需要长期存活的登录态（微信读书扫码 / 公众号平台 cookie）
- 需要 Playwright + Chromium，重得可以跑满一个容器
- 会被限流/封控（上游原话：添加订阅频率过高容易封控），**不该**和你的行情工具
  共用同一个出口 IP 和同一个生命周期

所以：本服务放在一台便宜的小机器/NAS 上长期跑，`stock-advisor` 跑在你自己电脑上，
通过 HTTP 拉 RSS。

---

## 现状调研（2026-09-26 核实）

| 方案 | 结论 |
|---|---|
| **WeRSS**（`rachelos/we-mp-rss`） | ✅ 选它。MIT、4.7k star、Python + SQLite 单容器、**2026-09 仍在提交**。支持微信读书渠道 |
| wewe-rss（`cooderl/wewe-rss`） | ⚠️ 2026-05-11 已归档只读；部分接口需经作者的中转服务 `weread.111965.xyz`，不适合长期托管 |
| 公众号平台后台 API | ❌ 只能读自己运营的号 |
| 搜狗微信搜索 | ❌ 早已关停 |

---

## 快速开始

### 方式 A：只要一个 Dockerfile（推荐，最少步骤）

`Dockerfile` 是**完全自包含**的——不依赖本仓库任何文件、不需要 compose。
把它单独拷到任意一台服务器上：

```bash
# 1. 建镜像
docker build -t we-mp-rss .

# 2. 起服务（-v 的 data/ 一定要先建好且可写）
mkdir -p data
docker run -d --name we-mp-rss --restart unless-stopped \
    -p 8001:8001 \
    -v "$(pwd)/data:/app/data" \
    -e TZ=Asia/Shanghai \
    -e USERNAME=admin \
    -e 'PASSWORD=换成你自己的' \
    we-mp-rss

# 3. 看日志
docker logs -f we-mp-rss
```

浏览器打开 `http://<服务器IP>:8001/` → 微信读书扫码登录 → 订阅公众号。

### 方式 B：compose（想用 .env 管配置就选这个）

```bash
cp .env.example .env          # 至少改掉 WERSS_PASSWORD
docker compose up -d
docker compose logs -f
```

两种方式起的是**同一个镜像**，随便挑。

### 接回 stock-advisor

浏览器打开后确认能看到订阅号的文章，然后：

- 网页「⚙️ 调度 → 🧩 完整配置」的 `mp` 段里把 `base_url` 填成
  `http://<服务器IP>:8001`（配一次，网页添加订阅源时自动带入）
- 回「📰 公众号」页添加订阅源，`feed_id` 填 `all`（全部）或某个号的 id（单取一个）

> 服务自带的 `/rss` 路由是免鉴权的（上游源码里 `Depends(verify_rss_access)` 被注释掉，
> 旁边留了注释「真要放开请只允许内网」）。**所以请不要把这个端口直接暴露到公网**，
> 见下面的「加固」一节。

---

## 这个镜像里有什么 / 没有什么

**只有公众号订阅这一件事**：微信读书扫码登录 → 订阅公众号 → 抓文章 → 输出标准 RSS。

- ❌ 不含 stock-advisor（你的行情工具在你自己电脑上跑）
- ❌ 不含 MediaCrawler
- ❌ 不依赖本仓库任何文件 —— `Dockerfile` 单独拷走就能 build

选官方镜像当 base 而不是自己构建：上游 Dockerfile 依赖它私有的基础镜像
`ghcr.io/rachelos/base-full` + `install.sh`（建 venv）+ Playwright Chromium，
照着重写既脆又无意义——上游每次提交都在重新构建这个官方镜像。需要自己构建时
见「从源码构建」。

## 镜像里加了什么

| | 说明 |
|---|---|
| **入口自检** | 启动前先验证数据卷可写，不可写就用一段人话说明怎么修（两种办法都写进日志）并退出。最常见的故障是「容器起来了但写不进去」，默认日志里只有一堆看不懂的 sqlite readonly，甚至要等到抓文章时才暴露 |
| **健康检查** | 上游没有 `/healthz` 这种探针端点，所以用「首页能否返回 HTTP 响应」代替——返回 4xx/5xx 也说明进程活着，是**端口没起来**才判不健康。够挡住「进程崩了」，不精确但不会误报 |
| **时区** | 强制 `Asia/Shanghai`。不设对的话「文章发布时间」会整体偏移 |
| **运行时默认值** | 不传任何 `-e` 也能起（admin/admin@123 + SQLite），但**远程部署请务必改密码** |
| **OCI labels** | 版本/来源/许可证可追溯 |

## 配置

`docker run -e` / `.env`（见 `.env.example`）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `TZ` | `Asia/Shanghai` | 不设对会让文章发布时间整体偏移 |
| `USERNAME` / `PASSWORD` | `admin` / `admin@123` | Web UI 凭据，**远程必须改** |
| `DB` | `sqlite:///data/db.db` | 数据库，见「换 MySQL」 |
| `AUTO_RELOAD` | `False` | 代码热重载；长期运行保持 False，否则改文件会自动重启 |
| `PROXY_ENABLED` / `PROXY_URL` | `False` | 出口代理，仅当这台服务器直连不上微信读书/微信时才需要 |
| `WERSS_DATA_DIR` | `/app/data` | 镜像内路径；**宿主机那侧**用 `-v` 挂，建议放仓库里的 `./data` |

### stock-advisor 侧的 `mp` 段

`stock-advisor/config.yaml`（网页「⚙️ 调度 → 🧩 完整配置」也能改）：

```yaml
mp:
  base_url: "http://<服务器IP>:8001"   # 远程 WeRSS 地址，配一次，网页添加源时自动带入
  auth: ""                            # 远程有鉴权时填（见「加固」）
  use_fresh: false                    # 远程共享实例建议 false（见下）
  interval_minutes: 30
  notify_new: true
```

---

## fresh 与缓存（远程部署的重要取舍）

`stock-advisor` 拉 WeRSS 时会走 `/rss/{feed_id}/fresh`，这个路由会**先让 WeRSS 去
上游抓一轮**再返回（否则只能吃它自己的缓存，而它默认一天才刷两次）。

- **本机自用**：用 fresh，保证新鲜。
- **远程共享实例**：建议 `use_fresh: false`，走 `/rss/{feed_id}` 吃缓存。原因有两个：
  1. 上游明确提示「添加订阅频率过高容易被封控」，`stock-adervisor` 每 30 分钟轮一次
     相当于每 30 分钟催它爬一次，共享实例上这是不礼貌的；
  2. 抓到的是 WeRSS 自己的缓存，**它挂了不影响你的轮询频率**。
  代价是文章最多晚它一个刷新周期才出现。

每个订阅源也可以单独覆盖 `use_fresh`（网页上逐源勾选），所以「大多数源吃缓存、
个别急用的源走 fresh」是可以的。

---

## 加固（远程暴露前必做）

`/rss` 免鉴权是上游的设计，滥用会打到微信的封控策略，而且别人能白嫖你的爬取配额。
按需要挑一种：

### 方案 A：只监听回环 + SSH 端口转发（最简单、最安全）

`docker-compose.yml` 里把端口改成只绑本机：

```yaml
ports:
  - "127.0.0.1:8001:8001"
```

然后在你自己电脑上开隧道：

```bash
ssh -N -L 8001:127.0.0.1:8001 user@服务器
```

`stock-advisor` 里地址填 `http://127.0.0.1:8001`。服务完全不暴露公网。

### 方案 B：反代 + Basic Auth

在前面套 nginx/Caddy，加 Basic Auth，然后给 `stock-advisor` 配上凭据：

```yaml
mp:
  base_url: "https://mp.example.com"
  auth: "Basic <base64(用户名:密码)>"   # 前端/配置里填，会掩码显示
```

`auth` 原样放进请求头，所以 `Basic ...` / `Bearer ...` 都支持。

### 方案 C：只允许 stock-advisor 的 IP

反代里按来源 IP 白名单放行 `/rss/*`，其余 403。

---

## 换 MySQL

默认 SQLite 够用（单文件，备份=复制）。要换：

```yaml
# docker-compose.yml 加一个 db 服务
  db:
    image: mysql:8.4
    environment:
      MYSQL_ROOT_PASSWORD: ${MYSQL_ROOT_PASSWORD}
      MYSQL_DATABASE: we_mp_rss
      MYSQL_USER: ${MYSQL_USER}
      MYSQL_PASSWORD: ${MYSQL_PASSWORD}
    volumes:
      - ${MYSQL_DATA_DIR:-./mysql}:/var/lib/mysql
  # werss 依赖它
    depends_on: [db]
```

`.env` 里：

```
WERSS_DB=mysql+pymysql://<user>:<password>@db:3306/we_mp_rss?charset=utf8mb4
MYSQL_ROOT_PASSWORD=...
MYSQL_USER=...
MYSQL_PASSWORD=...
```

---

## 从源码构建

一般**不需要**——官方镜像每次上游提交都会重新构建。想自己构建（审计、或要打补丁）时，
用上游自带的 Dockerfile，它依赖它自己的基础镜像和 install.sh：

```bash
git clone https://github.com/rachelos/we-mp-rss.git
cd we-mp-rss
docker build -t we-mp-rss:local .
docker run -d --name we-mp-rss -p 8001:8001 \
    -v "$(pwd)/data:/app/data" \
    -e USERNAME=admin -e 'PASSWORD=换成你自己的' we-mp-rss
```

上游 Dockerfile 里值得知道的两件事：

- 基础镜像是 `ghcr.io/rachelos/base-full:latest`，`install.sh` 会建 venv
  （`ENV PLANT_PATH=/app/env`）
- 装了 **Playwright Chromium**，用于微信读书 Cookie 自动刷新
  （`WEREAD_PROFILE_DIR=/app/data/weread-chrome-profile`，所以 `data/` 必须挂卷）

本目录的 `Dockerfile` 是**薄层**：拿官方镜像当 base，只加「入口自检 + 健康检查 +
时区 + OCI labels」。

---

## 运维

```bash
docker compose logs -f          # 看日志
docker compose restart          # 重启
docker compose pull && docker compose up -d -t 0   # 升级（先备份 data/）
docker compose down             # 停止（数据保留在 data/）
```

**备份**：`data/` 目录整个复制走即可（含 SQLite、文章缓存、Chrome profile）。

**常见问题**：

| 现象 | 原因 |
|---|---|
| 首页打得开但文章列表空 | 微信读书没登录成功，或 `data/` 权限不对（容器内是 777 的 uid，宿主机目录也要可写） |
| 昨天还好好的，今天全空 | 多半是登录态过期，重新扫码；或被限流，等 24 小时（上游的「小黑屋」机制） |
| stock-advisor 那边「全部源失败」 | 地址/端口错，或走了 `fresh` 而 WeRSS 卡住；把 `use_fresh` 设 false 试 |
| 加订阅提示频率过高 | 被限流，**停手等 24 小时**，别继续加 |

---

## 与本仓库的关系

```
你的电脑                          远程服务器
stock-advisor  ──HTTP 拉 RSS──▶  mp-service (WeRSS)
  ├ wechat_mp.py                     ├ 爬公众号（持登录态）
  ├ sa_mp_sources / sa_mp_articles   └ data/（SQLite + Chrome profile）
  └「📰 公众号」页
```

`stock-advisor` 侧的实现在 `stock-advisor/wechat_mp.py`（含选型说明与
RSS/Atom/JSON 三类源的解析细节），配置与使用见
[`stock-advisor/README.md`](../stock-advisor/README.md#微信公众号监控wechat_mppy)。
