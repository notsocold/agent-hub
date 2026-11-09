# 前端对接反馈（M1 真实联调完成）

- 日期：2026-11-09
- 前端仓库：`agent-hub-web`（Vue 3 + TS + Vite）
- 后端地址：`http://127.0.0.1:8000`
- 对照材料：`docs/feedback/2026-11-09-frontend-contract-feedback.md`、后端 `docs/openapi.json`、`agent-hub-web/docs/handoff/contract-delta.md`
- 结论：**前端 M1 主链路已完成真实联调**；反馈文档第 3 节的 12 个契约点已全部落地。首轮联调发现的 **P0 分页缺陷**（`has_more=true` 时 500）及 OpenAPI/文档缺口，后端已于 2026-11-09 热修；前端复验通过（见 §11）。

---

## 1. 一句话结论

前端已经按 M1 真实契约完成适配，注册 → 登录 → 会话 → SSE 对话 → 停止 → 历史 → 运行事件 → 审批空态 → 能力降级全部跑通；幂等回放、并发 `409 details.run_id`、CORS 暴露头也已在真实后端验证。

当前唯一会阻断分页主链路的是后端 cursor 编码缺陷：**只要 `has_more=true`，会话列表、消息历史、审批列表都会 500**；这不是前端可绕过的问题，建议作为后端 M1 热修。

---

## 2. 联调环境与验证结果

| 项 | 结果 |
|---|---|
| 前端开发服务器 | Vite 8，`http://127.0.0.1:5173`，`/api`、`/a2a`、`/healthz`、`/readyz` 代理到 8000 |
| mock 开关 | `.env.development.local` 中 `VITE_USE_MOCK=false`（注意：`.env.local` 不会覆盖 `.env.development`，必须用 `development.local`） |
| 认证 | 注册/登录/`/auth/me`/登出通过；access `expires_in=900`；refresh 轮换端点本次未单独做并发复验，前端沿用 axios 单飞刷新 + SSE 共享刷新 |
| 系统信息 | `GET /system/info` 通过；`agents/tasks` 字段存在且为 false，`approvals=true` |
| SSE | `X-Run-ID`、`conversation.created` → `run.started` → `message.created` → `message.started` → `message.delta` → `message.done` 顺序通过 |
| 幂等回放 | 同 `conversation_id + client_message_id` 返回 200，`X-Run-ID` 与首次一致，不新增消息 |
| 并发 409 | 第二请求返回 `run_in_progress`，`details.run_id` 指向占锁 run |
| 停止 | `cancel` 返回 200，流以 `finish_reason=canceled` 结束 |
| 历史 | `created_at DESC`、user/assistant 相差 1 微秒、状态不暴露 `streaming`，均通过 |
| 运行事件 | `id=<run_id>:<seq>`、`tokens` 为对象/null、`error` 为字符串/null、`payload` 无 `_event`，均通过 |
| 能力关闭 | `skills`/`knowledge_categories` → 403 `capability_disabled`；`workflow_id` → 409 `workflow_disabled`；非法模型 → 422 `model_not_allowed` |
| 审批 | `GET /approvals` 空列表、`stats` 正常；`POST /approvals/{id}/decision` M1 不产生真实事件 |
| CORS | 实际响应带 `Access-Control-Expose-Headers: X-Request-ID, X-Run-ID`（GET 与 POST 均验证） |
| 前端检查 | `pnpm type-check`、`pnpm test`（55 个单测）、`pnpm build` 全部通过 |
| 前端真实联调 | `pnpm test:real` 2 个用例通过（前端 store + `fetch-event-source` 直连后端） |
| 协议冒烟 | `pnpm smoke:real` **97 项断言通过**（含 cursor 翻页；Vite 代理 → 后端） |
| 后端测试 | `pytest -q` **27 passed**（含分页、cursor 单元、500 request_id） |

复验命令：

