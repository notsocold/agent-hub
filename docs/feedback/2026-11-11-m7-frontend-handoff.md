# M7 前端交接：SSE 续流、审批字段与写操作 RBAC（2026-11-11）

> 后端范围：M7「契约补齐与安全收口」（M7 Task 1–6）。契约真相以 [`docs/api.md`](../api.md)
> §1/§2.3/§2.4/§2.5/§3.3/§7.7/§7.8/§8.2/§9/§11 与 [`docs/openapi.json`](../openapi.json) 为准；
> 本文件是前端接入清单，不替代 OpenAPI。
>
> M7 **无数据库迁移**，登录/令牌/既有事件语义不变。除两处破坏性变更（普通用户调用 agents/tasks
> 写接口会 403、A2A `authorization_url` 可能为 `null`）外，未接入新端点、不传新字段的旧前端可继续运行。

## 1. 变更速览

| 变更 | 兼容性 | 前端动作 |
|---|---|---|
| 新增 `GET /api/v1/runs/{id}/stream?after_seq=`（SSE 续流） | 新能力（可选接入） | 断线后从最后 `seq` 续流**已落库事件** + 后续实时事件；断线窗口的 `message.delta` 不补发，终态后需拉消息终稿 |
| `GET /api/v1/runs/{id}/events` 每项新增必填 `event` | 向后兼容（多字段） | 用 `event` 判断事件类型，不再解析 `payload._event`（该键不会返回） |
| `approval.required.data` 新增 5 个字段 | 向后兼容（多字段） | 审批卡优先用新字段展示，缺失时回退到旧字段 |
| agents 写接口、tasks 动作接口改为**仅管理员** | **有破坏性**（普通用户 403） | 按 `/auth/me` 的 `role` 隐藏/禁用写操作；处理 403 `details.required_role` |
| A2A `auth_request.authorization_url` 可能为 `null` | **有破坏性**（类型变可空） | 为 `null` 显示「授权链接不可用」，不要渲染成 `<a href>` |
| 无登录、令牌、错误信封结构变化 | 兼容 | 无需改登录链路 |

---

## 2. 运行事件续流（新端点）

### 2.1 端点与鉴权

```http
GET /api/v1/runs/{run_id}/stream?after_seq=<lastSeq>
Authorization: Bearer <access_token>
Accept: text/event-stream
```

- 需要登录；**不能用浏览器原生 `EventSource`**：它无法设置 `Authorization` 请求头，也不支持自定义头。
  请用 `fetch` 读 `ReadableStream`，或使用 `@microsoft/fetch-event-source`（下方示例）。
- `after_seq`：只回放 `seq > after_seq` 的已落库事件，默认 `0`；回放结束后继续跟随实时事件。
  首次建连传 `0`；断线重连传**最后收到事件的 `id`/`seq`**。
- 服务端**不读取 `Last-Event-ID` 请求头**，重连必须显式传 `after_seq`。
- 响应头：`X-Run-ID`（被续流的 run）、`X-Request-ID`（排障用，错误体 `request_id` 与之一致）、
  `Cache-Control: no-cache`、`X-Accel-Buffering: no`。
- 帧格式与 `/chat/stream` 相同：`id: <seq>`、`event: <事件名>`、`data: <单行 JSON>`；
  心跳是注释帧 `: ping`（每 15s，不占 `seq`）。
- **回放只覆盖已写入 `run_events` 的事件**：`message.delta` / `reasoning.delta` 以
  `persist=False` 发出，不落库，断线窗口内的增量文本**不会补发**。重连后只从服务端当前实时点继续。
- **只有 run 进入终态后才拉消息终稿**：收到 `message.done`/`error`（run 为
  `succeeded`/`failed`/`canceled`）后，用
  `GET /api/v1/conversations/{conversation_id}/messages` 拉取该条助手消息的终稿覆盖本地累积文本。
- **`run.interrupted` 期间不要拉消息终稿**：此时助手消息内容虽已落库，但 `messages.status` 仍为
  `streaming`，历史接口会过滤掉它、**不会返回该消息**；需等审批恢复（`run.resumed` → 后续终态）
  或审批超时取消、run 进入终态后再拉取。

### 2.2 推荐接入方式（`@microsoft/fetch-event-source`）

