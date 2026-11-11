# M8 前端交接：管理台（用户 / 概览 / 系统项）接入（2026-11-11）

> 后端范围：M8「管理台后端」（Task 1–6 已交付，Task 7 契约文档）。契约真相以
> [`docs/api.md`](../api.md) §1 / §2.12 / §7.6 / §12 与 [`docs/openapi.json`](../openapi.json) 为准；
> 本文件是前端接入清单，不替代 OpenAPI。
>
> M8 **无数据库迁移、无新增配置项/环境变量**；登录、令牌刷新与错误信封结构不变。
> 后端状态：**实施完成，待独立审查；前端尚未接入**。后端不交付任何前端页面。
>
> 新增 15 个 `/admin/*` 端点：全部要求 `users.role='admin'`，普通用户 403
> `forbidden` + `details.required_role="admin"`（未登录 401 `unauthorized`）。
> 管理端是**配置面**：`INSTRUCTIONS_ENABLED` / `SKILLS_ENABLED` / `MCP_ENABLED=false`
> 时这些端点仍可读写（R1）；运行态接口不受影响（仍 403 `capability_disabled`）。

## 1. 变更速览

| 变更 | 兼容性 | 前端动作 |
|---|---|---|
| 新增 15 个 `/admin/*` 端点（用户 3、概览 1、平台指令 2、系统技能 3、系统 MCP 4、全局 Agent 2） | 新能力（仅管理员） | 按 `/auth/me` 的 `role` 显示管理台入口；统一处理 403 |
| 用户管理：列表/详情/PATCH `role`/`status` | 新能力 | 用户页；自己的行禁用改角色/停用控件；映射 422/409 |
| 只读概览 `GET /admin/overview` | 新能力 | 管理首页统计卡；刷新间隔建议 ≥30s |
| 平台默认指令 `GET/PUT /admin/settings/instruction` | 新能力 | 指令配置页；`null` 恢复内置默认 |
| 系统技能列表 + 全局启停 + 强制停用 | 新能力 | 技能页两个开关；`force_disabled` 优先 |
| 系统 MCP 列表/新建/更新/删除 | 新能力 | MCP 页；`secret:NAME` 需先在 `/admin/secrets` 建密钥 |
| 全局 Agent 列表/PATCH | 新能力 | Agent 页；可编辑字段白名单；`secret:` 仅管理员 |
| 管理端配置面绕过能力总开关（R1） | 与运行态不同 | 管理页不要按 `/system/info` 的能力开关隐藏配置入口 |
| 全局 Agent 采用「全部未软删」注册表口径（R8） | 与立项 spec 字面不同 | 列表不要按 `owner_user_id` 过滤；后端已裁定 |
| 无迁移、无新环境变量、无新依赖 | 兼容 | 无需改登录/部署链路 |

---

## 2. 认证与 401 / 403 处理

### 2.1 请求头

```http
Authorization: Bearer <access_token>
```

- 未登录或 access token 过期：401 `unauthorized` → 走既有刷新逻辑；刷新失败回登录页。
- 已登录但 `role != "admin"`：403 `forbidden`，信封形如：

```json
{
  "error": {
    "code": "forbidden",
    "message": "需要管理员权限",
    "request_id": "7f3c9a2e-...",
    "details": {"required_role": "admin"}
  }
}
```

- 403 时后端还会写审计 `security.rbac_denied`（前端无需处理）。
- **不要**把 403 当作登录失效去循环刷新 token：只有 `error.code=="forbidden"` 且
  `error.details.required_role=="admin"` 时展示「需要管理员权限」并隐藏/禁用管理入口。
- 管理端配置接口正常不会返回 403 `capability_disabled`（R1）；若在其他接口遇到该 code，
  按「能力未启用」提示即可。

