# 叠层构建：基座是 sa-data-service:1.0.1（已有 fastapi/uvicorn/requests）
# ⚠️ 101 磁盘只剩 1.9G/96%，**绝不能全量重建**（要重装 pandas/numpy/akshare）。
#    叠层只加几十 KB。
# ⚠️ 基础镜像 USER 是 svc(uid 10001)，COPY 必须带 --chown=svc:svc，
#    否则文件归 root，运行时写不了 data/ 缓存目录。
FROM sa-data-service:1.0.1

USER root
WORKDIR /app
COPY --chown=svc:svc hk_supply.py hk_lockup.py hk_lockup_llm.py hk_supply_scan.py /app/
COPY --chown=svc:svc service.py /app/

# 自检：PYTHONPATH 与 data 目录可写。构建期失败好过运行期静默写不进缓存。
RUN python3 -c "import sys; sys.path.insert(0,'/app'); import service; print('import ok')" \
 && mkdir -p /app/data && chown -R svc:svc /app/data

USER svc
EXPOSE 8090

# ⚠️⚠️ 必须**覆盖**基座镜像的 HEALTHCHECK，否则容器永远 unhealthy。
#   基座 sa-data-service:1.0.1 里是
#       HEALTHCHECK curl -fsS http://127.0.0.1:8080/health
#   `FROM` 会把它**继承**下来，而本服务在 **8090** —— 于是健康检查永远
#   探错端口，实测连续 1202 次 FailingStreak（20 小时）全是
#   `curl: (7) Failed to connect to 127.0.0.1 port 8080`，
#   而服务本身一直好好地返回 200。
#   症状极具误导性：容器 unhealthy，但 curl /health 是通的。
#   （这就是 AGENTS.md 说的「200 但没数据」同一类：**信号与事实不符**，
#     差点让我去查代码，而真因是元数据继承了别人的端口。）
HEALTHCHECK --interval=60s --timeout=15s --start-period=40s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8090/health || exit 1

CMD ["python3", "-m", "uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8090", "--log-level", "info"]