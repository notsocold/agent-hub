# M8 管理台后端交付报告

- 日期：2026-11-11
- 范围：M8 管理台后端（原 X2，Task 1–8，**不含前端页面**）
- 基线：M7 收尾 **760 passed, 1 skipped**，迁移 head `2d8f6a1c3e70`，OpenAPI 与代码一致
- 设计：[M7–M9 立项](../superpowers/specs/2026-11-11-m7-m9-backend-roadmap-design.md) §4
- 计划：[M8 实施计划](../superpowers/plans/2026-11-11-m8-admin-console.md)
- 前端交接：[M8 前端交接](2026-11-11-m8-frontend-handoff.md)
- 状态：**实施完成，待独立审查**；M8 前端接入尚未开始（本文不构成前端已集成的声明）

**目标**：在不新增数据库迁移的前提下，为管理控制台交付真实契约：用户列表/详情/角色与状态管理、
只读概览、平台默认指令、系统技能、系统 MCP、全局 Agent 的查询与管理；复用 M7 的 `AdminUser`
门禁、既有领域服务、统一错误信封与游标分页，并同步 OpenAPI / `docs/api.md` / 前端交接。

## 1. 交付内容

| 任务 | 文件 | 说明 |
|---|---|---|
| Task 1 路由骨架 + 用户管理 | `app/modules/admin/{__init__,router,schemas,service}.py`、`app/api/v1/router.py`、`tests/integration/test_admin_users.py`（10 项） | 新增 `GET /admin/users`（`q/role/status/limit/cursor` + `stats`，排序 `created_at DESC, id DESC`，LIKE 通配符转义）、`GET /admin/users/{id}`、`PATCH /admin/users/{id}`；自操作 422 `cannot_change_own_role`/`cannot_disable_self`、最后活跃管理员 409 `last_admin_protected`（`ORDER BY id FOR UPDATE` + 锁后 `refresh` 再计数）、审计 `admin.user.update`；`AdminUserOut` 不含 `password_hash` |
| Task 2 只读概览 | `app/modules/admin/schemas.py`、`admin/service.py`、`admin/router.py`、`tests/integration/test_admin_overview.py`（4 项） | `GET /admin/overview` 固定 8 组字段；`build_overview` 每表一条聚合 SQL（共 8 条）、不加载 ORM 行；附件排除 `deleted`、状态键恒在；测试用 SQLAlchemy 事件统计 SELECT 上界（实测 9 ≤ 10）；无审计 |
| Task 3 平台默认指令 | `app/modules/instructions/service.py`、`admin/{schemas,service,router}.py`、`tests/integration/test_admin_instructions.py`（9 项） | `GET/PUT /admin/settings/instruction`；复用 `system_settings["instruction.default"]`（兼容 `{"content": ...}` 与裸字符串）；`content=null`/空白恢复内置默认；超长 422 `instruction_too_long`；`is_default/max_chars/user_override_count/updated_by(_email)/updated_at`；审计 `instruction.update`（`detail.scope="platform"`，不落正文）；`INSTRUCTIONS_ENABLED=false` 仍可读写（R1） |
| Task 4 系统技能 | `app/modules/skills/service.py`、`admin/{schemas,router}.py`、`tests/integration/test_admin_skills.py`（9 项） | `GET /admin/skills?q=`（仅 `scope='system'`、名称升序、不分页、`q` 转义）、`PUT /admin/skills/{id}/enabled`、`PUT /admin/skills/{id}/force-disable`；`effective_enabled = enabled and not force_disabled`，`force_disabled` 压过用户覆盖；私有技能/不存在 404；审计 `skill.enable`/`skill.force_disable`；R1 |
| Task 5 系统 MCP | `app/modules/mcp/schemas.py`、`mcp/service.py`、`admin/router.py`、`tests/integration/test_admin_mcp.py`（11 项） | `GET/POST/PATCH/DELETE /admin/mcp/servers`；只操作 `scope='system'`；复用 `_validate_create`/`normalize_headers(allow_platform_secret=True)`/`_server_out`/`_check_and_sync`；slug 任意 scope 冲突 409；`secret:`/`env:` 引用；工具发现失败仍保存（`unhealthy`）；删除清理 `user_capability_overrides` + 工具级联；审计 `mcp.server.create/update/delete`；503 `secret_store_unavailable`；R1 |
| Task 6 全局 Agent | `app/modules/agents/schemas.py`、`agents/service.py`、`admin/router.py`、`tests/integration/test_admin_agents.py`（7 项） | `GET /admin/agents`（query 同 `GET /agents`，全局注册表口径，R8）、`PATCH /admin/agents/{id}`；可编辑 `name/enabled/requires_approval/auth/headers`，只读字段 `extra="forbid"` 422；`name_edited` 保护；`secret:` 仅管理员；`agent.updated`（`detail.source="admin"`）；同值不写审计 |
| Task 5–6 审查修复 | `app/core/ssrf.py`、`mcp/schemas.py`、`agents/schemas.py`、两个管理端测试文件 | 审查发现 1 Important + 3 Minor：`normalize_headers` 的 503 错误码统一为 `secret_store_unavailable`（HTTP 状态不变）；管理端 PATCH `name` 纯空白 422；补 404 scope 边界与软删 404 两条覆盖（新增 3 条独立用例） |
| Task 7 契约文档 / OpenAPI / 交接 | `docs/api.md`、`docs/openapi.json`、`docs/feedback/2026-11-11-m8-frontend-handoff.md`（新增）、`README.md`、`docs/roadmap.md`、`docs/architecture.md`、`docs/database.md` | `docs/api.md` §1 错误码（新增/交叉引用 7 个码）、§2.12 管理端真实路由、§7.6 分页补充、新增 §12 M8 实现状态；OpenAPI 重新导出；前端 handoff（认证/403、用户/概览/系统项页面、错误码、curl、自测清单）；README/roadmap 标记「实施完成，待独立审查」，architecture 增管理面小节、database 注明无迁移 |
| Task 8 全量回归与报告 | `docs/feedback/2026-11-11-m8-backend-report.md`（本文件） | 全量回归、证据归档、R1–R10 与 R8 偏差、遗留项、spec §4.4 验收对照；无业务代码/测试改动 |

