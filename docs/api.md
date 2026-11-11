# API 契约

- Base URL：`http://localhost:8000`
- 业务前缀：`/api/v1`
- 协议：HTTP + JSON；聊天流式用 SSE
- 文档：FastAPI 自动生成 `/docs`（Swagger）与 `/redoc`
- 状态：M1 / M1.5 / M2 / M4–M8 已实现（M8 已完成，独立审查通过；前端尚未接入）；M3 入站已按 2026-11-10 需求变更取消；各里程碑实测差异见 §7–§12

## 1. 通用约定

| 约定 | 规则 |
|---|---|
| 时间 | ISO 8601 UTC，例：`2026-11-04T15:30:00Z` |
| ID | UUID v4 字符串 |
| 字段命名 | `snake_case` |
| 分页 | cursor 分页：`?limit=20&cursor=<opaque>`，响应含 `next_cursor` / `has_more` |
| 幂等 | 写接口可带 `Idempotency-Key` 头；同 key 重复请求返回首次结果 |
| 追踪 | 响应头 `X-Request-ID`；SSE 事件含 `run_id` |
| 内容类型 | 请求/响应 `application/json`；上传 `multipart/form-data`；流式 `text/event-stream` |

### 认证

除注册/登录/健康检查外，所有用户侧接口要求：

```http
Authorization: Bearer <access_token>
```

- access token 短期（默认 15 分钟），refresh token 长期（默认 7 天）并旋转；
- 令牌 payload 只放身份（`sub`、`jti`、`exp`），权限从服务端读取；
- 401 表示未认证/令牌过期；403 表示已认证但无权限。

### 错误信封

```json
{
  "error": {
    "code": "not_found",
    "message": "会话不存在",
    "request_id": "7f3c9a2e..."
  }
}
```

| HTTP | code | 说明 |
|---|---|---|
| 400 | `bad_request` | 参数语义错误 |
| 400 | `replay_too_large` | SSE 续流待回放事件超过 1000 条（M7；改用分页回放或更接近的 `after_seq`） |
| 401 | `unauthorized` | 未登录 / token 无效 |
| 403 | `forbidden` | 无权限；M7 起 agents/tasks 写接口要求管理员，M8 起全部 `/admin/*` 要求管理员；`details.required_role="admin"`，拒绝写审计 `security.rbac_denied` |
| 403 | `capability_disabled` | 能力总开关关闭（仅运行态接口；M8 管理端配置面不受影响，见 §12.1） |
| 404 | `not_found` | 资源不存在（也用于隐藏他人资源） |
| 409 | `conflict` | 状态冲突（重复审批、并发 run、M8 系统 MCP slug 跨 scope 冲突） |
| 409 | `last_admin_protected` | M8：目标是最后一个活跃管理员，降级/停用被拒绝；`details.active_admins=1` |
| 413 | `payload_too_large` | 技能包/上传文件超限 |
| 422 | `validation_error` | 请求体/查询参数校验失败（FastAPI 默认结构会包装为本信封） |
| 422 | `skill_invalid` | 技能包校验失败 |
| 422 | `instruction_too_long` | 指令超过字符上限（用户指令与 M8 平台默认指令共用） |
| 422 | `cannot_change_own_role` | M8：管理员不能修改自己的角色 |
| 422 | `cannot_disable_self` | M8：管理员不能停用自己的账号 |
| 422 | `mcp_transport_not_allowed` | MCP 传输被禁用（用户自建仅远程；M8 系统 MCP 同样校验） |
| 422 | `mcp_ssrf_blocked` | MCP URL 未通过 SSRF/主机白名单校验（用户侧与 M8 系统 MCP 共用） |
| 422 | `secret_ref_not_allowed` | 普通用户写入 `secret:` 平台密钥引用被拒绝（M8 系统 MCP/Agent 管理员路径允许） |
| 422 | `secret_ref_invalid` | `secret:` 引用格式非法 |
| 429 | `rate_limited` | 触发限流 |
| 500 | `internal_error` | 未预期异常 |
| 503 | `service_unavailable` | 依赖服务不可用（M4：Embedding 服务故障时的检索） |
| 503 | `database_unavailable` | 数据库连接不可用（M6；连接/pool 故障） |
| 503 | `secret_store_unavailable` | 敏感请求头以明文写入但未配置 `CAPABILITY_SECRET_KEY`（M5 `/admin/secrets`；M8 系统 MCP/Agent 写接口交叉引用） |
| 502 | `a2a_unavailable` | 远程智能体不可用 |
| 502 | `mcp_unreachable` | 显式选择的 MCP 服务器不可达 |
| 504 | `a2a_timeout` | 远程调用超时 |

> **M8 交叉引用（2026-11-11）**：管理端用户 PATCH 额外返回 422 `cannot_change_own_role` /
> `cannot_disable_self` 与 409 `last_admin_protected`；系统 MCP/全局 Agent 的写接口复用既有
> MCP/凭据校验，可能出现 422 `mcp_ssrf_blocked` / `mcp_transport_not_allowed` /
> `secret_ref_not_allowed` / `secret_ref_invalid` 与 503 `secret_store_unavailable`；
> 平台指令 PUT 超长返回 422 `instruction_too_long`。所有新增 `/admin/*` 端点：
> 未登录 401 `unauthorized`，非管理员 403 `forbidden` + `details.required_role="admin"`
> （与 M7 的 agents/tasks 写接口一致）。

> M6：启用 Trace 且采样时响应头可能带 `X-Trace-ID`；`X-Request-ID` 始终存在。

## 2. 端点总表

### 2.1 认证

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/auth/register` | 注册（邮箱 + 密码） |
| POST | `/api/v1/auth/login` | 登录，返回 access/refresh |
| POST | `/api/v1/auth/refresh` | 刷新令牌（旋转 refresh token） |
| POST | `/api/v1/auth/logout` | 注销（吊销当前 refresh token） |
| GET | `/api/v1/auth/me` | 当前用户 |

### 2.2 会话与消息

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/conversations` | 会话列表（按最近消息排序） |
| POST | `/api/v1/conversations` | 创建会话 |
| GET | `/api/v1/conversations/{id}` | 会话详情 |
| PATCH | `/api/v1/conversations/{id}` | 重命名/归档 |
| DELETE | `/api/v1/conversations/{id}` | 删除会话（软删） |
| GET | `/api/v1/conversations/{id}/messages` | 消息历史（倒序分页） |

### 2.3 聊天与运行

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/chat/stream` | 发送消息，SSE 流式返回 |
| GET | `/api/v1/runs/{run_id}` | 运行状态与用量 |
| GET | `/api/v1/runs/{run_id}/events` | 事件分页回放（不跟随实时流；每项含 `event` 原始事件名，M7） |
| GET | `/api/v1/runs/{run_id}/stream` | 运行事件 SSE 续流：先回放 `seq > after_seq` 的已落库事件，再跟随实时事件（M7） |
| POST | `/api/v1/runs/{run_id}/cancel` | 取消运行 |
| POST | `/api/v1/runs/{run_id}/resume` | 恢复 `input-required` 的运行 |

> 续流端点鉴权：`Authorization: Bearer <access token>`；未登录/无效 token 401 `unauthorized`，
> 非本人或不存在统一 404 `not_found`（不泄露存在性），待回放事件 >1000 条 400 `replay_too_large`，
> `after_seq` 非法 422 `validation_error`；**不支持 `Last-Event-ID`**，重连必须显式传 `after_seq`。
> 回放只覆盖已落库事件：`message.delta`/`reasoning.delta` 不落 `run_events`，断线窗口内的增量
> 不会补发；run **进入终态**（`succeeded`/`failed`/`canceled`）后用
> `GET /conversations/{id}/messages` 取终稿覆盖。`run.interrupted` 期间助手消息仍为
> `messages.status=streaming`，历史接口不会返回，需等审批恢复或超时取消。详见 §7.8。

### 2.4 远程智能体（M2 已实现）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/agents/preview` | 拉取并校验 Agent Card，不落库（`{valid, card, normalized, warnings}`） |
| POST | `/api/v1/agents` | 注册远程 Agent（拉取 Agent Card；同 url+slug 幂等返回已有）（**仅管理员**） |
| GET | `/api/v1/agents` | 已注册 Agent 列表：`health_status`、`enabled`、`q`、`limit`、`cursor` + `stats` |
| GET | `/api/v1/agents/{id}` | Agent 详情（含原始 Card、skills、auth_hint、last_*、recent_tasks） |
| PATCH | `/api/v1/agents/{id}` | 修改 `{enabled?, name?, auth?{type,secret_ref}, headers?}`（headers 敏感头只存引用/密文）（**仅管理员**） |
| DELETE | `/api/v1/agents/{id}` | 软删除（有运行中任务 409 `agent_has_active_tasks`）（**仅管理员**） |
| POST | `/api/v1/agents/{id}/refresh` | 重新拉取 Agent Card，返回 `{agent, changed, changed_fields}`；失败保留旧 Card 标 unhealthy（**仅管理员**） |
| POST | `/api/v1/agents/{id}/health` | 主动健康检查（拉 Card + tasks/get 探测），返回 `{health_status, latency_ms, checked_at, error}`（**仅管理员**） |

字段与错误码：

- 列表项：`id/slug/name/description/url/protocol_version/skills[{id,name,description}]/skill_count/auth_type(none|api_key|oauth2)/auth_scheme(bearer|header|oauth2|none)/auth_hint/capabilities{streaming,push_notifications}/health_status(unknown|healthy|degraded|unhealthy)/enabled/last_checked_at/last_latency_ms/last_error/recent_task{id,a2a_task_id,state,duration_ms,finished_at}|null/created_at/updated_at`；
- `stats`：`{total, healthy, degraded, unhealthy, disabled, enabled}`；
- 注册/预览失败：422 `agent_card_invalid`（Card 非法/不可达/超时/跨源重定向）或 `agent_url_not_allowed`（协议/内网/凭据 URL）；
- 健康阈值：成功且延迟 <2s → healthy，≥2s → degraded；连续失败 ≥3 → unhealthy；`A2A_ALLOW_LOOPBACK` 仅 dev/test 默认放行回环（本地假 Agent 联调），prod 默认拒绝；
- **写接口仅管理员（M7）**：上表 `POST /agents`、`PATCH/DELETE /agents/{id}`、`POST /agents/{id}/refresh`、`POST /agents/{id}/health` 要求管理员（`AdminUser`）；普通用户返回 403 `forbidden` + `details.required_role="admin"`，并写审计 `security.rbac_denied`（detail 含 method/path/required_role）。`GET /agents*` 与 `POST /agents/preview` 对所有登录用户开放。

