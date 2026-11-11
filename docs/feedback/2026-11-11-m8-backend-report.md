# M8 管理台后端交付报告

- 日期：2026-11-11
- 范围：M8 管理台后端（原 X2，Task 1–8，**不含前端页面**）
- 基线：M7 收尾 **760 passed, 1 skipped**，迁移 head `2d8f6a1c3e70`，OpenAPI 与代码一致
- 设计：[M7–M9 立项](../superpowers/specs/2026-11-11-m7-m9-backend-roadmap-design.md) §4
- 计划：[M8 实施计划](../superpowers/plans/2026-11-11-m8-admin-console.md)
- 前端交接：[M8 前端交接](2026-11-11-m8-frontend-handoff.md)
- 最终审查修复：`504cd39 fix: 修复最后管理员并发缺口与管理台 no-op 语义（M8 最终审查修复）`
- 状态：**已完成（2026-11-11，独立审查通过）**；M8 前端接入尚未开始
  （本文不构成前端已集成的声明）

**目标**：在不新增数据库迁移的前提下，为管理控制台交付真实契约：用户列表/详情/角色与状态管理、
只读概览、平台默认指令、系统技能、系统 MCP、全局 Agent 的查询与管理；复用 M7 的 `AdminUser`
门禁、既有领域服务、统一错误信封与游标分页，并同步 OpenAPI / `docs/api.md` / 前端交接。

## 1. 交付内容