```ts
// 统一拦截器（示意）
async function adminFetch(path: string, init: RequestInit = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { ...(init.headers ?? {}), Authorization: `Bearer ${getAccessToken()}` },
  })
  if (res.status === 401) {
    await refreshAccessTokenOrRedirectToLogin()
    throw new Error('unauthorized')
  }
  if (res.status === 403) {
    const body = await res.json().catch(() => ({}))
    const err = body?.error ?? {}
    if (err.code === 'forbidden' && err.details?.required_role === 'admin') {
      showNoAdminPermission() // 隐藏管理台入口；不要刷新 token 重试
    } else if (err.code === 'capability_disabled') {
      showCapabilityDisabled()
    } else {
      showForbidden(err.message)
    }
    throw err
  }
  return res
}
```

> 管理入口建议同时按 `/auth/me` 返回的 `role` 做静态显隐；403 处理是兜底（角色可能中途被降级）。

---

## 3. 用户管理页

### 3.1 列表

```http
GET /api/v1/admin/users?q=alice&role=user&status=active&limit=20&cursor=<opaque>
```

| 参数 | 类型 | 说明 |
|---|---|---|
| `q` | string? | ≤100 字符；匹配邮箱或展示名，大小写不敏感，`%`/`_` 已被后端转义 |
| `role` | `user \| admin`? | 角色过滤 |
| `status` | `active \| disabled`? | 状态过滤 |
| `limit` | int | 1–100，默认 20 |
| `cursor` | string? | 上一页 `next_cursor`，不透明串，**不要解析/拼接** |

响应示例：

```json
{
  "items": [
    {
      "id": "0f6f5e2c-...-...",
      "email": "alice@example.com",
      "display_name": "Alice",
      "role": "user",
      "status": "active",
      "last_login_at": "2026-11-11T08:00:00Z",
      "created_at": "2026-11-01T02:00:00Z",
      "updated_at": "2026-11-11T08:00:00Z"
    }
  ],
  "next_cursor": "eyJjcmVhdGVkX2F0Ijoi...",
  "has_more": true,
  "stats": {"total": 3, "active": 2, "disabled": 1, "admins": 1}
}
```

- 排序固定 `created_at DESC, id DESC`；`has_more=false` 时 `next_cursor=null`。
- 非法/缺字段游标 → 422 `validation_error`（不要用本地值重试，回到第一页）。
- `stats` 是**当前筛选条件下**的全量计数（不含游标），不是当前页计数；
  `admins` 统计 `role='admin'` 的**所有状态**行（含停用管理员），不要用它反推「活跃管理员数」。
- 响应永不包含 `password_hash`。

### 3.2 详情

```http
GET /api/v1/admin/users/{user_id}
```

与列表项同形；不存在 404 `not_found`（message「用户不存在」）。

### 3.3 修改角色 / 状态

```http
PATCH /api/v1/admin/users/{user_id}
Content-Type: application/json

{"role": "user", "status": "disabled"}
```

- 只接受 `role`/`status`，至少一项；未知字段（如 `email`/`display_name`）→ 422 `validation_error`。
- 同值提交 → 200 返回当前值，**不产生审计**（后端 no-op）。
- 错误映射：

| HTTP / code | 触发 | 前端文案与动作 |
|---|---|---|
| 422 `cannot_change_own_role` | 修改自己的 `role` | 「不能修改自己的角色」；回滚控件并刷新该行 |
| 422 `cannot_disable_self` | 停用自己的 `status` | 「不能停用自己的账号」；回滚开关 |
| 409 `last_admin_protected` | 降级/停用后没有活跃管理员 | 「系统至少需要保留一名活跃管理员」；刷新列表与 `stats` |
| 422 `validation_error` | 空对象、未知字段、非法枚举 | 按字段错误提示 |
| 404 `not_found` | 用户不存在 | 从列表移除并提示 |

- **自己的行**：UI 应禁用「改角色」为 `user` 与「停用」控件（用 `/auth/me` 的 `id` 对比），
  避免用户点出 422。服务端仍是最终防线。
- 409 的 `error.details.active_admins` 为 `1`（仅保护触发时返回），可用于日志排查。
- 成功修改写审计 `admin.user.update`（`detail.fields/role/status`），前端无需展示。

