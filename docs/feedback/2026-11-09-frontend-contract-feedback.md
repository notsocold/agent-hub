# 前端交接契约反馈（M1 后端落地版）

- 日期：2026-11-09
- 对照基线：`agent-hub-web/docs/handoff/` v1.0（2026-11-08）、`agent-hub-web/src/types/*.ts`
- 后端状态：M1 对话最小闭环已实现并通过契约测试；M1.5 / M2 / M3 / M4 / M5 未实现
- 契约源：以本文第 3 节的差异和 `docs/openapi.json` 为准；交接包作为需求证据保留
- 结论：**M1 主链路可以联调**；有 12 处需要前端同步调整，8 个产品决策建议按本文推荐项执行，其余能力按降级矩阵隐藏/置灰

---

## 1. 一句话结论

后端的 M1 不是「照抄原型」，而是按「可上线的对话闭环」裁剪：

1. 认证、会话、消息、聊天 SSE、运行/事件回放、审批最小接口、系统信息已经可用；
2. 原型里最显眼的智能体、任务、知识库、Skill、MCP、长期记忆、Instruction、工作流、管理端在 M1 **明确返回能力关闭或 404**，不返回假数据；
3. 需要前端优先配合的只有 12 个点（第 3 节），其中 4 个会直接导致联调失败：错误 `details`、幂等重放、`reasoning_efforts` 为空、能力开关缺失。

---

## 2. 已实现 / 未实现

### 2.1 已实现（M1）

| 模块 | 端点 | 说明 |
|---|---|---|
| 认证 | `POST /api/v1/auth/register`、`/login`、`/refresh`、`/logout`、`GET/PATCH /auth/me`、`POST /auth/change-password` | access 15 分钟；refresh 7 天；refresh token 落库并旋转，旧 token 立即失效；密码 argon2 |
| 会话 | `GET/POST /conversations`、`GET/PATCH/DELETE /conversations/{id}`、`GET /conversations/{id}/messages` | 标题搜索、状态筛选、cursor 分页、`message_count`、`last_message_preview`、`pending_approval_count`、`stats` |
| 聊天 | `POST /chat/stream` | SSE：`conversation.created`、`run.started`、`message.created`、`message.started`、`message.delta`、`message.done`、`error`；15 秒心跳；`X-Run-ID`；同会话并发 409 |
| 运行 | `GET /runs/{id}`、`GET /runs/{id}/events`、`POST /runs/{id}/cancel`、`POST /runs/{id}/resume` | 运行详情、事件回放（`after_seq`/`types`/`q`）、取消、恢复端点（M1 只会返回 409） |
| 审批 | `GET /approvals`、`GET /approvals/{id}`、`POST /approvals/{id}/decision` | 数据模型与接口已就绪；M1 没有工具，因此不会产生 `approval.required` 事件 |
| 系统 | `/healthz`、`/readyz`、`GET /api/v1/system/info` | 模型目录、推理等级（可为空）、能力开关、限额、A2A 地址 |

### 2.2 未实现（按里程碑延后）

| 能力 | 计划 | M1 行为 |
|---|---|---|
| Skill / 记忆 / Instruction | M1.5-A | `capabilities.*=false`；请求携带相关字段返回 403 `capability_disabled` |
| 附件上传/下载 | M1.5-B | `capabilities.attachments=false`；请求携带附件返回 403 |
| MCP | M1.5-B | `capabilities.mcp=false`；请求携带 MCP 返回 403 |
| A2A 出站（智能体） | M2 | `capabilities.agents=false`；`/agents*` 未注册（404） |
| A2A 任务 | M2/M3 | `capabilities.tasks=false`；`/tasks*` 未注册（404） |
| A2A 入站 Server | M3 | `/a2a*` 未注册；`/system/info.a2a.enabled=false` |
| 知识库 | M4 | `capabilities.knowledge=false`；`/knowledge*` 未注册（404） |
| 工作流 | M5+（待决策 D1） | `capabilities.workflows=false`；`workflow_id` 返回 409 `workflow_disabled` |
| 管理控制台/RBAC | M5+（待决策 D2） | 只有 `users.role`；`/admin*` 未注册（404） |

