# correct_your_life

个人生活/投资辅助工具集。

## 项目结构

```
correct_your_life/
├── stock-advisor/    # 股票跟踪分析助手（FastAPI 本地服务）
├── MediaCrawler/     # 社媒爬虫（git submodule → hellostronger/MediaCrawler）
├── DESIGN.md         # LifeReflector 个人生活反思助手设计文档（尚未实现）
└── .env              # 环境变量/密钥（不入库）
```

## 依赖关系

**stock-advisor 硬依赖 MediaCrawler**：B站动态监听（`bili_monitor.py`）和微博博主
监听（`wb_monitor.py`）以子进程方式调用本仓库根目录的 `MediaCrawler/` CLI 抓取
动态+评论，依赖其补丁版提交（适配无人值守抓取）。路径约定为
`stock-advisor/../MediaCrawler`，两者必须并列放在本仓库根目录下。

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

## 使用

见 [stock-advisor/README.md](stock-advisor/README.md)（行情、持仓、分析报告、
B站/微博动态监听、通知推送、止盈策略）。
