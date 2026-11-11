# M6 生产化与可观测后端交付报告

- 日期：2026-11-11
- 范围：M6 生产化与可观测后端 + M5 承接项（**不含前端**）
- 设计：[M6 设计](../superpowers/specs/2026-11-11-m6-production-observability-design.md)
- 计划：[M6 实施计划](../superpowers/plans/2026-11-11-m6-production-observability.md)
- 用户拍板：复用已有服务做本地故障注入、单实例生产化、Dockerfile + 生产 Compose、
  M5 两项承接都纳入、可观测按生产常规做法（2026-11-11）

## 1. 交付内容

| 层 | 文件 | 说明 |
|---|---|---|
| 配置 | `app/core/config.py`、`.env.example` | M6 全部配置项与校验 |
| 日志 | `app/core/logging.py`、`app/core/log_context.py` | JSON/text 可切换；request/run/conversation/trace 字段 |
| Trace | `app/core/telemetry.py`、`app/agents/callbacks.py` | OTel provider、OTLP/HTTP、LLM 回调 |
| 指标 | `app/core/metrics.py`、`app/modules/system/metrics_router.py` | 独立 registry、`/metrics`、可选 Bearer |
| HTTP/SSE | `app/core/observability.py` | HTTP span/指标、SSE 连接数、`X-Trace-ID` |
| 业务埋点 | `app/modules/chat/*`、`a2a/tool.py`、`knowledge/service.py`、`approvals/service.py`、`core/rate_limit.py` | chat/tool/A2A/检索/审批/限流/事件 |
| 健康 | `app/modules/system/health.py`、`app/core/migrations.py` | `/readyz` 组件、迁移 head、排水、DB 503 |
| 停机 | `app/modules/chat/registry.py`、`app/main.py` | `drain()`、lifespan 排水顺序 |
| 恢复 | `app/modules/maintenance/recovery.py` | 孤儿 running→failed、interrupted 恢复/restart_lost |
| 清理 | `app/modules/maintenance/cleanup.py`、`scripts/cleanup.py` | checkpoint/run_events/audit/附件/token |
| 流式过滤 | `app/core/stream_filter.py` | 密钥跨 chunk、危险 HTML 整段丢弃 |
| 部署 | `Dockerfile`、`.dockerignore`、`docker-compose.prod.yml`、`deploy/` | 非 root 镜像、Compose、Prometheus/Grafana/Nginx |
| 压测/演练 | `scripts/load_test_sse.py`、`db_outage_drill.py`、`graceful_shutdown_drill.py` | 50 并发、DB 故障、优雅停机 |
| CI | `.github/workflows/ci.yml` | lint/test/migrations/contract/docker |
| 文档 | `docs/observability.md`、`docs/deployment.md`、架构/开发/数据库/README/路线图 | M6 运维口径 |

## 2. 实测证据

### 2.1 全量测试与静态检查

```text
TEST_DATABASE_URL=... .venv/bin/python -m pytest -q
→ 471 passed, 1 skipped in 94.73s

.venv/bin/python -m ruff check .
→ All checks passed!

docker compose -f docker-compose.prod.yml config
→ exit 0
```

- 基线 M5 395 项 → M6 **471 项**（+76）；
- 收尾时本机 `dev_postgres` 已停止（遵守「不擅自启动容器」），最后一次复跑为
  `236 passed, 236 skipped`（集成用例因无测试库跳过）；**471 全量证据为 2026-11-11 上午运行结果**，
  此后只有文档/注释变更；
- 唯一 skip 仍是知识库语义评测组（需真实 Embedding key，与 M4 一致）；
- 迁移基线：临时库空库 `alembic upgrade head` 成功，`alembic check` 输出
  `No new upgrade operations detected.`；M6 未新增迁移，head 仍为 `c4e7a91b2f36`；
- 新增/更新关键用例：