```bash
cd /home/zhaodan/work/NodeJsProjects/agent-hub-web
pnpm test
pnpm build
REAL_TEST_EMAIL=<已有账号> REAL_TEST_PASSWORD=<密码> pnpm test:real
SMOKE_EMAIL=<已有账号> SMOKE_PASSWORD=<密码> pnpm smoke:real
```

> 注册接口有频率限制，自动化请复用已有账号；两个脚本都支持环境变量登录。

---

## 3. 12 个契约点逐项回复

| # | 反馈点 | 前端状态 | 落地说明 |
|---|---|---|---|
| 1 | P0 `error.details` | ✅ 已实现 | `ApiErrorBody.error.details?`；`ApiRequestError` 保留 `details`；SSE `onopen` 也会解析 409/403 JSON 信封，不再只抛 HTTP 状态 |
| 2 | P0 幂等回放 | ✅ 已实现 | 失败/重试复用同一 `client_message_id`；新连接把 `lastSeq` 重置为 0，避免回放 `seq=1` 被旧基线丢弃；回放事件按 `seq` 去重 |
| 3 | P0 `reasoning_efforts` 可为空 | ✅ 已实现 | `reasoningEfforts.length === 0` 时隐藏推理等级选择器；`run.started.reasoning_effort` 仍按必填消费 |
| 4 | P0 `capabilities.agents/tasks` | ✅ 已实现 | 类型新增可选项；导航/路由按能力置灰或退回对话，M1 不请求 `/agents*`、`/tasks*`、`/knowledge*`、`/workflows*` |
| 5 | P1 运行事件字段 | ✅ 已实现 | `RunEvent.id` 字符串、`tokens` 可空、`error` 字符串/null；运行详情抽屉接入真实 `/runs/{id}` + `/runs/{id}/events` |
| 6 | P1 消息历史新 → 旧 | ✅ 已实现 | 前端反转为时间线顺序；按 ISO 字符串稳定排序，避免 `Date.parse` 把 1 微秒差截断为毫秒后重排；支持 `cursor` 加载更早 |
| 7 | P1 能力关闭错误码 | ✅ 已实现 | 发送前按 `capabilities` 隐藏入口；后端 403/409 作为可展示错误兜底，并保留请求重试能力 |
| 8 | P1 附件下载端点 | ✅ 已降级 | `download_url` 为空时下载按钮置灰并标注 M1.5；类型不变，端点上线后自动可点 |
| 9 | P1 审批卡 M1 不出现 | ✅ 已实现 | 审批卡/审批弹窗按契约实现；`resumed=false` + `resume_error` 不当失败，提示“审批已记录但运行未自动恢复” |
| 10 | P1 interrupted 占锁 | ✅ 已实现 | 新增运行恢复条：启动时读取 `conversation.last_run`，409 读取 `details.run_id`，轮询 run 状态；提供“停止/放弃本轮”“处理审批”“刷新状态” |
| 11 | P2 字段命名 | ✅ 字段已对齐 | 类型与请求字段保持 `knowledge_categories`；M1 尚无知识库选择入口与 chip 渲染，M4 接入时直接以分类名作为 chip 文案；`docs/pages.md` 的推理等级降级规则已修正 |
| 12 | P2 时间与 `stats` | ✅ 已实现 | 时间按 UTC ISO 解析展示；`stats` 当全量计数使用；`next_cursor` 不解析、不拼接，`has_more=false` 不再请求 |

---

## 4. 实测证据摘录

### 4.1 SSE 帧

```text
id: 1
event: conversation.created
data: {"run_id":"...","seq":1,"conversation_id":"...","title":"..."}

id: 2
event: run.started
data: {"run_id":"...","seq":2,"conversation_id":"...","user_message_id":"...","client_message_id":"...","model":"deepseek-flash","reasoning_effort":"low"}

...

id: 37
event: message.done
data: {"run_id":"...","seq":37,"conversation_id":"...","message_id":"...","usage":{...},"finish_reason":"stop","model":"deepseek-flash","first_token_ms":966,"duration_ms":1196,"active_duration_ms":1196,"citations":[],"artifacts":[],"reasoning":null}
```

