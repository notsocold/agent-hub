# API 契约

- Base URL：`http://localhost:8000`
- 业务前缀：`/api/v1`
- 协议：HTTP + JSON；聊天流式用 SSE
- 文档：FastAPI 自动生成 `/docs`（Swagger）与 `/redoc`
- 状态：M1 / M1.5 / M2 已实现；M3 入站已按 2026-11-10 需求变更取消；M4–M6 待实现

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
| 401 | `unauthorized` | 未登录 / token 无效 |
| 403 | `forbidden` | 无权限 |
| 403 | `capability_disabled` | 能力总开关关闭 |
| 404 | `not_found` | 资源不存在 |
| 409 | `conflict` | 状态冲突（重复审批、并发 run） |
| 413 | `payload_too_large` | 技能包/上传文件超限 |
| 422 | `validation_error` | 请求体校验失败（FastAPI 默认结构会包装为本信封） |
| 422 | `skill_invalid` | 技能包校验失败 |
| 422 | `instruction_too_long` | 指令超过字符上限 |
| 422 | `mcp_transport_not_allowed` | 用户自建 MCP 使用了被禁用的传输 |
| 429 | `rate_limited` | 触发限流 |
| 500 | `internal_error` | 未预期异常 |
| 502 | `a2a_unavailable` | 远程智能体不可用 |
| 502 | `mcp_unreachable` | 显式选择的 MCP 服务器不可达 |
| 504 | `a2a_timeout` | 远程调用超时 |

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
| GET | `/api/v1/runs/{run_id}/events` | 事件回放（断线恢复，规划） |
| POST | `/api/v1/runs/{run_id}/cancel` | 取消运行 |
| POST | `/api/v1/runs/{run_id}/resume` | 恢复 `input-required` 的运行 |

### 2.4 远程智能体（M2 已实现）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/agents/preview` | 拉取并校验 Agent Card，不落库（`{valid, card, normalized, warnings}`） |
| POST | `/api/v1/agents` | 注册远程 Agent（拉取 Agent Card；同 url+slug 幂等返回已有） |
| GET | `/api/v1/agents` | 已注册 Agent 列表：`health_status`、`enabled`、`q`、`limit`、`cursor` + `stats` |
| GET | `/api/v1/agents/{id}` | Agent 详情（含原始 Card、skills、auth_hint、last_*、recent_tasks） |
| PATCH | `/api/v1/agents/{id}` | 修改 `{enabled?, name?, auth?{type,secret_ref}, headers?}`（headers 敏感头只存引用/密文） |
| DELETE | `/api/v1/agents/{id}` | 软删除（有运行中任务 409 `agent_has_active_tasks`） |
| POST | `/api/v1/agents/{id}/refresh` | 重新拉取 Agent Card，返回 `{agent, changed, changed_fields}`；失败保留旧 Card 标 unhealthy |
| POST | `/api/v1/agents/{id}/health` | 主动健康检查（拉 Card + tasks/get 探测），返回 `{health_status, latency_ms, checked_at, error}` |

字段与错误码：

- 列表项：`id/slug/name/description/url/protocol_version/skills[{id,name,description}]/skill_count/auth_type(none|api_key|oauth2)/auth_scheme(bearer|header|oauth2|none)/auth_hint/capabilities{streaming,push_notifications}/health_status(unknown|healthy|degraded|unhealthy)/enabled/last_checked_at/last_latency_ms/last_error/recent_task{id,a2a_task_id,state,duration_ms,finished_at}|null/created_at/updated_at`；
- `stats`：`{total, healthy, degraded, unhealthy, disabled, enabled}`；
- 注册/预览失败：422 `agent_card_invalid`（Card 非法/不可达/超时/跨源重定向）或 `agent_url_not_allowed`（协议/内网/凭据 URL）；
- 健康阈值：成功且延迟 <2s → healthy，≥2s → degraded；连续失败 ≥3 → unhealthy；`A2A_ALLOW_LOOPBACK` 仅 dev/test 默认放行回环（本地假 Agent 联调），prod 默认拒绝。