```ts
// 依赖：pnpm add @microsoft/fetch-event-source
import { fetchEventSource } from '@microsoft/fetch-event-source'

type RunStreamHandlers = {
  onEvent: (event: string, data: Record<string, unknown>) => void
  onFatal: (message: string) => void
  // 服务端已终态但回放中没有终态事件时回调（此时应停止重连并刷新终态/消息终稿）。
  onRunEnd?: (status: 'succeeded' | 'failed' | 'canceled') => void
}

export function connectRunStream(params: {
  baseUrl: string
  runId: string
  accessToken: string
  afterSeq?: number
  handlers: RunStreamHandlers
}) {
  const { baseUrl, runId, handlers } = params
  let accessToken = params.accessToken
  const ctrl = new AbortController()
  let lastSeq = params.afterSeq ?? 0
  let terminal = false
  let retryDelay = 300

  const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

  const fetchRunStatus = async (): Promise<string | null> => {
    const response = await fetch(`${baseUrl}/api/v1/runs/${runId}`, {
      headers: { Authorization: `Bearer ${accessToken}` },
    })
    if (!response.ok) return null
    return (await response.json())?.status ?? null
  }

  const loop = async () => {
    while (!ctrl.signal.aborted && !terminal) {
      try {
        await fetchEventSource(
          `${baseUrl}/api/v1/runs/${runId}/stream?after_seq=${lastSeq}`,
          {
            method: 'GET',
            headers: {
              Authorization: `Bearer ${accessToken}`,
              Accept: 'text/event-stream',
            },
            signal: ctrl.signal,
            openWhenHidden: true,
            // 返回非 2xx 时库不会重连；先把错误信封读出来，再交给外层重试/终止。
            onopen: async (response) => {
              if (response.ok) return
              const body = await response.json().catch(() => null)
              const code = body?.error?.code ?? 'http_error'
              const error = new Error(`HTTP ${response.status} ${code}`)
              Object.assign(error, { status: response.status, code })
              throw error
            },
            onmessage: (ev) => {
              const seq = Number(ev.id)
              // 重连窗口内可能重复回放：已处理过的 seq 直接丢弃。
              if (Number.isFinite(seq) && seq <= lastSeq) return
              if (Number.isFinite(seq)) lastSeq = seq
              if (!ev.data) return
              const data = JSON.parse(ev.data) as Record<string, unknown>
              handlers.onEvent(ev.event, data)
              if (ev.event === 'message.done' || ev.event === 'error') {
                // 终态事件：正常结束，不再重连。
                terminal = true
                ctrl.abort()
              }
            },
            // 让库停止自动重试，由外层 loop 用最新 lastSeq 重连。
            onerror: (err) => {
              throw err
            },
          },
        )
      } catch (err) {
        if (terminal || ctrl.signal.aborted) return
        const status = (err as { status?: number }).status
        const code = (err as { code?: string }).code

        if (status === 401) {
          // 先刷新 access token（替换为项目现有 refresh 实现）；刷新失败再登出，不要无限重连。
          const refreshed = await refreshAccessToken()
          if (!refreshed) return handlers.onFatal('登录已过期')
          accessToken = refreshed
          retryDelay = 300
          continue
        }
        if (status === 404) {
          return handlers.onFatal('运行不存在或无权访问')
        }
        if (status === 403) {
          // 续流端点不要求管理员，403 属通用拒绝（当前正常路径不会出现），按不可恢复处理。
          return handlers.onFatal('无权访问该运行')
        }
        if (status === 400 && code === 'replay_too_large') {
          return handlers.onFatal('待回放事件过多，请先用 /runs/{id}/events 分页回放已落库历史')
        }
        if (status === 422) {
          return handlers.onFatal('续流参数非法（after_seq 必须是 >=0 的整数）')
        }

        // 网络错误 / 5xx / 429：指数退避后用最新 lastSeq 重连（只补已落库事件，delta 不补）。
        await sleep(retryDelay + Math.random() * 200)
        retryDelay = Math.min(retryDelay * 2, 5000)
        continue
      }

      // 正常 EOF：若还没收到终态事件，先查一次 run 状态再决定是否重连。
      if (!terminal && !ctrl.signal.aborted) {
        // 服务端对「已终态且回放里没有终态事件」的 run 会回放完直接关闭：
        // 此时重连只会每 5 秒空转，必须停止重连并刷新终态。
        const status = await fetchRunStatus().catch(() => null)
        if (status === 'succeeded' || status === 'failed' || status === 'canceled') {
          handlers.onRunEnd?.(status)
          return
        }
        // 仍为 running/interrupted 或状态查询失败：按最后 seq 退避重连。
        await sleep(retryDelay + Math.random() * 200)
        retryDelay = Math.min(retryDelay * 2, 5000)
        continue
      }
      return
    }
  }

  void loop()
  return { close: () => ctrl.abort(), getLastSeq: () => lastSeq }
}
```