### 3.4 新增管理员？

M8 **不提供**创建用户/重置密码/强制下线；仍由注册流程 + 运维脚本
`scripts/grant_admin.py` 设置管理员。用户页只做查询与角色/状态调整。

---

## 4. 只读概览页

```http
GET /api/v1/admin/overview
```

- 无 query 参数、无审计；响应字段树与完整示例见 [`api.md` §12.3](../api.md#123-只读概览)。
- 计数是**全局实时值**，不是当前用户/筛选值；所有状态键始终存在，无数据时为 `0`。
- 重要口径：
  - `users`/`conversations`/`messages`/`runs`/`a2a_tasks`/`knowledge` 含软删、失败、终态行；
  - `conversations.deleted` 单列，仍计入 `conversations.total`；
  - `users.admins` 统计所有状态的 `role='admin'`（含停用管理员）；
  - `attachments.total`/`storage_bytes` **排除** `deleted` 行；`deleted` 单独计数；
  - `runs.canceled` 是单 L 拼写（不是 `cancelled`）；`a2a_tasks` 为 8 个固定状态键。
- `generated_at` 为 UTC（`Z` 结尾），可直接展示「数据时间」。
- **刷新频率建议 ≥30s**（页面打开时拉一次 + 手动刷新按钮即可）：该接口执行 8 条精确聚合
  查询，O(表大小)，不支持秒级轮询；不要放在全局定时器里高频请求。
- 该接口只读，不写审计；403/401 处理同 §2。

---

## 5. 平台默认指令页

### 5.1 读取

```http
GET /api/v1/admin/settings/instruction
```

```json
{
  "content": "你是 Agent Hub 的主智能体……",
  "is_default": true,
  "max_chars": 4000,
  "user_override_count": 2,
  "updated_by": null,
  "updated_by_email": null,
  "updated_at": null
}
```

- `content` 即「无个人覆盖用户实际注入的文本」；不返回同义的 `effective_preview`（R5）。
- `is_default=true`：当前使用代码内置默认，没有 `system_settings` 覆盖；
  `updated_by(_email)`/`updated_at` 均为 `null`。
- `user_override_count`：写了个人指令的用户数（只写 `null` 的不计）。
- `max_chars`：正文上限，用于输入框计数/截断提示。

### 5.2 保存 / 恢复默认

```http
PUT /api/v1/admin/settings/instruction
Content-Type: application/json

{"content": "新的平台默认指令"}
```

```http
PUT /api/v1/admin/settings/instruction

{"content": null}
```

- `content` 是**必填字段**，但值可为 `null`；`null` 或纯空白字符串等价于「恢复内置默认」。
- 响应与 GET 同形：保存后 `is_default=false`、`updated_by(_email)`/`updated_at` 非空；
  恢复后 `is_default=true`、元信息回到 `null`。
- 超过 `max_chars` → 422 `instruction_too_long`，文案「指令超过 N 字符上限」。
- 该端点属于配置面：`INSTRUCTIONS_ENABLED=false` 时仍可读写（R1）；运行态
  `GET/PUT /api/v1/instructions` 仍按能力开关 403 `capability_disabled`。
- 审计 `instruction.update`（`detail.scope="platform"`，保存含长度/指纹、恢复含
  `restored_default`），**不含正文**；前端无需处理。

---

## 6. 系统技能页

```http
GET /api/v1/admin/skills?q=周报
```

- 只返回 `scope='system'` 的技能，按 `name ASC`；`q`（≤100）大小写不敏感匹配名称/描述；
  系统技能数量有上界，**不分页**。
- 响应：`{"items": [SkillOut...], "stats": {"total": 2, "enabled": 1, "force_disabled": 0}}`。
  `stats` 基于当前筛选结果（`total == len(items)`）。
- 列表项关键字段：`id/slug/name/description/version/source/enabled/force_disabled/
  effective_enabled/estimated_tokens/size_bytes/file_count/created_at/updated_at`。
- **两个开关语义不同**：

| 开关 | 端点 | 含义 |
|---|---|---|
| 全局启停 | `PUT /admin/skills/{id}/enabled` `{"enabled": bool}` | 该技能对所有人是否启用（系统级 `skills.enabled`） |
| 强制停用 | `PUT /admin/skills/{id}/force-disable` `{"force_disabled": bool}` | 最高优先级：为 `true` 时压过任何用户个人覆盖 |

- 管理端展示的 `effective_enabled = enabled && !force_disabled` 是**全局口径**，
  **不合并用户个人覆盖**；用户端 `/skills` 的 `effective_enabled` 可能因个人覆盖而不同，
  不要用管理端值推断某个用户的实际生效状态。
- 恢复 `force_disabled=false` 后，用户个人覆盖仍按原值生效（后端测试已固定该行为）。
- 私有技能/不存在 → 404 `not_found`（「系统技能不存在」，不泄露存在性）。
- 审计：`skill.enable`（`detail.enabled/scope="system"`）、`skill.force_disable`。
- `SKILLS_ENABLED=false` 时列表与两个写接口仍可用（R1）。

---

## 7. 系统 MCP 页

### 7.1 列表

```http
GET /api/v1/admin/mcp/servers
```

- 只返回 `scope='system'`，按 `created_at DESC`，**不分页**。
- `stats` 与用户侧同形：`{"total", "system", "user", "enabled", "healthy"}`；
  管理端 `user=0`、`total==system==len(items)`、`enabled` 按全局有效态计。
- 列表项 `McpServerOut`：`id/scope/slug/name/description/transport/url/enabled/auto_inject/
  force_disabled/effective_enabled/health_status/last_checked_at/last_error/latency_ms/
  tool_count/credentials_hint/created_at/updated_at`。
  **响应永远不含 `headers`/密文**；`credentials_hint` 形如 `Authorization: secret:MCP_KEY`。

### 7.2 新建表单

```http
POST /api/v1/admin/mcp/servers
Content-Type: application/json

{
  "name": "内部检索",
  "slug": "internal-search",
  "description": "公司内网检索 MCP",
  "transport": "streamable_http",
  "url": "https://mcp.example.com/mcp",
  "headers": {"Authorization": "secret:MCP_KEY"},
  "auto_inject": true
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | 是 | 1–128；纯空白由前端先 trim |
| `slug` | 是 | 2–64，`^[a-z0-9][a-z0-9-]{1,63}$`；**创建后不可改** |
| `description` | 否 | ≤2000 |
| `transport` | 否 | `streamable_http`（默认）/`sse`/`stdio`；**创建后不可改** |
| `url` | 远程必填 | ≤2048；后端做 SSRF/主机白名单校验 |
| `headers` | 否 | 敏感头用 `secret:NAME` 或 `env:NAME` 引用，**不要发明文** |
| `command`/`args` | stdio | `command` 必填且受 `MCP_STDIO_COMMAND_ALLOWLIST` 限制；`args` ≤32 |

- **`secret:NAME` 要求先在 `/admin/secrets` 创建同名平台密钥**；没有密钥时改用
  `env:ENV_NAME`（运行时读环境变量）。
- 成功 201；响应 `scope="system"`、`enabled=true`、`auto_inject=true`、
  `force_disabled=false`。
- **不可达仍会保存**：工具发现失败时仍返回 201，`health_status="unhealthy"`、
  `last_error` 截断、`tool_count=0`。UI 应提示「配置已保存，但连接失败：<last_error>」，
  不要因为 unhealthy 就丢弃表单或重复提交（会 409）。
- `slug` 在**任意 scope**（系统或任何用户自建）已存在 → 409 `conflict`；
  建议前端把 `slug` 当作全局唯一标识提示。

### 7.3 更新

```http
PATCH /api/v1/admin/mcp/servers/{id}
Content-Type: application/json

{"enabled": true, "force_disabled": false, "name": "内部检索（新）"}
```

- 可编辑：`name`/`description`/`url`/`headers`/`auto_inject`/`enabled`/`force_disabled`；
  至少一项；`slug`/`transport`/`command`/`args` 只读，提交 → 422 `validation_error`。
- 空对象 `{}` → 422；`name` 纯空白 → 422。
- **PATCH 没有同值 no-op 判定**：只要提交就写 `mcp.server.update` 审计并触发一次强制工具发现
  （即使字段值未变）。表单「保存」按钮建议只在有改动时可用，避免无意义刷新健康状态。
- `enabled=false` 或 `force_disabled=true` 都会让 `effective_enabled=false`；
  `force_disabled` 压过用户个人覆盖。
- 更新后同样可能 `unhealthy`（响应 200 但连接失败）；用 `last_error` 提示。
- `DELETE` → 204（清理该服务器的用户级启停覆盖，工具缓存级联删除）；再删 → 404。

### 7.4 错误码

| HTTP / code | 触发 | 前端动作 |
|---|---|---|
| 409 `conflict` | slug 已被任意 scope 占用 | 提示换 slug |
| 422 `mcp_ssrf_blocked` | URL 指向内网/回环/非白名单主机 | 提示 URL 不允许 |
| 422 `mcp_transport_not_allowed` | 传输被禁用（普通用户仅远程等） | 提示传输不可用 |
| 422 `secret_ref_invalid` | `secret:`/`env:` 引用格式错误 | 提示引用格式 |
| 422 `secret_ref_not_allowed` | 非管理员写 `secret:` | 管理端正常不会出现；按无权限提示 |
| 422 `validation_error` | 字段缺失/枚举/只读字段/空对象 | 按字段提示 |
| 503 `secret_store_unavailable` | 敏感头明文且未配置 `CAPABILITY_SECRET_KEY` | 提示改用 `secret:` 或 `env:` 引用 |
| 404 `not_found` | 系统 MCP 不存在（含用户自建 id） | 从列表移除 |

- `MCP_ENABLED=false` 时管理端仍可读写（R1）。
- 可选：用既有 `POST /api/v1/mcp/servers/{id}/check` 手动重试连接（受 `MCP_ENABLED` 影响）。

---

## 8. 全局 Agent 页

### 8.1 列表

```http
GET /api/v1/admin/agents?health_status=healthy&enabled=true&q=code&limit=20&cursor=<opaque>
```

- 查询参数与 `GET /agents` 完全一致：`health_status`（≤16）、`enabled`、`q`（≤100）、
  `limit`（1–100）、`cursor`（≤500）。
- 响应 `AgentPage`：`items` 为 `AgentOut`（字段见 [`api.md` §2.4](../api.md#24-远程智能体m2-已实现)），
  `stats={total, healthy, degraded, unhealthy, disabled, enabled}`。
- **全局注册表（R8）**：返回**所有未软删 Agent**，后端**不按 `owner_user_id` 过滤**。
  `owner_user_id` 只是保留字段、不参与可见性；管理员通过 `POST /agents` 创建的 Agent
  （owner 是管理员）同样会出现在列表。这与立项 spec §4.2 的 `owner_user_id IS NULL` 字面
  不同（已裁定，见交付报告与 `api.md` §12.7）；前端不要自行按 owner 过滤。

### 8.2 编辑

```http
PATCH /api/v1/admin/agents/{agent_id}
Content-Type: application/json

{"name": "代码助手（管理台）", "enabled": false, "requires_approval": true}
```

- 可编辑：`name`（1–128，纯空白 422）、`enabled`、`requires_approval`、
  `auth`（`{type, secret_ref?}`）、`headers`；至少一项。
- 只读（提交即 422 `validation_error`）：`slug`、`url`、`description`、`protocol_version`、
  `card`、`skills`、`capabilities`、`health_status`、`owner_user_id`。
  `description` 由 Agent Card 派生且 `refresh` 会覆盖（R7）。
- 改名后 `name_edited=true`：后续 `POST /agents/{id}/refresh` 不会用 Card 名称覆盖；
  UI 可提示「该名称已由管理台固定」。
- `auth.secret_ref`/`headers` 支持 `secret:NAME`（**仅管理员**；普通用户仍只能 `env:`）；
  `secret:NAME` 需先在 `/admin/secrets` 创建。响应只回 `auth_type`/`auth_scheme`/`auth_hint`，
  不回 `headers`/明文/密文。
- 只有真实变化才写 `agent.updated`（`detail.source="admin"`）；同值 PATCH 200 但不写审计。
- 不存在或已软删 → 404 `not_found`。
- 已知边界（后端延后处理）：清空 `headers`/`auth` 时旧 `auth_hint` 文本可能残留；
  UI 不要把它当作当前凭据的强校验依据。

---

## 9. 错误码与 OpenAPI

| HTTP | code | 说明 | 前端动作 |
|---|---|---|---|
| 401 | `unauthorized` | 未登录/令牌无效 | 刷新或登录 |
| 403 | `forbidden` | 非管理员（`details.required_role="admin"`） | 隐藏管理入口，提示无权限；不要刷新 token 重试 |
| 403 | `capability_disabled` | 能力总开关关闭（运行态接口） | 提示能力未启用 |
| 404 | `not_found` | 资源不存在 | 从列表移除/提示 |
| 409 | `conflict` | 状态冲突（如系统 MCP slug 占用） | 提示换值 |
| 409 | `last_admin_protected` | 最后一个活跃管理员保护（`details.active_admins=1`） | 提示保留管理员并刷新 |
| 422 | `validation_error` | 参数/请求体校验失败 | 按字段提示 |
| 422 | `cannot_change_own_role` | 不能改自己角色 | 回滚并提示 |
| 422 | `cannot_disable_self` | 不能停用自己 | 回滚并提示 |
| 422 | `instruction_too_long` | 指令超长 | 提示 `max_chars` |
| 422 | `mcp_ssrf_blocked` / `mcp_transport_not_allowed` / `secret_ref_invalid` / `secret_ref_not_allowed` | MCP/凭据校验失败 | 按字段提示 |
| 503 | `secret_store_unavailable` | 敏感头明文且未配置密钥 | 改用 `secret:`/`env:` 引用 |
| 503 | `service_unavailable` / `database_unavailable` | 依赖不可用 | 通用重试提示 |

- 机器可读契约：[`docs/openapi.json`](../openapi.json)；人读契约：
  [`docs/api.md` §12](../api.md#12-m8-实现状态2026-11-11)。
- 判断逻辑请用 `error.code`（字符串契约），不要解析 `message` 文案。
- 共享凭据校验的 503 **code 变化**：M8 审查修复把「敏感头明文且无 `CAPABILITY_SECRET_KEY`」
  的错误码从 `service_unavailable` 统一为 `secret_store_unavailable`（HTTP 状态仍 503，
  与 `/admin/secrets` 一致）。若前端按旧 code 判断需同步。

---

## 10. 兼容性、迁移与配置

- **无数据库迁移**：迁移 head 仍为 `2d8f6a1c3e70`，未新增表/列/索引；
- **无新增配置项/环境变量**：`.env.example` 与 `Settings` 零变更；系统 MCP/Agent 的
  `secret:` 引用依赖既有 `CAPABILITY_SECRET_KEY` 与 `/admin/secrets`；
- **无新增第三方依赖**；
- 登录、令牌刷新、错误信封、`X-Request-ID` 均不变；
- 既有接口行为不变；唯一共享代码的行为变化是上述 503 code 更精确（HTTP 状态不变）；
- 管理端配置面在能力关闭时仍可用（R1）；运行态行为不变；
- M8 不交付前端页面本身，前端接入后需自行联调验收。

---

## 11. curl 速查

```bash
BASE=http://localhost:8000/api/v1
TOKEN='<access_token>'   # 先 POST /auth/login 获取；示例值不得使用真实密钥

# 登录
curl -s "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d '{"email":"admin@example.com","password":"<password>"}'

# 用户列表 / 详情 / 改状态
curl -s "$BASE/admin/users?role=user&limit=20" -H "Authorization: Bearer $TOKEN"
curl -s "$BASE/admin/users/<user_id>" -H "Authorization: Bearer $TOKEN"
curl -s -X PATCH "$BASE/admin/users/<user_id>" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"status":"disabled"}'

