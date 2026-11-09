# M1.5 个人能力层后端交接报告

- 日期：2026-11-09
- 仓库：`agent-hub`（FastAPI + SQLAlchemy async + Alembic + LangGraph）
- 范围：指令 / 长期记忆 / 技能 / MCP / 附件 / HITL 审批 / 聊天与 SSE 扩展 / 系统信息
- 结论：**M1.5 全部交付并验证通过**；前端可开始设置页四个区块与对话页能力卡片的真实联调。

---

## 1. 一句话结论

M1.5 按任务口径完整落地：8 张新表 + 可逆迁移、4 个设置区块接口、MCP JSON-RPC 客户端、HITL 审批全链路（审批决定可实时唤醒运行中的工具）、聊天能力装配（指令/记忆/技能/MCP/附件）、`tool_invocations` 轨迹与审计。**85 个测试全绿、ruff 通过、迁移 upgrade/downgrade/upgrade 验证通过、8000 实例已热重载并 curl 验证。**

## 2. 实现清单

### 2.1 数据模型与迁移（revision `7f3a9c21b4d6`，可逆，`alembic check` 无漂移）

| 表 | 关键字段 |
|---|---|
| `attachments` | user_id/conversation_id/filename/mime_type/size_bytes/checksum/storage_path/status(processing\|ready\|failed\|deleted)/purpose/text_excerpt/expires_at |
| `skills` | scope(system\|user)/owner_user_id/slug/name/description/content/version/metadata/source(builtin\|upload\|manual)/enabled/force_disabled/estimated_tokens/size_bytes/file_count |
| `skill_files` | skill_id/path/content/size_bytes，UNIQUE(skill_id,path) |
| `mcp_servers` | scope/slug/name/transport(streamable_http\|sse\|stdio)/url/command/args/env_refs/headers(JSONB，仅 secret_ref 或 AES-GCM 密文)/credentials_hint/enabled/auto_inject/force_disabled/health_status/last_error/last_checked_at/latency_ms/tools_synced_at |
| `mcp_tools` | server_id/name/exposed_name/description/input_schema/annotations/risk(read\|write\|destructive\|unknown)/requires_approval/admin_only/user_allowed/enabled |
| `user_capability_overrides` | PK(user_id, capability, target_id)/enabled |
| `memories` | user_id/kind(profile\|preference\|fact)/title/content/source/pinned/status(active\|archived) |
| `tool_invocations` | run_id/tool_call_id/tool_name/source(native\|mcp\|a2a\|knowledge\|skill)/source_id/status(pending\|ok\|error\|rejected\|canceled)/args/result/duration_ms/error |

部分唯一索引保证系统/用户 slug 隔离；`system_settings`/`user_settings`/`approvals`/`audit_logs` 复用，无结构变更。

### 2.2 端点（22 个新端点 + 审批/系统适配）

- 附件：`POST/GET /attachments`、`GET/DELETE /attachments/{id}`、`GET /attachments/{id}/download`
- 指令：`GET/PUT /instructions`
- 记忆：`GET/POST /memories`、`GET/PATCH/DELETE /memories/{id}`、`DELETE /memories?confirm=true`、`GET /memories/export`
- 技能：`GET/POST /skills`、`POST /skills/import`、`GET/PATCH/DELETE /skills/{id}`、`PUT /skills/{id}/enabled`、`GET /skills/{id}/files`、`GET /skills/{id}/files/{path}`、`GET /skills/catalog`
- MCP：`GET/POST /mcp/servers`、`GET/PATCH/DELETE /mcp/servers/{id}`、`PUT /mcp/servers/{id}/enabled`、`POST /mcp/servers/{id}/check`、`POST /mcp/servers/{id}/refresh-tools`、`GET /mcp/servers/{id}/tools`、`PATCH /mcp/tools/{id}`、`GET /mcp/available`
- 审批：`GET /approvals`（stats 已含 pending/approved/rejected/expired/canceled）、`GET /approvals/{id}`、`POST /approvals/{id}/decision`（决定实时唤醒运行）
- 系统：`GET /system/info.capabilities` → instructions/memories/skills/mcp/attachments/approvals=true；knowledge/workflows/agents/tasks=false

### 2.3 聊天 / SSE / 系统提示词

- 请求新增 `attachments(≤5)/skills(≤3)/mcp_servers(≤5)/mcp_tools(≤10)`；他人资源 404、已停用 422；
- 装配顺序固定：基础规则 → 用户指令 → 记忆 → 技能目录 → 激活技能正文 → 工具规则（不写 checkpoint，每轮生成）；
- 新事件：`skill.activated`（首个 delta 前）、`memory.saved`、`notice`（MCP 降级/工具裁剪）、`tool.start/tool.result`（source=native/skill/mcp，MCP 带 server）、`approval.required`；
- 审批超时：`message.done.finish_reason=interrupted`（`reason=approval_timeout`），run=interrupted，pending 审批置 expired；`message.done` 其余字段与 M1 完全兼容；
- 原生工具：`save_memory`（去重合并，成功后发 memory.saved）、`read_skill_file`（仅本轮激活技能，≤32KB）；MCP 工具名 `mcp__<slug>__<tool>`（≤64，冲突加短哈希）；工具调用全部落 `tool_invocations`；
- 工具循环上限 `MAX_TOOL_ROUNDS=8`；MCP 不可达发 notice 降级不中断。

## 3. 与契约/设计稿的偏差（重要，前端注意）

