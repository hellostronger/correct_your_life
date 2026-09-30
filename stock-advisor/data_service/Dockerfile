# 板块数据服务镜像。
#
# 只装这个服务真正需要的包，不装 stock-advisor 的全部依赖 ——
# 服务是无状态 HTTP 采集器，不碰数据库、不碰 FastAPI 主应用。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

# 代理是这个环境的坑：不设 NO_PROXY，pip 和数据源都会报
# ProxyError / RemoteDisconnected（stock-advisor/app.py:31 早就踩过）
ENV NO_PROXY="*" \
    no_proxy="*"

WORKDIR /app

# curl 供 healthcheck 用
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# 依赖分层：pandas/numpy 是 akshare 的硬依赖，版本锁住避免上游变动打脸
# 构建上下文就是 data_service/ 本身（compose 里 context: .，
# 而 . 就是 data_service 目录），所以这里直接 COPY . 而不是 COPY data_service
COPY requirements.txt ./
RUN pip install --no-cache-dir \
        --index-url https://mirrors.aliyun.com/pypi/simple/ \
        --trusted-host mirrors.aliyun.com \
        -r requirements.txt akshare levistock adata \
 && python -c "import akshare, levistock, adata; print('deps ok')"

COPY . ./data_service

# 非 root 运行
RUN useradd -m -u 10001 svc && chown -R svc:svc /app
USER svc

# python 必须在 /app 下才能 import 到 data_service 包
ENV PYTHONPATH=/app

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=15s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

CMD ["python", "-m", "uvicorn", "data_service.api:app", \
     "--host", "0.0.0.0", "--port", "8080", "--log-level", "info"]
