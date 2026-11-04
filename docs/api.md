# API 契约

- Base URL：`http://localhost:8000`
- 业务前缀：`/api/v1`
- 协议：HTTP + JSON；聊天流式用 SSE
- 文档：FastAPI 自动生成 `/docs`（Swagger）与 `/redoc`
- 状态：设计稿，M1 起逐步实现

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
| 404 | `not_found` | 资源不存在 |
| 409 | `conflict` | 状态冲突（重复审批、并发 run） |
| 422 | `validation_error` | 请求体校验失败（FastAPI 默认结构会包装为本信封） |
| 429 | `rate_limited` | 触发限流 |
| 500 | `internal_error` | 未预期异常 |
| 502 | `a2a_unavailable` | 远程智能体不可用 |
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

### 2.4 远程智能体

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/agents` | 已注册 Agent 列表 |
| POST | `/api/v1/agents` | 注册远程 Agent（拉取 Agent Card） |
| GET | `/api/v1/agents/{id}` | Agent 详情（含原始 Card） |
| PATCH | `/api/v1/agents/{id}` | 启用/禁用、更新鉴权配置 |
| DELETE | `/api/v1/agents/{id}` | 删除注册 |
| POST | `/api/v1/agents/{id}/refresh` | 重新拉取 Agent Card |
| POST | `/api/v1/agents/{id}/health` | 主动健康检查 |

### 2.5 任务

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/tasks` | A2A 任务列表（按 run/agent/状态过滤） |
| GET | `/api/v1/tasks/{id}` | 任务详情与结果 |
| POST | `/api/v1/tasks/{id}/cancel` | 取消任务 |

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

### 2.8 系统

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 存活探针（不查库） |
| GET | `/readyz` | 就绪探针（检查 DB/迁移版本，规划） |
| GET | `/api/v1/system/info` | 版本、模型配置、A2A 地址、能力开关 |

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
  "attachments": []
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `conversation_id` | 否 | 为空时创建新会话，首个事件返回 `conversation.created` |
| `message` | 是 | 用户输入，1–8000 字符 |
| `client_message_id` | 否 | 前端幂等键，重试时避免重复消息 |
| `attachments` | 否 | 附件 ID 列表（文件上传接口后续提供） |

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
| `tool.start` | 工具开始执行 | `tool_call_id`、`tool`、`args` |
| `tool.result` | 工具执行结束 | `tool_call_id`、`status`、`summary` |
| `a2a.task` | 远程任务状态变化 | `task_id`、`agent`、`state`、`progress` |
| `a2a.artifact` | 远程产出片段 | `task_id`、`artifact` |
| `a2a.input_required` | 远程要求补充信息 | `task_id`、`question` |
| `a2a.auth_required` | 远程要求授权 | `task_id`、`agent` |
| `approval.required` | 需要人工确认 | `approval_id`、`tool`、`reason`、`payload` |
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

## 5. 限流与配额

| 接口 | 默认限制 | 说明 |
|---|---|---|
| `POST /auth/login` | 10 次/5 分钟/IP | 防爆破 |
| `POST /auth/register` | 5 次/小时/IP | |
| `POST /chat/stream` | 20 次/分钟/用户，同时 1 个运行 | 超出返回 429 |
| `POST /agents` | 30 次/小时/用户 | 管理操作 |
| A2A 入站 | 按 `client_id` 限流（默认 60 次/分钟） | 与用户侧独立 |

## 6. 兼容与版本

- 路径版本 `/api/v1`；破坏性变更升 `/api/v2`，旧版本保留至少一个迭代；
- 响应只增字段不删字段；新增枚举值前端需容忍未知值；
- SSE 事件新增类型时前端忽略未知事件，不报错；
- OpenAPI 作为契约源，前端类型可由 `/openapi.json` 生成（规划）。