- `id:` 行存在，但 M1 不依赖 `Last-Event-ID`；前端断线后通过历史 + 运行事件恢复。
- `message.done` 实际多带 `conversation_id`，前端已按可选字段兼容。

### 4.2 409 错误信封

```json
{"error":{"code":"run_in_progress","message":"该会话已有运行中的请求，请等待完成或先停止当前运行","request_id":"...","details":{"run_id":"dc709765-..."}}}
```

### 4.3 运行事件

```text
31f40eff-...:1  1  message  ok      创建会话    tokens=null
31f40eff-...:2  2  request  ok      用户请求    tokens=null
31f40eff-...:4  4  message  running 助手消息    tokens=null
31f40eff-...:37 37 message  ok      助手回复    tokens={input_tokens:158,...}
```

`seq` 允许跳号（`message.delta` 不落库），与反馈 5.1 一致。

### 4.4 实时系统信息与反馈文案的差异

当前实例：

```json
{
  "default_model": "deepseek-flash",
  "reasoning_efforts": [
    {"id":"low","name":"标准"},
    {"id":"medium","name":"深度"},
    {"id":"high","name":"最大"}
  ],
  "capabilities": {
    "instructions": false, "memories": false, "skills": false, "mcp": false,
    "knowledge": false, "attachments": false, "workflows": false,
    "approvals": true, "agents": false, "tasks": false
  }
}
```

反馈文档写的是 `deepseek-chat` 且 `reasoning_efforts=[]`，实际实例因 `LLM_REASONING_EFFORT_SUPPORTED=true` 返回 3 个等级。前端不硬编码模型，按 `/system/info` 渲染，两种配置都已兼容；建议后端在文档或 `.env.example` 中注明该开关。

---

## 5. 后端问题首轮清单（均已修复/复验）

### 5.1 P0：`has_more=true` 时分页接口 500（已修复，2026-11-09 复验通过）

**复现：**

```bash
TOKEN=<access_token>
curl -i "http://127.0.0.1:8000/api/v1/conversations?limit=2" -H "Authorization: Bearer $TOKEN"
# HTTP/1.1 500
# {"error":{"code":"internal_error","message":"服务器内部错误，请稍后重试","request_id":"unknown"}}

curl -i "http://127.0.0.1:8000/api/v1/conversations/<id>/messages?limit=1" -H "Authorization: Bearer $TOKEN"
# HTTP/1.1 500
```

`limit` 大于等于当前总数时正常（`has_more=false`，不会编码 cursor）；只要需要返回 `next_cursor` 就 500。

**根因：** `app/core/cursor.py::encode_cursor` 直接 `json.dumps(payload)`，调用方传入的是 `UUID` 对象，不是字符串：

- `app/modules/conversations/service.py::list_conversations`：
  `encode_cursor({"t": ..., "id": last.id})`
- `app/modules/conversations/service.py::list_messages`：
  `encode_cursor({"t": last.created_at.isoformat(), "id": last.id})`
- `app/modules/approvals/service.py` 同样存在。

`json.dumps` 遇到 `UUID` 抛 `TypeError: Object of type UUID is not JSON serializable`，被全局错误处理成 500。

**建议修复：** cursor 编码统一 `str()`，例如在 `encode_cursor` 里对值做 JSON-safe 转换，或在三处调用点传 `str(last.id)`；同时补 `limit < total` 的分页契约测试（会话、消息、审批各一条）。

**影响：** 前端会话列表用 `limit=30`、历史用 `limit=100`，当前数据量下未触发；一旦超过阈值，列表/历史直接 500。这是 M1 的 P0 热修项。

**后端修复与前端复验（2026-11-09）：**