1. **记忆存储用业务表 `memories` 而非 LangGraph Store**（任务口径）；`q` 为 ILIKE，语义检索标注 M4；
2. **MCP 客户端为自研 JSON-RPC 2.0 over httpx**（未引入官方 mcp SDK）；initialize → notifications/initialized → tools/list → tools/call，兼容 `text/event-stream` 响应与 SSE 会话传输；stdio 仅管理员、命令白名单；
3. **`mcp_tools` 增加 `admin_only/user_allowed` 落库字段**：系统 MCP 写/破坏性工具对普通用户 `user_allowed=false`（列表可见可置灰，聊天目录自动过滤，显式选择 403）；普通用户工具目录不含系统写工具；
4. **管理端 `/api/v1/admin/…` 未实现**（本轮范围为用户侧）：`users.role`、系统技能/系统 MCP 的数据模型与 `force_disabled` 已就绪，M2 会话补管理端路由即可；
5. **附件删除策略**：被消息引用 → 软删（status=deleted + 移除磁盘文件，历史快照保留元数据）；未引用 → 物理删除；
6. **记忆去重**：内容完全一致的 active 条目合并（`action=updated`），语义去重后置；
7. 记忆/技能正文按数据包裹注入（`<memory>`/`<skill>`），系统提示声明不可覆盖基础规则；
8. 敏感请求头：AES-GCM（`CAPABILITY_SECRET_KEY`）或 `env:VAR` secret_ref；未配置密钥时明文凭据 503 拒存；响应只回 `credentials_hint`。

## 4. 测试与验证结果（实际跑过）

| 项目 | 命令 | 结果 |
|---|---|---|
| 全量测试 | `TEST_DATABASE_URL=postgresql+psycopg://devuser:devpass@localhost:54321/agent_hub_test uv run pytest -q` | **85 passed in 12.44s**（27 个 M1 回归 + 20 个单元 + 38 个集成） |
| 静态检查 | `uv run ruff check .` | All checks passed |
| 迁移 | 测试库 `upgrade head → downgrade -1 → upgrade head` | 通过 |
| 迁移一致性 | `alembic check` | No new upgrade operations detected |
| 生产库 | `uv run alembic upgrade head`（agent_hub） | `7f3a9c21b4d6 (head)` |
| 8000 实例 | curl：system/info、instructions GET/PUT、memories、skills（内置 weekly-report 已同步）、mcp/servers、attachments 上传/删除、带 skills 的 chat/stream | 全部可用（uvicorn 热重载已生效，SSE 实时流式） |
| OpenAPI | `uv run python scripts/export_openapi.py` | docs/openapi.json 含 22 个新端点 |

## 5. 前端联调注意事项

- **HITL 联调时序**：真实 uvicorn 下 SSE 是实时流式的；审批决定接口 `POST /approvals/{id}/decision` 返回 `resumed=true` 即已唤醒运行，流内随后出现 `tool.result` 与 `message.done`。审批超时 30 分钟（生产默认），超时后 run=interrupted，会话被未结束 run 占用，需前端调用 `POST /runs/{id}/cancel` 解锁；
- **能力开关**：`/system/info.capabilities` 五个新能力为 true，前端应放开设置页入口；`knowledge/workflows/agents/tasks` 仍 false 继续置灰；
- **凭据**：设置页保存 MCP 请求头后仅返回 `credentials_hint`，编辑时不回显明文（表单留空=保持不变，提交 `{}` 会清空）；
- **系统 MCP 写工具**：`user_allowed=false` 的前端置灰并说明「仅管理员可用」；普通用户显式选择会被 403；
- **技能导入**：zip 校验失败 422 `skill_invalid`（信息在 message）；同名 slug 409；`skill.activated` 事件在首个 `message.delta` 之前；
- **附件**：上传响应 `duplicate=true` 表示复用已有文件；下载用 `Content-Disposition: attachment`；
- **测试注入**：后端测试的 ASGITransport 会缓冲整个 SSE（仅测试环境现象），联调请用 8000 真实服务。

## 6. 未完成 / 风险项

- 管理端路由（见 §3.4）；
- 记忆语义检索（M4，`memories` 表预留 embedding 空间，届时补列 + pgvector 索引或迁移到 Store）；
- stdio MCP 无进程隔离保证（默认关闭，`MCP_USER_STDIO_ENABLED=false`）；
- MCP 客户端每次调用重建连接（initialize 握手开销），未做连接缓存/串行化——多工具高频调用时延迟偏高，M2 可加连接池；
- 审批等待基于进程内 registry：多实例部署时决定接口跨实例无法唤醒（单实例部署前提下不阻塞，M6 评估 Redis 事件总线）；
- 附件未做过期清理后台任务（`expires_at` 已落库，清理任务建议 M6 数据清理一并做）；
- MCP 工具 schema 转 Pydantic 仅支持基础类型（string/integer/number/boolean/array/object），复杂 JSON Schema 会退化为宽松类型。

## 7. M2 衔接点（A2A 出站）

- `tool_invocations.source` 已含 `a2a`，`source_id` 可存 remote_agent_id，直接复用本轮的落库与 `tool.start/tool.result` 事件管道；
- `ToolRunner` 按 `exposed_name` 分派，M2 增加 `agent_<slug>__<skill>` 工具只需扩展 `_classify`/新增执行分支；
- `collect_chat_tools` 的装配模式（显式选择 + 上限 + notice 降级）可直接套用于 A2A 能力选择；
- SSRF 校验（`app/core/ssrf.py`）与 A2A 共用（`A2A_ALLOWED_HOSTS` 作为回退白名单）；
- HITL 的 `approval_waiters`/`resolve_approval` 机制与 `approvals` 表复用：A2A 写操作或 input-required 可直接挂同一条审批链路；`/runs/{id}/resume` 目前对 interrupted 返回 409 `run_not_resumable`，M2 接入 `Command(resume=...)` 后放开。
