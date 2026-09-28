# correct_your_life

个人生活/投资辅助工具集。

## 项目结构

```
correct_your_life/
├── stock-advisor/    # 股票跟踪分析助手（FastAPI 本地服务）
├── mp-service/       # 公众号监控上游 WeRSS（独立部署，可单独放远程）
├── MediaCrawler/     # 社媒爬虫（git submodule → hellostronger/MediaCrawler）
├── DESIGN.md         # LifeReflector 个人生活反思助手设计文档（尚未实现）
└── .env              # 环境变量/密钥（不入库）
```

## 依赖关系

**stock-advisor 硬依赖 MediaCrawler**：B站动态监听（`bili_monitor.py`）和微博博主
监听（`wb_monitor.py`）以子进程方式调用本仓库根目录的 `MediaCrawler/` CLI 抓取
动态+评论，依赖其补丁版提交（适配无人值守抓取）。路径约定为
`stock-advisor/../MediaCrawler`，两者必须并列放在本仓库根目录下。

公众号与 X 监控**不依赖** MediaCrawler：
- 公众号靠 `mp-service/`（WeRSS）产出的 RSS，本项目只当客户端
- X 靠 `twscrape` 库（详见 stock-advisor README）

## Clone

```powershell
git clone --recurse-submodules https://github.com/hellostronger/correct_your_life.git
# 已 clone 过的补一步：
git submodule update --init
```

## 更新 submodule

```powershell
cd MediaCrawler
git pull            # MediaCrawler 上游更新（注意重打 stock-advisor 依赖的补丁，见其 README）
cd ..
git add MediaCrawler
git commit -m "chore: bump MediaCrawler submodule"
```

## Docker 一键运行

前提：已安装 Docker Desktop（Windows/Mac），MediaCrawler submodule 已就位。

```powershell
git submodule update --init      # 首次构建前
docker compose build             # 构建镜像（含 Chrome + uv + 两个项目）
docker compose up -d             # 启动，页面 http://127.0.0.1:8686/
docker compose logs -f           # 看日志
docker compose down              # 停止
```

说明：

- 镜像 = stock-advisor + MediaCrawler 一体：app 以子进程调 `uv run main.py`
  抓取，两个项目共用一套镜像环境，无需宿主机装 Python/uv/Chrome。
- **数据库**：复用根目录 `.env` 的 `DB_*`（云 PostgreSQL）。若 `DB_HOST` 是
  `127.0.0.1`，compose 会覆盖为 `host.docker.internal`（宿主机）。
- **登录态持久化**：`mediacrawler-browser/`（browser_data）挂在宿主机，重启容器
  不丢 B站/微博登录。
- **扫码登录**：容器无桌面，首次登录时二维码写成 PNG 落盘而非弹窗：

  ```powershell
  # 网页点「登录B站」并等几秒后：
  docker exec stock-advisor cat /srv/MediaCrawler/browser_data/bili_login_qrcode.png > bili_qr.png
  # 双击打开 bili_qr.png 用手机B站扫码；登录态落在 mediacrawler-browser/，之后无需再扫
  ```

- 微信通知登录的二维码由 iLink 接口直接返回 base64、在网页上显示，容器内可正常使用；
  止盈监控、邮件通知等其余功能与本地直跑一致（见 stock-advisor README）。

### 微信公众号监控：独立部署在 `mp-service/`

「📰 公众号」页要拉的是 **WeRSS**（`rachelos/we-mp-rss`，MIT）产出的 RSS —— 微信没有
官方接口能读别人公众号的文章，必须有个常驻服务去爬。它需要长期持有的登录态 +
Chromium，所以**从主 compose 里拆了出来**，单独放一个目录，可直接扔到远程机器：

```bash
cd mp-service
cp .env.example .env          # 至少改掉 WERSS_PASSWORD
docker compose up -d
# 浏览器打开 http://<服务器IP>:8001/ → 微信读书扫码登录 → 订阅公众号
```

然后在 stock-advisor 的「⚙️ 调度 → 🧩 完整配置」的 `mp` 段里把
`base_url` 填成 `http://<服务器IP>:8001`（配一次，网页添加订阅源时自动预填），
回「📰 公众号」页添加订阅源即可。

两边**不共享配置与数据**。远程暴露前请看 `mp-service/README.md` 的「加固」一节
（`/rss` 路由是免鉴权的，别直接暴露公网）。完整部署/运维/换 MySQL/从源码构建见该目录。

不装 WeRSS 也能用 —— 公众号页支持粘贴任意 RSS 地址。

### X(Twitter) 监控

「🐦 X」页用 `twscrape` 监控指定用户的推文。容器内外都需要先装依赖（带 curl 后端，
X 会做 TLS 指纹识别）：

```bash
pip install "twscrape[curl]"
```

然后在网页里粘贴自己 X 账号的 `auth_token=…; ct0=…` cookie。
Nitter 已于 2026-09 被 X Corp 要求下架、snscrape/Twint 随匿名 guest token 关闭
一起失效，twscrape 是目前唯一可用的开源免费方案。

## 使用

见 [stock-advisor/README.md](stock-advisor/README.md)（行情、持仓、分析报告、
B站/微博动态监听、通知推送、止盈策略）。
