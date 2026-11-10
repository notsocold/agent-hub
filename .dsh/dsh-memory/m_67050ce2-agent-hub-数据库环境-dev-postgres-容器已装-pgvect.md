---
id: "m_67050ce2"
title: "agent-hub 数据库环境：dev_postgres 容器已装 pgvector 0.8.7"
summary: "agent-hub 用的 dev_postgres 容器是官方 postgres:latest（PG 18.4）原本没有 pgvector，已在容器内安装并在 agent_hub / agent_hub_test 两库建好扩展。"
kind: "project"
importance: 4
tags: ["agent-hub","postgres","pgvector","环境"]
pinned: false
status: "active"
created: "2026-11-10T06:29:28.079Z"
modified: "2026-11-10T06:29:28.079Z"
revision: 1
origin: "auto"
sessionId: "session-3bc6169d-3a64-443d-9cef-444da4c4c01a"
sessionTitle: "开始 M4 阶段"
model: "deepseek-official/deepseek-flash"
---

agent-hub 的 PostgreSQL 是已存在的 dev_postgres 容器，镜像为官方 postgres:latest（PG 18.4），原镜像不含 pgvector（pg_available_extensions 里只有 pg_trgm）。处理方式：容器内 apt 安装 postgresql-18-pgvector 0.8.7，并在 agent_hub 与 agent_hub_test 两个库执行 CREATE EXTENSION vector；devuser 是 superuser，`'[1,2,3]'::vector <=> '[1,2,4]'::vector` 实测可用。注意：容器重建/换镜像后需要重新安装扩展，不能假设开箱即有。