---

## 3. 前端必须同步修改的 12 个契约点

> 优先级 P0：不改会联调失败；P1：不改会出现错误提示/状态不对；P2：体验或一致性问题。

### 3.1 P0：错误信封新增 `error.details`

后端在保留三个必填字段的前提下，允许 `error.details`：

```json
{
  "error": {
    "code": "run_in_progress",
    "message": "该会话已有运行中的请求……",
    "request_id": "…",
    "details": {"run_id": "…"}
  }
}
```

- `src/types/api.ts` 的 `ApiErrorBody.error` 增加 `details?: Record<string, unknown>`；
- `409 run_in_progress` 必须读取 `details.run_id`，恢复该 run 的展示/停止按钮，而不是只弹 toast；
- `409 run_not_cancelable` 的 `details.status` 可用于刷新运行状态；
- 这是**向后兼容的扩展**：老前端忽略 `details` 不会崩，但会丢失 run 恢复能力。

### 3.2 P0：幂等重试返回「同一 run 的事件回放」，不是 409

交接包允许「回放或 409」二选一。后端选择回放：

- 同一 `conversation_id + client_message_id` 再次请求，返回 200 SSE，`X-Run-ID` 与首次相同，事件 `seq` 从 1 开始；
- 前端重试时**不要重新生成 `client_message_id`**，否则会创建新 run；
- 若首次 run 仍在运行，重放会从已落库事件开始、再跟随实时事件；前端需要按 `seq` 去重（事件可能同时来自回放与实时队列）；
- 交接包中「重复请求返回 409 conflict + run_id」的建议**不采用**。

### 3.3 P0：`reasoning_efforts` 可以为空

- 当前默认模型 `deepseek-chat` 不支持 `reasoning_effort`，`/system/info.reasoning_efforts=[]`；
- 前端必须按「空数组 → 隐藏推理等级菜单」处理，不能渲染一个只有默认项的选择器；
- 后端仍接受 `reasoning_effort` 字段（`low/medium/high`）并落库，但不会影响模型行为；只有配置 `LLM_REASONING_EFFORT_SUPPORTED=true` 时才返回三个选项；
- `run.started.reasoning_effort` 始终有值（默认 `low`），满足 TS 的必填约束。

### 3.4 P0：系统信息新增 `capabilities.agents` / `capabilities.tasks`

交接包的 `system.ts` 能力对象没有智能体/任务开关，但原型有这两个一级导航。后端补充：

```json
"capabilities": {
  "instructions": false,
  "memories": false,
  "skills": false,
  "mcp": false,
  "knowledge": false,
  "attachments": false,
  "workflows": false,
  "approvals": true,
  "agents": false,
  "tasks": false
}
```

- 前端 `SystemCapabilities` 增加两个可选布尔字段，默认 `false`；
- 导航/页面入口按 `agents`、`tasks` 置灰；M1 不要请求 `/agents`、`/tasks`。

### 3.5 P1：`GET /runs/{id}/events` 的 `id` 不是 UUID

- 返回 `id = "<run_id>:<seq>"`（例如 `9a8b...:12`），满足交接包「事件 ID 为不透明字符串」的约定；
- `tokens` 可能为 `null`（没有 token 的事件）；
- `error` 为字符串摘要或 `null`，不是对象；结构化的错误在 `message.done` / `error` 事件里；
- `payload` 已去掉内部字段 `_event`，前端不要依赖它。

### 3.6 P1：消息历史固定「新 → 旧」

- `GET /conversations/{id}/messages` 返回 `created_at DESC, id DESC`；
- `before` 与 `cursor` 二选一；同时传时以 `cursor` 为准；
- 历史消息的 `status` 只可能是 `completed` / `failed` / `canceled`，不会返回内部值 `streaming`；
- 同一轮 user/assistant 消息的 `created_at` 显式错开 1 微秒，前端不要假设时间完全相同。

### 3.7 P1：能力关闭时的错误码