- 后端在 `app/core/cursor.py` 编码层统一做 JSON-safe 归一化（UUID → 字符串、datetime/date → ISO 8601），并补了会话/消息/审批分页回归测试；
- 同时修正 `before` + `cursor` 同时传时按 `cursor` 优先；
- 真实 HTTP 复验：`GET /conversations?limit=1` → 200 + `has_more=true` + `next_cursor`，第二页不重复；`GET /messages?limit=1` → 200，第二页严格更旧；`before` + `cursor` 同时传时返回 cursor 指定的第二页；
- 非法 cursor → 422 `validation_error`；404/422 响应体的 `request_id` 与响应头 `X-Request-ID` 一致；
- 后端 `pytest -q` 27 passed，前端 `smoke:real` 97 项断言通过。

### 5.2 P1：OpenAPI 未覆盖 M1 真实行为（已补齐）

- `POST /api/v1/chat/stream` 只声明 `200`（`schema: {}`）和 `422`，没有声明：
  - `403 capability_disabled`、`409 run_in_progress`、`409 workflow_disabled`、`422 model_not_allowed` 的错误信封；
  - `X-Run-ID` / `X-Request-ID` 响应头；
  - SSE 事件名与 `data` 结构。
- 错误信封的 `details` 字段未建模，前端只能按 `Record<string, unknown>` 处理。
- `RunEventOut.tokens`、`error` 在 schema 中非必填（实际总会返回，可为 null）；`ApprovalDecisionResult.resume_error` 也非必填（实际只在 `resumed=false` 返回），前端已兼容。
- `SystemCapabilitiesOut.agents/tasks` 是可选字段（默认 false），建议在 OpenAPI 描述中明确，避免前端误判必填。

**复验：** 8000 的 `/openapi.json` 已包含 `ErrorEnvelopeOut` / `ErrorBodyOut`；`chat/stream` 的 200 已改为 `text/event-stream` 并声明 400/401/403/404/409/413/422/429/500、`X-Run-ID`、`X-Request-ID` 与事件说明；`RunEventOut.tokens/error`、`ApprovalDecisionResult.resume_error` 已改为 required + nullable。前端已同步收紧 TS 类型。

### 5.3 P1：文档与实时环境不一致（已修正）

- 默认模型：反馈文档写 `deepseek-chat`，实时为 `deepseek-flash`。
- 推理等级：反馈文档写“默认为空”，实时实例返回 3 个等级。
- `message.done.conversation_id` 实际返回但未写入契约/OpenAPI。
- `POST /runs/{id}/resume` 对已结束运行返回 `409 run_not_resumable`，反馈只写了“M1 只返回 409”，建议补错误码。
- `run_events` 的 `id` 为 `<run_id>:<seq>`、`payload` 去掉 `_event`、`seq` 允许跳号，建议写入 `docs/api.md`。

**复验：** `docs/api.md` 已补分页、运行事件、`message.done`、`resume` 错误码与推理等级开关说明；实时值以 `/system/info` 为准，前端不硬编码模型。

### 5.4 P2：错误可观测性（已修复）

- 500 响应体的 `request_id` 为 `"unknown"`：

```json
{"error":{"code":"internal_error","message":"服务器内部错误，请稍后重试","request_id":"unknown"}}
```

这会丢失真实请求 ID，排障时无法与后端日志关联。建议全局异常兜底读取当前请求的 `X-Request-ID`，并返回同一个值。

**复验：** 404/422 响应体的 `request_id` 与响应头 `X-Request-ID` 完全一致；后端测试 `test_internal_error_returns_real_request_id` 覆盖 500 注入场景。

### 5.5 P2：注册限流对本地联调不友好（已处理）

连续注册测试用户会触发：

```json
{"error":{"code":"rate_limited","message":"操作过于频繁，请在 2885 秒后重试"}}
```

前端会原样展示，但本地自动化难以反复跑。建议：

- 在 `.env.example` 中暴露限流开关/窗口，开发环境放宽或提供测试绕过；
- 或提供固定的 dev 账号 seed 脚本；
- 前端脚本已改为优先用既有账号登录，但首次使用者仍会踩到。