| 用例 | 覆盖 |
|---|---|
| `test_config_m6_unit.py` | M6 默认值/校验 |
| `test_logging_unit.py` | JSON 字段、run 上下文、异常、extra 白名单 |
| `test_telemetry_unit.py` | 关闭 no-op、InMemory span、非法 endpoint 降级 |
| `test_metrics_unit.py` | 工具标签白名单、渲染/重置、首 token 缺省 |
| `test_observability_middleware_unit.py` | HTTP 指标、SSE gauge、request id |
| `test_metrics_hooks_unit.py` | 限流/活跃 run 指标 |
| `test_observability_chat.py` | chat/tool/LLM span、指标、跨 chunk 密钥打码 |
| `test_readyz.py` / `test_db_unavailable_unit.py` | 组件、排水 503、DB 503 信封 |
| `test_registry_drain_unit.py` | 排水等待/超时 |
| `test_startup_recovery.py` | 孤儿 failed、pending 保留、restart_lost、幂等 |
| `test_cleanup_jobs.py` | 审计/事件/token、活跃会话 checkpoint 不删、标记 |
| `test_stream_filter_unit.py` | 29 个切分点密钥不泄漏、HTML 块、正常文本 |
| `test_deploy_assets_unit.py` | Dockerfile/Compose/dashboard/nginx 静态校验 |

### 2.2 50 并发 SSE

服务端：单 uvicorn、`LLM_PROVIDER=fake`、`RATE_LIMIT_ENABLED=false`、本机 `dev_postgres`；
脚本：`scripts/load_test_sse.py --concurrency 50 --rounds 1 --json-out .tmp/m6-load.json`。

| 指标 | 结果 |
|---|---|
| 总请求 | 50 |
| 成功 | 50 |
| 错误率 | 0% |
| 状态码 | 200×50 |
| 首 token P50 / P95 / P99 | 2079ms / 2183ms / 2186ms |
| 总时长 P50 / P95 / P99 | 3213ms / 3464ms / 3466ms |
| 总耗时 | 7.62s |

> 口径：本机单实例 + 假模型，样本为 1 轮 50 并发；不代表真实模型/多实例容量。
> 结果满足 roadmap 「错误率 < 1%」，首 token P95 也低于 M1 的 3s 目标。

### 2.3 数据库短时不可用

`scripts/db_outage_drill.py`（TCP 代理切断，不停止/启动 PostgreSQL）：

```text
故障中 /readyz 503: True
故障中业务请求: HTTP 503 {"error":{"code":"database_unavailable",...}}
恢复后 /readyz 200: True
恢复后聊天: HTTP 200 包含 message.done=True
API 进程未重启: True
结果：PASS
```

### 2.4 优雅停机

`scripts/graceful_shutdown_drill.py`（两个 uvicorn 实例、先摘流量再 SIGTERM、两轮）：

```text
round 0: 在跑 SSE done=1，另一实例成功=3/3，被停实例 exit=-15
round 1: 在跑 SSE done=1，另一实例成功=3/3，被停实例 exit=-15
结果：PASS
```

- 在跑 SSE 均收到 `message.done`（排水窗口生效）；
- 排水期间另一实例 0 失败；被停实例重启后 `/readyz` 恢复；
- `exit=-15` 是 SIGTERM 终止信号，属预期；Compose 的 `stop_grace_period` 与其对应。

### 2.5 Trace

`test_observability_chat.py` 用 `InMemorySpanExporter` 断言：

- `chat.run` 存在，带 `app.run_id` / `app.conversation_id` / `gen_ai.request.model`；
- `tool.execute` 存在，带 `app.tool.name` / `app.tool.source`；
- `llm.chat` 存在，带 `gen_ai.usage.*`；
- 普通聊天可产生 `http.request` 父 span（中间件）。

未实际启动 Grafana/Tempo 做可视化验收（用户约定不新起容器）；看板 JSON、PromQL 已交付。

## 3. 关键决策与偏离

