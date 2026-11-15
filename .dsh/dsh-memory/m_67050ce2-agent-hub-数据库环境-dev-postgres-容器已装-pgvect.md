---
id: "m_67050ce2"
title: "agent-hub 本地依赖容器：dev_postgres 已装 pgvector，dev_redis/dev_minio 可复用"
summary: "agent-hub 用的 dev_postgres 容器是官方 postgres:latest（PG 18.4），已在容器内装好 pgvector 并在 agent_hub / agent_hub_test 两库建好扩展；dev_redis、dev_minio 已在运行，M6 验收直接复用、不新起容器。"
kind: "project"
importance: 4
tags: ["agent-hub","postgres","pgvector","环境"]
pinned: false
status: "active"
created: "2026-11-10T06:29:28.079Z"
modified: "2026-11-11T03:31:17.615Z"
revision: 2
origin: "auto"
sessionId: "session-cfda4151-8ee5-4b95-a6c9-6d2e450f10b3"
sessionTitle: "开始进行 M6"
model: "deepseek-official/deepseek-flash"
---

agent-hub 使用的 dev_postgres 容器是官方 postgres:latest（PG 18.4），原本没有 pgvector，已在容器内安装并在 agent_hub / agent_hub_test 两个库建好扩展。本机另有 dev_redis 与 dev_minio 在运行；M6 的 50 并发 SSE 压测与故障注入直接复用这些已有服务，不需要新启动任何容器（与用户「外部环境由用户自行处理、不要新启容器」的偏好一致）。
