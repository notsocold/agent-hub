---
id: "m_bf4efbaa"
title: "agent-hub 使用用户本机 PostgreSQL，不依赖 Docker"
summary: "agent-hub 后端默认使用用户本机已有 PostgreSQL、不自启任何容器；docker-compose 仅作为可选项保留。"
kind: "project"
importance: 3
tags: ["agent-hub","environment","postgres","no-docker"]
pinned: false
status: "active"
created: "2026-11-09T09:10:11.617Z"
modified: "2026-11-09T09:23:11.467Z"
revision: 3
origin: "auto"
sessionId: "session-ca2ef162-8077-4b24-bdde-e8e74222aa25"
sessionTitle: "后端开发与前端交接文档对接"
model: "deepseek-official/deepseek-flash"
---

agent-hub 不依赖 Docker：默认连接用户本机已有的 PostgreSQL。

约定与实现：
- app/core/config.py 中 DATABASE_URL 没有默认值（不再默认指向 Docker 的 5433 端口），必须由用户在 .env 显式配置，缺失时启动报清晰错误；.env.example 示例为本机 localhost:5432。
- 集成测试不再默认连 Docker 库：通过独立环境变量 TEST_DATABASE_URL 指向本机测试库，未配置时自动 skip（而非擅自杀向数据库）。
- README / docs/development.md / tests/README.md 等文档以 createdb + 本机连接串为默认路径；docker-compose.yml 与 docker/initdb 仅在「本机没有 PostgreSQL」时作为可选方案，文件中已注明。
- 项目之外的运行环境（容器、数据库、服务）由用户自行处理，助手不改动、不新启。
