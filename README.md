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

## 使用

见 [stock-advisor/README.md](stock-advisor/README.md)（行情、持仓、分析报告、
B站/微博动态监听、通知推送、止盈策略）。