| 任务 | 文件 | 说明 |
|---|---|---|
| Task 1 路由骨架 + 用户管理 | `app/modules/admin/{__init__,router,schemas,service}.py`、`app/api/v1/router.py`、`tests/integration/test_admin_users.py`（14 项） | `GET /admin/users`（`q/role/status/limit/cursor` + `stats`，排序 `created_at DESC, id DESC`，LIKE 通配符转义）、`GET /admin/users/{id}`、`PATCH /admin/users/{id}`；自操作 422 `cannot_change_own_role`/`cannot_disable_self`、最后活跃管理员 409 `last_admin_protected`（行锁 + 锁后重算）；审计 `admin.user.update`；`AdminUserOut` 不含 `password_hash` |
| Task 2 只读概览 | `app/modules/admin/schemas.py`、`admin/service.py`、`admin/router.py`、`tests/integration/test_admin_overview.py`（5 项） | `GET /admin/overview` 固定 8 组字段；`build_overview` 每表一条聚合 SQL（共 8 条）、不加载 ORM 行；附件排除 `deleted`、状态键恒在；测试统计 SELECT 上界（实测 9 ≤ 10）；无审计（新增「连续两次请求不产生审计」用例） |
| Task 3 平台默认指令 | `app/modules/instructions/service.py`、`admin/{schemas,service,router}.py`、`tests/integration/test_admin_instructions.py`（11 项） | `GET/PUT /admin/settings/instruction`；复用 `system_settings["instruction.default"]`（兼容 `{"content": ...}` 与裸字符串）；`content=null`/空白恢复内置默认（无覆盖行时 no-op、不写审计）；超长 422 `instruction_too_long`；审计 `instruction.update`（`detail.scope="platform"`，不落正文）；`INSTRUCTIONS_ENABLED=false` 仍可读写（R1）；`updated_at` 手动刷新（无 `onupdate`） |
| Task 4 系统技能 | `app/modules/skills/service.py`、`admin/{schemas,router}.py`、`tests/integration/test_admin_skills.py`（12 项） | `GET /admin/skills?q=`（仅 `scope='system'`、`name ASC, id ASC`、不分页、`q` 转义）、`PUT .../enabled`、`PUT .../force-disable`；`effective_enabled = enabled and not force_disabled`，`force_disabled` 压过用户覆盖；同值启停 no-op（不写审计）；私有技能/不存在 404；审计 `skill.enable`/`skill.force_disable`；R1 |
| Task 5 系统 MCP | `app/modules/mcp/schemas.py`、`mcp/service.py`、`admin/router.py`、`tests/integration/test_admin_mcp.py`（13 项） | `GET/POST/PATCH/DELETE /admin/mcp/servers`；只操作 `scope='system'`；复用 `_validate_create`/`normalize_headers(allow_platform_secret=True)`/`_server_out`/`_check_and_sync`；创建拒绝纯空白 `name`；slug 任意 scope 冲突 409；`secret:`/`env:` 引用；工具发现失败仍保存（`unhealthy`）；PATCH 同值 no-op（不审计、不触发发现），真实变更才审计 + 强制发现；删除清理覆盖 + 工具级联；503 `secret_store_unavailable`；R1 |
| Task 6 全局 Agent | `app/modules/agents/schemas.py`、`agents/service.py`、`admin/router.py`、`tests/integration/test_admin_agents.py`（10 项） | `GET /admin/agents`（query 同 `GET /agents`，全局注册表口径 R8；**stats 忽略 cursor** 保持筛选全量）、`PATCH /admin/agents/{id}`；可编辑 `name/enabled/requires_approval/auth/headers`，只读字段 `extra="forbid"` 422；`name_edited` 保护；`secret:` 仅管理员；`agent.updated`（`detail.source="admin"`）；同值不写审计；清空 `auth`（none/oauth2）或 `headers` 时同步清空 `auth_hint` |
| 审查修复轮 1 | `app/core/ssrf.py`、`mcp/schemas.py`、`agents/schemas.py`、两个管理端测试文件 | 503 错误码统一为 `secret_store_unavailable`（HTTP 状态不变）；管理端 PATCH `name` 纯空白 422；补 404 scope 边界与软删 404 覆盖（+3 条独立用例） |
| 最终审查修复轮 | `app/modules/admin/service.py`、`admin/router.py`、`app/modules/{agents,instructions,mcp,skills}/service.py`、`mcp/schemas.py`、`app/api/{schemas.py,v1/router.py}`、6 个管理端测试文件 | 最后管理员并发缺口（所有 PATCH 先锁 active admins + 目标行，锁后重算 changed/remaining）；管理端 no-op 语义统一；`GET /admin/agents` stats 忽略 cursor；Agent `auth_hint` 清空；MCP 创建空白 name 拒绝；OpenAPI 管理端 403/409/422/503 与 `ErrorBodyOut.code` 描述补全；+15 条测试 |
| Task 7 契约文档 / OpenAPI / 交接 | `docs/api.md`、`docs/openapi.json`、`docs/feedback/2026-11-11-m8-frontend-handoff.md`（新增）、`README.md`、`docs/roadmap.md`、`docs/architecture.md`、`docs/database.md` | `docs/api.md` §1 错误码、§2.12 管理端真实路由、§7.6 分页补充、新增 §12 M8 实现状态；OpenAPI 重新导出；前端 handoff（认证/403、用户/概览/系统项页面、no-op 语义、错误码、curl、自测清单）；README/roadmap 标记完成（最终收口），architecture 增管理面小节、database 注明无迁移 |
| Task 8 全量回归与报告 | `docs/feedback/2026-11-11-m8-backend-report.md`（本文件） | 全量回归、空库迁移验证、证据归档、R1–R10 与 R8 偏差、遗留项、spec §4.4 验收对照；最终收口只更新文档 + 修一条测试行宽，无业务逻辑改动 |

数据库：M8 **无迁移**，未新增/修改表、列或索引，迁移 head 保持 `2d8f6a1c3e70`；
`Settings` 与 `.env.example` **零变更**（无新增配置项/环境变量）。

## 2. 实测证据

### 2.1 全量验证

2026-11-11 最终收口执行，复用 `dev_postgres`（localhost:54321），业务测试库为 `agent_hub_test`
（由 `.env` 的开发库 `agent_hub` 派生，**不触碰开发库数据**）：