M1 对「能点但后端没有」的能力采用显式拒绝，而不是默默忽略：

| 请求 | HTTP | code |
|---|---|---|
| 携带 `skills` / `mcp_servers` / `mcp_tools` | 403 | `capability_disabled` |
| 携带 `knowledge_categories` | 403 | `capability_disabled` |
| 携带 `attachments` | 403 | `capability_disabled` |
| 携带 `workflow_id` | 409 | `workflow_disabled` |
| 携带非白名单 `model` | 422 | `model_not_allowed` |
| 同一会话已有运行 | 409 | `run_in_progress` |

前端应在发送前按 `/system/info.capabilities` 和 `limits` 提前禁用；后端的 403/409 是兜底。

### 3.8 P1：附件下载端点需要新增

交接包只定义了 `GET /attachments/{id}` 详情，但消息产物 `download_url` 指向「下载仍走 GET /attachments/{id}」，语义不清。后端计划提供：

- `GET /api/v1/attachments/{id}/download`（受控下载，校验所有者，M1.5）；
- `MessageArtifact.download_url` 由后端生成该路径；
- 在前端 `MessageArtifact` 类型不变的前提下，`download_url` 从 `null` 变成可点链接。

> 这是**交接包缺口**，不是前端实现问题；M1.5 后端落地前，前端把产物卡下载按钮置灰。

### 3.9 P1：审批卡在 M1 不会出现

- `GET /approvals` 可用，但 M1 没有会触发 `interrupt()` 的工具，因此 **不会发送 `approval.required`**；
- 前端审批卡/审批列表可以按契约实现，但联调时只能用 `fixtures.md` 的样例，不能等真实事件；
- `POST /approvals/{id}/decision` 在 M1 对「非 interrupted 的 run」返回 `resumed=false` + `resume_error`，前端不要把它当失败。

### 3.10 P1：同一会话的 `interrupted` 运行会占锁

- 数据库有「同一会话只能有一个未结束 run」的唯一索引（含 `interrupted`）；
- 用户不处理审批时，该会话无法发送新消息，会一直收到 `409 run_in_progress`；
- 前端需要提供「处理审批 / 放弃本轮」入口；后端在 M2 接入 interrupt 后，建议增加「新消息自动放弃旧 interrupted run」策略（需要产品确认，见 D8）。

### 3.11 P2：字段命名以 TS 类型为准

- 检索范围字段是 `knowledge_categories`（不是 `knowledge_base_ids`）；`agent-hub-web/docs/prototypes/review-2026-11-08.md` 里的 `knowledge_base_ids` 是旧写法，请以 `src/types/chat.ts` 为准；
- 知识库 chip 的文案应是「分类名」（如「客服」），不是知识库名（如 `support-kb`）；
- `agent-hub-web/docs/pages.md` 中「模型参数只读」需要按 `reasoning_efforts` 为空时的降级规则修正。

### 3.12 P2：时间与 `stats`

- 所有时间统一 UTC，序列化以 `Z` 结尾；前端不要再用本地时区拼接口参数；
- 列表 `stats` 是「当前筛选条件下的全量计数」，不是当前页计数；
- `next_cursor` 是不透明字符串，前端不得解析；`has_more=false` 时不要继续请求。

---

## 4. 需要拍板的产品/架构决策（D1–D8）

> 下表是交接包第 5 节的决策，后端给出推荐与 M1 现状。未拍板前按「推荐」实现，已实现部分如需变更会返工。