### 2.5 任务（M2 已实现）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/tasks` | A2A 任务列表：`state`（可重复/逗号）、`agent_id`、`run_id`、`conversation_id`、`q`、`from`、`to`、`limit`、`cursor` + `stats` |
| GET | `/api/v1/tasks/{id}` | 任务详情（request/result/error/input_request/auth_request/timeline） |
| POST | `/api/v1/tasks/{id}/cancel` | 取消（submitted/working/input_required/auth_required；尽力远程 tasks/cancel） |
| POST | `/api/v1/tasks/{id}/resume` | 补充输入（仅 input_required；`{input}` → `{task, run:{id,status}|null}`，同一 a2a_task_id 继续） |
| POST | `/api/v1/tasks/{id}/authorize` | 授权重试（仅 auth_required；`{scopes?, note?}`） |
| POST | `/api/v1/tasks/{id}/reject` | 拒绝（auth_required 或 input_required；`{reason?}`，state=rejected） |

字段与错误码：

- 列表项：`id/a2a_task_id/agent{id,slug,name}/run_id/conversation{id,title}|null/state/progress/duration_ms/request_summary/error_summary/available_actions/created_at/updated_at`；
- `stats`：`{total, submitted, working, input_required, auth_required, completed, failed, canceled, rejected}`；
- 详情补：`tool_call_id/request{tool,args,summary}/result{artifact{type,summary,parts,data},summary}/error{code,message,retryable,at}/input_request{question,input_schema,expires_at}/auth_request{scopes,authorization_url,expires_at}/timeline[{state,at,duration_ms,note}]/started_at/finished_at`；
- `available_actions` 只含后端可执行动作（cancel/resume/authorize/reject），由 state 推导；
- 动作错误 409：`task_not_cancelable` / `task_not_resumable` / `task_not_authorizable` / `task_not_rejectable`；
- 等待态 `expires_at` 到期懒过期置 canceled（timeline 追加「等待超时」，审计 `task.expired`）；`A2A_INPUT_TIMEOUT_SECONDS` 默认 30 分钟。

### 2.6 知识库

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/knowledge/documents` | 上传/创建文档（异步入库） |
| GET | `/api/v1/knowledge/documents` | 文档列表 |
| GET | `/api/v1/knowledge/documents/{id}` | 文档详情 |
| DELETE | `/api/v1/knowledge/documents/{id}` | 删除文档与向量 |
| POST | `/api/v1/knowledge/search` | 检索调试（返回片段与分数） |
| POST | `/api/v1/knowledge/rebuild` | 全量重建索引（管理端） |

### 2.7 审批

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/approvals` | 审批列表（默认 pending） |
| GET | `/api/v1/approvals/{id}` | 审批详情 |
| POST | `/api/v1/approvals/{id}/decision` | 批准/拒绝并恢复运行 |

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

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/PUT | `/api/v1/admin/settings/instruction` | 平台默认指令 |
| GET/POST/PATCH/DELETE | `/api/v1/admin/mcp/servers[...]` | 系统 MCP（允许 stdio） |
| PUT | `/api/v1/admin/mcp/servers/{id}/enabled` | 系统 MCP 启停 |
| PUT | `/api/v1/admin/mcp/servers/{id}/force-disable` | 强制停用（压过用户覆盖） |
| GET | `/api/v1/admin/skills` | 系统技能列表 |
| PUT | `/api/v1/admin/skills/{id}/enabled` | 系统技能启停 |
| PUT | `/api/v1/admin/skills/{id}/force-disable` | 强制停用（压过用户覆盖） |

> 管理端接口要求 `users.role='admin'`，越权返回 403。管理员由运维脚本 `scripts/grant_admin.py` 设置，注册流程不自动提权。