说明：

- 每收到一帧就把 `ev.id` 记为 `lastSeq`（`id` 就是 `seq`，纯数字）；重连时作为 `after_seq`，
  服务端只回放 `seq > after_seq` 的已落库事件。示例在 `onmessage` 里显式丢弃 `seq <= lastSeq`
  的重复帧，防止重连窗口导致消息重复。
- 收到 `message.done` / `error`：属于终态事件，**不要重连**；UI 以 `GET /api/v1/runs/{id}` 的终态为准，
  并用 `GET /api/v1/conversations/{id}/messages` 拉取助手消息终稿，覆盖本地累积的增量文本。
- EOF 且未收到终态事件时分两种：
  1. **run 已终态**（`succeeded`/`failed`/`canceled`）但回放里没有终态事件：服务端回放完就关闭，
     示例会先查 `GET /runs/{id}`，命中终态则停止重连并回调 `onRunEnd`；
  2. **run 仍运行 / `interrupted`**（或状态查询失败）：按最后 `seq` 退避重连；`interrupted` 连上后
     继续等审批恢复。
- 单实例部署：后端事件总线是进程内实现。多实例/重部署时实时帧可能丢失，重连后的回放只能补齐
  **已落库**事件；`message.delta`/`reasoning.delta` 不落库，断线窗口的增量文本不补，需在 run
  进入终态后靠消息历史终稿覆盖（`interrupted` 期间历史接口不返回该消息）。

### 2.3 用原生 `fetch` 接入（无第三方依赖）

关键点：`fetch` 的 `response.body` 是 `ReadableStream`，按 `\n\n` 切帧；跳过 `:` 开头的注释帧；
`data:` 行拼成 JSON。示例只保留骨架：

```ts
const response = await fetch(
  `${baseUrl}/api/v1/runs/${runId}/stream?after_seq=${lastSeq}`,
  {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'text/event-stream' },
    signal: abortController.signal,
  },
)

if (!response.ok) {
  const body = await response.json().catch(() => null)
  // 错误信封：{ error: { code, message, request_id, details? } }，见 docs/api.md §1。
  throw Object.assign(new Error(body?.error?.message ?? '续流失败'), {
    status: response.status,
    code: body?.error?.code,
  })
}

const reader = response.body!.getReader()
const decoder = new TextDecoder()
let buffer = ''

while (true) {
  const { value, done } = await reader.read()
  if (done) break
  buffer += decoder.decode(value, { stream: true })

  let boundary = buffer.indexOf('\n\n')
  while (boundary >= 0) {
    const block = buffer.slice(0, boundary)
    buffer = buffer.slice(boundary + 2)
    // 注释帧（: ping）没有字段，直接忽略；其余帧解析 id/event/data。
    const lines = block.split('\n').filter((line) => line && !line.startsWith(':'))
    const frame: Record<string, string> = {}
    for (const line of lines) {
      const [field, ...rest] = line.split(':')
      frame[field] = rest.join(':').trim()
    }
    if (frame.id) lastSeq = Number(frame.id)
    if (frame.data) {
      const data = JSON.parse(frame.data)
      if (frame.event === 'message.done' || frame.event === 'error') terminal = true
    }
    boundary = buffer.indexOf('\n\n')
  }
}
```

（生产建议直接用 `@microsoft/fetch-event-source`，它已处理分块、注释帧与字段拼接。
原始 `fetch` 版本同样需要按 §2.2 的规则去重、终态后停止、EOF 时先查 `GET /runs/{id}`，
并在 run 进入终态后拉消息历史终稿（`interrupted` 期间历史接口不返回该消息）。）

### 2.4 续流生命周期