```text
$ TEST_DATABASE_URL="$(grep '^DATABASE_URL=' .env | cut -d= -f2- | sed 's#/agent_hub$#/agent_hub_test#')" \
    .venv/bin/python -m pytest -q
825 passed, 1 skipped in 90.56s (0:01:30)

$ .venv/bin/python -m ruff check .
All checks passed!

$ .venv/bin/python -m alembic check
No new upgrade operations detected.

$ .venv/bin/python -m alembic heads
2d8f6a1c3e70 (head)

$ .venv/bin/python -m alembic current
2d8f6a1c3e70 (head)

$ .venv/bin/python scripts/export_openapi.py && git diff --exit-code docs/openapi.json
已导出 /home/zhaodan/work/PythonProjects/agent-hub/docs/openapi.json
（git diff 退出码 0：无差异）

$ git status --short alembic/versions
（空：无新迁移文件）
```

- 基线 M7 **760 项** → M8 **825 项**（+65：Task 1–6 功能用例 50 条 + 最终审查修复 15 条；
  唯一 skip 仍是知识库语义评测组，需真实 Embedding key，与 M4–M7 一致）；
- ruff 全绿；`alembic check` 无漂移、head 不变（无迁移）；OpenAPI 重新导出后无 diff；
- 测试库 `agent_hub_test` 与开发库 `agent_hub` 分离；`alembic` 检查读取开发库（只读）验证
  迁移与模型一致，未写入测试数据；另做了空库迁移验证（§2.5）。

### 2.2 关键用例

| 用例文件 | 数量 | 覆盖 |
|---|---|---|
| `tests/integration/test_admin_users.py` | 14 | 列表筛选/`stats`/游标翻页、`q` 通配符与反斜杠转义、详情/404、角色/状态 PATCH + 审计、自改角色 422、自停用 422、最后活跃管理员 409、仍有两个活跃管理员时可停用、3 端点 403 + `details.required_role` + 401、非法游标/请求体 422、**并发「提升+停用」与「互降」各 1 条真实并发回归**、停用管理员计入 `stats.admins` |
| `tests/integration/test_admin_overview.py` | 5 | 空库叶子计数为 0（含 8 个 A2A 状态键）、落库数据逐项计数（含软删会话/附件、失败文档、终态 run）、SELECT ≤10（`before_cursor_execute` 实测 9）、**成功请求不写审计**、普通用户 403 + 401 |
| `tests/integration/test_admin_instructions.py` | 11 | 内置默认读取、历史两种形态、PUT 后用户端生效跟随、`null` 恢复、**无覆盖行恢复为 no-op 且不写审计**、**第二次 PUT 严格刷新 `updated_at`**、个人覆盖计数、超长/多余字段 422、审计不含正文、403/401、能力关闭仍可配置（R1） |
| `tests/integration/test_admin_skills.py` | 12 | 仅系统技能/名称升序/**同名按 id tie-break**/`q` 命中与**反斜杠转义**、`q` 超长 422、全局启停、**同值启停 no-op 不写审计**、`force_disabled` 压过用户覆盖且恢复后覆盖仍在、私有技能 404、写校验 422、审计、403/401、R1 |
| `tests/integration/test_admin_mcp.py` | 13 | 创建 + 工具发现、不可达仍保存、`secret:` 不回显（响应/审计/DB）、SSRF/重复/跨 scope slug/缺 url、**PATCH 同值 no-op（不审计/不发现）**、**PATCH URL SSRF 拦截不落库**、PATCH 字段 + force_disable + 改 URL 重新发现、删除清理、404 scope 边界、空白 name 422、503 `secret_store_unavailable`（POST/PATCH）、403/401、R1 |
| `tests/integration/test_admin_agents.py` | 10 | 全局注册表（管理员创建仍可见）、**管理端 stats 忽略 cursor / 用户侧口径不变**、PATCH 字段 + 审计 + refresh 不覆盖改名、`secret:`/headers 不回显、**清空 auth/headers 时 auth_hint 清空**、9 个只读字段 422、空体/404/软删 404/空白 name、403/401 |