### 2.5 任务（M2 已实现）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/tasks` | A2A 任务列表：`state`（可重复/逗号）、`agent_id`、`run_id`、`conversation_id`、`q`、`from`、`to`、`limit`、`cursor` + `stats` |
| GET | `/api/v1/tasks/{id}` | 任务详情（request/result/error/input_request/auth_request/timeline） |
| POST | `/api/v1/tasks/{id}/cancel` | 取消（submitted/working/input_required/auth_required；尽力远程 tasks/cancel）（**仅管理员**） |
| POST | `/api/v1/tasks/{id}/resume` | 补充输入（仅 input_required；`{input}` → `{task, run:{id,status}|null}`，同一 a2a_task_id 继续）（**仅管理员**） |
| POST | `/api/v1/tasks/{id}/authorize` | 授权重试（仅 auth_required；`{scopes?, note?}`）（**仅管理员**） |
| POST | `/api/v1/tasks/{id}/reject` | 拒绝（auth_required 或 input_required；`{reason?}`，state=rejected）（**仅管理员**） |

字段与错误码：

- 列表项：`id/a2a_task_id/agent{id,slug,name}/run_id/conversation{id,title}|null/state/progress/duration_ms/request_summary/error_summary/available_actions/created_at/updated_at`；
- `stats`：`{total, submitted, working, input_required, auth_required, completed, failed, canceled, rejected}`；
- 详情补：`tool_call_id/request{tool,args,summary}/result{artifact{type,summary,parts,data},summary}/error{code,message,retryable,at}/input_request{question,input_schema,expires_at}/auth_request{scopes,authorization_url,expires_at}/timeline[{state,at,duration_ms,note}]/started_at/finished_at`；`auth_request.authorization_url` 可能为 `null`（M7 白名单过滤，见 §9）；
- `available_actions` 只含后端可执行动作（cancel/resume/authorize/reject），由 state 推导；是否可执行还取决于当前用户角色（写动作仅管理员）；
- 动作错误 409：`task_not_cancelable` / `task_not_resumable` / `task_not_authorizable` / `task_not_rejectable`；
- 等待态 `expires_at` 到期懒过期置 canceled（timeline 追加「等待超时」，审计 `task.expired`）；`A2A_INPUT_TIMEOUT_SECONDS` 默认 30 分钟；
- **动作接口仅管理员（M7）**：`POST /tasks/{id}/cancel`、`/resume`、`/authorize`、`/reject` 要求管理员；普通用户 403 `forbidden` + `details.required_role="admin"`，并写审计 `security.rbac_denied`；`GET /tasks*` 保持所有登录用户可用。

### 2.6 知识库

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/knowledge/documents` | 上传文档（**同步入库**，返回终态）；新建 201、幂等命中 200 |
| GET | `/api/v1/knowledge/documents` | 文档列表（`q`/`category`/`status`/`owner_scope` + 游标分页 + `stats`） |
| GET | `/api/v1/knowledge/documents/{id}` | 文档详情 |
| DELETE | `/api/v1/knowledge/documents/{id}` | 删除文档、分块与原始文件 |
| POST | `/api/v1/knowledge/documents/{id}/retry` | 失败文档重试（重读原始文件重新入库） |
| POST | `/api/v1/knowledge/search` | 检索调试（返回片段与分数） |
| POST | `/api/v1/knowledge/rebuild` | 重建索引（管理员）；`scope` = `all` / `system` / `own` |

可见性：检索恒为「系统文档 + 本人文档」，管理员不放大；管理接口可用 `owner_scope=all`（仅管理员）查看全部。

文档对象恰好 11 个字段：`id, title, source_uri, category, status, chunk_count, size_bytes, owner, error, created_at, updated_at`（**不含** `filename` / `storage_key`；`owner` 为 `null` 表示系统文档，`error` 为 `null` 表示无错误）。

上传约束：允许 `.md/.markdown/.txt/.pdf/.csv/.json`；超过 `KNOWLEDGE_MAX_UPLOAD_BYTES`（默认 10MB）返回 **413 `payload_too_large`**；扩展名不支持返回 **422 `validation_error`**；`scope=system` 需要管理员。

### 2.7 审批

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/approvals` | 审批列表（默认 pending） |
| GET | `/api/v1/approvals/{id}` | 审批详情 |
| POST | `/api/v1/approvals/{id}/decision` | 批准/拒绝；该 run 无其他 pending 审批时调度恢复 |

M5 起审批恢复走 LangGraph `Command(resume=...)`，决定接口的 `resumed` 语义：

- `resumed=true`：审批已全部处理完，恢复任务已调度；运行会继续发 `run.resumed` 与后续 `message.delta`；
- 该 run 仍有其他 `pending` 审批：`resumed=false`、`resume_error="本轮还有其他审批待处理"`，最后一个决定后才恢复；
- run 已结束：`resumed=false`，前端按「审批已记录」处理，不要当致命错误；
- 重复决定 409 `approval_already_decided`；过期 409 `approval_expired`。

### 2.8 MCP

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/mcp/servers` | 系统项 + 自己创建的 MCP 服务器（含有效启停状态） |
| POST | `/api/v1/mcp/servers` | 创建（用户侧仅远程 URL，stdio 默认禁用） |
| GET | `/api/v1/mcp/servers/{id}` | 详情 |
| PATCH | `/api/v1/mcp/servers/{id}` | 修改名称/URL/凭据/自动注入 |
| DELETE | `/api/v1/mcp/servers/{id}` | 删除（系统项不可删） |
| PUT | `/api/v1/mcp/servers/{id}/enabled` | 启停（系统项写用户覆盖表） |
| POST | `/api/v1/mcp/servers/{id}/check` | 连通性检查 + 工具发现 |
| POST | `/api/v1/mcp/servers/{id}/refresh-tools` | 强制刷新工具缓存 |
| GET | `/api/v1/mcp/servers/{id}/tools` | 工具列表（含风险/审批/启停） |
| PATCH | `/api/v1/mcp/tools/{id}` | 工具启停、审批策略（所有者） |
| GET | `/api/v1/mcp/available` | 输入框选择器用的精简目录 |

> 系统 MCP 对普通用户只返回只读工具；写/破坏性工具仅管理员可用，`GET /mcp/servers/{id}/tools` 会返回 `user_allowed=false` 供前端置灰说明。

### 2.9 技能（本地 Skill）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/skills` | 系统 + 自己的技能（含有效启停） |
| POST | `/api/v1/skills` | 手写创建（name/description/content） |
| POST | `/api/v1/skills/import` | 上传 zip 技能包（multipart） |
| GET | `/api/v1/skills/{id}` | 详情（含参考文件列表） |
| PATCH | `/api/v1/skills/{id}` | 修改自己的技能 |
| DELETE | `/api/v1/skills/{id}` | 删除自己的技能 |
| PUT | `/api/v1/skills/{id}/enabled` | 启停（系统项写用户覆盖表） |
| GET | `/api/v1/skills/{id}/files` | 参考文件列表 |
| GET | `/api/v1/skills/{id}/files/{path}` | 读取参考文件（文本） |
| GET | `/api/v1/skills/catalog` | 输入框选择器用的精简目录 |

### 2.10 长期记忆

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/memories` | 列表：`limit`、`cursor`、`kind`、`status`、`q`（有 q 走语义检索） |
| POST | `/api/v1/memories` | 手动新增 |
| GET | `/api/v1/memories/{id}` | 详情 |
| PATCH | `/api/v1/memories/{id}` | 修改内容/类型/标题/置顶/状态 |
| DELETE | `/api/v1/memories/{id}` | 物理删除 |
| DELETE | `/api/v1/memories` | 清空全部（`?confirm=true`） |
| GET | `/api/v1/memories/export` | 导出 JSON |

### 2.11 指令

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/instructions` | 生效指令、来源（user/system_default）、默认值、上限 |
| PUT | `/api/v1/instructions` | 保存用户指令；`null` 表示恢复默认 |

### 2.12 管理端