**复验：** `.env.example` 已参数化 `RATE_LIMIT_REGISTER_MAX` 等开关，新增 `scripts/seed_dev_account.py`；本轮直接用 `dev@example.com` 登录，未再触发注册限流。

---

## 6. 前端当前降级策略（M1）

| 能力 | 前端行为 |
|---|---|
| `reasoning_efforts=[]` | 隐藏推理等级下拉；仍发送默认 `reasoning_effort` |
| `capabilities.agents=false` | “智能体”导航置灰；直接访问 `/agents` 退回对话并提示能力关闭 |
| `capabilities.tasks=false` | “任务”导航置灰；不请求 `/tasks*` |
| `capabilities.knowledge=false` | “知识库”导航置灰；不请求 `/knowledge*` |
| `capabilities.workflows=false` | “工作流”导航置灰；不请求 `/workflows*`；请求带 `workflow_id` 时展示 409 `workflow_disabled` |
| `capabilities.attachments=false` | “+”菜单提示 M1.5；产物下载按钮置灰 |
| `capabilities.approvals=true` 但无真实事件 | 审批卡/审批弹窗按契约实现，空态说明 M1 无会触发审批的工具 |
| 运行占用/中断 | 恢复条 + 停止/放弃 + 处理审批 + 刷新；轮询 `GET /runs/{id}` 直到终态 |
| 409 `run_not_cancelable` | 读取 `details.status` 更新运行状态并重取历史，不弹致命错误 |
| 500/网络错误 | 保留乐观消息，允许用同一 `client_message_id` 重试；服务端受理则走幂等回放 |

---

## 7. 对 D1–D14 的前端立场

| ID | 前端立场 |
|---|---|
| D1 工作流 | 接受“轻量步骤模板，M5+”；M1 只保留置灰导航，不做无接口编辑器 |
| D2 v8 管理端 | 接受 M5.0 最小集；M1 不接 `/admin*`，管理端保持独立原型 |
| D3 模型/推理等级 | 接受服务端白名单；菜单按 `models`/`reasoning_efforts` 渲染，空则降级 |
| D4 知识库字段 | 接受复用 `knowledge_categories`；chip 显示分类名 |
| D5 附件存储 | 接受本地磁盘先行；下载只接受后端受控 URL，不暴露 `storage_key` |
| D6 任务动作 | 接受任务级 resume/authorize/reject；M1 任务页入口置灰 |
| D7 知识库权限 | 接受 `owner_user_id`；普通用户上传入口待 M4 恢复 |
| D8 HITL 期间 SSE | 接受保持连接 + 超时 + 事件回放；前端已做运行恢复条与轮询兜底 |
| D9–D12 | 接受 refresh token 落库、内部 streaming 不暴露、标题截断、部分唯一索引 |
| D13 智能体注册表 | 待 M2；若要多租户需先定 owner 语义，前端不提前做个人 Agent 过滤 |
| D14 auth_required | 接受 v1 返回 403 `auth_flow_unavailable`，前端不伪造授权成功 |

---

## 8. 下一步（P0/P1/P2 已收口）

1. **后端 P0（已完成）**：cursor UUID 编码已修复，会话/消息分页真实 HTTP 复验通过；审批分页由后端集成测试覆盖（M1 无真实审批事件）。
2. **后端 P1（已完成）**：`chat/stream` 错误响应、SSE 头/事件、`details`、`message.done.conversation_id`、`resume` 错误码已写入 OpenAPI / `docs/api.md`。
3. **后端 P2（已完成）**：500 错误返回真实 `request_id`；限流参数化并新增 dev 账号 seed 脚本；推理等级开关与默认模型已说明。
4. **前端 M1.5**：附件上传/受控下载、技能/MCP/知识库上下文选择；届时按 `capabilities` 打开入口。
5. **共同**：M2 接入 interrupt 后，确认 `approval.required` 事件、HITL SSE 生命周期与“新消息自动放弃旧 interrupted run”策略（D8）。