数据库：M8 **无迁移**，未新增/修改表、列或索引，迁移 head 保持 `2d8f6a1c3e70`；
`Settings` 与 `.env.example` **零变更**（无新增配置项/环境变量）。

## 2. 实测证据

### 2.1 全量验证

2026-11-11 本机执行，复用 `dev_postgres`（localhost:54321），业务测试库为 `agent_hub_test`
（由 `.env` 的开发库 `agent_hub` 派生，**不触碰开发库数据**）：

```text
$ TEST_DATABASE_URL="$(grep '^DATABASE_URL=' .env | cut -d= -f2- | sed 's#/agent_hub$#/agent_hub_test#')" \
    .venv/bin/python -m pytest -q
810 passed, 1 skipped in 102.95s (0:01:42)

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

- 基线 M7 **760 项** → M8 **810 项**（+50 条管理台集成用例；唯一 skip 仍是知识库语义评测组，
  需真实 Embedding key，与 M4–M7 一致）；
- ruff 全绿；`alembic check` 无漂移、head 不变（无迁移）；OpenAPI 重新导出后无 diff；
- 测试库 `agent_hub_test` 与开发库 `agent_hub` 分离；`alembic` 检查读取开发库（只读）验证
  迁移与模型一致，未写入测试数据。

### 2.2 关键用例

| 用例文件 | 数量 | 覆盖 |
|---|---|---|
| `tests/integration/test_admin_users.py` | 10 | 列表筛选/`stats`/游标翻页、`q` 通配符转义、详情/404、角色/状态 PATCH + 审计、自改角色 422、自停用 422、最后活跃管理员 409（服务层）、仍有两个活跃管理员时可停用、3 端点 403 + `details.required_role` + 401、非法游标/请求体 422 |
| `tests/integration/test_admin_overview.py` | 4 | 空库叶子计数为 0（含 8 个 A2A 状态键）、落库数据逐项计数（含软删会话/附件、失败文档、终态 run）、SELECT ≤10（`before_cursor_execute` 实测 9）、普通用户 403 + 401 |
| `tests/integration/test_admin_instructions.py` | 9 | 内置默认读取、历史两种形态、PUT 后用户端生效跟随、`null` 恢复、个人覆盖计数、超长/多余字段 422、审计不含正文、403/401、能力关闭仍可配置（R1） |
| `tests/integration/test_admin_skills.py` | 9 | 仅系统技能/名称升序/`q` 命中、`q` 超长 422、全局启停、`force_disabled` 压过用户覆盖且恢复后覆盖仍在、私有技能 404、写校验 422、审计、403/401、R1 |
| `tests/integration/test_admin_mcp.py` | 11 | 创建 + 工具发现、不可达仍保存、`secret:` 不回显（响应/审计/DB）、SSRF/重复/跨 scope slug/缺 url、PATCH 字段 + force_disable + 改 URL 重新发现、删除清理、404 scope 边界、空白 name 422、503 `secret_store_unavailable`（POST/PATCH）、403/401、R1 |
| `tests/integration/test_admin_agents.py` | 7 | 全局注册表（管理员创建仍可见）、PATCH 三字段 + 审计 + refresh 不覆盖改名、`secret:`/headers 不回显、9 个只读字段 422、空体/404/软删 404/空白 name、403/401 |

六个文件合计 **50 条**（T1 10 + T2 4 + T3 9 + T4 9 + T5 11 + T6 7）。

### 2.3 契约与 OpenAPI

- OpenAPI：导出后 **74 个 path、148 个 schema**；`/api/v1/admin` 下 14 个 path 组、
  **20 个操作**（M8 新增 15 个 + 既有 `audit-logs` 1 + `secrets` 4），全部声明 403 响应；
- 语义对比 M7 基线：**既有 path 与 schema 零变化**，仅新增 M8 path/schema（19 个新 schema）
  以及 admin 路由先挂载导致的 JSON 键顺序变化；`info`/`openapi`/`securitySchemes` 不变；
- `AdminUserOut` 无 `password_hash`；`AdminUserPatchRequest.additionalProperties=false`；
  `AdminInstructionOut` 7 个字段全部 required 且无 `effective_preview`；
  `AdminOverviewOut` 8 个顶层字段全部 required；`AdminMcpServerUpdateRequest` 恰为
  7 个可编辑字段；`AdminAgentUpdateRequest` 恰为 5 个可编辑字段；
- `docs/api.md`：§1 错误码表、§2.12 管理端真实路由、§7.6 分页补充、新增 §12（端点总表、
  用户规则、概览字段与口径、平台指令、系统技能、系统 MCP、全局 Agent/R8、审计动作、验证与迁移）；
- 前端 handoff：`docs/feedback/2026-11-11-m8-frontend-handoff.md`（12 节，含 403 拦截器示例、
  页面接入、错误码映射、curl 速查与联调自测清单）。

## 3. 关键决策与裁定（R1–R10）

| # | 决策 | M8 实现 |
|---|---|---|
| R1 | 管理端配置面不受能力总开关影响 | 平台指令/系统技能/系统 MCP/全局 Agent 的管理路径不调用各模块 `_require_enabled()`，也不走带开关门禁的可见性查询；`INSTRUCTIONS_ENABLED`/`SKILLS_ENABLED`/`MCP_ENABLED=false` 时仍可读写，运行态接口行为不变；四个测试文件各有 R1 用例 |
| R2 | 用户 PATCH 只改 `role`/`status` | `AdminUserPatchRequest` `extra="forbid"` 且至少一项；`email/display_name/password` 不在 M8 |
| R3 | 禁止自我锁死 | 改自己角色 422 `cannot_change_own_role`、停用自己 422 `cannot_disable_self`（比 spec 仅禁改角色更严）；同值自 PATCH 是 200 no-op |
| R4 | 最后活跃管理员 | 409 `last_admin_protected` + `details.active_admins=1`；锁 `role='admin' AND status='active'` 行（`ORDER BY id FOR UPDATE`），锁后 `refresh` 再计数；自操作规则优先 |
| R5 | 不返回 `effective_preview` | 平台默认 `content` 即无个人覆盖用户实际注入的文本；仅返回 7 个字段 |
| R6 | 系统技能/MCP 不分页；用户详情不带活动计数 | 系统项数量有上界，列表一次返回 + `stats`；用户详情与列表项同形 |
| R7 | 全局 Agent 可编辑白名单 | `name/enabled/requires_approval/auth/headers`；`description` 由 Card 派生且 refresh 会覆盖，只读；`slug/url/protocol_version/card/skills/capabilities/health_status/owner_user_id` 全部 422 |
| R8 | 全局 Agent 口径 | **全部未软删**，不按 `owner_user_id IS NULL` 过滤（与 spec §4.2 字面不一致，详见 §4） |
| R9 | 审计过滤不扩展 | `/admin/audit-logs` 保持 M5 的 `actor_type/actor_id/action/resource_type/resource_id/start/end/cursor`；`request_id`/全文搜索留后续 |
| R10 | 审计动作命名 | `admin.user.update`、`instruction.update`（`scope=platform`）、`skill.enable`、`skill.force_disable`、`mcp.server.create/update/delete/check`、`agent.updated`（`source=admin`）；`detail` 只放字段名/状态值/长度/指纹/计数 |

### 3.1 R8 偏差：全局 Agent 注册表口径

- **spec 字面**（§4.2）：`GET/PATCH /admin/agents` 复用 `remote_agents.owner_user_id IS NULL`
  的全局注册表。
- **代码现状**：v1 `remote_agents` 本就是**全局注册表**——`GET /agents` 对所有登录用户返回
  全部未软删行；`POST /agents` 创建时写 `owner_user_id=当前管理员`。若按 `owner_user_id IS NULL`
  过滤，所有现存 Agent 与管理员新建的 Agent 都会在管理端“消失”。
- **M8 裁定**：管理端返回**全部未软删 Agent**，`owner_user_id` 仅作保留字段、不参与可见性；
  测试 `test_admin_agents_covers_global_registry` 先断言创建行 `owner_user_id==admin.id`，
  再断言 `GET /admin/agents` 可见且 `stats.total==1`，固定该口径。
- **代价与后续**：与立项 spec 字面不一致，需产品最终确认。若坚持 NULL 口径，需同时修改
  `create_agent` 写 NULL、对历史行做数据回填（脚本或迁移），会突破 M8「无迁移」前提，
  应单独立项。该偏差已写入 `docs/api.md` §12.7、前端 handoff §8 与本报告。

## 4. 前端交接索引

| 主题 | 交接位置 |
|---|---|
| 变更速览与兼容性 | [handoff §1](2026-11-11-m8-frontend-handoff.md#1-变更速览) |
| 认证、401/403 拦截器 | [handoff §2](2026-11-11-m8-frontend-handoff.md#2-认证与-401--403-处理) |
| 用户页（筛选/游标/stats/自操作/错误码） | [handoff §3](2026-11-11-m8-frontend-handoff.md#3-用户管理页) |
| 概览页（字段/口径/刷新 ≥30s） | [handoff §4](2026-11-11-m8-frontend-handoff.md#4-只读概览页) |
| 平台指令页（null 恢复/is_default/updated_by_email） | [handoff §5](2026-11-11-m8-frontend-handoff.md#5-平台默认指令页) |
| 系统技能页（两个开关/force_disabled 优先级） | [handoff §6](2026-11-11-m8-frontend-handoff.md#6-系统技能页) |
| 系统 MCP 页（表单/`secret:`/PATCH/unhealthy/503） | [handoff §7](2026-11-11-m8-frontend-handoff.md#7-系统-mcp-页) |
| 全局 Agent 页（可编辑/只读/R8） | [handoff §8](2026-11-11-m8-frontend-handoff.md#8-全局-agent-页) |
| 错误码表与 OpenAPI 链接 | [handoff §9](2026-11-11-m8-frontend-handoff.md#9-错误码与-openapi) |
| 无迁移、无新环境变量 | [handoff §10](2026-11-11-m8-frontend-handoff.md#10-兼容性迁移与配置) |
| curl 速查 / 联调自测清单 | [handoff §11](2026-11-11-m8-frontend-handoff.md#11-curl-速查)、[§12](2026-11-11-m8-frontend-handoff.md#12-联调自测清单) |

## 5. 规格 §4.4 验收清单

| 验收项 | 状态 | 证据 |
|---|---|---|
| 管理员可查用户、改角色/状态；「不能改自己」「不能禁用/降级最后一个活跃 admin」有测试 | **已验证（后端）** | `test_admin_users.py` 10 项：HTTP 422 `cannot_change_own_role`/`cannot_disable_self`、409 `last_admin_protected` + `details.active_admins=1`，服务层用例绕过路由规则直接触发保护；行锁 + 锁后 refresh |
| 平台指令、系统技能、系统 MCP、全局 Agent 可读可改；`force_disabled` 压过用户覆盖 | **已验证（后端）** | `test_admin_instructions.py` 9 项、`test_admin_skills.py` 9 项（技能用户覆盖压制）、`test_admin_mcp.py` 11 项（MCP 用户覆盖压制）、`test_admin_agents.py` 7 项 |
| 普通用户访问任何新增 `/admin/*` 返回 403 `forbidden` + `details.required_role="admin"`；未登录 401 | **已验证（后端）** | 六个测试文件均含 403/401 断言；OpenAPI 20 个 admin 操作全部声明 403；`security.rbac_denied` 由 M7 `require_admin` 写入 |
| 关键写操作全部写审计；`GET /admin/overview` 返回计数且无 N+1/ORM 行加载 | **已验证（后端）** | 用户/指令/技能/MCP/Agent 的审计断言；`mcp.server.check` 工具发现审计；`test_overview_uses_bounded_queries` 实测 9 条 SELECT（8 聚合 + 鉴权 1） |
| OpenAPI / `docs/api.md` / 前端 handoff / README / roadmap 同步；无迁移；全量测试与 ruff 全绿 | **已验证（后端）** | OpenAPI 导出后无 diff；`docs/api.md` §12；handoff 12 节；README/roadmap/architecture/database 已同步；810 passed/1 skipped、ruff 全绿、`alembic check` 无漂移 |
| M8 前端页面接入与联调 | **待前端（不在后端范围）** | 后端交付 15 个端点与 handoff；前端页面/联调尚未开始，本文不声明前端已集成 |
| 里程碑独立审查 | **进行中** | Task 1–6 任务级审查已完成（T5–T6 有一轮修复并复跑）；M8 全量交付的独立审查由控制器安排，结论确认前保持「实施完成，待独立审查」 |

## 6. 已知缺口、遗留与延后项

**延后（审查已记录，M8 不修）**

1. **系统 MCP PATCH 无同值 no-op 判定**：只要提交就写 `mcp.server.update` 审计并触发一次强制
   工具发现（字段值未变也一样）；与用户侧 `update_server` 语义一致。若产品要求“无变化不写审计”，
   需在服务层增加字段比较（可能影响前端审计筛选），延后。
2. **清空 `headers`/`auth` 时 `auth_hint` 可能残留**：沿用既有 `_apply_auth`/`update_agent` 的
   `if hint: agent.auth_hint = hint` 语义，未扩大改动；需要严格清理时单独立项。
3. **跨 scope slug 唯一是应用层检查**：数据库只有 `scope='system'` 全局唯一索引与
   `(owner_user_id, slug)` 用户唯一索引，没有跨 scope 唯一约束；并发窗口下理论上仍可能插出
   跨 scope 重名。低频管理操作可接受，彻底解决需迁移。
4. **删除系统 MCP 的用户覆盖清理存在 TOCTOU**：`DELETE user_capability_overrides` 与删除
   server 非同一约束保证，极端并发下可能留孤儿覆盖；与既有删除语义一致，M8 无迁移前提下接受。
5. **创建路径空白 `name` 未校验**：管理端 PATCH 已在审查修复中改为纯空白 422；
   `POST /admin/mcp/servers`（与用户侧 Agent 注册）创建路径仍只校验 `min_length=1`，
   纯空白名可落库，延后统一处理。
6. **`SkillEnabledRequest` 未加 `extra="forbid"`**：`PUT /admin/skills/{id}/enabled` 复用既有
   请求体，多余字段目前被 Pydantic 忽略；`force-disable` 请求体已 `forbid`。为保持用户侧
   `PUT /skills/{id}/enabled` 兼容，M8 未改，若要求两端一致需单独回归。
7. **T1 加锁后未重算 changed**：并发窗口下可能对已变为同值的请求仍写一次 `admin.user.update`
   审计（幂等更新），对最后管理员保护的正确性无影响。
8. **审计过滤未扩展（R9）**：`/admin/audit-logs` 无 `request_id`/全文/`q` 过滤；前端审计页
   如需要，后续单独加。

**持久提醒 / 风险**

9. **`create_all` 与迁移的差异**：pytest 用 `create_all` 建测试库；`alembic check` 针对本机
   开发库（只读）验证模型与迁移一致，M8 无迁移所以风险低。生产仍必须在启动前执行
   `alembic upgrade head`（`/readyz` 会检查迁移），本次 T8 未另建空库重跑 `upgrade head`
   （M6/M7 已做过，M8 无迁移）。
10. **概览是精确计数**：8 条聚合查询 O(表大小)，表很大时仍可能扫表；这是计划明确接受的
    管理端低频只读开销，前端刷新 ≥30s；统计表/缓存不在 M8。
11. **系统 MCP stdio 依赖命令白名单**：管理员可创建 stdio 系统 MCP，但受既有
    `MCP_STDIO_ALLOWED_COMMANDS`/命令白名单约束；本次测试只用 HTTP mock，未实测本地进程。
12. **OpenAPI 通用 403 描述仍是 M7 文案**：`app/api/v1/router.py::COMMON_ERROR_RESPONSES`
    的 403 描述未逐条列 `details.required_role` 与 M8 新码；受「只改文档/不碰业务代码」约束，
    M8 在 `docs/api.md` §1/§12 中补齐，未改 OpenAPI description。
13. **前端尚未集成**：M8 只交付后端契约与 handoff，管理台页面、权限显隐、联调与验收均未开始；
    本报告不声明「已完成」，状态保持「实施完成，待独立审查」直到控制器确认审查结论。

## 7. 提交记录

```text
d2b6680 feat: 管理台用户列表/详情/PATCH 与路由骨架（M8 Task 1）
95412e4 feat: 管理台只读概览 GET /admin/overview（M8 Task 2）
c74dfbf feat: 管理台平台指令与系统技能管理（M8 Task 3-4）
f2ece89 feat: 管理台系统 MCP 与全局 Agent 管理（M8 Task 5-6）
d2c37ea fix: 管理台 503 专用错误码与名称空白校验（M8 T5-T6 审查修复）
3dd4178 docs: M8 管理台契约文档、OpenAPI 与前端交接（M8 T7）
（本报告所在提交）docs: M8 交付报告与最终回归（M8 T8）
```

均已本地提交、**未推送**；所有提交使用显式路径，未触碰 `.dsh/` 等运行时文件。
