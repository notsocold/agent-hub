# 文档-实现一致性审查（M1–M2）

- 日期：2026-11-10
- 触发：需求变更（A2A 仅出站）后，复核此前里程碑是否存在「文档写一套、实现另一套」且未披露的情况；重点核对 `a2a-sdk` 版本与用法。
- 方法：逐个里程碑对照 `docs/` 设计文档与 `app/` 代码、`pyproject.toml`/`uv.lock`、`.env.example`、测试断言；能直接修正的文档已修正，需要决策的单列。

## 1. 结论摘要

| # | 发现 | 文档位置 | 实际情况 | 处理 |
|---|---|---|---|---|
| A | `a2a-sdk` 版本范围与运行时使用 | `docs/a2a.md`、`README.md` 写 1.3.x；`pyproject.toml` 为 `>=1.3.0,<2.0` | PyPI 最新稳定版即 `1.3.0`；`app/`、`tests/`、`scripts/` **零处 import a2a-sdk**（M2 自研客户端） | 依赖保持 `>=1.3.0,<2.0`（当前锁 1.3.0，允许后续 1.x）；文档注明「当前自研实现，SDK 运行时迁移列为后续任务」 |
| B | `database.md` 的 M2 表结构与模型不一致 | `remote_agents`、`a2a_tasks` | 模型多出 `owner_user_id`/`capabilities`/`auth_scheme`/`secret_ref`/`headers`/`timeline`/`progress` 等字段，索引与可空性也不同 | 已按 `app/db/models/remote.py` 重写 §4.5 / §4.6 |
| C | `api.md` 实现状态过时 | §7.3 称 `a2a.*`/`skill.activated` 等「后续里程碑接入」；§7.4 称 `agents`/`tasks` 仍为 false | M1.5/M2 已接入，`agents`/`tasks=true` | 已加当前状态说明（保留 M1 时点快照） |
| D | `.env.example` 缺配置项 | — | `Settings` 96 项，模板仅 94 项 | 已补 `DB_POOL_PRE_PING`、`KNOWLEDGE_CATEGORIES_PER_MESSAGE` |
| E | `architecture.md` 记忆存储描述过时 | §7 决策表、§13.1、§13.3 写 `AsyncPostgresStore` | 实际为业务表 `memories`（`api.md` §8.3 已披露） | 已同步为业务表并注明原计划 |
| F | README/架构图仍把本服务写成 A2A Server | 多处 | 需求变更后仅出站 | 已全部改为「仅出站 Client」 |

## 2. 已披露、无需改代码的偏差（备查）

| 项 | 文档位置 | 说明 |
|---|---|---|
| M1 首 token P95 未压测 | `roadmap.md` M1 验收 | 只有单次实测 966ms，P95 基线并入 M6 |
| 长期记忆改业务表 | `api.md` §8.3 | 原计划 `AsyncPostgresStore`，M1.5 改为 `memories` 业务表；语义检索 M4 再做 |
| MCP 自研客户端 | `api.md` §8.3 | 未用官方 `mcp` SDK，自研 JSON-RPC over httpx |
| MCP stdio 默认关闭 | `api.md` §8.3 | 无隔离保证，v1 默认关闭 |
| M1.5 管理端未实现 | `api.md` §8.3 | 数据模型已就绪，路由未做 |
| M2 A2A 客户端自研 | `feedback/2026-11-09-m2-backend-report.md` §3 | 未用 a2a-sdk 运行时，仅用 0.3/1.0 通用方法名 |
| M3 入站取消 | `roadmap.md`、`feedback/2026-11-10-requirement-change-outbound-only.md` | 2026-11-10 需求变更 |

## 3. 待决策

1. **`a2a-sdk` 运行时迁移**（已定方向：使用 a2a-sdk 最新稳定版）：
   - 依赖保持 `>=1.3.0,<2.0`，当前锁定最新稳定 1.3.0；
   - 代码仍为自研 JSON-RPC 客户端；迁移到 SDK 运行时需重写出站协议层并重跑 M2 全部测试与假 Agent 联调，列为后续任务。
2. **前端仓库同步**：`agent-hub-web` 的路线图仍需删除「M3 A2A 入站」并确认 `system/info.a2a` 置空后的展示；本次只改后端仓库。

## 4. 验证

- `TEST_DATABASE_URL=postgresql+psycopg://devuser:devpass@localhost:54321/agent_hub_test .venv/bin/python -m pytest -q` → 103 passed；
- `.venv/bin/ruff check app tests` → 通过；
- `UV_CACHE_DIR=.uv-cache uv lock --check` → 通过（更新后）；
- `.venv/bin/python scripts/export_openapi.py` → `docs/openapi.json` 仅新增字段描述，字段仍必填。