---

## 9. 前端改动入口（供后端对照）

| 文件 | 作用 |
|---|---|
| `src/types/api.ts` | `error.details` |
| `src/types/system.ts` | `capabilities.agents/tasks`、`reasoning_efforts` 可空注释 |
| `src/types/chat.ts` | `ChatRecovery`、`runStatus`、`message.done.conversation_id`、错误 `details` |
| `src/types/run.ts` | 事件 id/tokens/error、`RunResume*` |
| `src/api/client.ts` | `ApiRequestError.details`、SSE 共享单飞刷新、统一错误构造 |
| `src/api/chat.ts` | `onopen` 解析 JSON 错误、401 刷新重试、409/403 透传 |
| `src/api/runs.ts` | `resume` 端点 |
| `src/stores/chat.ts` | 幂等重试、seq 基线重置、409 恢复、历史重取/倒序、停止、轮询 |
| `src/stores/chat-reducer.ts` | `runStatus`/`recovery` 状态收敛 |
| `src/components/chat/RunRecoveryBanner.vue` | 占锁/中断/失败恢复条 |
| `src/components/chat/RunTraceDrawer.vue` | 真实运行事件回放 |
| `src/views/chat/ChatView.vue` | 恢复条、审批弹窗、历史加载 |
| `src/components/layout/SideNav.vue`、`src/router/*`、`src/layouts/AppLayout.vue` | 能力置灰/路由拦截 |
| `scripts/smoke-real-backend.mjs`、`scripts/real-backend.spec.ts` | 真实后端冒烟与 store/SSE 联调 |
| `docs/handoff/contract-delta.md` §17 | 本文件对应的交接包变更记录 |

---

## 10. 附：复验清单

- [x] `GET /conversations?limit=1` 返回 200，`has_more=true`、`next_cursor` 非空；带 cursor 第二页 200 且不重复。
- [x] `GET /conversations/{id}/messages?limit=1` 同上，第二页严格更旧；`before` + `cursor` 同时传时 cursor 优先。
- [x] `GET /approvals?limit=1`（>1 条审批）不再 500：由后端集成测试 `test_approval_pagination_has_more` 覆盖。
- [x] `POST /chat/stream` 的 403/409/422 等在 OpenAPI 中可见。
- [x] 404/422 响应 `request_id` 与响应头 `X-Request-ID` 一致；500 由后端测试覆盖。
- [x] 默认模型/推理等级以 `/system/info` 为准，`.env.example` 已说明开关。

---

## 11. 后端热修复复验记录（2026-11-09 22:03）

- 后端文档：`docs/feedback/2026-11-09-backend-hotfix-response.md`。
- `app/core/cursor.py` 已在编码层做 JSON-safe 归一化（UUID/datetime/date）；会话、消息、审批分页回归测试通过。
- 真实 HTTP：`GET /conversations?limit=1` → 200 + `has_more=true` + `next_cursor`；带 cursor 第二页 200 且无重复；`GET /messages?limit=1` 第二页严格更旧；`before`+`cursor` 同时传时 cursor 优先；非法 cursor → 422 `validation_error`。
- 请求追踪：404/422 的 `request_id` 与 `X-Request-ID` 一致；后端 27 个测试通过（含 500 注入）。
- OpenAPI：`RunEventOut.tokens/error`、`ApprovalDecisionResult.resume_error` 已 required+nullable；前端类型已同步收紧；`chat/stream` 已声明 `text/event-stream`、错误响应与响应头。
- 前端回归：`pnpm type-check`、`pnpm test`（55）、`pnpm test:real`（2）、`pnpm smoke:real`（**97 项断言**）、`pnpm build` 全部通过。
- 结论：首轮反馈的 P0/P1/P2 均已在 8000 实例上复验；未发现新增契约差异，可以进入 M1.5。