| # | 决策 | 说明 |
|---|---|---|
| 1 | 在 master 内联实施，不建 worktree | 用户既有偏好（关闭工作树）+ 本次批准直接实施 |
| 2 | 单实例、单 worker | 进程内事件总线/run 注册表；多实例留后续 |
| 3 | 手写 span，不装 `opentelemetry-instrumentation-*` | 控制 SSE/LangGraph 调用链，减少依赖 |
| 4 | 独立 Prometheus registry，标签白名单 | 测试可重置，防基数爆炸 |
| 5 | 启动依赖失败快速退出 | 不带病启动，交给 Compose/crash-loop |
| 6 | 启动恢复启动一次 + 周期巡检，排除本进程活跃 run | 防止误杀在线 run；超限由周期接手 |
| 7 | `restart_lost` 分支直接 `_finalize_canceled` | `cancel_interrupted_run` 会等待当前 resume 任务而自锁；测试发现并修复 |
| 8 | 流式过滤只处理密钥 + 危险 HTML | 正常 Markdown 不改写；落库前仍跑完整 `sanitize_output` |
| 9 | checkpoint 清理用 `metadata_.checkpoint_cleaned_at` + `updated_at` 失效 | 不新增表/迁移；会话再活跃后自动重新入选 |
| 10 | 不交付 Helm | 用户拍板 Dockerfile + Compose；无 K8s 环境 |

## 4. 前端契约

M6 **无破坏性变更**，前端无需适配；新增可选项：

1. `GET /readyz` 响应新增 `version`/`build`/`env`/`draining`，`components` 增加
   `migrations`/`checkpoint`/`storage`；
2. `GET /healthz` 新增 `version`/`build`/`env`；
3. 启用 Trace 且采样时，响应头可能有 `X-Trace-ID`；
4. 数据库不可用返回 503 `database_unavailable`（此前可能是 500）；
5. 流式 `message.delta` 现在会对密钥/危险 HTML 实时打码（比 M5 更早，落库内容同样打码）；
6. `GET /metrics` 为根路径运维端点，不在 OpenAPI 中。

## 5. 未做 / 后续

- **真实 Grafana/Tempo 可视化与告警规则**：只交付配置/PromQL，未启动容器验收；
- **真实 LB 下的滚动发布**：只做应用排水与双进程演练；Compose 自身不做零停机滚动；
- **多实例**：Redis 事件总线、共享限流、跨实例审批恢复/SSE；
- **Helm/K8s**：未交付；
- **运行中 run 的断点续跑**：崩溃的 `running` 一律 failed，不尝试恢复 LLM 流；
- **消息/运行正文保留策略**：M6 只清理运维类数据（checkpoint/事件/审计/附件/token）；
- **对象存储孤儿文件巡检**：M4 遗留，M6 只做附件过期清理；
- **前端 M4/M5 适配**：仍在 `agent-hub-web` 侧。

## 6. 部署注意

1. 生产必须先 `alembic upgrade head`；`READINESS_CHECK_MIGRATIONS=true` 时未迁移实例 `/readyz` 503；
2. 新配置项见 `.env.example` 的「生产化与可观测（M6）」段；
3. `ENV=prod` 要求 `JWT_SECRET` ≥32 字节；知识库启用时要求真实 Embedding key 或显式
   `EMBEDDING_PROVIDER=fake`；
4. 单实例不要开多 worker/多副本；需要多实例时先补共享事件总线；
5. `METRICS_TOKEN` 为空表示内网裸奔；暴露公网前设置 token 或用反代保护。

## 7. 提交记录

```text
2818d4c feat: M6 配置项与可观测依赖
96078e2 feat: M6 结构化日志与运行上下文
9910faa feat: M6 OpenTelemetry 初始化与 span 辅助
62d0fdd feat: M6 Prometheus 指标与 /metrics
bf81a1d feat: M6 HTTP/SSE 观测中间件
91e6074 feat: M6 业务链路指标与 span
be0804e feat: M6 就绪检查与数据库不可用映射
43f8da0 feat: M6 优雅停机排水
431a53f feat: M6 启动恢复与孤儿 run 终态
bd98cc9 feat: M6 数据生命周期清理
9b1534b feat: M6 流式输出实时过滤
ff5bba8 feat: M6 生产镜像与 Compose 编排
59b4926 feat: M6 50 并发 SSE 压测脚本
1c3c3f2 test: M6 数据库故障与优雅停机演练脚本
（本报告提交：docs: M6 交付报告、运维文档与 CI）
```