| 场景 | 服务端行为 | 前端处理 |
|---|---|---|
| 正常回放 + 运行中 | 回放**已落库**事件后持续推实时帧 | 正常渲染；无帧时等待心跳；断线窗口的 delta 不补 |
| 回放/实时收到 `message.done` 或 `error` | 立即关闭流 | 视为终态，**不重连**；用 `GET /runs/{id}` 刷新终态 |
| run 已终态且回放里有终态事件 | 回放完立即关闭 | 收到终态事件后停止 |
| run 已终态（`succeeded`/`failed`/`canceled`）但回放里没有终态事件 | 回放完直接关闭（避免空挂） | 收到 EOF 后查一次 `GET /runs/{id}`；确认终态则停止重连、不要无限重连，并在终态后拉消息终稿 |
| `interrupted`（等待审批） | 保持连接，最多空闲 `HITL_STREAM_TIMEOUT_SECONDS`（默认 900s），期间 15s 一次 `: ping` | 展示待审批卡片；收到 `run.resumed` 后继续渲染增量；**此阶段不要拉消息终稿**（消息 `status=streaming`，历史接口不返回），等恢复/取消至终态后再拉；服务端超时关闭后再按 `after_seq` 重连 |
| 回放超过 1000 条 | 建流前返回 400 `replay_too_large`（不会开始 SSE） | 改用分页回放补历史，再以最新 `seq` 重连续流；不要把同一个 `after_seq` 原样重试 |
| `after_seq` 非法（负数/超 int32/非整数） | 422 `validation_error` | 修正参数；不要自动重试 |
| 未登录/令牌过期 | 401 `unauthorized` | 刷新令牌后从原 `lastSeq` 重试；刷新失败则登出 |
| 非本人 run / 不存在 | 404 `not_found` | 停止重连，提示「运行不存在或无权访问」 |
| 403 | 403 `forbidden`（通用；续流不要求管理员，当前正常路径不返回） | 停止重连，按通用无权限提示 |
| 断线期间产生的 `message.delta` | **不落库、不回放** | 无法补齐增量文本；run 进入终态后拉消息历史终稿覆盖（`interrupted` 期间历史接口不返回该消息） |

> 心跳 `: ping` **不是事件**，不要写进消息列表，也不要因为 15s 没数据就主动断开。
> 浏览器/网关的空闲超时要大于 15s（Nginx 已由 `X-Accel-Buffering: no` 关闭缓冲，仍建议
> `proxy_read_timeout` ≥ 60s）。

---

## 3. 事件回放新增 `event` 字段

`GET /api/v1/runs/{id}/events` 的每一项现在是：

```json
{
  "id": "<run_id>:41",
  "seq": 41,
  "ts": "2026-11-11T12:00:00Z",
  "type": "message",
  "event": "message.created",
  "status": "ok",
  "title": "用户消息",
  "subtitle": "你好",
  "duration_ms": null,
  "tokens": null,
  "payload": {
    "run_id": "...",
    "seq": 41,
    "message_id": "...",
    "role": "user",
    "client_message_id": null,
    "created_at": "2026-11-11T12:00:00Z"
  },
  "error": null
}
```

- `event` 是**必填字符串**：原始 SSE 事件名（例如 `run.started`、`message.done`、`approval.required`）；
  历史数据缺少内部 `_event` 时为 `""`。未知事件按「忽略/通用展示」处理，不要抛异常。
- `payload` 仍会剔除内部字段 `_event`，其余与 SSE 帧 `data` 一致；`type` 仍是原分类字段，语义不变。
- 用 `event`（而不是 `type`）判断事件种类；`type` 继续用于分组/筛选。
- **注意**：回放里不会出现 `message.delta` / `reasoning.delta`（二者不落库）；不要用回放示例假设
  能恢复断线期间的增量文本；完整回复请在 run 进入终态后以消息历史终稿为准（`interrupted` 期间历史接口不返回该消息）。

---

## 4. `approval.required` 新增字段

审批事件 `data` 在原有字段（`approval_id`、`tool_call_id`、`tool`、`source`、`risk`、`reason`、
`payload`、`expires_at`）基础上新增 5 个展示字段：

| 字段 | 类型 | 含义 / 展示建议 |
|---|---|---|
| `tool_display_name` | string | 工具展示名（如「委派给 代码助手」）；为空时回退 `tool` |
| `executor` | string | 执行者 slug：MCP 服务器 slug 或远程 Agent slug |
| `target` | string | 执行目标：MCP 服务器 URL 或远程 Agent URL（最长 2000 字符） |
| `side_effect` | string | 副作用中文说明（只读/写/破坏性/未知） |
| `payload_preview` | string | 参数预览（compact JSON，约 500 字符，可能以 `…` 结尾）；**按纯文本渲染并转义** |

兼容要求：

- 旧字段全部保留、语义不变；新字段是增量，旧前端忽略即可。
- `payload` 仍用于完整参数展示（后端已做脱敏）；`payload_preview` 是给卡片快速展示的截断预览。
- 审批决定仍走 `POST /api/v1/approvals/{id}/decision`，请求/响应不变。