六个文件合计 **65 条**（T1 14 + T2 5 + T3 11 + T4 12 + T5 13 + T6 10）。

### 2.3 最终审查修复（2026-11-11，`504cd39`）

最终审查只剩 1 个 Minor（测试行宽）与文档收口；此前的复审修复已把最后管理员并发缺口补上：

1. **最后活跃管理员并发缺口（Important，已修）**：旧实现只在「降级/停用活跃管理员」时加锁，
   存在「提升普通用户为 admin」与「停用该用户」交错时两事务都基于陈旧快照、最终 0 活跃管理员的
   窗口。现在**所有管理端用户 PATCH 都先按 `id ASC` 锁全部活跃管理员行、再对目标行
   `WITH FOR UPDATE` refresh**，然后重算 `changed` 与 `remaining`：
   - 无实际变化：取快照后 `rollback` 释放行锁，不写审计、不提交；
   - 移除活跃管理员且 `remaining == 0`：409 `last_admin_protected`；
   - 新增 2 条真实并发回归：`test_concurrent_promote_then_disable_keeps_an_active_admin`、
     `test_concurrent_mutual_demotion_keeps_one_active_admin`（断言恰好一个成功、一个 409，
     最终始终剩 1 个活跃管理员）。
2. **管理端 no-op 语义统一**：
   - 平台指令恢复默认：无覆盖行时直接返回内置默认，**不写审计**；
   - 系统技能启停/强制停用：同值直接返回当前视图，不写审计、不提交；
   - 系统 MCP PATCH：逐字段比较后判定，同值直接返回当前视图，**不写 `mcp.server.update`、
     不触发 `_check_and_sync`**；
   - 用户 PATCH：锁后重算 changed，同值不写审计不提交；
   - Agent PATCH：同值不写审计（原有）；`auth_hint` 在清空 `auth`（none/oauth2）或 `headers={}`
     时同步清空，不再残留旧凭据提示。
3. **`GET /admin/agents` 的 stats 口径**：管理端翻页时 `stats` 是筛选条件下的**全量计数**，
   不随 cursor 变小；用户侧 `GET /agents` 保持既有口径（`stats` 随页内过滤），通过
   `list_agents(stats_ignore_cursor=True)` 区分。
4. **MCP 创建空白 name**：`McpServerCreateRequest.name` 先 strip 再判空，纯空白 422
   （用户侧与管理端 MCP 创建共用该 schema）。
5. **OpenAPI 错误描述补全**：管理端路由统一覆盖 403 描述
   （`forbidden` + `details.required_role="admin"`）；公共 403/409/422/503 描述补
   `last_admin_protected`、管理台业务 422 码、`secret_store_unavailable`；
   `ErrorBodyOut.code` 描述补管理台错误码。JSON 语义对比 T7 导出：**无 path/schema
   结构变化**，仅 99 个操作与 `ErrorBodyOut` 的 description 变化。
6. **测试行宽**：`tests/integration/test_admin_overview.py` 中一条 104 字符的列表推导换行，
   满足 ruff `line-length=100`。
7. 新增 **15 条**回归用例（六个文件由 50 → 65 条），全量 810 → **825 passed**。

### 2.4 契约与 OpenAPI

- OpenAPI：导出后 **74 个 path、148 个 schema**；`/api/v1/admin` 下 14 个 path 组、
  **20 个操作**（M8 新增 15 个 + 既有 `audit-logs` 1 + `secrets` 4），全部声明 403 响应；
  M8 的 15 个操作 403 描述统一为
  `需要管理员权限（forbidden，details.required_role="admin"）`；
- 语义对比 T7 导出：**无新增/删除 path 与 schema，无结构变化**；仅公共错误响应
  （403/409/422/503）与 `ErrorBodyOut.code` 的描述增强（99 个操作 description-only 变化）；
