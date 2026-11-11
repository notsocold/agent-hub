# 生产部署与运维（M6）

> 适用：单实例生产化。多实例（Redis 事件总线/共享限流/跨实例 resume）不在 M6 范围。
> 本文与 [deploy/README.md](../deploy/README.md) 配套；可观测细节见 [observability.md](observability.md)。

## 1. 镜像

```bash
docker build -t agent-hub:0.1.0 .
```

- 多阶段构建；运行用户 `app`（UID 1000），非 root；
- 单容器单 worker：进程内事件总线与 run 注册表决定不能开多 worker；
- `HEALTHCHECK` 请求 `/healthz`；SIGTERM 后按 `SHUTDOWN_GRACE_SECONDS` 排水（默认 30s）；
- 数据目录 `/app/data` 挂卷（附件、知识库原文）。

## 2. 首次部署

```bash
cp .env.example .env
# 必改：JWT_SECRET（>=32 字节）、POSTGRES_PASSWORD、LLM_API_KEY（或 LLM_PROVIDER=fake）、
#       CAPABILITY_SECRET_KEY；生产 ENV=prod
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml run --rm api alembic upgrade head
```

> **迁移必须先执行**：测试库用 `create_all` 建表，不代表生产库已迁移。
> Compose 的 `READINESS_CHECK_MIGRATIONS=true` 会让未迁移实例的 `/readyz` 返回 503。

检查：

```bash
curl -s http://localhost:8000/healthz
curl -s http://localhost:8000/readyz | jq .
curl -s http://localhost:8000/metrics | head
```

## 3. 可观测 profile

```bash
docker compose -f docker-compose.prod.yml --profile observability up -d
# Prometheus http://localhost:9090
# Grafana    http://localhost:3000（admin / GRAFANA_ADMIN_PASSWORD）
```

- Prometheus 抓取 `api:8000/metrics`；若设置 `METRICS_TOKEN`，按
  `deploy/prometheus/prometheus.yml` 注释补 `authorization.credentials`；
- Grafana 自动加载 `agent-hub-overview` 看板（HTTP/SSE/chat/LLM/工具/A2A/检索 P95）；
- 生产建议把 Prometheus/Grafana 放在内网，或用反代加认证。

## 4. 就绪与排水

`/healthz` 是存活探针，不查依赖；`/readyz` 检查：

| 组件 | 失败影响 |
|---|---|
| database | 关键，503 |
| migrations（启用时） | 关键，503 |
| checkpoint | 关键，503 |
| storage | 非关键，200 + `degraded` |

停机顺序：`draining=true` → 停巡检 → `registry.drain(SHUTDOWN_GRACE_SECONDS)` →
剩余任务 cancel 并写终态 → 关闭 tracing/DB。SSE 在排水窗口内会收到 `message.done`。

## 5. 滚动发布

Compose 本身不能零停机滚动。生产需要 Nginx/LB：

1. LB 先摘流量（或 `max_fails` 标记 down），确认 `/readyz` 不再被路由；
2. 向旧容器发 SIGTERM，等待在跑 run 完成（`stop_grace_period: 40s`）；
3. 启动新容器，`/readyz` 通过后加回流量；
4. 本地演练：`.venv/bin/python scripts/graceful_shutdown_drill.py`。

Nginx SSE 关键配置（`deploy/nginx.conf`）：

```nginx
proxy_buffering off;
proxy_read_timeout 900s;
proxy_send_timeout 900s;
```

## 6. 数据库不可用

- `pool_pre_ping=true`，连接失效时自动重建；
- 连接类 `OperationalError` / `InterfaceError` 映射为 503 `database_unavailable`，不返回 500 堆栈；
- `/readyz` 故障期间 503，恢复后自动 200；
- 演练：`.venv/bin/python scripts/db_outage_drill.py`（TCP 代理切断/恢复，不停止 PostgreSQL）。

## 7. 数据清理

```bash
.venv/bin/python scripts/cleanup.py --dry-run
.venv/bin/python scripts/cleanup.py            # 全部
.venv/bin/python scripts/cleanup.py --only checkpoints,audit
```

| 数据 | 默认保留 | 配置 |
|---|---|---|
| checkpoint 线程 | 30 天（无活跃 run） | `CHECKPOINT_RETENTION_DAYS` |
| `run_events` | 90 天（终态 run） | `RUN_EVENTS_RETENTION_DAYS` |
| `audit_logs` | 180 天 | `AUDIT_RETENTION_DAYS` |
| 附件 | `expires_at`（30 天） | `ATTACHMENT_RETENTION_HOURS` |
| refresh token | 过期/吊销后 30 天 | `REFRESH_TOKEN_RETENTION_DAYS` |

- 后台调度：`CLEANUP_ENABLED=true` + `CLEANUP_INTERVAL_SECONDS`；
- 清理不会删除仍有 `queued/running/interrupted` run 的会话 checkpoint；
- 会话清理后再次对话会生成新 thread，`updated_at` 前移后会被下一轮重新选中。

## 8. 备份与恢复

- 每日 `pg_dump`（`public` + `langgraph`）+ `/app/data` 卷；
- `pgvector` 索引不进入逻辑备份，恢复后需 `REINDEX`；
- 恢复后先跑 `alembic upgrade head` 与 `/readyz`，再放流量；
- 生产建议 WAL 归档/PITR；每季度做一次恢复演练。

## 9. 环境变量

完整清单见 [.env.example](../.env.example)。M6 关键项：

- `LOG_FORMAT=json`、`LOG_LEVEL=INFO`
- `OTEL_ENABLED` / `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` / `OTEL_SAMPLE_RATIO`
- `METRICS_ENABLED` / `METRICS_PATH` / `METRICS_TOKEN`
- `READINESS_CHECK_MIGRATIONS=true`
- `SHUTDOWN_GRACE_SECONDS`
- `STARTUP_RECOVERY_ENABLED` / `RECOVERY_INTERVAL_SECONDS` / `RECOVERY_GRACE_SECONDS`
- `CLEANUP_*` 与各保留天数