| ID | 决策 | 后端推荐 | M1 现状 | 对前端的影响 |
|---|---|---|---|---|
| D1 | 工作流定位 | **A. 轻量步骤模板，M5+ 落地** | 未实现，`capabilities.workflows=false` | 工作流导航置灰；不要做「能点但无接口」的编辑器 |
| D2 | v8 管理端 | **B. M5.0 最小集**（概览/审计/用户只读/系统设置） | 只有 `role` 字段 | 管理端页面保持占位；M1 不要接 `/admin*` |
| D3 | 模型/推理等级 | **A. 用户可选 + 服务端白名单** | 白名单已实现；推理等级能力开关默认关闭 | 菜单按 `models` / `reasoning_efforts` 渲染 |
| D4 | 知识库选择字段 | **A. 复用 `knowledge_categories`** | M4 实现 | chip 文案改为分类名 |
| D5 | 附件存储 | **本地磁盘先行，对象存储 M6** | M1.5 实现 | 下载走受控端点，不暴露 `storage_key` |
| D6 | 任务后续动作 | **A. 任务级 `resume/authorize/reject`，运行级保留兼容** | M2 实现 | 任务页按钮按 `available_actions` 渲染 |
| D7 | 知识库权限 | **B. `owner_user_id`，null=系统，系统+个人合并检索** | M4 实现 | 普通用户保留上传入口，按 owner 控制写权限 |
| D8 | HITL 期间 SSE | **A. 保持连接 + 15 分钟超时，同时提供事件回放** | 事件回放已实现；保持连接待 M2 interrupt 接入 | 超时后显示 `interrupted`，可查事件/恢复 |

### 4.1 新增决策（交接包未覆盖）

| ID | 问题 | 后端建议 | 理由 |
|---|---|---|---|
| D9 | refresh token 存储 | **新增 `refresh_tokens` 表**（已落地） | JWT 无状态无法吊销；轮换/登出/改密必须落库 |
| D10 | 消息流式状态 | **DB 内部 `streaming`，API 不暴露**（已落地） | 先占位 assistant message，崩溃时可巡检；API 只返回 completed/failed/canceled |
| D11 | 会话标题 | **首条消息截断生成，不额外调用模型**（已落地） | 省一次模型调用；需要更好标题时可异步生成（M1.5） |
| D12 | 同会话并发 | **数据库部分唯一索引 + 409 `run_in_progress`**（已落地） | 比应用层锁可靠，多实例也成立 |
| D13 | 智能体注册表归属 | **全局注册表，写操作管理员/显式权限**（M2 定） | 现设计无 `owner_user_id`；若产品要个人 Agent 需加列，避免默认按 owner 过滤 |
| D14 | `auth_required` 用户级授权 | **v1 不做，返回 403 `auth_flow_unavailable`** | 后端 A2A 设计只到「通知管理员更新凭据」；不伪造授权成功 |

---

## 5. 后端实现备注（前端可据此写 mock/降级）

### 5.1 SSE 行为

| 场景 | 行为 |
|---|---|
| 首事件 | 新会话首事件 `conversation.created`，随后 `run.started`；`seq` 从 1 开始 |
| `X-Run-ID` | 建连时即返回；与 `run.started.run_id` 一致 |
| 心跳 | 每 15 秒 `: ping` 注释帧，不占 `seq` |
| 断开 | 不取消运行；重连用 `GET /runs/{id}` + `GET /runs/{id}/events?after_seq=`；生成中的助手消息不进入历史，前端先显示「生成中」，完成后重取消息 |
| 部分文本恢复 | M1 **不保证**：`message.delta` 不落库，重连后只能看到已落库的终态消息；Last-Event-ID/续流在 M6 评估 |
| 取消 | `POST /runs/{id}/cancel` 立即返回；流在 `message.done.finish_reason="canceled"` 后关闭；已生成内容保留 |
| 重复请求 | 同 `client_message_id` 回放同一 run 的事件；不再创建消息 |
| 错误 | 致命错误发送 `error` 后关闭流；不会在 `error` 之后再发 `message.done` |
| 事件持久化 | `message.delta`/`reasoning.delta` 不落 `run_events`（避免写放大）；`run_events` 的 `seq` 允许跳号 |
| 多实例 | 单实例事件总线；多实例需要 Redis Stream（M6），当前不要水平扩展 |

### 5.2 关联 ID 与状态

- `conversation_id` 同时是 LangGraph `thread_id`；
- `run_id` 由后端生成，前端所有取消/查询都用它；
- `messages.id` 在 `message.started` 前已生成，前端可先用它做流式占位；
- `run.status`：`queued/running/succeeded/failed/canceled/interrupted`；M1 实际只会出现 `running/succeeded/failed/canceled`；
- `interrupted` 在 M2 接入审批后才会出现。