- `AdminUserOut` 无 `password_hash`；`AdminUserPatchRequest.additionalProperties=false`；
  `AdminInstructionOut` 7 个字段全部 required 且无 `effective_preview`；
  `AdminOverviewOut` 8 个顶层字段全部 required；`AdminMcpServerUpdateRequest` 恰为
  7 个可编辑字段；`AdminAgentUpdateRequest` 恰为 5 个可编辑字段；
- `docs/api.md`：§1 错误码表、§2.12 管理端真实路由、§7.6 分页补充、§12（端点总表、
  用户规则、概览字段与口径、平台指令、系统技能、系统 MCP、全局 Agent/R8、审计动作、
  no-op 语义、验证与迁移）；
- 前端 handoff：`docs/feedback/2026-11-11-m8-frontend-handoff.md`（认证/403、页面接入、
  no-op 语义、错误码映射、curl 速查与联调自测清单）。

### 2.5 空库迁移验证（T8）

按计划的「空库 upgrade head」做法，在 `dev_postgres` 上临时创建空库 `agent_hub_m8_mig`
（psycopg 连接 `postgres` 库执行 `CREATE DATABASE`，未新起容器），执行后已 `DROP DATABASE`：

```text
$ DATABASE_URL="postgresql+psycopg://devuser:***@localhost:54321/agent_hub_m8_mig" \
    .venv/bin/python -m alembic upgrade head
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO  [alembic.runtime.migration] Running upgrade  -> 45f5db481e8c, m1 core tables
INFO  [alembic.runtime.migration] Running upgrade 45f5db481e8c -> 7f3a9c21b4d6, m15 personal capability tables
INFO  [alembic.runtime.migration] Running upgrade 7f3a9c21b4d6 -> 8a2f7c31e9d5, m2 a2a outbound tables
INFO  [alembic.runtime.migration] Running upgrade 8a2f7c31e9d5 -> c4e7a91b2f36, m4 knowledge tables
INFO  [alembic.runtime.migration] Running upgrade c4e7a91b2f36 -> 9c1d4e7a2b60, m5 platform secrets
INFO  [alembic.runtime.migration] Running upgrade 9c1d4e7a2b60 -> 2d8f6a1c3e70, m5 approvals tool_call_id

$ DATABASE_URL="...agent_hub_m8_mig" .venv/bin/python -m alembic check
No new upgrade operations detected.

$ DATABASE_URL="...agent_hub_m8_mig" .venv/bin/python -m alembic heads
2d8f6a1c3e70 (head)
```

- 空库可从 0 一路 upgrade 到 head，`alembic check` 无漂移，`heads` 仍为 `2d8f6a1c3e70`；
- **测试库 `agent_hub_test` 不执行 upgrade**（它是 pytest `create_all` 建的），避免污染；
- 临时库已在验证后删除（先 `pg_terminate_backend` 断开连接，再 `DROP DATABASE`）。

## 3. 关键决策与裁定（R1–R10）