> M8（2026-11-11）起以下端点**全部要求 `users.role='admin'`**：未登录 401 `unauthorized`，
> 非管理员 403 `forbidden` + `details.required_role="admin"`（并写审计 `security.rbac_denied`）。
> 管理员由运维脚本 `scripts/grant_admin.py` 设置，注册流程不自动提权。详细契约与实测
> 字段见 §12；本节只列路由。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/admin/users` | 用户分页列表（`q`/`role`/`status`/`limit`/`cursor` + `stats`） |
| GET | `/api/v1/admin/users/{id}` | 用户详情（与列表项同形，不含 `password_hash`） |
| PATCH | `/api/v1/admin/users/{id}` | 改 `role`/`status`（自操作 422；最后一个活跃管理员 409 `last_admin_protected`） |
| GET | `/api/v1/admin/overview` | 只读概览计数（不写审计） |
| GET/PUT | `/api/v1/admin/settings/instruction` | 平台默认指令；`null` 恢复内置默认 |
| GET | `/api/v1/admin/skills` | 系统技能列表（`scope='system'`，不分页） |
| PUT | `/api/v1/admin/skills/{id}/enabled` | 全局启停系统技能 |
| PUT | `/api/v1/admin/skills/{id}/force-disable` | 强制停用/恢复（压过用户覆盖） |
| GET/POST | `/api/v1/admin/mcp/servers` | 系统 MCP 列表 / 新建 |
| PATCH/DELETE | `/api/v1/admin/mcp/servers/{id}` | 系统 MCP 更新 / 删除 |
| GET | `/api/v1/admin/agents` | 全局 Agent 注册表（分页，查询参数同 `GET /agents`） |
| PATCH | `/api/v1/admin/agents/{id}` | 全局 Agent 白名单字段更新 |
| GET | `/api/v1/admin/audit-logs` | 全局审计检索（M5，M8 不变） |
| GET/POST/PATCH/DELETE | `/api/v1/admin/secrets[...]` | 平台密钥管理（M5，M8 不变；永不回显明文） |

> **配置面不受能力总开关影响（R1）**：`INSTRUCTIONS_ENABLED` / `SKILLS_ENABLED` /
> `MCP_ENABLED=false` 时，上表对应的管理端配置接口仍可读写；运行态接口
> `/instructions`、`/skills`、`/mcp/*` 与聊天行为不变（仍 403 `capability_disabled`）。
> M8 **不扩展** `/admin/audit-logs` 的过滤参数，也不改 `/admin/secrets` 接口（R9）。

### 2.13 系统

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 存活探针（不查依赖；返回 version/build/env） |
| GET | `/readyz` | 就绪探针（DB/迁移/checkpoint/存储/排水；关键失败 503） |
| GET | `/metrics` | Prometheus 指标（根路径，M6；可用 `METRICS_TOKEN` 保护，关闭时 404） |
| GET | `/api/v1/system/info` | 版本、模型配置、A2A 入站状态、能力开关 |
| GET | `/metrics` | Prometheus 文本指标；不在 OpenAPI schema 中 |

`system/info.a2a` 字段（2026-11-10 起）：

- 本服务只做 A2A 出站 Client，不提供入站 Server；
- `enabled` 恒 `false`，`card_url` / `jsonrpc_url` 恒为空字符串，`auth_type` / `card_security` 恒 `none`；
- 五个字段保留只为兼容前端契约，不表示可被调用的 A2A 地址。

### 2.14 审计日志

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/audit-logs` | 当前用户的审计记录（固定 `actor_type=user` + 自己），支持 `action`、`resource_type`、`start`、`end`、`cursor` |
| GET | `/api/v1/admin/audit-logs` | 管理员全局检索，额外支持 `actor_type`、`actor_id`、`resource_id` |

响应：`{items, next_cursor, has_more}`，按 `created_at DESC, id DESC` 排序；游标不透明。
普通用户访问 `/admin/*` 返回 403。

### 2.15 平台密钥

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/admin/secrets` | 列表（`enabled` 可过滤；只回 name/description/enabled/时间） |
| POST | `/api/v1/admin/secrets` | 创建 `{name,value,description}`；无 `CAPABILITY_SECRET_KEY` → 503 `secret_store_unavailable` |
| PATCH | `/api/v1/admin/secrets/{name}` | 轮换 `value`、改描述或启停 |
| DELETE | `/api/v1/admin/secrets/{name}` | 204；不检查引用 |

`secret:NAME` 引用只允许管理员写入（普通用户 → 422 `secret_ref_not_allowed`），运行时无条件解析；
接口与审计永不回显明文或密文。

## 3. 聊天 SSE 契约

### 3.1 请求

```http
POST /api/v1/chat/stream
Authorization: Bearer <token>
Content-Type: application/json
Accept: text/event-stream

{
  "conversation_id": "0f1e...",
  "message": "帮我查一下退货政策，然后让代码助手看看这段代码",
  "client_message_id": "c-uuid",
  "attachments": [],
  "skills": ["<skill_id>"],
  "mcp_servers": ["<server_id>"],
  "mcp_tools": ["<tool_id>"],
  "knowledge_categories": ["售后"]
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `conversation_id` | 否 | 为空时创建新会话，首个事件返回 `conversation.created` |
| `message` | 是 | 用户输入，1–8000 字符 |
| `client_message_id` | 否 | 前端幂等键，重试时避免重复消息 |
| `attachments` | 否 | 附件 ID 列表（文件上传接口后续提供） |
| `skills` | 否 | 本轮显式激活的本地技能，≤ 3 个 |
| `mcp_servers` | 否 | 本轮强制纳入的 MCP 服务器，≤ 5 个 |
| `mcp_tools` | 否 | 本轮强制纳入的 MCP 工具，≤ 10 个 |
| `knowledge_categories` | 否 | 本轮检索的知识库分类（≤ `KNOWLEDGE_CATEGORIES_PER_MESSAGE`，默认 5）；作为 `search_knowledge` 工具的默认分类过滤 |

> 显式选择只影响本次请求，不写回账号配置；MCP 写工具仍需审批。

### 3.2 响应

`Content-Type: text/event-stream`，每帧：

```text
id: <递增序号>
event: <事件名>
data: <单行 JSON>

```

服务端每 15 秒发送注释心跳 `: ping`。

### 3.3 事件类型

| 事件 | 时机 | data 关键字段 |
|---|---|---|
| `conversation.created` | 新会话创建 | `conversation_id` |
| `message.created` | 用户/助手消息落库 | `message_id`、`role` |
| `message.delta` | 模型增量 token | `message_id`、`delta` |
| `tool.start` | 工具开始执行 | `tool_call_id`、`tool`、`args`、`source`、`server`（MCP 时） |
| `tool.result` | 工具执行结束 | `tool_call_id`、`status`、`summary`、`source` |
| `a2a.task` | 远程任务状态变化 | `task_id`（本地）、`a2a_task_id`、`agent`（slug）、`agent_detail{id,slug,name}`、`state`、`progress`、`request_summary`、`result_summary`、`duration_ms`、`error` |
| `a2a.artifact` | 远程产出片段 | `task_id`、`a2a_task_id`、`artifact{type,summary,parts,data}` |
| `a2a.input_required` | 远程要求补充信息 | `task_id`、`a2a_task_id`、`agent`、`agent_detail`、`question`、`input_schema`、`expires_at` |
| `a2a.auth_required` | 远程要求授权 | `task_id`、`a2a_task_id`、`agent`、`agent_detail`、`scopes`、`authorization_url`、`expires_at` |
| `approval.required` | 需要人工确认（gate 创建审批时） | `approval_id`、`tool_call_id`、`tool`、`tool_display_name`、`source`、`executor`、`target`、`side_effect`、`risk`、`reason`、`payload`、`payload_preview`、`expires_at`（M7 起补齐 5 个展示字段） |
| `run.interrupted` | 图停在审批 gate（M5） | `run_id`、`conversation_id`、`message_id`、`status="interrupted"`、`approvals`（pending 审批 id 列表） |
| `run.resumed` | 审批决定后恢复执行（M5） | `run_id`、`conversation_id`、`status="running"` |
| `skill.activated` | 本轮技能激活 | `skill_id`、`slug`、`name`、`scope`、`estimated_tokens` |
| `memory.saved` | 记忆保存成功 | `memory_id`、`kind`、`action`、`preview` |
| `notice` | 能力降级/提示 | `level`、`code`、`message` |
| `message.done` | 本轮结束 | `run_id`、`message_id`、`usage`、`finish_reason`；审批超时取消时 `finish_reason="canceled"`、`reason="approval_timeout"` |
| `error` | 流内错误 | `code`、`message`、`request_id` |
| `ping` | 心跳（也可用注释） | — |

> M7：事件回放接口 `GET /runs/{id}/events` 的每一项新增 `event` 字段，值即本表的原始 SSE
> 事件名（未知/历史缺失时为 `""`）；`payload` 仍剔除内部字段 `_event`，分类字段 `type` 不变。
> 详见 §7.7。

### 3.4 示例

```text
id: 1
event: conversation.created
data: {"conversation_id":"0f1e...","title":"新对话"}

id: 2
event: message.created
data: {"message_id":"a1b2...","role":"user"}

id: 3
event: tool.start
data: {"tool_call_id":"call_1","tool":"search_knowledge","args":{"query":"退货政策"}}

id: 4
event: tool.result
data: {"tool_call_id":"call_1","status":"ok","summary":"命中 3 个片段","sources":["退货政策.md#3"]}

id: 5
event: a2a.task
data: {"task_id":"b7c8...","agent":"code-assistant","state":"working","progress":0.3}

id: 6
event: message.delta
data: {"message_id":"e5f6...","delta":"根据退货政策，"}

id: 7
event: message.done
data: {"run_id":"9a8b...","message_id":"e5f6...","usage":{"input_tokens":812,"output_tokens":156},"finish_reason":"stop"}
```

### 3.5 断线与取消

- 客户端断开 SSE **不会自动取消**后端运行；运行继续并落库；
- 前端重连后用 `GET /api/v1/runs/{run_id}` 取最终状态；
- 需要停止时显式调用 `POST /api/v1/runs/{run_id}/cancel`；
- `message.done` 后服务端关闭流；`error` 事件后也会关闭流。

## 4. 关键端点示例

### 4.1 注册

```http
POST /api/v1/auth/register
Content-Type: application/json

{"email": "alice@example.com", "password": "S3cure!pass", "display_name": "Alice"}
```

```json
{
  "user": {"id": "...", "email": "alice@example.com", "display_name": "Alice"},
  "access_token": "...",
  "refresh_token": "...",
  "token_type": "bearer",
  "expires_in": 900
}
```

### 4.2 会话列表

```http
GET /api/v1/conversations?limit=20&status=active
```

```json
{
  "items": [
    {
      "id": "...",
      "title": "退货政策咨询",
      "status": "active",
      "last_message_at": "2026-11-04T15:30:00Z",
      "created_at": "2026-11-04T15:00:00Z"
    }
  ],
  "next_cursor": null,
  "has_more": false
}
```

### 4.3 注册远程 Agent

```http
POST /api/v1/agents
Authorization: Bearer <token>
Content-Type: application/json

{
  "url": "https://code-agent.example.com/a2a",
  "slug": "code-assistant",
  "auth": {"type": "api_key", "secret_ref": "env:CODE_AGENT_KEY"}
}
```

```json
{
  "id": "...",
  "slug": "code-assistant",
  "name": "Code Assistant",
  "url": "https://code-agent.example.com/a2a",
  "protocol_version": "1.0",
  "skills": [{"id": "review_code", "name": "代码审查"}],
  "health_status": "healthy",
  "enabled": true
}
```

> `secret_ref` 只存引用（环境变量/密钥管理服务），数据库不落明文密钥。

### 4.4 审批决定

```http
POST /api/v1/approvals/{id}/decision
{"decision": "approve", "note": "确认执行"}
```

```json
{"id": "...", "status": "approved", "run_id": "...", "resumed": true}
```

`decision` 取值：`approve` / `reject`。审批超时后返回 409 `conflict`。

### 4.5 知识库检索调试

```http
POST /api/v1/knowledge/search
{"query": "退货需要保留吊牌吗", "top_k": 5, "filters": {"category": "售后"}}
```

```json
{
  "hits": [
    {
      "chunk_id": "...",
      "document_id": "...",
      "title": "退货政策 > 吊牌要求",
      "document_title": "退货政策",
      "document_filename": "return-policy.md",
      "category": "售后",
      "chunk_index": 0,
      "content": "客户申请退货时必须保留商品吊牌……",
      "score": 0.83,
      "updated_at": "2026-11-10T13:26:28.581291Z"
    }
  ],
  "elapsed_ms": 8
}
```

- `title` 是片段所属章节标题（解析不到时回退为文档标题）；`updated_at` 取**文档**的更新时间；
- `elapsed_ms` 为真实耗时（示例值非契约）；空库返回 `{"hits": [], "elapsed_ms": 0..}`，不是错误；
- `score = round(1 - 余弦距离, 4)`；fake embedding 模式下不应用分数阈值（词袋模型的短查询对长分块余弦上界约 0.11），真实 provider 才使用 `VECTOR_SCORE_THRESHOLD`；
- Embedding 服务不可用时返回 **503 `service_unavailable`**。

### 4.6 创建 MCP 服务器

```http
POST /api/v1/mcp/servers
Authorization: Bearer <token>
Content-Type: application/json

{
  "name": "GitHub",
  "slug": "github",
  "transport": "streamable_http",
  "url": "https://mcp.example.com/mcp",
  "headers": {"Authorization": "Bearer ghp_xxx"}
}
```

```json
{
  "id": "...",
  "scope": "user",
  "slug": "github",
  "name": "GitHub",
  "transport": "streamable_http",
  "enabled": true,
  "effective_enabled": true,
  "health_status": "healthy",
  "tool_count": 12,
  "credentials_hint": "Bearer ****xxx",
  "last_checked_at": "2026-11-05T15:30:00Z"
}
```

> 请求头只写不回；响应只返回 `credentials_hint`，明文凭据不落接口。

### 4.7 记忆列表

```http
GET /api/v1/memories?limit=20&kind=preference&status=active
```

```json
{
  "items": [
    {
      "id": "mem_9f2c...",
      "kind": "preference",
      "title": "称呼",
      "content": "用户希望被称为「老赵」",
      "pinned": true,
      "status": "active",
      "source": {"conversation_id": "...", "message_id": "...", "created_by": "agent"},
      "created_at": "2026-11-05T10:00:00Z",
      "updated_at": "2026-11-05T10:00:00Z"
    }
  ],
  "next_cursor": null,
  "has_more": false
}
```

### 4.8 生效指令

```http
GET /api/v1/instructions
```

```json
{
  "effective": "请用简洁的中文回答，先给结论，再给依据。",
  "source": "user",
  "user_value": "请用简洁的中文回答，先给结论，再给依据。",
  "default_value": "回答使用中文；专业问题给出依据。",
  "max_chars": 4000,
  "updated_at": "2026-11-05T10:00:00Z"
}
```

## 5. 限流与配额

| 接口 | 默认限制 | 说明 |
|---|---|---|
| `POST /auth/login` | 10 次/5 分钟/IP（`RATE_LIMIT_LOGIN_*` 可调） | 防爆破 |
| `POST /auth/register` | 5 次/小时/IP（`RATE_LIMIT_REGISTER_*` 可调） | 防批量注册；本地联调用 `scripts/seed_dev_account.py` |
| `POST /chat/stream` | 30 次/60 秒/用户（`RATE_LIMIT_CHAT_*` 可调），同时 1 个运行 | 超出返回 429 `rate_limited`，`details.retry_after_seconds` 为建议重试秒数 |
| `POST /agents` | 30 次/小时/用户 | 管理操作 |
| `POST /mcp/servers` | 30 次/小时/用户 | 管理操作 |
| `POST /mcp/servers/{id}/check` | 10 次/小时/用户 | 连接测试 |
| `POST /skills/import` | 20 次/小时/用户 | 上传校验成本高 |
| `POST /memories` | 60 次/分钟/用户 | |
| 能力目录读取（`/mcp/available`、`/skills/catalog`） | 120 次/分钟/用户 | |

## 6. 兼容与版本

- 路径版本 `/api/v1`；破坏性变更升 `/api/v2`，旧版本保留至少一个迭代；
- 响应只增字段不删字段；新增枚举值前端需容忍未知值；
- SSE 事件新增类型时前端忽略未知事件，不报错；
- OpenAPI 作为契约源，前端类型可由 `/openapi.json` 生成（规划）。

---

## 7. M1 实现状态（2026-11-09）

> 本节记录相对上文设计稿的落地差异；机器可读契约见 [`openapi.json`](openapi.json)。

### 7.1 已实现端点

| 模块 | 端点 | 备注 |
|---|---|---|
| 认证 | `POST /auth/register`、`POST /auth/login`、`POST /auth/refresh`、`POST /auth/logout`、`GET /auth/me`、`PATCH /auth/me`、`POST /auth/change-password` | refresh token 落库、旋转、吊销；access 15 分钟、refresh 7 天 |
| 会话 | `GET/POST /conversations`、`GET/PATCH/DELETE /conversations/{id}`、`GET /conversations/{id}/messages` | 标题搜索、状态筛选、cursor 分页、`stats` |
| 聊天 | `POST /chat/stream` | SSE + `X-Run-ID`；单会话并发 409 |
| 运行 | `GET /runs/{id}`、`GET /runs/{id}/events`、`POST /runs/{id}/cancel`、`POST /runs/{id}/resume` | 事件回放支持 `after_seq`、`types`、`q`；`resume` 在 M1 返回 409 `run_not_resumable`（见 7.8） |
| 审批 | `GET /approvals`、`GET /approvals/{id}`、`POST /approvals/{id}/decision` | M1 无工具触发，仅数据模型与接口 |
| 系统 | `GET /healthz`、`GET /readyz`、`GET /system/info` | 能力开关、模型白名单、限额 |

### 7.2 错误信封扩展

`error` 对象在 `code` / `message` / `request_id` 之外允许可选的 `details`：

```json
{"error":{"code":"run_in_progress","message":"…","request_id":"…","details":{"run_id":"…"}}}
```

- `run_in_progress.details.run_id`：当前占用会话的 run；
- `run_not_cancelable.details.status`：当前状态；
- `rate_limited.details.retry_after_seconds`：建议重试秒数。

### 7.3 SSE 实现范围

已实现：`conversation.created`、`run.started`、`message.created`、`message.started`、`message.delta`、`message.done`、`error`，心跳 `: ping`。

- `seq` 从 1 开始单调递增；`message.delta` 不落 `run_events`，因此事件回放的 `seq` 允许跳号；
- 取消后 `message.done.finish_reason=canceled`，已生成内容保留；
- 相同 `conversation_id + client_message_id` 返回同一 run 的事件回放，不创建新消息；
- 致命错误只发 `error`，不发 `message.done`；
- `reasoning.delta/done`、`tool.*`、`a2a.*`、`skill.activated`、`approval.required`、`memory.saved`、`notice` 已在 M1.5/M2 接入（详见 §8.2）；`workflow.step` 尚未实现，工作流已立项为 M9。

### 7.4 能力开关

`GET /system/info.capabilities` 在 M1.5 返回：

```json
{
  "instructions": true, "memories": true, "skills": true, "mcp": true,
  "knowledge": true, "attachments": true, "workflows": false,
  "approvals": true, "agents": false, "tasks": false
}
```

- 请求携带未启用的能力字段时返回 403 `capability_disabled`；
- 携带 `workflow_id` 返回 409 `workflow_disabled`；
- `knowledge_categories` **M4 起已开放**（不再 403），作为 `search_knowledge` 工具的默认分类过滤；`KNOWLEDGE_ENABLED=false` 时该字段与所有知识库端点一起返回 403 `capability_disabled`；`agents`/`tasks` 自 M2 起均为 true；
- `reasoning_efforts` 默认空数组（模型不支持或 `LLM_REASONING_EFFORT_SUPPORTED=false` 时），`default_reasoning_effort` 仍返回 `low`；打开该开关后返回 `low/medium/high` 三项。

### 7.5 数据层差异

- 新增 `refresh_tokens` 表（用于 refresh token 轮换/吊销）；
- `messages.status` 增加内部值 `streaming`，API 只返回 `completed/failed/canceled`；
- `messages` 增加 `updated_at`；`runs` 按交接包补齐用量、首 token、时长、工作流占位列；
- 同一会话未结束 run 由部分唯一索引 `uq_runs_active_conversation` 保证唯一；
- `messages.run_id` 与 `runs.user_message_id/assistant_message_id` 互相引用，迁移中用 `ALTER TABLE` 补外键。

### 7.6 分页契约（M1 实测，M8 追加管理端）

以下分页端点共用同一套不透明游标：`GET /conversations`、`GET /conversations/{id}/messages`、
`GET /approvals`、`GET /runs/{id}/events`，以及 M8 的 `GET /admin/users`、
`GET /admin/agents`（与 `GET /agents` 同函数、同游标）。

- `next_cursor` 是 urlsafe base64 编码的排序键，前端不得解析、拼接或当作 UUID；
- `has_more=false` 时 `next_cursor=null`，不要继续请求；
- 非法游标返回 422 `validation_error`，不会 500；
- `stats` 是「当前筛选条件下的全量计数」，不是当前页计数；
- 会话排序 `last_message_at DESC NULLS LAST, id DESC`；消息与审批 `created_at DESC, id DESC`；
  运行事件 `seq ASC`；
- **M8 `GET /admin/users`**：排序固定 `created_at DESC, id DESC`；游标 payload 为
  `{created_at, id}`（不透明串，前端不得解析），解码失败或缺字段 → 422 `validation_error`；
  `stats` 是当前 `q`/`role`/`status` 筛选条件（不含游标）下的全量计数
  `{total, active, disabled, admins}`，其中 `admins` 统计 `role='admin'` 的**所有状态**行；
- **M8 `GET /admin/agents`**：查询参数与游标语义和 `GET /agents` 完全一致
  （`health_status`/`enabled`/`q`/`limit`/`cursor`），`stats` 为当前筛选下、排除已软删的全量计数；
- 消息历史 `before` 与 `cursor` 二选一，同时传以 `cursor` 为准；
- 所有时间统一 UTC、序列化以 `Z` 结尾。

> 2026-11-09 热修：此前 cursor 编码把 ORM 的 `UUID` 对象直接交给 `json.dumps`，
> 只要 `has_more=true` 需要生成 `next_cursor` 就会 500。现在编码层统一把
> `UUID` / `datetime` 归一化为字符串，并新增会话/消息/审批分页回归测试。

### 7.7 运行事件回放（`GET /runs/{id}/events`）

| 字段 | 实际行为 |
|---|---|
| `id` | 形如 `<run_id>:<seq>` 的不透明字符串，不是 UUID |
| `seq` | 运行内递增，允许跳号（`message.delta` 不落库） |
| `type` | 事件分类（历史字段，取值不变） |
| `event` | 原始 SSE 事件名（M7 新增；由 `payload._event` 映射，未知/历史缺失时为 `""`） |
| `tokens` | 对象或 `null`，键始终存在 |
| `error` | 字符串摘要或 `null`，不是对象；结构化错误在 `message.done`/`error` 事件里 |
| `payload` | 已剔除内部字段 `_event`，其余与 SSE 帧 `data` 一致 |
| 过滤 | `after_seq`、`types`（逗号分隔）、`q`（标题/副标题模糊匹配）、`cursor` |
| 分页 | `after_seq` 与 `cursor` 可同时递增；`has_more=false` 即取完 |

> **回放边界（M7）**：本接口只返回已写入 `run_events` 的事件。`message.delta` 与
> `reasoning.delta` 以 `persist=False` 发送，**不落库、不可回放**；断线期间的增量文本永久缺失。
> 需要完整回复时，以 run 进入终态（`message.done`/`error`，或 `interrupted` 后恢复/取消至终态）后
> `GET /conversations/{id}/messages` 返回的消息正文为准。`run.interrupted` 期间该助手消息
> `messages.status` 仍为 `streaming`，历史接口不会返回。

### 7.8 运行续流与恢复端点

#### 7.8.1 SSE 续流（`GET /runs/{id}/stream?after_seq=`，M7）

| 项 | 实际行为 |
|---|---|
| 鉴权 | `Authorization: Bearer <access token>`；未携带/无效/过期 401 `unauthorized` |
| 所有权 | run 不存在或不属于当前用户统一 404 `not_found`（不泄露存在性） |
| `after_seq` | 默认 0；只回放 `seq > after_seq` 的已落库事件，随后跟随实时事件；取值 `0..2147483647`，非法 422 `validation_error` |
| 回放上限 | 待回放事件 >1000 条在建流前返回 400 `replay_too_large`；改用 `GET /runs/{id}/events?after_seq=` 分页回放，或传入更接近的 `after_seq` |
| 终态关闭 | 回放或实时收到 `message.done`/`error` 后立即关闭；run 已终态（`succeeded`/`failed`/`canceled`）且回放里没有终态事件时，回放完即关闭 |
| `interrupted` | run 停在审批时保持连接等待 `run.resumed` 与后续事件；空闲超过 `HITL_STREAM_TIMEOUT_SECONDS`（默认 900s）后关闭，客户端按最后 `seq` 重连 |
| 心跳 | 等待实时事件期间每 15s 发送注释帧 `: ping`，不占 `seq`，客户端忽略 |
| `Last-Event-ID` | **不支持**；重连必须显式把最后收到事件的 `seq` 作为 `after_seq` 传入（`id:` 行的值可直接使用） |
| 回放边界 | 只回放已写入 `run_events` 的事件；`message.delta`/`reasoning.delta` 不落库、**断线期间不补发**；run 进入终态（`succeeded`/`failed`/`canceled`）后用 `GET /conversations/{id}/messages` 取终稿；`interrupted` 期间该消息 `status=streaming`，历史接口不返回 |
| 部署 | 事件总线为进程内实现（单实例）；多实例部署时实时帧可能丢失，靠 `after_seq` 回放**已落库**事件补齐（增量 delta 不补） |
| 响应头 | `X-Run-ID`、`X-Request-ID`、`Cache-Control: no-cache`、`X-Accel-Buffering: no` |

```text
GET /api/v1/runs/9a8b.../stream?after_seq=42
Authorization: Bearer <token>
Accept: text/event-stream

id: 43
event: message.created
data: {"run_id":"9a8b...","seq":43,"message_id":"e5f6...","role":"user"}

: ping

id: 44
event: message.done
data: {"run_id":"9a8b...","seq":44,"message_id":"e5f6...","finish_reason":"stop"}
```

> 旧端点 `POST /chat/stream` 语义不变：它本身不设回放上限、也不在「run 已终态」时提前关闭。
>
> **重连终态判断**：EOF 且未收到 `message.done`/`error` 时，先查 `GET /runs/{id}`：
> 若已 `succeeded`/`failed`/`canceled`（回放里没有终态事件）则**停止重连**并按终态刷新；
> 若仍 `running`/`interrupted` 才按最后 `seq` 重连。`interrupted` 时消息正文虽已落库，但
> `messages.status` 仍为 `streaming`，历史接口不会返回；需等审批恢复或超时取消、run 进入终态后
> 再用 `GET /conversations/{id}/messages` 获取完整正文并覆盖本地累积文本（`message.delta` 本身不回放）。

#### 7.8.2 运行恢复端点（占位）

`POST /runs/{id}/resume` 仍保留占位，恒返回 `409 run_not_resumable`：

- M5 的审批恢复由 `POST /approvals/{id}/decision` 自动调度 `Command(resume=...)`，不需要前端点击恢复；
- `GET /runs/{id}` 的 `pending` 字段会给出待处理审批/远程任务；
- 通用 resume（ask_user 类工具）留待后续里程碑。

### 7.9 `message.done` 的实际字段

除 §3.3 已列字段，M1 的 `message.done.data` 还携带：

- `conversation_id`：本次运行所属会话，便于断线后定位；
- `model`、`first_token_ms`、`duration_ms`、`active_duration_ms`；
- `citations` 自 M4 起由 `search_knowledge` 命中填充（形状见下），`artifacts` 仍为空数组；
- `citations` 元素恰好 7 个字段：`index`（从 1 连续编号）、`document_id`、`chunk_id`、`title`、`category`、`score`、`updated_at`；**不带 content**；同时写入助手消息的 `messages.citations`，因此刷新页面后仍可见；
- `reasoning`（可为 `null`）。

前端把 `conversation_id` 当可选字段消费即可；其余未知键按「忽略」处理。

### 7.10 错误可观测性

- 所有 4xx/5xx 都返回 §1.3 的错误信封；
- 500 响应体 `request_id` 与响应头 `X-Request-ID` 一致（此前 500 会退化为 `"unknown"`）；
- 客户端可自带 `X-Request-ID`，服务端原样回显，便于前后端日志串联。

### 7.11 限流与本地联调

注册/登录按 IP 限流，默认值可通过环境变量调整：

| 配置 | 默认 | 说明 |
|---|---|---|
| `RATE_LIMIT_ENABLED` | `true` | 测试环境由 conftest 关闭 |
| `RATE_LIMIT_REGISTER_MAX` / `RATE_LIMIT_REGISTER_WINDOW_SECONDS` | 5 / 3600 | 注册 |
| `RATE_LIMIT_LOGIN_MAX` / `RATE_LIMIT_LOGIN_WINDOW_SECONDS` | 10 / 300 | 登录 |

前端自动化请复用固定账号：`scripts/seed_dev_account.py` 可幂等创建/重置开发账号
（默认 `dev@example.com` / `dev-password-123`），避免反复注册触发限流。

### 7.12 M1 热修记录（2026-11-09）

| 问题 | 状态 | 处理 |
|---|---|---|
| `has_more=true` 时列表/历史/审批 500 | 已修复 | `encode_cursor` 统一 JSON-safe；补 3 条分页回归测试 |
| 500 响应 `request_id="unknown"` | 已修复 | 中间件写 `request.state`，500 处理器回填真实 ID 与响应头 |
| `chat/stream` 的 403/409/422 未进 OpenAPI | 已补 | 声明错误信封、`X-Run-ID`/`X-Request-ID`、SSE 事件说明 |
| `error.details` 未建模 | 已补 | 新增 `ErrorBodyOut` / `ErrorEnvelopeOut` |
| `RunEventOut.tokens/error` 非必填 | 已改 | 改为 required + nullable，并补字段说明 |
| `message.done.conversation_id` 未写入契约 | 已补 | 见 §7.9 |
| `resume` 只写「返回 409」 | 已补 | 明确错误码 `run_not_resumable`，见 §7.8 |
| 注册限流固定 5 次/小时 | 已改 | 配置化 + 开发账号 seed 脚本，见 §7.11 |

## 8. M1.5 实现状态（2026-11-09）

> 本节记录 M1.5「个人能力层」相对 §2 设计稿的落地差异；机器可读契约见 [`openapi.json`](openapi.json)。

### 8.1 已实现端点

| 模块 | 端点 | 备注 |
|---|---|---|
| 附件 | `POST /attachments`、`GET /attachments`、`GET/DELETE /attachments/{id}`、`GET /attachments/{id}/download` | multipart 上传；sha256 去重（`duplicate=true`）；MIME 白名单 + 20MB 上限（413）；非所有者 404；被消息引用删除走软删 |
| 指令 | `GET/PUT /instructions` | 覆盖式；`content=null` 恢复默认；超长 422 `instruction_too_long`；修改写审计 |
| 记忆 | `GET/POST /memories`、`GET/PATCH/DELETE /memories/{id}`、`DELETE /memories?confirm=true`、`GET /memories/export` | `q` 走 ILIKE（语义检索 M4）；stats 形状 `{total,profile,preference,fact,active,archived}` |
| 技能 | `GET/POST /skills`、`POST /skills/import`、`GET/PATCH/DELETE /skills/{id}`、`PUT /skills/{id}/enabled`、`GET /skills/{id}/files`、`GET /skills/{id}/files/{path}`、`GET /skills/catalog` | 系统技能仓库同步（`app/agents/skills/`）；zip 校验（路径穿越/大小/数量/frontmatter）；系统项只读，用户覆盖写 `user_capability_overrides` |
| MCP | `GET/POST /mcp/servers`、`GET/PATCH/DELETE /mcp/servers/{id}`、`PUT /mcp/servers/{id}/enabled`、`POST /mcp/servers/{id}/check`、`POST /mcp/servers/{id}/refresh-tools`、`GET /mcp/servers/{id}/tools`、`PATCH /mcp/tools/{id}`、`GET /mcp/available` | 自研 JSON-RPC 2.0 客户端（Streamable HTTP/SSE/stdio）；SSRF 主机校验；敏感请求头 AES-GCM 加密或 `env:` secret_ref；普通用户 stdio 422 `mcp_transport_not_allowed` |
| 审批 | `GET /approvals`、`GET /approvals/{id}`、`POST /approvals/{id}/decision` | HITL 全链路：`approval.required` → 决定唤醒运行 → 继续/拒绝；重复决定 409 `approval_already_decided`、过期 409 `approval_expired`；取消运行清理 pending |
| 系统 | `GET /system/info` | `capabilities` 见 §7.4 |

### 8.2 聊天请求与 SSE 扩展

- 请求新增可选字段：`attachments`（≤5）、`skills`（≤3）、`mcp_servers`（≤5）、`mcp_tools`（≤10）；
- 选择不存在/他人的资源 404；已停用资源 422；`knowledge_categories` 自 M4 起可用（`KNOWLEDGE_ENABLED=false` 时 403）；`workflow_id` 仍禁用；
- 新事件：`skill.activated`（首个 `message.delta` 之前）、`memory.saved`、`notice`、`tool.start`/`tool.result`（`source` ∈ native/skill/mcp/a2a；MCP 带 `server`）、`approval.required`；
- `tool.result` 在工具产生了知识库引用时带 `citations` 字段（与 `message.done.citations` 同形状）；
- `approval.required.data`：`approval_id`、`tool_call_id`、`tool`、`tool_display_name`、`source`、`executor`、`target`、`side_effect`、`risk`、`reason`、`payload`、`payload_preview`、`expires_at`；M7 新增 `tool_display_name`/`executor`/`target`/`side_effect`/`payload_preview` 五个展示字段，旧字段与语义不变（`target` 截断到 2000 字符、`payload_preview` 截断到约 500 字符并以 `…` 结尾）；
- 审批超时后 `message.done.finish_reason="canceled"`（`reason="approval_timeout"`），run 状态 `canceled`；
- `message.done` 保持 M1 字段，新增 `reason`（审批超时/取消等场景出现）。

### 8.3 与设计稿的偏差（M1.5 实测）

| 项 | 设计稿 | M1.5 落地 | 原因 |
|---|---|---|---|
| 记忆存储 | LangGraph `AsyncPostgresStore` | 业务表 `memories`（ILike 检索） | 任务口径；避免 store/embedding 初始化复杂度，语义检索 M4 接 embedding 时再迁移或双写 |
| 记忆去重 | 语义检索 top3 阈值 0.92 | 内容完全一致的 active 条目合并（`action=updated`） | 语义检索依赖 embedding（M4） |
| MCP 协议库 | 官方 `mcp` SDK | 自研 JSON-RPC 2.0 over httpx（initialize → notifications/initialized → tools/list → tools/call） | 任务口径；少一个重依赖，测试可注入 transport |
| 系统 MCP 写工具 | 动态过滤 | `mcp_tools.admin_only/user_allowed` 落库字段 | 列表接口需展示置灰原因，前端友好 |
| stdio | 管理员全功能 | 支持创建/校验/客户端实现；命令白名单 `MCP_STDIO_ALLOWED_COMMANDS` | v1 无隔离保证，默认关闭 |
| 管理端 | `/api/v1/admin/…` 全套 | M1.5 未实现管理端路由；`users.role` 与数据模型已就绪，系统项可直接写库 | 任务范围仅用户侧；完整管理控制台已在 M8 实现（2026-11-11，见 §12；M5 只交付审计与平台密钥） |
| 附件删除 | 未定义 | 被消息引用 → 软删（status=deleted + 移除磁盘文件）；未引用 → 物理删除 | 历史消息快照仍可展示元数据 |

### 8.4 数据层差异

- 新增表：`attachments`、`skills`、`skill_files`、`mcp_servers`、`mcp_tools`、`user_capability_overrides`、`memories`、`tool_invocations`（迁移 `7f3a9c21b4d6`，可逆）；
- `mcp_servers.headers` 只存 secret_ref / AES-GCM 密文，绝不落明文；响应只回 `credentials_hint`；
- `tool_invocations`：`source ∈ native/mcp/a2a/knowledge/skill`、`source_id=server_id`、args/result 截断脱敏；
- `runs.capabilities` 增加 `attachments` 键（`MessageCapabilitiesOut` 不感知，忽略即可）。

### 8.5 测试与本地联调

- 集成测试见 `tests/README.md`；MCP 测试用 `httpx.MockTransport` 注入 + `MCP_ALLOWED_HOSTS` 白名单；
- **注意**：`httpx.ASGITransport` 会缓冲整个 SSE 响应，HITL 实时交互测试走服务层（`collect_run_events_live`）；真实 uvicorn（8000）下 SSE 为实时流式，前端联调不受影响。

## 9. A2A 出站与入站状态（2026-11-10）

- **出站（M2，已实现）**：`/api/v1/agents*` 注册/刷新/健康检查、`send_message` 工具、`/api/v1/tasks*` 任务查询与动作、SSE `a2a.*` 事件；协议层已迁移到 `a2a-sdk` 1.3.0，对外契约不变；
- **入站（M3，已取消）**：本服务不发布 Agent Card、不暴露 JSON-RPC 端点；`/a2a` 与 `/a2a/.well-known/agent-card.json` 返回 404；
- 需求变更记录见 [feedback/2026-11-10-requirement-change-outbound-only.md](feedback/2026-11-10-requirement-change-outbound-only.md)。

### 9.1 `authorization_url` 后端白名单（M7，2026-11-11）

A2A `auth_required` 的 `authorization_url` 来自远程 Agent，属不可信数据。M7 起后端在**落库前**做纯离线白名单校验（不联网），任务详情展示历史值时再按当前配置过滤一次：

- **协议**：仅 `https`；`dev`/`test` 额外允许 `http://localhost` 与 `http://127.0.0.1`（精确字面量）；
- **拒绝**：`javascript:`/`data:`/`file:`/`ftp:`/无 scheme 等非 https；URL 内嵌用户名或密码；回环/私网/链路本地/保留/组播/未指定 IP 字面量；浏览器等价 IP 写法（十进制 `2130706433`、十六进制 `0x7f000001`、简写 `127.1`、含八进制段）；主机先做百分号解码再判断（编码后的回环/私网 IP 仍会被拒，双重编码残留 `%` 等非法字符也拒）；非 ASCII/IDN 主机；反斜杠；单标签主机；`*.local`/`*.internal`/`*.localhost`；长度 >2000 字符；
- **违规处理**：`authorization_url` 收敛为 `null`，A2A 任务**继续运行**（不报错、不改任务状态）；后端记 warning 并写审计 `security.auth_url_rejected`（detail 只含脱敏后的 `url_prefix` 与 `reason`，不落完整 URL 或凭据）；
- **历史数据**：已落库的旧值不修改，只在任务详情返回时按当前配置重新过滤（展示层过滤不写审计）；
- **配置 `A2A_AUTH_URL_ALLOWED_HOSTS`**：逗号分隔的精确主机名（大小写不敏感），默认空 = 不额外限制；非空时主机必须命中白名单；**只收窄不放宽**——即使命中白名单，私网/回环等主机仍被拒绝；非 ASCII 主机一律拒绝（如需 IDN 请先转 punycode 再配置）；
- **已知限制**：静态离线校验无法识别「公网域名 DNS 解析到内网」的别名（如 nip.io），需网络层/出口策略兜底。

## 10. M5 审批与安全加固实现状态（2026-11-10）

### 10.1 审批链路（行为变化）

- 审批由图的独立 `approval_gate` 节点创建并 `interrupt()` 挂起；一轮多个待审工具只挂起一次；
- `POST /approvals/{id}/decision` 在「该 run 已无 pending 审批」时调度 `Command(resume=...)`，
  `resumed=true` 表示恢复任务已调度（不是同步完成）；
- 图挂起时 run → `interrupted`，内容已落库但 `messages.status` 保持 `streaming`（未终态），历史接口此时不返回该消息；
- 审批超时：approval → `expired`，run → `canceled`，发
  `message.done(finish_reason="canceled", reason="approval_timeout")`；
- 取消 interrupted run → 终态 canceled，并用专用 halt gate 清理 checkpoint，同一会话可继续发消息。

### 10.2 新事件与 SSE 生命周期

- `run.interrupted`（非终态）：`run_id`、`conversation_id`、`message_id`、`status`、`approvals`；
  收到后 **SSE 保持连接**（按 D8，空闲 `HITL_STREAM_TIMEOUT_SECONDS` 后关闭）；
- `run.resumed`（非终态）：审批决定后恢复执行；
- 断线后 `GET /runs/{id}/events?after_seq=` 分页回放已落库事件；**不跟随实时流**。
  M7 起改用 `GET /runs/{id}/stream?after_seq=` 续流：先回放已落库事件再跟随实时事件，待回放 >1000 条
  返回 400 `replay_too_large`，收到 `message.done`/`error` 后关闭，`interrupted` 保持连接等审批恢复，
  空闲每 15s 发 `: ping`，**不支持 `Last-Event-ID`**（见 §7.8）；`message.delta` 不落库，
  断线窗口内的增量不补发，run 进入终态后以消息历史终稿为准（`interrupted` 期间该消息
  `status=streaming`，历史接口不返回）；事件回放每项新增 `event` 字段；
- `POST /runs/{id}/resume` 仍为占位（409），审批恢复无需调用它。

### 10.3 安全能力

| 能力 | 行为 |
|---|---|
| 聊天限流 | 30 次/60s/用户（可配），429 `rate_limited` + `details.retry_after_seconds` |
| 注入检测 | 命中规则写审计 `security.injection_suspected`，并在本轮 system prompt 追加安全提醒；不硬拦输入 |
| 外部内容包裹 | MCP/A2A/知识库/技能文件结果包 `<untrusted source="...">`；知识库「未找到/错误」系统文案不包 |
| 输出过滤 | 高置信密钥打码为 `***`、剥离危险 HTML；命中写审计 `security.output_redacted`；已发出的 delta 不回溯 |
| 平台密钥 | `/admin/secrets` CRUD；`secret:NAME` 仅管理员可写、运行时无条件解析；接口/审计无明文 |

### 10.4 前端契约变更清单（M5）

1. 收到 `approval.required` 后，等待 `run.interrupted`（run=`interrupted`），**不会**再收到
   `message.done(finish_reason="interrupted")`；
2. 审批决定后监听 `run.resumed` 与后续 `message.delta`/`message.done`；断线用
   `GET /runs/{id}/stream?after_seq=`（M7）续流已落库事件 + 后续实时事件（断线窗口的增量 delta 不补；
   run 进入终态后用 `GET /conversations/{id}/messages` 取终稿覆盖，`interrupted` 期间历史接口
   不返回该消息）；只需历史查询时用 `GET /runs/{id}/events?after_seq=` 分页回放；
3. 审批超时：`message.done.finish_reason="canceled"`、`reason="approval_timeout"`；
4. 新端点：`GET /audit-logs`、`GET /admin/audit-logs`、`/admin/secrets` CRUD；
5. 聊天 429：`rate_limited`，`details.retry_after_seconds`；
6. 多审批：每次决定可能返回 `resumed=false` + `resume_error="本轮还有其他审批待处理"`，属正常。

> **M7 增量（2026-11-11）**：① 新增 `GET /runs/{id}/stream?after_seq=` 续流端点；② `/runs/{id}/events` 每项新增 `event`；
> ③ `approval.required` 补 5 个展示字段；④ agents/tasks 写接口仅管理员，普通用户 403 `forbidden` +
> `details.required_role="admin"`（拒绝写审计 `security.rbac_denied`）；⑤ A2A `authorization_url` 可能为 `null`。详见 §11。

## 11. M7 实现状态（2026-11-11）

> 机器可读契约以 [`openapi.json`](openapi.json) 为准（由 `scripts/export_openapi.py` 导出，应与代码一致）。
> M7 无数据库迁移，迁移 head 仍为 `2d8f6a1c3e70`；前端接入见
> [M7 前端交接](feedback/2026-11-11-m7-frontend-handoff.md)。

### 11.1 端点与字段

| 项 | 实际行为 |
|---|---|
| `GET /runs/{id}/events` 的 `event` | 新增必填 `str`：原始 SSE 事件名（由 `payload._event` 映射），未知/历史缺失时为 `""`；`payload` 仍剔除 `_event`，`type` 不变 |
| `GET /runs/{id}/stream?after_seq=` | 新 SSE 端点；Bearer 鉴权；先回放 `seq > after_seq` 再跟随实时；401/404/400/422 语义见 §7.8 |
| 回放上限 | 1000 条（建流前返回 400 `replay_too_large`） |
| 终态/中断 | `message.done`/`error` 关闭；已终态 run 回放完即关闭；`interrupted` 保持连接等恢复，空闲上限 `HITL_STREAM_TIMEOUT_SECONDS` |
| 回放边界 | 只回放已落库事件；`message.delta`/`reasoning.delta` 不落库、断线不补；run 进入终态后终稿以 `GET /conversations/{id}/messages` 为准（`interrupted` 期间历史接口不返回该消息） |
| 心跳/部署 | 15s 注释帧 `: ping`；**不支持 `Last-Event-ID`**；进程内事件总线，单实例语义 |
| `approval.required` | 新增 `tool_display_name`、`executor`、`target`、`side_effect`、`payload_preview`（旧字段不变） |

### 11.2 权限与审计

| 端点组 | 权限 | 拒绝行为 |
|---|---|---|
| `GET /agents*`、`POST /agents/preview` | 任意登录用户 | 401 `unauthorized` |
| `POST /agents`、`PATCH/DELETE /agents/{id}`、`POST /agents/{id}/refresh`、`POST /agents/{id}/health` | 管理员 | 403 `forbidden` + `details.required_role="admin"`，审计 `security.rbac_denied` |
| `GET /tasks*` | 任意登录用户 | 401 `unauthorized` |
| `POST /tasks/{id}/cancel`、`resume`、`authorize`、`reject` | 管理员 | 403 `forbidden` + `details.required_role="admin"`，审计 `security.rbac_denied` |
| `GET /runs/{id}/stream` | 登录用户且为 run 所有者 | 401；非本人/不存在统一 404 |

- RBAC 拒绝审计由 `require_admin` 依赖集中写入（独立 session；审计写失败只记 warning，不影响 403 返回）；
- `security.auth_url_rejected`：A2A 授权 URL 白名单拒绝审计（`resource_type=a2a_task`，detail 为脱敏 `url_prefix` + `reason`）。

### 11.3 配置

| 配置 | 默认 | 说明 |
|---|---|---|
| `A2A_AUTH_URL_ALLOWED_HOSTS` | 空 | 逗号分隔精确主机名；只收窄不放宽；空 = 不额外限制；不能放行私网/回环主机 |
| `HITL_STREAM_TIMEOUT_SECONDS` | `900` | `interrupted` 续流保持连接的最长空闲时间 |

> `.env.example` 与 `Settings` 均为 145 项；M7 新增 `A2A_AUTH_URL_ALLOWED_HOSTS`。

### 11.4 验证证据（2026-11-11）

- OpenAPI：`scripts/export_openapi.py` 导出后 `git diff --exit-code docs/openapi.json` 干净；
- 测试（最终）：全量 **760 passed, 1 skipped**（Task 6 修复轮时为 733，最终审查修复后 760；
  含全部 DB 集成用例；首轮曾因 PostgreSQL 未运行出现 454 passed/280 skipped，DB 恢复后已复跑全绿）。
  `ruff check .` 全绿；
- M7 专项用例：`tests/integration/test_run_stream.py`、`test_rbac.py`、`test_tasks.py`、
  `test_hitl_interrupt.py`、`tests/test_auth_url_unit.py`；
- 无数据库迁移（迁移 head `2d8f6a1c3e70`）。

## 12. M8 实现状态（2026-11-11）

> 机器可读契约以 [`openapi.json`](openapi.json) 为准（由 `scripts/export_openapi.py` 导出，
> 应与代码一致）。M8 状态：**已完成（2026-11-11，独立审查通过）；前端尚未接入**。前端接入清单见
> [M8 前端交接](feedback/2026-11-11-m8-frontend-handoff.md)，回归证据见
> [M8 后端交付报告](feedback/2026-11-11-m8-backend-report.md)。M8 **无数据库迁移**，
> 迁移 head 仍为 `2d8f6a1c3e70`；**无新增配置项/环境变量**。

### 12.1 端点总表

| 方法 | 路径 | 成功 | 响应模型 | 说明 |
|---|---|---|---|---|
| GET | `/api/v1/admin/users` | 200 | `AdminUserPage` | 分页 + `q`/`role`/`status` 过滤 + `stats` |
| GET | `/api/v1/admin/users/{user_id}` | 200 | `AdminUserOut` | 详情；不存在 404 `not_found` |
| PATCH | `/api/v1/admin/users/{user_id}` | 200 | `AdminUserOut` | 只改 `role`/`status`；422/409 见 §12.2 |
| GET | `/api/v1/admin/overview` | 200 | `AdminOverviewOut` | 只读计数、不写审计 |
| GET | `/api/v1/admin/settings/instruction` | 200 | `AdminInstructionOut` | 平台默认指令与元信息 |
| PUT | `/api/v1/admin/settings/instruction` | 200 | `AdminInstructionOut` | `null` 恢复内置默认；超长 422 |
| GET | `/api/v1/admin/skills` | 200 | `AdminSkillListOut` | 系统技能，不分页 |
| PUT | `/api/v1/admin/skills/{skill_id}/enabled` | 200 | `SkillOut` | 全局启停 |
| PUT | `/api/v1/admin/skills/{skill_id}/force-disable` | 200 | `SkillOut` | 强制停用/恢复 |
| GET | `/api/v1/admin/mcp/servers` | 200 | `McpServerListOut` | 系统 MCP，不分页 |
| POST | `/api/v1/admin/mcp/servers` | 201 | `McpServerOut` | 复用 SSRF/`secret:`/工具发现 |
| PATCH | `/api/v1/admin/mcp/servers/{server_id}` | 200 | `McpServerOut` | 白名单字段；真实变更才审计并强制工具发现 |
| DELETE | `/api/v1/admin/mcp/servers/{server_id}` | 204 | — | 清理用户覆盖，工具级联删除 |
| GET | `/api/v1/admin/agents` | 200 | `AgentPage` | 全局注册表；查询参数同 `GET /agents`，`stats` 忽略游标 |
| PATCH | `/api/v1/admin/agents/{agent_id}` | 200 | `AgentDetailOut` | 白名单字段；只读字段 422 |

- 全部要求管理员：未登录 401 `unauthorized`；非管理员 403 `forbidden` +
  `details.required_role="admin"`（拒绝写审计 `security.rbac_denied`）。OpenAPI 中 M8 的
  15 个操作统一覆盖 403 描述为「需要管理员权限（`forbidden`，
  `details.required_role="admin"`）」；公共 409/422/503 描述也补充了管理台错误码。
- **R1 配置面**：`INSTRUCTIONS_ENABLED` / `SKILLS_ENABLED` / `MCP_ENABLED=false` 时，
  上表指令/技能/MCP 的管理端读写仍返回 200/201/204；运行态接口仍 403 `capability_disabled`。
- **no-op 语义**：用户/指令/技能/MCP/Agent 的同值 PATCH 返回 200 当前视图，
  **不写审计、不触发副作用**（MCP 不触发工具发现）；详见 §12.4–§12.8。
- 所有响应带 `X-Request-ID`；错误信封与全站一致（§1）。

### 12.2 用户管理规则

- `GET /admin/users`：`q`（≤100，匹配 `email`/`display_name`，大小写不敏感，LIKE 通配符转义）、
  `role`（`user|admin`）、`status`（`active|disabled`）、`limit`（1–100，默认 20）、
  `cursor`（≤500）。排序 `created_at DESC, id DESC`。
- `stats` 是**当前筛选条件下**的全量计数（不含游标）：`{total, active, disabled, admins}`；
  `admins` 统计 `role='admin'` 的**所有状态**行（停用管理员也计入）。
- `AdminUserOut`：`id/email/display_name/role/status/last_login_at/created_at/updated_at`；
  **永不返回 `password_hash`**；详情与列表项同形，不带会话/消息计数（R6）。
- `PATCH` 只接受 `role`/`status`（`extra="forbid"`，未知字段 422 `validation_error`），
  且至少一项非空；无实际变化时 200 返回当前值、**不写审计**。
- 规则与错误码：

| 场景 | HTTP | code | 说明 |
|---|---|---|---|
| 目标用户不存在 | 404 | `not_found` | message「用户不存在」 |
| 修改自己的 `role` | 422 | `cannot_change_own_role` | message「不能修改自己的角色」 |
| 停用自己的 `status` | 422 | `cannot_disable_self` | message「不能停用自己的账号」 |
| 移除最后一个活跃管理员 | 409 | `last_admin_protected` | `details={"active_admins": 1}`；message「不能移除最后一个活跃管理员」 |
| 请求体非法/空对象/未知字段 | 422 | `validation_error` | |

- **所有** PATCH 都先串行化：`SELECT ... WHERE role='admin' AND status='active'
  ORDER BY id FOR UPDATE` 锁全部活跃管理员行（固定 `id ASC` 防死锁），再对目标行
  `WITH FOR UPDATE` refresh，锁后重算 `changed` 与 `remaining`。堵住「并发提升普通用户为
  admin + 停用」与「两个管理员并发互降」两种竞态；无实际变化时取快照后 rollback 释放锁。
- 真实变更写审计 `admin.user.update`，`detail={"fields": [...], "role": ..., "status": ...}`；
  同值 PATCH 200、不写审计、不提交。

### 12.3 只读概览

```json
{
  "generated_at": "2026-11-11T12:00:00Z",
  "users": {"total": 3, "active": 2, "disabled": 1, "admins": 1},
  "conversations": {"total": 4, "active": 2, "archived": 1, "deleted": 1},
  "messages": {"total": 5},
  "runs": {"total": 6, "queued": 1, "running": 1, "succeeded": 1,
           "failed": 1, "canceled": 1, "interrupted": 1},
  "a2a_tasks": {"total": 2, "submitted": 0, "working": 0, "input_required": 0,
                "auth_required": 0, "completed": 1, "failed": 1,
                "canceled": 0, "rejected": 0},
  "knowledge": {"documents": 2, "chunks": 3, "documents_pending": 0,
                "documents_indexing": 0, "documents_ready": 1, "documents_failed": 1},
  "attachments": {"total": 2, "storage_bytes": 300, "deleted": 1}
}
```

- `users` / `conversations` / `messages` / `runs` / `a2a_tasks` / `knowledge` 为**全表原始计数**
  （含软删、失败、终态行）；`conversations.deleted` 单列且仍计入 `total`；
  `users.admins` 统计 `role='admin'` 的所有状态行。
- `attachments.total` 只计 `status != 'deleted'` 的行（含 processing/failed），
  `storage_bytes` 为这些行的 `COALESCE(SUM(size_bytes), 0)`（deleted 行字节不计入），
  `deleted` 单独计数。
- 所有状态键**始终存在**，无数据时为 `0`；`generated_at = datetime.now(UTC)`（`Z` 结尾）。
  注意 `runs.canceled` 为单 L 拼写。
- 性能口径：精确计数，O(表大小)；一次请求固定 **8 条聚合 SQL**（+ 鉴权 1 条，
  上限测试断言 ≤10），不加载 ORM 行、无 N+1、无缓存。管理端低频只读，建议前端刷新 ≥30s；
  表规模增大后再评估统计表，不在 M8。

### 12.4 平台默认指令

`AdminInstructionOut`（GET/PUT 同形）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `content` | `str` | 平台默认正文本体（无个人覆盖用户实际注入的内容） |
| `is_default` | `bool` | 无 `system_settings["instruction.default"]` 行（或历史形态非法）时为 `true` |
| `max_chars` | `int` | `settings.instruction_max_chars`（当前 4000） |
| `user_override_count` | `int` | `user_settings.instructions IS NOT NULL` 的用户数 |
| `updated_by` | `uuid \| null` | 最后保存该配置的管理员 |
| `updated_by_email` | `str \| null` | 上述管理员邮箱；用户已删/不存在时为 `null` |
| `updated_at` | `datetime \| null` | 行更新时间；使用内置默认时为 `null` |

- `PUT` 请求体 `{"content": str | null}`：`content` **必填但可显式传 `null`**；strip 后空串等价于 `null`。
- `null` → 删除 `instruction.default` 覆盖、恢复代码内置默认；**无覆盖行时是 no-op**，
  不写审计、不改库（响应仍为 `is_default=true`、`updated_at=null`）。
- 非空且 `len(content) > max_chars` → 422 `instruction_too_long`。
- 保存写 `value={"content": ...}`、`updated_by` 并**手动刷新** `updated_at`（模型无 `onupdate`）；
  `SystemSetting` 兼容历史两种形态：`{"content": "..."}` 与裸字符串。
- 审计 `instruction.update`（`resource_type=system_setting`、`resource_id=instruction.default`）：
  保存 `detail={"scope":"platform","length":N,"fingerprint":"..."}`，恢复
  `detail={"scope":"platform","restored_default":true}`；**不落正文**。
- 不返回 `effective_preview`（R5：`content` 已同义）；`INSTRUCTIONS_ENABLED=false` 时仍可读写（R1）。

### 12.5 系统技能

- `GET /admin/skills?q=`：只返回 `scope='system'`，按 `name ASC, id ASC`（同名按 id tie-break）；
  `q`（≤100）大小写不敏感匹配名称/描述并转义 LIKE 通配符（含反斜杠）；系统技能数量有上界，
  **不分页**（R6）。
- `AdminSkillListOut = {items: SkillOut[], stats: {total, enabled, force_disabled}}`；
  `stats` 基于当前筛选结果（`total == len(items)`），`enabled` 指全局有效启用数。
- 管理端 `items[].effective_enabled = enabled and not force_disabled`（**全局口径**，不合并任何
  用户覆盖；用户覆盖只影响运行态 `/skills` 与 catalog）。
- `PUT /admin/skills/{id}/enabled`：body `{"enabled": bool}`；写全局 `skills.enabled`。
- `PUT /admin/skills/{id}/force-disable`：body `{"force_disabled": bool}`；`force_disabled=true`
  **压过任何用户级覆盖**使技能对所有人失效；恢复 `false` 后用户覆盖仍按原值生效（未被破坏）。
- **同值启停是 no-op**：直接返回当前全局视图，不写审计、不提交。
- 只允许系统技能：不存在或属于用户私有技能统一 404 `not_found`（「系统技能不存在」，不泄露存在性）。
- 审计：`skill.enable`（`detail={"enabled": ..., "scope": "system"}`）、
  `skill.force_disable`（`detail={"force_disabled": ...}`）；`SKILLS_ENABLED=false` 时仍可读写（R1）。

### 12.6 系统 MCP

- `GET /admin/mcp/servers`：只返回 `scope='system'`，按 `created_at DESC, id DESC`，**不分页**；
  `stats` 与用户侧同形 `{total, system, user, enabled, healthy}`，其中 `user=0`、
  `total==system==len(items)`、`enabled` 按全局有效态 `enabled and not force_disabled` 计。
- `POST` body 复用 `McpServerCreateRequest`：`name` 1–128（**strip 后纯空白 → 422**）、
  `slug` 2–64（小写/数字/连字符）、`description` ≤2000、`transport ∈ streamable_http|sse|stdio`、
  `url` ≤2048、`headers`、`command`（stdio 必填）、`args` ≤32。新建固定 `scope='system'`、
  `enabled=true`、`auto_inject=true`、`force_disabled=false`、`health_status='unknown'`；
  创建请求不接收 `auto_inject`，需要关闭时创建后用 PATCH 修改。
- 校验复用用户侧：stdio 仅管理员 + command 白名单、URL SSRF、`secret:`/`env:` 凭据引用；
  **slug 在任意 scope 已存在 → 409 `conflict`**（系统工具名 `mcp__<slug>__*` 必须全局唯一）。
- 创建/更新提交后立即做一次强制工具发现：可达则同步工具并标 `healthy`；**不可达仍返回
  201/200**，`health_status='unhealthy'`、`last_error` 截断（400 字符）、`tool_count=0`。
- `PATCH` 可编辑 `name`/`description`/`url`/`headers`/`auto_inject`/`enabled`/`force_disabled`
  （至少一项）；`slug`/`transport`/`command`/`args` 为创建时确定的只读字段，提交显式 422。
  **逐字段比较**：同值 PATCH 是 no-op，直接返回当前视图，**不写 `mcp.server.update`、不触发
  工具发现**；至少一个字段真实变化才写审计并强制重新发现一次。
  注意：同一条**明文**敏感头重复提交时，`normalize_headers` 会用新的随机 nonce 重新加密，
  落库形态不同会被视为真实变更（写审计 + 触发发现）；前端应保存后回填 `secret:`/`env:` 引用。
- `DELETE` → 204：先清理该服务器的 `user_capability_overrides`（`capability='mcp_server'`），
  `mcp_tools` 由外键 `ON DELETE CASCADE` 删除。
- **密钥不泄露**：`McpServerOut` 从不返回 `headers`；敏感头只落 `{"secret_ref": ...}` 或密文，
  响应只回 `credentials_hint`（如 `Authorization: secret:MCP_KEY`），审计与日志同样不含明文/密文。
- 错误码：404 `not_found`；409 `conflict`（slug 任意 scope 占用）；422 `mcp_ssrf_blocked` /
  `mcp_transport_not_allowed` / `secret_ref_not_allowed` / `secret_ref_invalid` / `validation_error`；
  **503 `secret_store_unavailable`**（敏感头以明文提交且未配置 `CAPABILITY_SECRET_KEY`）；
  `MCP_ENABLED=false` 时仍可读写（R1）。
- 系统 MCP 的 `effective_enabled = enabled and not force_disabled`，同样压过用户覆盖。

### 12.7 全局 Agent（R8 口径）

- `GET /admin/agents` 的 query 与 `GET /agents` 完全一致：`health_status`、`enabled`、`q`、
  `limit`（1–100）、`cursor`；响应 `AgentPage`（`items` 为 `AgentOut`，`stats` 同上）。
- **stats 口径**：管理端翻页时 `stats` 是当前筛选条件下的**全量计数**（忽略 cursor，不会因翻页
  变小）；用户侧 `GET /agents` 保持既有口径（`stats` 随页内过滤），两者行为不同。
- **全局注册表口径（R8）**：返回**所有未软删 Agent**，不按 `owner_user_id IS NULL` 过滤。
  v1 `remote_agents` 是全局注册表，`owner_user_id` 只是保留字段、不参与可见性；
  管理员创建的行写 `owner_user_id=管理员 id`，管理端仍必须可见。这与立项 spec §4.2 的字面
  表述不一致，属已裁定偏差；若坚持 NULL 口径需改创建逻辑并回填历史数据（超出 M8「无迁移」前提）。
- `PATCH` 可编辑：`name`（1–128，纯空白 422）、`enabled`、`requires_approval`、`auth`、`headers`；
  至少一项，`extra="forbid"`。
- 只读字段（提交即 422 `validation_error`，不静默忽略）：`slug`、`url`、`description`、
  `protocol_version`、`card`、`skills`、`capabilities`、`health_status`、`owner_user_id`；
  `description` 由 Agent Card 派生且 `refresh` 会覆盖（R7）。
- 改名会置 `name_edited=true`，后续 `POST /agents/{id}/refresh` 不再用 Card 名称覆盖。
- `auth` 支持 `secret:NAME` 平台密钥引用（**仅管理员**可写；普通用户注册/PATCH 仍只能 `env:`）；
  `headers` 敏感头只落引用/密文；响应只回 `auth_type`/`auth_scheme`/`auth_hint`，不回 `headers`。
- 只有真实变化才写 `agent.updated` 审计（`detail={"fields": [...], "source": "admin"}`）并提交；
  同值 PATCH 直接返回详情、不写审计。不存在或已软删 → 404 `not_found`。
- 清空 `auth`（`type=none|oauth2`）或提交 `headers={}` 时，`auth_hint` 同步清空，不再残留旧提示。

### 12.8 审计动作

| action | 触发 | resource_type | detail 要点 |
|---|---|---|---|
| `security.rbac_denied` | 非管理员访问 `/admin/*`、agents/tasks 写接口 | 请求资源 | `method`/`path`/`required_role`（M7） |
| `admin.user.update` | `PATCH /admin/users/{id}` 真实变更（同值 no-op 不写） | `user` | `fields`（`role`/`status`）、变更后 `role`/`status` |
| `instruction.update` | `PUT /admin/settings/instruction` 真实保存/恢复（无覆盖行恢复 no-op 不写） | `system_setting` | `scope="platform"`；保存含 `length`/`fingerprint`，恢复含 `restored_default`；**不含正文** |
| `skill.enable` | `PUT /admin/skills/{id}/enabled` 真实变更（同值 no-op） | `skill` | `enabled`、`scope="system"` |
| `skill.force_disable` | `PUT /admin/skills/{id}/force-disable` 真实变更（同值 no-op） | `skill` | `force_disabled` |
| `mcp.server.create` | `POST /admin/mcp/servers` | `mcp_server` | `slug`/`transport`/`url`/`scope="system"`（不含 headers/凭据） |
| `mcp.server.update` | `PATCH /admin/mcp/servers/{id}` 真实变更（同值 no-op，不写审计、不触发发现） | `mcp_server` | `fields`（字段名）、`scope="system"` |
| `mcp.server.delete` | `DELETE /admin/mcp/servers/{id}` | `mcp_server` | `slug`、`scope="system"` |
| `mcp.server.check` | 系统 MCP 创建/更新后的强制工具发现 | `mcp_server` | `healthy`/`tools`/`latency_ms` 或截断 `error`（`actor_type=system`，既有动作） |
| `agent.updated` | `PATCH /admin/agents/{id}` 真实变更 | `agent` | `fields`、`source="admin"`（与 M7 用户侧 PATCH 复用同一动作） |

> 审计 `detail` 只放字段名/状态值/长度/指纹/计数，**不放正文、明文密钥、密文或请求头**。

### 12.9 验证与迁移

- 全量 `pytest -q`：**825 passed, 1 skipped**（M7 基线 760 + M8 新增 65 条管理台集成用例：
  Task 1–6 功能 50 条 + 最终审查修复 15 条；1 skipped 为既有需真实 Embedding key 的知识库语义评测）；
- `.venv/bin/python -m ruff check .`：`All checks passed!`；
- `.venv/bin/python -m alembic check`：`No new upgrade operations detected.`；`alembic heads`
  仍为 `2d8f6a1c3e70 (head)`；`alembic/versions/` 无新增文件；
- **空库迁移验证（T8）**：临时空库 `agent_hub_m8_mig` 从 0 `alembic upgrade head` 成功跑完
  6 个迁移，`alembic check` 无漂移，`alembic heads` 为 `2d8f6a1c3e70`；验证后已删除临时库
  （测试库 `agent_hub_test` 由 pytest `create_all` 建，**不执行 upgrade**）；
- `scripts/export_openapi.py` 导出后 `git diff --exit-code docs/openapi.json` 干净；
  M8 的 15 个操作 403 描述统一为「需要管理员权限（`forbidden`，
  `details.required_role="admin"`）」，公共 409/422/503 与 `ErrorBodyOut.code` 描述补充管理台错误码；
- **M8 无迁移**：未新增/修改表、列或索引；未新增配置项或环境变量；迁移 head 仍为 `2d8f6a1c3e70`。