---

## 5. agents / tasks 写接口改为管理员

以下端点自 M7 起要求管理员（`users.role == "admin"`）：

| 模块 | 端点 |
|---|---|
| agents | `POST /agents`、`PATCH /agents/{id}`、`DELETE /agents/{id}`、`POST /agents/{id}/refresh`、`POST /agents/{id}/health` |
| tasks | `POST /tasks/{id}/cancel`、`/resume`、`/authorize`、`/reject` |

保持不变（任意登录用户可用）：

- `GET /agents`、`GET /agents/{id}`、`POST /agents/preview`；
- `GET /tasks`、`GET /tasks/{id}`。

非管理员调用写接口时：

```json
{
  "error": {
    "code": "forbidden",
    "message": "需要管理员权限",
    "request_id": "...",
    "details": { "required_role": "admin" }
  }
}
```

- HTTP 403，`error.code === "forbidden"`，`error.details.required_role === "admin"`；
- 后端会写审计 `security.rbac_denied`（含 method/path/required_role），前端不需要上报；
- 前端应在登录后读取 `GET /auth/me` 的 `role`，对非管理员**隐藏或禁用**上述写操作按钮；
  若仍收到 403，展示「需要管理员权限」，不要自动重试；
- 未登录/令牌无效仍是 401 `unauthorized`，与 403 区分处理。

---

## 6. A2A `authorization_url` 可能为 `null`

`GET /api/v1/tasks/{id}` 的 `auth_request.authorization_url` 类型为 `string | null`。后端对远程
Agent 返回的授权链接做白名单校验（仅 https 公网主机；dev/test 额外允许
`http://localhost`、`http://127.0.0.1`），未通过时置为 `null`，但任务本身继续运行，
`auth_required` 状态与 `auth_request.scopes`/`expires_at` 仍然可用。

前端要求：

- 先判空：`authorization_url` 为 `null` 时不渲染「去授权」链接，显示「授权链接不可用，请联系管理员/检查远程 Agent 配置」；
- 不要拼接、补全或猜测 URL，也不要把 `null` 当字符串 `"null"` 渲染；
- 授权动作（`POST /tasks/{id}/authorize`）本身不变，但仅管理员可调用；
- 历史任务的旧 URL 也会在详情返回时按当前配置重新过滤，因此旧页面同样可能出现 `null`。

---

## 7. 兼容性清单

- **旧聊天流不受影响**：`POST /chat/stream` 的请求/事件顺序/关闭语义与 M6 一致；M7 没有改动
  已发事件的字段（`approval.required` 只是新增字段）。
- **`/runs/{id}/events`**：新增 `event` 字段是向后兼容的；`payload` 中依旧没有 `_event`，
  已有解析逻辑无需改动，但建议逐步切到 `event`。
- **`/runs/{id}/stream`** 是新增端点，不接入不影响现有断线恢复（仍可用事件分页 + `GET /runs/{id}`）。
- **破坏性变更只有两处**：agents/tasks 写接口的 403（非管理员）与 `authorization_url` 可为 `null`；
  其余均为增量。
- **无数据库迁移**，无需前端/部署额外执行 `alembic upgrade`。
- 错误信封不变：`{"error": {"code", "message", "request_id", "details"?}}`；续流端点在建流前的
  4xx 也返回同一信封（`Content-Type: application/json`），不要按 SSE 解析。

## 8. 联调自测清单

- [ ] 未登录请求续流 → 401；用他人 run → 404；`after_seq=-1` → 422。
- [ ] 完整跑一轮聊天，断线后用最后 `seq` 续流，能拿到断线期间缺失的**已落库事件**与终态事件，且无重复帧；
      确认 `message.delta` 不会补发，终态后从 `GET /conversations/{id}/messages` 拉到的终稿完整。
- [ ] 收到 `message.done` 后停止重连（观察不再发新请求）。
- [ ] 触发审批：`approval.required` 能展示 5 个新字段；`run.interrupted` 后连接保持，收到 `: ping`
      不报错；审批通过后收到 `run.resumed` 与后续增量。
- [ ] 普通用户点击 agents/tasks 写操作 → 403 `forbidden` + `details.required_role="admin"`；
      管理员操作成功。
- [ ] 构造 `authorization_url=null` 的任务详情，页面不出现空链接/`"null"`。
- [ ] 400 `replay_too_large` 时走分页回放兜底，能补齐**已落库**历史后继续。