| # | 决策 | M8 实现 |
|---|---|---|
| R1 | 管理端配置面不受能力总开关影响 | 平台指令/系统技能/系统 MCP 的管理路径不调用各模块 `_require_enabled()`，也不走带开关门禁的可见性查询；`INSTRUCTIONS_ENABLED`/`SKILLS_ENABLED`/`MCP_ENABLED=false` 时仍可读写，运行态接口行为不变；测试固定 |
| R2 | 用户 PATCH 只改 `role`/`status` | `AdminUserPatchRequest` `extra="forbid"` 且至少一项；`email/display_name/password` 不在 M8 |
| R3 | 禁止自我锁死 | 改自己角色 422 `cannot_change_own_role`、停用自己 422 `cannot_disable_self`（比 spec 仅禁改角色更严）；同值自 PATCH 是 200 no-op |
| R4 | 最后活跃管理员 | 409 `last_admin_protected` + `details.active_admins=1`；**所有**用户 PATCH 先锁 `role='admin' AND status='active'` 行（`ORDER BY id FOR UPDATE`）、再锁目标行并 refresh，锁后重算 changed/remaining；自操作规则优先；新增 2 条真实并发回归 |
| R5 | 不返回 `effective_preview` | 平台默认 `content` 即无个人覆盖用户实际注入的文本；仅返回 7 个字段 |
| R6 | 系统技能/MCP 不分页；用户详情不带活动计数 | 系统项数量有上界，列表一次返回 + `stats`；用户详情与列表项同形 |
| R7 | 全局 Agent 可编辑白名单 | `name/enabled/requires_approval/auth/headers`；`description` 由 Card 派生且 refresh 会覆盖，只读；`slug/url/protocol_version/card/skills/capabilities/health_status/owner_user_id` 全部 422 |
| R8 | 全局 Agent 口径 | **全部未软删**，不按 `owner_user_id IS NULL` 过滤（与 spec §4.2 字面不一致，详见 §3.1；仍待产品最终确认） |
| R9 | 审计过滤不扩展 | `/admin/audit-logs` 保持 M5 的 `actor_type/actor_id/action/resource_type/resource_id/start/end/cursor`；`request_id`/全文搜索留后续（仍待产品确认） |
| R10 | 审计动作命名 | `admin.user.update`、`instruction.update`（`scope=platform`）、`skill.enable`、`skill.force_disable`、`mcp.server.create/update/delete/check`、`agent.updated`（`source=admin`）；`detail` 只放字段名/状态值/长度/指纹/计数；同值 no-op 不写审计 |

### 3.1 R8 偏差：全局 Agent 注册表口径

- **spec 字面**（§4.2）：`GET/PATCH /admin/agents` 复用 `remote_agents.owner_user_id IS NULL`
  的全局注册表。
- **代码现状**：v1 `remote_agents` 本就是**全局注册表**——`GET /agents` 对所有登录用户返回
  全部未软删行；`POST /agents` 创建时写 `owner_user_id=当前管理员`。若按 `owner_user_id IS NULL`
  过滤，所有现存 Agent 与管理员新建的 Agent 都会在管理端“消失”。
- **M8 裁定**：管理端返回**全部未软删 Agent**，`owner_user_id` 仅作保留字段、不参与可见性；
  测试 `test_admin_agents_covers_global_registry` 先断言创建行 `owner_user_id==admin.id`，
  再断言 `GET /admin/agents` 可见且 `stats.total==1`，固定该口径。
- **性质**：这是 spec 字面与 v1 数据模型/既有 `GET /agents` 行为的偏差，**不是 bug**，
  仍待产品最终确认。若坚持 NULL 口径，需同时修改 `create_agent` 写 NULL、对历史行做数据回填
  （脚本或迁移），会突破 M8「无迁移」前提，应单独立项。已写入 `docs/api.md` §12.7、handoff §8。

## 4. 前端交接索引

