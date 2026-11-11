# 部署说明（M6）

## 镜像

```bash
docker build -t agent-hub:0.1.0 .
```

- 多阶段构建；运行用户 `app`（UID 1000），非 root；
- 单容器单 worker：进程内事件总线与 run 注册表决定不能开多 worker；
- `HEALTHCHECK` 请求 `/healthz`；SIGTERM 后按 `SHUTDOWN_GRACE_SECONDS` 排水。

## Compose

```bash
cp .env.example .env   # 修改 JWT_SECRET / POSTGRES_PASSWORD / LLM_API_KEY 等
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml run --rm api alembic upgrade head
```

> **必须执行 `alembic upgrade head`**：测试库用 `create_all` 建表，不能替代生产迁移；
> Compose 里 `READINESS_CHECK_MIGRATIONS=true` 会在迁移未执行时让 `/readyz` 返回 503。

可观测 profile：

```bash
docker compose -f docker-compose.prod.yml --profile observability up -d
# Prometheus http://localhost:9090
# Grafana    http://localhost:3000（admin / GRAFANA_ADMIN_PASSWORD）
```

- Prometheus 抓取 `api:8000/metrics`；若设置 `METRICS_TOKEN`，按 `deploy/prometheus/prometheus.yml`
  注释补 `authorization.credentials`；
- Grafana 自动加载 `agent-hub-overview` dashboard（HTTP/SSE/chat/LLM/工具/A2A/检索 P95）。

Nginx（SSE 关闭缓冲）：

```bash
docker compose -f docker-compose.prod.yml --profile proxy up -d
# http://localhost:8080
```

## 滚动发布

Compose 本身不能零停机滚动，生产需要在 `api` 前放 Nginx/LB：

1. LB 先摘流量（或 `max_fails` 标记 down），确认 `/readyz` 不再被路由；
2. 向旧容器发 SIGTERM，等待 `stop_grace_period` / uvicorn graceful timeout 内的在跑 run 完成；
3. 启动新容器，`/readyz` 通过后加回流量；
4. 本机演练：`python scripts/graceful_shutdown_drill.py`。

## 数据备份与清理

- 备份：`pg_dump` 业务库 + `langgraph` schema；`/app/data` 附件/知识库原文卷；
- 清理：`python scripts/cleanup.py --dry-run` 查看，去掉 `--dry-run` 执行；
- 默认保留：checkpoint 30 天、run_events 90 天、audit 180 天、过期附件 30 天、过期 refresh token 30 天；
- 所有保留天数均可在 `.env` 调整。