### 5.3 认证

- 注册/登录返回 `{user, access_token, refresh_token, token_type, expires_in}`；
- refresh token 旋转：每次刷新旧 token 立即失效；前端并发 401 必须单飞，避免两个请求同时刷新导致一个失败；
- 改密/登出会吊销该用户全部未过期 refresh token；
- access token 过期后前端按现有拦截器刷新一次并重放原请求；
- 邮箱大小写不敏感；密码 8–128 字符。

### 5.4 数据库与迁移

- M1 迁移 `alembic/versions/..._m1_core_tables.py` 已通过 `upgrade → downgrade → upgrade`；
- 新增 `refresh_tokens` 表（交接包/数据库文档未列）；
- `messages` 增加内部 `streaming` 状态与 `updated_at`；
- `runs` 与 `messages` 互相引用，迁移里用 `ALTER TABLE` 补外键，避免建表顺序问题；
- LangGraph checkpoint 表由 `PostgresSaver.setup()` 创建在 `langgraph` schema，不进 Alembic。

---

## 6. 环境与验证现状

| 项 | 结果 |
|---|---|
| 依赖安装 | `uv sync` 成功（Python 3.13.11；FastAPI 0.162.2 / LangGraph 1.2.20 / a2a-sdk 1.3.0） |
| PostgreSQL | 使用本地 PostgreSQL 16.2 完成迁移与集成测试，未依赖 Docker；`pgvector` 未安装，M4 前不影响 |
| 迁移 | `upgrade → downgrade base → upgrade` 通过；`alembic check` 无新操作 |
| 契约测试 | 17 个用例通过（认证、会话、消息、SSE 顺序、幂等、并发 409、取消、运行事件、系统信息） |
| 真实 HTTP | uvicorn + TCP 验证：响应头 19ms、首 delta 95ms、取消后 `finish_reason=canceled`、部分内容保留 |
| Postgres checkpoint | `CHECKPOINT_BACKEND=postgres` 启动、建 schema、对话流验证通过 |
| 真实模型 | 未配置密钥，使用假流式模型；真实 DeepSeek 需配置 `LLM_API_KEY` |
| 多实例 SSE | 未验证；当前不支持 |

---

## 7. 联调步骤（前端）

1. 后端启动：

```bash
cd /home/zhaodan/work/PythonProjects/agent-hub
cp .env.example .env
# 填 DATABASE_URL、JWT_SECRET；无密钥时 LLM_PROVIDER=fake
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
curl http://localhost:8000/healthz
```

2. 前端开发服务器代理 `/api/v1` 到 `127.0.0.1:8000`；确认 CORS 的 `X-Run-ID`、`X-Request-ID` 可读；
3. 先联调：注册 → 登录 → 会话列表 → 发消息（看 SSE 帧）→ 停止 → 刷新页面看历史 → 运行事件；
4. 再联调：`PATCH /auth/me`、改密、审批列表空态；
5. 智能体/任务/知识库/设置四能力/工作流/管理端：按 `capabilities` 置灰，M2 之后逐个打开；
6. 契约类型建议从 `docs/openapi.json` 生成，前端手写类型保留 UI 视图类型。

---

## 8. 下一步建议

- **前端 P0**：本文 3.1–3.4；**后端 P0**：补 `docs/api.md` 的 M1 增量、把 `reasoning_efforts` 与能力开关写进 OpenAPI 示例；
- **共同 P1**：D8 的 HITL 流生命周期、D7 知识库权限、D5 附件下载端点，在 M1.5/M2 排期前确认；
- **暂缓**：工作流（D1）、v8 管理端（D2）不要在前端继续加交互，先等后端 M5 范围确认；
- 本文与 `agent-hub-web/docs/handoff/contract-delta.md` 冲突时，**以本文 + `docs/openapi.json` + 前端 `src/types` 为准**，并在交接包追加变更记录。
