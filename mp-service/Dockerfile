# syntax=docker/dockerfile:1
#
# ============================================================================
#  微信公众号订阅服务（WeRSS）—— 独立镜像
# ============================================================================
#
#  这个镜像里**只有公众号订阅这一件事**：微信读书扫码登录 → 订阅公众号 → 抓文章
#  → 输出标准 RSS。没有 stock-advisor、没有 MediaCrawler、不依赖本仓库任何文件。
#  可以单独拷到任意一台服务器/ NAS 上跑。
#
#  为什么需要它：微信**没有任何官方接口**能读别人公众号的文章（官方 API 只管
#  自己运营的号），所以必须有个常驻服务持有登录态去爬；它还要 Playwright +
#  Chromium，重且需要长期存活，不该和行情工具绑在一起。
#
#  ---------------------------------------------------------------- 用法 ----
#  单文件、零依赖（不需要 compose）：
#
#      docker build -t we-mp-rss .
#      docker run -d --name we-mp-rss --restart unless-stopped \
#          -p 8001:8001 \
#          -v "$(pwd)/data:/app/data" \
#          -e TZ=Asia/Shanghai \
#          -e USERNAME=admin \
#          -e 'PASSWORD=换成你自己的' \
#          we-mp-rss
#
#  然后浏览器打开 http://<服务器IP>:8001/ → 微信读书扫码登录 → 订阅公众号。
#  接着在 stock-advisor 的「⚙️ 调度 → 🧩 完整配置」里把 mp.base_url 填成
#  http://<服务器IP>:8001 即可。
#
#  国内拉不动 ghcr.io 时，换镜像源重新 build（见下面 BASE_IMAGE 的说明）。
# ============================================================================

# 上游镜像。上游 Dockerfile 依赖它自己的私有基础镜像
# (ghcr.io/rachelos/base-full) + install.sh(建 venv) + Playwright Chromium，
# 照着重写一遍既脆又无意义——上游每次提交都会重新构建这个官方镜像。
#
# 想固定版本就把下面换成具体 tag。**建议固定**：这个服务在动微信登录态与
# 反爬策略，`latest` 一次漂移就可能让「昨天还好好的」突然全空，而你无法归因。
#   ghcr.io 现有 tag：latest 1.4.5 1.3.8 beta（上游在 ghcr 和 Docker Hub 各发一份，
#   两者层数不同，是各自独立构建，不保证同一 commit —— 换源等于换版本）
#   国内换源（实测有效，按速度排序）：
#     mirror.ccs.tencentyun.com/rachelos/we-mp-rss:latest   腾讯云内网，最快
#     docker.1ms.run/rachelos/we-mp-rss:latest             通用代理
#     ghcr.io/rachelos/we-mp-rss:1.4.5                      直连，部分机房很慢
#   查看新版本：https://github.com/rachelos/we-mp-rss/releases
ARG BASE_IMAGE=ghcr.io/rachelos/we-mp-rss:1.4.5
FROM ${BASE_IMAGE}

# 必须在 FROM 之后再声明一次：FROM 之前声明的 ARG 只对 FROM 生效，
# 不会带进构建阶段，引用它只会静默展开成空字符串（buildkit 会给 UndefinedVar 警告）。
ARG BASE_IMAGE
# 仅用于 OCI label 留痕，不代表运行时逻辑。若 base 换成了 latest/镜像站，
# 请用 --build-arg WERSS_VERSION=<版本或 digest> 如实标注，别填想当然的版本号。
ARG WERSS_VERSION=1.4.5
ARG BUILD_DATE=unknown
ARG BASE_SOURCE=ghcr.io
LABEL org.opencontainers.image.title="WeRSS - 微信公众号订阅服务" \
      org.opencontainers.image.description="把公众号文章转成标准 RSS。独立部署，供 stock-advisor 等客户端拉取。" \
      org.opencontainers.image.version="${WERSS_VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.base.name="${BASE_IMAGE}" \
      org.opencontainers.image.base.source="${BASE_SOURCE}" \
      org.opencontainers.image.source="https://github.com/rachelos/we-mp-rss" \
      org.opencontainers.image.licenses="MIT"