# 概览
curl -s "$BASE/admin/overview" -H "Authorization: Bearer $TOKEN"

# 平台指令：保存 / 恢复内置默认
curl -s -X PUT "$BASE/admin/settings/instruction" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"content":"平台默认：先结论后依据"}'
curl -s -X PUT "$BASE/admin/settings/instruction" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"content":null}'

# 系统技能
curl -s "$BASE/admin/skills?q=周报" -H "Authorization: Bearer $TOKEN"
curl -s -X PUT "$BASE/admin/skills/<skill_id>/force-disable" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"force_disabled":true}'

# 系统 MCP：先建平台密钥，再引用
curl -s -X POST "$BASE/admin/secrets" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"name":"MCP_KEY","value":"<placeholder>"}'
curl -s -X POST "$BASE/admin/mcp/servers" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"内部检索","slug":"internal-search","transport":"streamable_http",
       "url":"https://mcp.example.com/mcp","headers":{"Authorization":"secret:MCP_KEY"}}'

# 全局 Agent
curl -s "$BASE/admin/agents?limit=20" -H "Authorization: Bearer $TOKEN"
curl -s -X PATCH "$BASE/admin/agents/<agent_id>" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"enabled":false,"requires_approval":true}'
```

> 以上示例值均为占位；不要提交真实密钥。`secret:NAME` 引用需要先创建同名平台密钥，
> 或用 `env:ENV_NAME` 引用环境变量。

---

## 12. 联调自测清单

- [ ] 普通用户打开管理台入口被隐藏；直接请求 `/admin/users` 返回 403 `forbidden` +
      `details.required_role="admin"`，页面提示无权限且**不**循环刷新 token；
- [ ] 管理员用户列表：筛选 `q/role/status`、`stats` 随筛选变化、`limit=1` 翻页无重复、
      非法 `cursor` 返回 422 后回到第一页；
- [ ] 自己的行不能改角色/停用；尝试时后端返回 422 对应 code，控件已提前禁用；
- [ ] 把最后一个活跃管理员降级/停用 → 409 `last_admin_protected`，列表与 `stats` 刷新；
- [ ] 概览字段完整（含 8 个 A2A 状态键）、空数据为 0、`generated_at` 可展示；
      刷新间隔 ≥30s；
- [ ] 平台指令：保存后 `is_default=false`、展示 `updated_by_email`；恢复 `null` 后
      `is_default=true`、元信息为 `null`；超长提示 422 `instruction_too_long`；
- [ ] 系统技能：两个开关均生效；`force_disabled=true` 时 `effective_enabled=false`；
      恢复后用户覆盖仍生效（可用普通用户账号验证）；
- [ ] 系统 MCP：`secret:NAME` 需先建密钥；创建后即使 `unhealthy` 也保存成功并展示
      `last_error`；PATCH 只暴露白名单字段；slug 冲突提示 409；
- [ ] 全局 Agent：列表包含管理员创建的 Agent（R8）；只读字段提交返回 422；改名后
      执行 `refresh` 名称不被覆盖；`auth`/`headers` 只显示 hint、不显示明文；
- [ ] 能力开关关闭（如 `SKILLS_ENABLED=false`）时管理端技能页仍可读写，普通用户运行态
      接口仍 403 `capability_disabled`（如需可请后端配合改环境验证）；
- [ ] 全站错误信封解析统一用 `error.code`；401/403 分支与旧页面行为一致。
