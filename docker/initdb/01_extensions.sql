-- 可选：仅在使用 docker compose 且数据卷为空时执行；本机已有 PostgreSQL 时手动建扩展/schema。
-- 业务表、向量表与 LangGraph checkpoint 统一放在同一个数据库中，按 schema 隔离。

CREATE EXTENSION IF NOT EXISTS vector;

-- LangGraph PostgresSaver 的 checkpoint 表放独立 schema，避免与业务表混在一起。
-- 应用侧连接时通过 search_path 指向该 schema（见 docs/database.md）。
CREATE SCHEMA IF NOT EXISTS langgraph;
