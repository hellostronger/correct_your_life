# Stock Advisor + MediaCrawler 一体镜像
# stock-advisor 是 FastAPI 主服务；通过子进程调 ../MediaCrawler（uv 管理）
# 抓 B站/微博动态，因此两个项目打进同一个镜像，共用一套 Python 环境。
#
# 构建：docker compose build
# 运行：docker compose up -d        页面 http://127.0.0.1:8686/
FROM python:3.11-slim

# MediaCrawler 需要 Chrome（代码里 channel="chrome"）；git 供 uv 拉依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget gnupg2 fonts-liberation fonts-noto-cjk xdg-utils git curl \
    && wget -qO- https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/google.gpg] https://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# uv：MediaCrawler 由 uv.lock 锁定依赖
COPY --from=ghcr.io/astral-sh/uv:0.8.24 /uv /usr/local/bin/uv

# stock-advisor 依赖（app 服务本身很薄：fastapi/uvicorn/psycopg2/requests/yaml）
WORKDIR /app
COPY stock-advisor/requirements.txt /app/stock-advisor/requirements.txt
RUN pip install --no-cache-dir -r stock-advisor/requirements.txt

# MediaCrawler（submodule，构建前需 git submodule update --init）
# 先只拷依赖清单做 uv sync，源码变动不打穿这层缓存
WORKDIR /srv/MediaCrawler
COPY MediaCrawler/pyproject.toml MediaCrawler/uv.lock MediaCrawler/.python-version ./
RUN uv sync --frozen --no-dev

# 再拷 MediaCrawler 全部源码，随后打容器专用补丁：
# 上游 fix #685 在 show_qrcode 里 `del ImageShow.UnixViewer.options` 后弹窗展示，
# Linux 容器无 X11 viewer（UnixViewer 属性缺失会 AttributeError，且弹窗没人看得见），
# 改成把二维码 PNG 落盘到 browser_data 卷，从宿主机取出来扫。
COPY MediaCrawler/ /srv/MediaCrawler/
RUN python - <<'EOF'
import pathlib
p = pathlib.Path("/srv/MediaCrawler/tools/crawler_util.py")
src = p.read_text(encoding="utf-8")
old = '    del ImageShow.UnixViewer.options["save_all"]\n    new_image.show()'
new = '    new_image.save("/srv/MediaCrawler/browser_data/bili_login_qrcode.png")  # 容器无显示：二维码落盘，取出扫码'
assert old in src, "show_qrcode pattern not found — MediaCrawler 上游变更，需重新适配补丁"
p.write_text(src.replace(old, new), encoding="utf-8")
print("patched show_qrcode -> save to browser_data/bili_login_qrcode.png")
EOF

# stock-advisor 源码
COPY stock-advisor/ /app/stock-advisor/

WORKDIR /app/stock-advisor
ENV UV_BIN=/usr/local/bin/uv \
    PYTHONUNBUFFERED=1

# 运行时数据走卷：reports（分析报告）、browser_data（登录态，重启不丢）、data（爬虫 jsonl 产出）
VOLUME ["/app/stock-advisor/reports", "/srv/MediaCrawler/browser_data", "/srv/MediaCrawler/data"]

# app.py 读 ENV_FILE = BASE_DIR.parent / ".env"，即 /app/.env；
# compose 把宿主机 .env 挂到 /correct_your_life/.env，这里链过去
RUN mkdir -p /correct_your_life && ln -s /correct_your_life/.env /app/.env

EXPOSE 8686
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8686"]
