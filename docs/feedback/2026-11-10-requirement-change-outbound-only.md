# 需求变更记录：A2A 仅出站（不提供入站 Server）

- 日期：2026-11-10
- 决策：本服务只承担 A2A **出站 Client** 角色，不发布 Agent Card、不暴露 JSON-RPC 入站端点，不接受其他智能体反向调用。
- 影响里程碑：M3「A2A 入站」作废；M4「知识库 RAG」顺延为下一个里程碑。
- 决策人：项目负责人（用户）

## 1. 变更内容

原 M3 规划（见 `docs/roadmap.md` 历史记录）包含：

1. Agent Card 构建与 well-known 路由；
2. `LangGraphAgentExecutor` 的 `execute()` / `cancel()`；
3. 入站鉴权（API Key，OAuth2 预留）；
4. `contextId` ↔ `conversationId` 映射；
5. 任务状态、artifact 流式回传。

上述范围全部**不实现**，相关设计保留在 `docs/a2a.md` §3、§4 与 `docs/architecture.md` §4.3 作为历史记录，并标注「不实现」。

## 2. 代码与契约改动

| 位置 | 改动 |
|---|---|
| `app/modules/system/service.py` | `system/info.a2a` 不再由 `A2A_PUBLIC_URL + A2A_AGENT_PATH` 计算：`enabled=false`、`card_url=""`、`jsonrpc_url=""`、`auth_type="none"`、`card_security="none"` |
| `app/modules/system/schemas.py` | 五个字段保留（只增不删），补描述说明「不提供入站」，避免前端误用 |
| `app/core/config.py` | `A2A_ENABLED` / `A2A_PUBLIC_URL` / `A2A_AGENT_PATH` 标为历史兼容字段，不参与运行逻辑；出站配置不变 |
| `app/modules/a2a/__init__.py` | 包说明改为「仅出站」 |
| `tests/integration/test_chat.py` | 系统信息断言改为「无入站」 |
| `tests/integration/test_a2a_outbound_only.py` | 新增 2 个契约测试：`system/info.a2a` 恒为无入站；`/a2a` 与两个 well-known 路径均 404 |
| `.env.example`、`README.md`、`docs/roadmap.md`、`docs/a2a.md`、`docs/architecture.md`、`docs/api.md`、两份设计 spec | 同步为「仅出站」 |

不涉及数据库迁移：`a2a_tasks` 表保持 M2 结构（未新增 `source`、`client_id`、`message_id` 等入站字段）。

## 3. 契约影响（前后端对齐）

- `GET /api/v1/system/info` 的 `a2a` 对象字段**不删除、类型不变**，仅取值变为「无入站」；
- 前端原计划的「A2A 入站地址展示」不再需要，可直接隐藏或展示「本服务仅出站」；
- 原 M3 前后端联调项（Card 发现、入站 JSON-RPC、入站任务）全部取消；
- M2 出站能力（`/api/v1/agents*`、`send_message`、`/api/v1/tasks*`、SSE `a2a.*`）不受影响。

## 4. 验证

- `TEST_DATABASE_URL=postgresql+psycopg://devuser:devpass@localhost:54321/agent_hub_test pytest -q`；
- 重点用例：`tests/integration/test_a2a_outbound_only.py`、`tests/integration/test_chat.py`；
- `ruff check app tests`；
- 重新导出 `docs/openapi.json`（`scripts/export_openapi.py`）。

## 5. 未做项（明确保留）

- 不做 A2A 入站 Server、Agent Card 发布、入站鉴权、入站任务表扩展；
- `A2A_ENABLED` 等历史配置字段保留但不再生效（避免旧 `.env` 报错）；
- 如果未来需要被其他智能体调用，建议独立部署一个 A2A Server 服务，而不是在本服务内恢复入站。
