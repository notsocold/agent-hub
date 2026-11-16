---
id: "m_67050ce2"
title: "agent-hub 本地依赖容器：dev_postgres 的 pgvector 装在容器层，重建即丢，用 ensure_pgvector.py 幂等恢复"
summary: "dev_postgres 来自 ~/work/docker_work/docker-compose.yaml 的官方 postgres:latest（PG 18.4），pgvector 需容器内安装且不落数据卷，容器重建后跑 scripts/ensure_pgvector.py 恢复；dev_redis/dev_minio 可复用。"
kind: "project"
importance: 4
tags: ["pgvector","postgres","docker","dev-environment","agent-hub"]
pinned: false
status: "active"
created: "2026-11-10T06:29:28.079Z"
modified: "2026-11-16T14:13:13.393Z"
revision: 3
origin: "auto"
sessionId: "session-20238cef-b54b-4956-80a2-2d866f210325"
sessionTitle: "PostgreSQL 缺少 vector 扩展"
model: "deepseek-official/deepseek-flash"
---

本地依赖容器现状（agent-hub）：
- dev_postgres 由另一个项目 ~/work/docker_work/docker-compose.yaml 提供，镜像写死 `postgres:latest`（实际 PG 18.4），数据卷 `docker_work_pg_data` 被多个项目共用（agent_hub、agent_hub_test、a2a_sessions、devdb）。
- 官方镜像不带 pgvector，需在容器内 `apt-get install postgresql-18-pgvector`（容器已配 pgdg `trixie-pgdg` 18 源，实测装到 0.8.7），再对每个库单独 `CREATE EXTENSION IF NOT EXISTS vector`（扩展是库级对象，需超级用户；devuser 是 superuser）。CREATE EXTENSION 无需重启 PG。
- 关键陷阱：这样装的扩展在容器可写层，不在数据卷里。`docker compose up --force-recreate` 或删容器重建后扩展消失，但数据仍在 → 现象是「数据还在、类型没了」，报 `extension "vector" is not available`。
- 恢复方式：`uv run python scripts/ensure_pgvector.py`（默认容器 dev_postgres，取 DATABASE_URL 库及其 _test 副本；可用 --container/--databases 覆盖）。脚本先查 pg_available_extensions，有 vector 就跳过 apt、不依赖网络，没有则按 pg_config --version 装 postgresql-<major>-pgvector，最后对每个库建扩展并回读版本，两次连跑幂等。文档已指向该脚本：scripts/README.md 脚本文档表、docs/development.md 的故障排查行。
- 彻底方案（本次未采纳，因共享库需短暂停机）：把该 compose 的 image 换成 pgvector/pgvector:pg18（tag 已确认存在，PG 18 数据目录可直接复用 pg_data 卷）。不建议用 agent-hub 自带 compose 另起 PG（与本项目「复用已有容器、不新起容器」的约定冲突）。
- dev_redis、dev_minio 已在运行，验收直接复用、不新起容器。