# 时区。不设对的话「文章发布时间」会整体偏移，进而影响「只看今天」这类判断。
# WERSS_DATA_DIR：数据目录（SQLite 库 + 文章缓存 + 微信读书的 Chrome profile 都在这，
# 上游默认写 /app/data，这里提出来便于部署到别的路径）。
# —— 注意别把注释写在 ENV 的续行里：Docker 会先剥掉注释再拼接续行，虽然结果碰巧
#    正确，但那是运气不是语义，注释一律放在指令上方。
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    WERSS_DATA_DIR=/app/data

# 运行时默认值。都能用 docker run -e 覆盖；这里给出是为了「不传任何 -e 也能起」。
# DB 用 SQLite（单文件，备份就是复制 data/ 目录），够用且不用多起一个容器。
ENV USERNAME=admin \
    PASSWORD=admin@123 \
    DB=sqlite:///data/db.db \
    AUTO_RELOAD=False \
    PROXY_ENABLED=False \
    PROXY_URL=

# 数据目录。**必须挂卷**（-v .../data:/app/data），否则重建容器 = 重新扫码 + 重新订阅。
# 目录属主跟随基础镜像里的运行用户，这里只保证存在。
RUN mkdir -p "${WERSS_DATA_DIR}"

# ---------------------------------------------------------------------------
#  入口脚本：先验证数据卷可写，再用一行失败信息说清原因，再启动真正的服务。
#  这么做的原因：最常见的故障是「容器起来了，但写不进去」，而默认日志里只有
#  一堆看不懂的 sqlite readonly 报错，甚至要等到抓文章时才暴露。把它挡在启动前。
#  写成单文件（printf 落盘）是为了让这个 Dockerfile 能独立拷走、不带旁挂文件。
# ---------------------------------------------------------------------------
RUN printf '%s\n' \
    '#!/bin/sh' \
    'set -e' \
    'D="${WERSS_DATA_DIR:-/app/data}"' \
    'if ! mkdir -p "$D" 2>/dev/null; then' \
    '  echo "[entrypoint] FATAL: 建不出数据目录 $D" >&2' \
    '  echo "[entrypoint]        多半是 bind mount 的宿主目录不存在或没权限" >&2' \
    '  exit 1' \
    'fi' \
    'if ! touch "$D/.write_probe" 2>/dev/null; then' \
    '  echo "[entrypoint] FATAL: 数据目录 $D 只读，写不进去" >&2' \
    '  echo "[entrypoint]        修法二选一：" >&2' \
    '  echo "[entrypoint]          1) 宿主机上先 mkdir -p data && chmod 777 data" >&2' \
    '  echo "[entrypoint]          2) docker run 加 --user \$(id -u):\$(id -g) 让容器用你的 uid" >&2' \
    '  exit 1' \
    'fi' \
    'rm -f "$D/.write_probe"' \
    'echo "[entrypoint] 数据目录 $D 可写，启动 WeRSS …"' \
    'exec "$@"' \
    > /usr/local/bin/wrss-entrypoint.sh \
    && chmod +x /usr/local/bin/wrss-entrypoint.sh

# 健康检查：没有 /healthz 这种探针端点（上游只有 Web UI 和 /rss），所以用
# 「首页能否返回 HTTP 响应」代替——返回 4xx/5xx 也说明进程活着，是**端口没起来**
# 才算不健康。够挡住「进程崩了/端口没监听」，不够精确但不会误报。
RUN printf '%s\n' \
    '#!/bin/sh' \
    'python3 - <<PYEOF' \
    'import sys, urllib.request as u, urllib.error as e' \
    'try:' \
    '    u.urlopen("http://127.0.0.1:8001/", timeout=8)' \
    'except e.HTTPError:' \
    '    pass' \
    'except Exception as exc:' \
    '    print("unhealthy:", exc, file=sys.stderr)' \
    '    sys.exit(1)' \
    'PYEOF' \
    > /usr/local/bin/wrss-healthcheck.sh \
    && chmod +x /usr/local/bin/wrss-healthcheck.sh

# 启动慢（要起 venv / 可能拉浏览器），给足 start-period，避免刚起就被判死反复重启
HEALTHCHECK --interval=60s --timeout=15s --start-period=120s --retries=3 \
    CMD /usr/local/bin/wrss-healthcheck.sh

EXPOSE 8001

ENTRYPOINT ["/usr/local/bin/wrss-entrypoint.sh"]
# 上游原本的启动命令（原样保留，不要改）
CMD ["/app/start.sh"]