| 主题 | 交接位置 |
|---|---|
| 变更速览与兼容性 | [handoff §1](2026-11-11-m8-frontend-handoff.md#1-变更速览) |
| 认证、401/403 拦截器 | [handoff §2](2026-11-11-m8-frontend-handoff.md#2-认证与-401--403-处理) |
| 用户页（筛选/游标/stats/自操作/错误码） | [handoff §3](2026-11-11-m8-frontend-handoff.md#3-用户管理页) |
| 概览页（字段/口径/刷新 ≥30s） | [handoff §4](2026-11-11-m8-frontend-handoff.md#4-只读概览页) |
| 平台指令页（null 恢复/is_default/updated_by_email） | [handoff §5](2026-11-11-m8-frontend-handoff.md#5-平台默认指令页) |
| 系统技能页（两个开关/force_disabled 优先级/no-op） | [handoff §6](2026-11-11-m8-frontend-handoff.md#6-系统技能页) |
| 系统 MCP 页（表单/`secret:`/PATCH/no-op/unhealthy/503） | [handoff §7](2026-11-11-m8-frontend-handoff.md#7-系统-mcp-页) |
| 全局 Agent 页（可编辑/只读/R8/stats 口径） | [handoff §8](2026-11-11-m8-frontend-handoff.md#8-全局-agent-页) |
| 错误码表与 OpenAPI 链接 | [handoff §9](2026-11-11-m8-frontend-handoff.md#9-错误码与-openapi) |
| 无迁移、无新环境变量 | [handoff §10](2026-11-11-m8-frontend-handoff.md#10-兼容性迁移与配置) |
| curl 速查 / 联调自测清单 | [handoff §11](2026-11-11-m8-frontend-handoff.md#11-curl-速查)、[§12](2026-11-11-m8-frontend-handoff.md#12-联调自测清单) |

## 5. 规格 §4.4 验收清单

| 验收项 | 状态 | 证据 |
|---|---|---|
| 管理员可查用户、改角色/状态；「不能改自己」「不能禁用/降级最后一个活跃 admin」有测试 | **已验证（后端）** | `test_admin_users.py` 14 项：HTTP 422 `cannot_change_own_role`/`cannot_disable_self`、409 `last_admin_protected` + `details.active_admins=1`；2 条真实并发回归覆盖「提升+停用」与「互降」；全量 PATCH 统一行锁 |
| 平台指令、系统技能、系统 MCP、全局 Agent 可读可改；`force_disabled` 压过用户覆盖 | **已验证（后端）** | `test_admin_instructions.py` 11 项、`test_admin_skills.py` 12 项（覆盖压制 + no-op）、`test_admin_mcp.py` 13 项（覆盖压制 + no-op + SSRF）、`test_admin_agents.py` 10 项（auth_hint 清理） |
| 普通用户访问任何新增 `/admin/*` 返回 403 `forbidden` + `details.required_role="admin"`；未登录 401 | **已验证（后端）** | 六个测试文件均含 403/401 断言；OpenAPI 20 个 admin 操作全部声明 403，M8 的 15 个操作 403 描述显式含 `details.required_role="admin"`；`security.rbac_denied` 由 M7 `require_admin` 写入 |
| 关键写操作全部写审计；`GET /admin/overview` 返回计数且无 N+1/ORM 行加载 | **已验证（后端）** | 用户/指令/技能/MCP/Agent 的审计断言 + no-op 不写审计断言；`mcp.server.check` 工具发现审计；`test_overview_uses_bounded_queries` 实测 9 条 SELECT（8 聚合 + 鉴权 1）；`test_overview_does_not_write_audit` 连续两次无记录 |
| OpenAPI / `docs/api.md` / 前端 handoff / README / roadmap 同步；无迁移；全量测试与 ruff 全绿 | **已验证（后端）** | OpenAPI 导出后无 diff，管理端 403/错误码描述已补；`docs/api.md` §12；handoff；README/roadmap/architecture/database 已同步；**825 passed/1 skipped**、ruff 全绿；空库 `alembic upgrade head`/`check` 通过、head `2d8f6a1c3e70` |
| 前端页面接入与联调 | **待前端（不在后端范围）** | 后端交付 15 个端点与 handoff；前端页面/联调尚未开始，本文不声明前端已集成 |
| 里程碑独立审查 | **已通过（2026-11-11）** | Task 1–8 任务级审查 + 全分支复审完成；最终修复轮 `504cd39`（并发缺口 + no-op 语义 + OpenAPI 描述 + 15 条测试），复审剩余 1 个 Minor 行宽已在收口提交修复；全量 825 passed |

## 6. 已知缺口、遗留与延后项

**与 spec/计划的偏差（待产品确认，不是 bug）**

1. **R8 全局 Agent 口径**：管理端返回「全部未软删 Agent」，而非 spec 字面
   `owner_user_id IS NULL`；理由与代价见 §3.1。若产品坚持 NULL 口径需改创建逻辑 + 回填，
   超出 M8「无迁移」前提，应单独立项。
2. **R9 审计过滤未扩展**：`/admin/audit-logs` 仍只有 M5 的过滤参数，无 `request_id`/全文/`q`；
   前端审计页若需要，后续单独立项。

**管理端 no-op 语义的边界（有意保留）**

3. **MCP PATCH 重放「明文敏感头」会被视为变更**：`normalize_headers` 对明文敏感头每次用随机
   nonce 重新加密，因此同一明文 headers 再次提交时落库形态不同，会被判定为真实变更
   （写审计 + 触发一次工具发现）。前端应保存后回填 `secret:`/`env:` 引用形态，或避免无意义重放。
4. **用户侧 MCP PATCH 的既有行为未动**：`McpServerUpdateRequest`（`PATCH /mcp/servers/{id}`）
   仍无同值 no-op 判定、`name` 纯空白仍可能落库；M8 只改管理端 `AdminMcpServerUpdateRequest`
   与共享的创建 schema。Agent 注册 `name` 的 `min_length=1` 既有校验也未扩大改动。
5. **`SkillEnabledRequest` 未加 `extra="forbid"`**：`PUT /admin/skills/{id}/enabled` 复用既有
   请求体，多余字段被 Pydantic 忽略；`force-disable` 请求体已 `forbid`。为保持用户侧
   `PUT /skills/{id}/enabled` 兼容，M8 未改。

**需迁移才能彻底解决（M8 无迁移前提下接受）**

6. **跨 scope slug 唯一是应用层检查**：数据库只有 `scope='system'` 全局唯一索引与
   `(owner_user_id, slug)` 用户唯一索引，没有跨 scope 唯一约束；并发窗口下理论上仍可能插出
   跨 scope 重名，需迁移加约束。
7. **删除系统 MCP 的用户覆盖清理存在 TOCTOU**：`DELETE user_capability_overrides` 与删除
   server 非同一约束保证，极端并发下可能留孤儿覆盖，需迁移/约束彻底消除。

**运维提醒**

8. **`create_all` 与迁移的差异**：pytest 用 `create_all` 建 `agent_hub_test`（不执行 upgrade）；
   T8 已另建空库 `agent_hub_m8_mig` 从 0 `alembic upgrade head` 并 `alembic check` 通过
   （§2.5），证明迁移链完整。生产仍必须在启动前执行 `alembic upgrade head`（`/readyz` 会检查）。
9. **概览是精确计数**：8 条聚合查询 O(表大小)，表很大时仍可能扫表；前端刷新 ≥30s；
   统计表/缓存不在 M8。
10. **系统 MCP stdio 依赖命令白名单**：管理员可创建 stdio 系统 MCP，但受既有
    `MCP_STDIO_ALLOWED_COMMANDS`/命令白名单约束；测试只用 HTTP mock，未实测本地进程。
11. **前端尚未集成**：M8 只交付后端契约与 handoff，管理台页面、权限显隐、联调与验收均未开始；
    本报告不声明前端已完成，前端接入见 [M8 前端交接](2026-11-11-m8-frontend-handoff.md)。

## 7. 提交记录

```text
d2b6680 feat: 管理台用户列表/详情/PATCH 与路由骨架（M8 Task 1）
95412e4 feat: 管理台只读概览 GET /admin/overview（M8 Task 2）
c74dfbf feat: 管理台平台指令与系统技能管理（M8 Task 3-4）
f2ece89 feat: 管理台系统 MCP 与全局 Agent 管理（M8 Task 5-6）
d2c37ea fix: 管理台 503 专用错误码与名称空白校验（M8 T5-T6 审查修复）
3dd4178 docs: M8 管理台契约文档、OpenAPI 与前端交接（M8 T7）
7de5202 docs: M8 交付报告与最终回归（M8 T8）
504cd39 fix: 修复最后管理员并发缺口与管理台 no-op 语义（M8 最终审查修复）
（本次最终收口提交）docs: M8 最终收口（状态、证据与文档同步）
```

均已本地提交、**未推送**；所有提交使用显式路径，未触碰 `.dsh/` 等运行时文件。