### 2.13 系统

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 存活探针（不查库） |
| GET | `/readyz` | 就绪探针（检查 DB/迁移版本，规划） |
| GET | `/api/v1/system/info` | 版本、模型配置、A2A 入站状态、能力开关 |

`system/info.a2a` 字段（2026-11-10 起）：

- 本服务只做 A2A 出站 Client，不提供入站 Server；
- `enabled` 恒 `false`，`card_url` / `jsonrpc_url` 恒为空字符串，`auth_type` / `card_security` 恒 `none`；
- 五个字段保留只为兼容前端契约，不表示可被调用的 A2A 地址。

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
  "mcp_tools": ["<tool_id>"]
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
| `approval.required` | 需要人工确认 | `approval_id`、`tool`、`reason`、`payload` |
| `skill.activated` | 本轮技能激活 | `skill_id`、`slug`、`name`、`scope`、`estimated_tokens` |
| `memory.saved` | 记忆保存成功 | `memory_id`、`kind`、`action`、`preview` |
| `notice` | 能力降级/提示 | `level`、`code`、`message` |
| `message.done` | 本轮结束 | `run_id`、`message_id`、`usage`、`finish_reason` |
| `error` | 流内错误 | `code`、`message`、`request_id` |
| `ping` | 心跳（也可用注释） | — |

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
    {"chunk_id": "...", "document_id": "...", "title": "退货政策", "content": "...", "score": 0.83}
  ],
  "elapsed_ms": 42
}
```

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
| `POST /chat/stream` | 20 次/分钟/用户，同时 1 个运行 | 超出返回 429 |
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
- `reasoning.delta/done`、`tool.*`、`a2a.*`、`skill.activated`、`approval.required`、`memory.saved`、`notice` 已在 M1.5/M2 接入（详见 §8.2）；`workflow.step` 仍待 M5+。

### 7.4 能力开关

`GET /system/info.capabilities` 在 M1.5 返回：

```json
{
  "instructions": true, "memories": true, "skills": true, "mcp": true,
  "knowledge": false, "attachments": true, "workflows": false,
  "approvals": true, "agents": false, "tasks": false
}
```

- 请求携带未启用的能力字段时返回 403 `capability_disabled`；
- 携带 `workflow_id` 返回 409 `workflow_disabled`；
- `knowledge_categories` 仍为 403（M4 再开）；`agents`/`tasks` 在 M1 时点为 false，**M2 起均为 true**（上方 JSON 为 M1 时点快照）；
- `reasoning_efforts` 默认空数组（模型不支持或 `LLM_REASONING_EFFORT_SUPPORTED=false` 时），`default_reasoning_effort` 仍返回 `low`；打开该开关后返回 `low/medium/high` 三项。

### 7.5 数据层差异

- 新增 `refresh_tokens` 表（用于 refresh token 轮换/吊销）；
- `messages.status` 增加内部值 `streaming`，API 只返回 `completed/failed/canceled`；
- `messages` 增加 `updated_at`；`runs` 按交接包补齐用量、首 token、时长、工作流占位列；
- 同一会话未结束 run 由部分唯一索引 `uq_runs_active_conversation` 保证唯一；
- `messages.run_id` 与 `runs.user_message_id/assistant_message_id` 互相引用，迁移中用 `ALTER TABLE` 补外键。

### 7.6 分页契约（M1 实测）

四个分页端点共用同一套不透明游标：`GET /conversations`、`GET /conversations/{id}/messages`、
`GET /approvals`、`GET /runs/{id}/events`。

- `next_cursor` 是 urlsafe base64 编码的排序键，前端不得解析、拼接或当作 UUID；
- `has_more=false` 时 `next_cursor=null`，不要继续请求；
- 非法游标返回 422 `validation_error`，不会 500；
- `stats` 是「当前筛选条件下的全量计数」，不是当前页计数；
- 会话排序 `last_message_at DESC NULLS LAST, id DESC`；消息与审批 `created_at DESC, id DESC`；
  运行事件 `seq ASC`；
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
| `tokens` | 对象或 `null`，键始终存在 |
| `error` | 字符串摘要或 `null`，不是对象；结构化错误在 `message.done`/`error` 事件里 |
| `payload` | 已剔除内部字段 `_event` |
| 过滤 | `after_seq`、`types`（逗号分隔）、`q`（标题/副标题模糊匹配）、`cursor` |
| 分页 | `after_seq` 与 `cursor` 可同时递增；`has_more=false` 即取完 |

### 7.8 运行恢复端点

`POST /runs/{id}/resume` 在 M1 始终拒绝，错误码统一为 `409 run_not_resumable`：

- 运行不是 `interrupted` → 409 `run_not_resumable`；
- 运行是 `interrupted` 但 M1 尚未接入 interrupt 工具 → 409 `run_not_resumable`；
- 前端按「恢复能力未上线」展示，不要当致命错误反复重试。

### 7.9 `message.done` 的实际字段

除 §3.3 已列字段，M1 的 `message.done.data` 还携带：

- `conversation_id`：本次运行所属会话，便于断线后定位；
- `model`、`first_token_ms`、`duration_ms`、`active_duration_ms`；
- `citations`、`artifacts`（M1 均为空数组）；
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
- 选择不存在/他人的资源 404；已停用资源 422；`knowledge_categories`/`workflow_id` 仍禁用；
- 新事件：`skill.activated`（首个 `message.delta` 之前）、`memory.saved`、`notice`、`tool.start`/`tool.result`（`source` ∈ native/skill/mcp，MCP 带 `server`）、`approval.required`；
- `approval.required.data`：`approval_id`、`tool_call_id`、`tool`、`source`、`risk`、`reason`、`payload`、`expires_at`；
- 审批超时后 `message.done.finish_reason=interrupted`（`reason=approval_timeout`），run 状态 `interrupted`；
- `message.done` 保持 M1 字段，新增 `reason`（仅 interrupted 时出现）。

### 8.3 与设计稿的偏差（M1.5 实测）

| 项 | 设计稿 | M1.5 落地 | 原因 |
|---|---|---|---|
| 记忆存储 | LangGraph `AsyncPostgresStore` | 业务表 `memories`（ILike 检索） | 任务口径；避免 store/embedding 初始化复杂度，语义检索 M4 接 embedding 时再迁移或双写 |
| 记忆去重 | 语义检索 top3 阈值 0.92 | 内容完全一致的 active 条目合并（`action=updated`） | 语义检索依赖 embedding（M4） |
| MCP 协议库 | 官方 `mcp` SDK | 自研 JSON-RPC 2.0 over httpx（initialize → notifications/initialized → tools/list → tools/call） | 任务口径；少一个重依赖，测试可注入 transport |
| 系统 MCP 写工具 | 动态过滤 | `mcp_tools.admin_only/user_allowed` 落库字段 | 列表接口需展示置灰原因，前端友好 |
| stdio | 管理员全功能 | 支持创建/校验/客户端实现；命令白名单 `MCP_STDIO_ALLOWED_COMMANDS` | v1 无隔离保证，默认关闭 |
| 管理端 | `/api/v1/admin/…` 全套 | M1.5 未实现管理端路由；`users.role` 与数据模型已就绪，系统项可直接写库 | 任务范围仅用户侧；M2 会话补管理端 |
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

- **出站（M2，已实现）**：`/api/v1/agents*` 注册/刷新/健康检查、`send_message` 工具、`/api/v1/tasks*` 任务查询与动作、SSE `a2a.*` 事件；
- **入站（M3，已取消）**：本服务不发布 Agent Card、不暴露 JSON-RPC 端点；`/a2a` 与 `/a2a/.well-known/agent-card.json` 返回 404；
- 需求变更记录见 [feedback/2026-11-10-requirement-change-outbound-only.md](feedback/2026-11-10-requirement-change-outbound-only.md)。
