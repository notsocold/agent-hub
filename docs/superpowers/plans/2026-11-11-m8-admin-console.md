# M8 管理台后端（原 X2）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不新增数据库迁移的前提下，为管理控制台交付真实契约：用户列表/详情/角色与状态管理、只读概览、平台默认指令、系统技能、系统 MCP、全局 Agent 的查询与管理，复用 M7 的 `AdminUser` 门禁、既有领域服务、统一错误信封与游标分页，并同步 OpenAPI / `docs/api.md` / 前端交接。

**Architecture:** 新增 `app/modules/admin/` 作为**薄 HTTP 适配层**（`router.py` / `schemas.py` / `service.py`）：只有「用户管理」与「概览」是全新业务，SQL 与规则放 `admin/service.py`；平台指令、系统技能、系统 MCP、全局 Agent 的校验、审计与生效优先级仍留在各自领域服务（`instructions` / `skills` / `mcp` / `agents`），管理路由只做参数转换与 `AdminUser` 门禁，避免复制 SSRF、`secret:` 解析、`force_disabled` 合并等易漂移逻辑。所有新端点挂在 `/api/v1/admin/*`，读接口不分页或复用不透明游标，写接口落审计；**无迁移**。

**Tech Stack:** FastAPI、SQLAlchemy 2.x async、Pydantic v2、pytest-asyncio、httpx、PostgreSQL（复用本机 `dev_postgres`）。不新增第三方依赖。

**Spec:** `docs/superpowers/specs/2026-11-11-m7-m9-backend-roadmap-design.md` §4（M8 范围/验收/非目标）

**前置基线:** M7-2 已交付 `AdminUser` RBAC（403 `forbidden` + `details.required_role="admin"` + `security.rbac_denied` 审计）；当前迁移 head `2d8f6a1c3e70`；M7 结束时的全量测试全绿。

## Global Constraints

- Python `>=3.13`；ruff `line-length = 100`；代码注释、docstring、API 描述、用户可见错误消息一律中文。
- **不新增迁移**：M8 只读写既有列，禁止修改 `app/db/models/` 与 `alembic/versions/`。已核对：
  `users.role/status`（M1）、`system_settings`/`user_settings`（M1）、`skills.scope/enabled/force_disabled` 与 `mcp_servers.scope/enabled/force_disabled`（M1.5）、`remote_agents.owner_user_id`（M2）、`user_capability_overrides`（M1.5）均已存在；
  T8 必须用 `alembic check` 证明无漂移，且 `alembic/versions/` 无新文件。
- 复用既有依赖/信封/分页/审计，不自造：
  - 路由依赖 `app.core.deps.AdminUser` / `DbSession`；403 由 M7 的 `require_admin` 统一产生；
  - 错误一律 `app.core.errors` 的 `DomainError` 子类 + `code` 参数；
  - 列表游标用 `app.core.cursor.encode_cursor/decode_cursor`，非法游标必须 422 `validation_error`；
  - 审计用 `app.core.audit.write_audit`，`detail` 只放长度/指纹/字段名/状态值，**不放正文、明文密钥、密文、请求头**。
- **管理端配置面不受能力总开关影响**（R1）：`GET/PUT /admin/settings/instruction`、`GET/PUT /admin/skills*`、`GET/POST/PATCH/DELETE /admin/mcp/servers*` 在 `INSTRUCTIONS_ENABLED/SKILLS_ENABLED/MCP_ENABLED=false` 时仍可读写持久化配置；运行态接口（`/instructions`、`/skills`、`/mcp`、聊天）行为不变。
- 测试零网络、零外部存储、零新容器：MCP 用 `app.modules.mcp.service.register_test_transport` + `tests/helpers.make_mcp_transport`；A2A 用 `app.modules.a2a.client.register_test_transport` + `tests/fixtures/fake_a2a_agent.FakeA2aAgent`；Embedding 用 fake；数据库用 `TEST_DATABASE_URL` 指向本机已有 PostgreSQL，缺库时 skip。
- 不新增配置项：`Settings` 与 `.env.example` **零变更**（如实现中想加开关，先停下确认，不在 M8 私自扩）。
- 不修改 M7 范围（续流/RBAC/审批字段/授权 URL 白名单）；M8 只消费 M7 的 403 契约。
- 不实现非目标：密码重置/强制下线、多角色 RBAC、数据导出/批量操作、前端页面本身。
- 提交只在本地、不推送、不删分支；提交时使用**显式路径**，禁止 `git add -A`（仓库存在 `.dsh/sessions` 等运行时文件）。

## Review Focus

1. **最后一个活跃管理员保护**：降级/停用目标后是否仍至少剩 1 个 `role='admin' AND status='active'`；并发互相降级时用行锁串行化。测试：T1 `test_last_active_admin_is_protected`。
2. **不能把自己锁死**：管理员不能修改自己的 `role`、不能停用自己的 `status`；自操作返回 422 而不是静默成功。测试：T1 `test_cannot_change_own_role` / `test_cannot_disable_self`。
3. **密钥不泄露**：系统 MCP / 全局 Agent 的响应、审计、日志不得出现明文/密文/请求头值，只允许 `secret:`/`env:` 引用与 `credentials_hint`/`auth_hint`。测试：T5 `test_system_mcp_secret_ref_never_echoed`、T6 `test_admin_agent_secret_ref_and_headers_not_echoed`。
4. **403 信封一致**：任何 `/admin/*` 非管理员访问都必须是 403 `forbidden` + `details.required_role="admin"`，未登录 401；不能出现 404/500 或空 `details`。测试：T1 `test_non_admin_forbidden_and_unauthorized`，T2–T6 各文件各有一条 403 用例。
5. **审计覆盖与不泄密**：用户改角色/状态、平台指令保存/恢复、系统技能启停/强制停用、系统 MCP 增删改、全局 Agent PATCH 都写审计，`action/resource_type/resource_id` 稳定；`detail` 不含正文与凭据。测试：T1/T3/T4/T5/T6 的 audit 断言。
6. **概览不做 N+1 / 不拉行**：`GET /admin/overview` 必须用 SQL `count`/`sum` + 单次 `GROUP BY`，不得 `select(Model)` 后 Python 循环；一次请求的 SELECT 数有上界（≤10，含鉴权）。测试：T2 `test_overview_uses_bounded_queries`。精确计数在 PostgreSQL 上仍可能扫表，这是可接受的（管理端低频只读），但不接受按状态逐条查询或加载 ORM 行。

## 文件结构

```text
app/modules/admin/                 # 新增：管理台薄适配层
├── __init__.py                    # 包声明
├── router.py                      # 全部 /admin/* 路由（prefix="/admin", tags=["管理台"]）
├── schemas.py                     # 管理台 DTO：用户/概览/平台指令/系统技能统计/请求体
└── service.py                     # 用户管理 + 概览（唯一新增业务 SQL）

app/modules/instructions/service.py  # 修改：平台默认读取/写入（复用 SYSTEM_KEY_INSTRUCTION）
app/modules/skills/service.py        # 修改：系统技能列表/启停/强制停用（复用 _skill_out/_effective_enabled）
app/modules/mcp/service.py           # 修改：系统 MCP 列表/创建/更新/删除（复用 _validate_create/normalize_headers/_check_and_sync）
app/modules/mcp/schemas.py           # 修改：AdminMcpServerUpdateRequest（含 enabled/force_disabled）
app/modules/agents/service.py        # 修改：admin_update_agent（复用 _apply_auth/_detail_with_recent）
app/modules/agents/schemas.py        # 修改：AdminAgentUpdateRequest（不可编辑字段显式 422）
app/api/v1/router.py                 # 修改：include admin_router（复用 COMMON_ERROR_RESPONSES）

tests/integration/test_admin_users.py
tests/integration/test_admin_overview.py
tests/integration/test_admin_instructions.py
tests/integration/test_admin_skills.py
tests/integration/test_admin_mcp.py
tests/integration/test_admin_agents.py

docs/api.md                          # 修改：§1 错误码、§2.12 管理端、§7.6 分页、新增 §11 M8 状态
docs/openapi.json                    # 重新导出
docs/feedback/2026-11-11-m8-frontend-handoff.md   # 新增
docs/feedback/2026-11-11-m8-backend-report.md     # 新增（T8）
README.md / docs/roadmap.md / docs/architecture.md / docs/database.md  # 状态与口径
```

**为什么这样拆：**

- 管理台 15 个端点里 11 个是「参数转换 + 调用领域服务」，单独拆 `users.py`/`mcp.py` 等只会增加文件数；集中一个 `router.py` 便于核对 `/admin/*` 的 403 与 OpenAPI tag。
- 系统技能/MCP/Agent 的写规则与既有用户侧写接口共享同一套校验（SSRF、`secret:`、`force_disabled` 优先级、工具发现、审计），必须放回领域服务；若放 `admin/service.py` 会复制逻辑，后续两边漂移。
- 用户管理与概览是管理台独有、无既有领域服务可复用，放 `admin/service.py`，与路由同包、改动内聚。

## 契约总览（新端点）

| # | 方法/路径 | 请求要点 | 成功 | 失败码（除通用 401/403） |
|---|---|---|---|---|
| T1 | `GET /api/v1/admin/users` | `q`、`role`、`status`、`limit=20`、`cursor` | 200 `AdminUserPage` | 422 `validation_error` |
| T1 | `GET /api/v1/admin/users/{user_id}` | 路径 UUID | 200 `AdminUserOut` | 404 `not_found` |
| T1 | `PATCH /api/v1/admin/users/{user_id}` | `{role?, status?}`，至少一项 | 200 `AdminUserOut` | 404；422 `cannot_change_own_role` / `cannot_disable_self`；409 `last_admin_protected` |
| T2 | `GET /api/v1/admin/overview` | 无 | 200 `AdminOverviewOut` | — |
| T3 | `GET /api/v1/admin/settings/instruction` | 无 | 200 `AdminInstructionOut` | — |
| T3 | `PUT /api/v1/admin/settings/instruction` | `{content: str\|null}`（null=恢复内置默认） | 200 `AdminInstructionOut` | 422 `instruction_too_long` |
| T4 | `GET /api/v1/admin/skills` | `q` | 200 `AdminSkillListOut` | — |
| T4 | `PUT /api/v1/admin/skills/{skill_id}/enabled` | `{enabled: bool}` | 200 `SkillOut` | 404 `not_found` |
| T4 | `PUT /api/v1/admin/skills/{skill_id}/force-disable` | `{force_disabled: bool}` | 200 `SkillOut` | 404 `not_found` |
| T5 | `GET /api/v1/admin/mcp/servers` | 无 | 200 `McpServerListOut` | — |
| T5 | `POST /api/v1/admin/mcp/servers` | `McpServerCreateRequest` | 201 `McpServerOut` | 409 `conflict`；422 `mcp_ssrf_blocked` / `mcp_transport_not_allowed` / `secret_ref_*`；503 `secret_store_unavailable` |
| T5 | `PATCH /api/v1/admin/mcp/servers/{server_id}` | `AdminMcpServerUpdateRequest` | 200 `McpServerOut` | 404；422（同上）；503 |
| T5 | `DELETE /api/v1/admin/mcp/servers/{server_id}` | 无 | 204 | 404 |
| T6 | `GET /api/v1/admin/agents` | 同 `GET /agents`：`health_status`、`enabled`、`q`、`limit`、`cursor` | 200 `AgentPage` | 422 `validation_error` |
| T6 | `PATCH /api/v1/admin/agents/{agent_id}` | `AdminAgentUpdateRequest` | 200 `AgentDetailOut` | 404；422 `validation_error` |

> 所有端点：未登录 401 `unauthorized`；非管理员 403 `forbidden`（`details.required_role="admin"`）；预置契约 403 描述由 `COMMON_ERROR_RESPONSES` 统一注入。所有端点响应头都有 `X-Request-ID`。

---

### Task 1: 管理台路由骨架 + 用户列表/详情/PATCH

**Files:**
- Create: `app/modules/admin/__init__.py`
- Create: `app/modules/admin/router.py`（本任务只含 `/admin/users*`；后续任务在同一文件追加）
- Create: `app/modules/admin/schemas.py`
- Create: `app/modules/admin/service.py`
- Modify: `app/api/v1/router.py`（`from app.modules.admin.router import router as admin_router`，`api_router.include_router(admin_router, responses=COMMON_ERROR_RESPONSES)`）
- Test: `tests/integration/test_admin_users.py`

**Interfaces:**

- Consumes: `app.core.deps.AdminUser`、`DbSession`；`app.core.audit.write_audit`；`app.core.cursor.encode_cursor/decode_cursor`；`app.core.errors.ValidationFailedError/NotFoundError/ConflictError`；`app.db.models.user.User`。
- Produces（后续任务共用路由骨架）：
  - `router = APIRouter(prefix="/admin", tags=["管理台"])`
  - `AdminUserOut`：`id: UUID`、`email: str`、`display_name: str`、`role: Literal["user","admin"]`、`status: Literal["active","disabled"]`、`last_login_at: datetime|None`、`created_at: datetime`、`updated_at: datetime`（**永不返回 `password_hash`**）
  - `AdminUserStats`：`total`、`active`、`disabled`、`admins`（均为 int）
  - `AdminUserPage`：`items: list[AdminUserOut]`、`next_cursor: str|None`、`has_more: bool`、`stats: AdminUserStats`
  - `AdminUserPatchRequest`：`role: Literal["user","admin"]|None = None`、`status: Literal["active","disabled"]|None = None`；`model_config = ConfigDict(extra="forbid")`；`model_validator` 保证至少一项非 None，否则 422 `validation_error`
  - `app.modules.admin.service`：
    - `async def list_users(db: AsyncSession, *, q: str|None, role: str|None, status: str|None, limit: int, cursor: str|None) -> AdminUserPage`
    - `async def get_user(db: AsyncSession, user_id: uuid.UUID) -> AdminUserOut`
    - `async def update_user(db: AsyncSession, admin: User, user_id: uuid.UUID, payload: AdminUserPatchRequest) -> AdminUserOut`

- 路由细节：
  - `GET /admin/users`：`q` 匹配 `email` 或 `display_name`，大小写不敏感，`max_length=100`，必须转义 `%`/`_`/`\`；`role`/`status` 为枚举；`limit` 默认 20、范围 1–100；`cursor` `max_length=500`。
  - 排序固定 `User.created_at DESC, User.id DESC`；游标 payload `{"created_at": <datetime>, "id": <UUID>}`；解码失败或字段缺失 → `ValidationFailedError("分页游标无效")` → 422。
  - `stats` 是**当前筛选条件下**的全量计数（与 `ConversationPage.stats` 口径一致），额外查一次聚合。
  - `GET /admin/users/{user_id}`：不存在 → `NotFoundError("用户不存在")`；响应与列表项同形，不额外返回会话/消息计数（R6）。
  - `PATCH /admin/users/{user_id}`：
    1. 目标不存在 → 404。
    2. 目标是自己且 `role` 有变化 → 422 `cannot_change_own_role`（"不能修改自己的角色"）。
    3. 目标是自己且 `status="disabled"` → 422 `cannot_disable_self`（"不能停用自己的账号"）。
    4. 只计算实际变化的字段；无变化直接 200 返回当前值（**不写审计**）。
    5. 若把「活跃管理员」降为 `user` 或停用，先执行：
       `SELECT id FROM users WHERE role='admin' AND status='active' ORDER BY id FOR UPDATE`，
       再 `await db.refresh(target)`，用锁定后的结果计算 `remaining = len(locked_ids) - (1 if target.id in locked_ids else 0)`；
       若目标仍是活跃管理员且 `remaining == 0` → 409 `last_admin_protected`（"不能移除最后一个活跃管理员"，`details={"active_admins": 1}`）。
       行锁用于防两个管理员并发互相降级；加锁顺序固定 `id ASC` 防死锁。
    6. 应用变更、`write_audit(actor_type="user", actor_id=str(admin.id), action="admin.user.update", resource_type="user", resource_id=str(target.id), detail={"fields": sorted(changed), "role": target.role, "status": target.status})`，`await db.commit()`、`await db.refresh(target)`，返回 `AdminUserOut`。

- [ ] **Step 1: 前置检查（M7-2 必须已落地）**

```bash
grep -n "required_role" app/core/deps.py
grep -rn "security.rbac_denied" app | head
```

Expected: 第一条能看到 `ForbiddenError` 带 `details={"required_role": "admin"}`；第二条能看到拒绝写审计。若缺失，**停止本计划**并报告“M8 依赖的 M7-2 未合入”，不要在本里程碑重复实现 M7 范围。

- [ ] **Step 2: 写失败测试** `tests/integration/test_admin_users.py`：
  - `test_list_users_filters_stats_and_cursor`：用 `admin`、`user` 夹具 + 直接落库一个 `status="disabled"` 用户；`GET /admin/users` 断言 `stats == {"total": 3, "active": 2, "disabled": 1, "admins": 1}`；`?role=user` 只剩普通用户；`?status=disabled` 只剩停用用户；`?q=<邮箱片段>` 与 `?q=<展示名片段>` 都能命中；`limit=1` 第一页 `has_more=true`、`next_cursor` 非空，带 cursor 取第二页后 id 不重复。
  - `test_get_user_detail_and_not_found`：详情 200 且字段齐全；随机 UUID → 404 `not_found`。
  - `test_patch_role_status_and_audit`：先造第二个管理员，`PATCH` 其为 `role="user"` → 200；`PATCH` 普通用户 `status="disabled"` → 200；查 `GET /admin/audit-logs?action=admin.user.update` 断言至少两条、`detail.fields` 含 `role`/`status`、`actor_id==admin.id`。
  - `test_cannot_change_own_role`：`PATCH` 自己 `{"role":"user"}` → 422 `cannot_change_own_role`；再查库确认 role 仍 `admin`。
  - `test_cannot_disable_self`：`PATCH` 自己 `{"status":"disabled"}` → 422 `cannot_disable_self`；库中 status 仍 `active`。
  - `test_last_active_admin_is_protected`（服务层，绕过自操作规则）：
    ```python
    # 夹具 admin 是唯一活跃管理员；服务层不校验 actor 角色（路由层已校验），
    # 直接用普通用户对象作 actor 触发最后管理员保护。
    actor = await db.get(User, uuid.UUID(registered["user"]["id"]))
    with pytest.raises(ConflictError) as exc:
        await admin_service.update_user(db, actor, admin.id, AdminUserPatchRequest(role="user"))
    assert exc.value.code == "last_admin_protected"
    await db.rollback()
    ```
  - `test_can_disable_other_admin_when_another_active_admin_remains`：造第二个管理员，PATCH 其 `{"status":"disabled"}` → 200（守卫不能误伤合法操作，当前管理员仍活跃）。
  - `test_non_admin_forbidden_and_unauthorized`：普通用户对 3 个端点均 403 `forbidden` + `details.required_role=="admin"`（PATCH 带合法 body，避免先触发 422）；无 token 均 401 `unauthorized`。
  - `test_invalid_cursor`：`?cursor=not-a-cursor` → 422 `validation_error`；`PATCH {}` 与 `{"display_name":"x"}` 均 422（extra forbid / 至少一项）。
- [ ] **Step 3: 运行测试确认失败**：`.venv/bin/python -m pytest tests/integration/test_admin_users.py -v`，Expected: FAIL（`app.modules.admin` 尚未实现，导入失败或路由 404 均属预期红）。
- [ ] **Step 4: 实现** `schemas.py`、`service.py`、`router.py`，在 `app/api/v1/router.py` 挂载；用户列表聚合查询用 `select(func.count(User.id))...where(*filters)`，分页用 `limit+1` 判断 `has_more`。
- [ ] **Step 5: 运行测试通过**，再跑 `pytest -q` 与 `ruff check app tests`。
- [ ] **Step 6: 记录待同步文档点**：在交付说明/任务备注里列出「`docs/api.md` §2.12 用户三端点」；所有文档统一在 T7 修改，避免与并行任务抢文件。

**Acceptance:** 管理员可分页/搜索/按角色与状态过滤用户；详情可查；角色/状态可改；自操作与最后一个活跃管理员有明确 422/409；写操作有审计；普通用户 3 个端点全 403 且信封与 `details.required_role` 正确；无迁移。

---

### Task 2: 只读概览 `GET /api/v1/admin/overview`

**Files:**
- Modify: `app/modules/admin/router.py`（追加 `/overview`）
- Modify: `app/modules/admin/schemas.py`
- Modify: `app/modules/admin/service.py`
- Test: `tests/integration/test_admin_overview.py`

**Interfaces:**

- Produces: `async def build_overview(db: AsyncSession) -> AdminOverviewOut`
- 路由：`GET /api/v1/admin/overview`，无 query 参数，200，无审计。
- 响应 `AdminOverviewOut`（字段名即前端契约）：
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
- 口径（写进 docstring 与 `docs/api.md`）：
  - `users`/`conversations`/`runs`/`a2a_tasks`/`knowledge` 为**全表原始计数**（含软删/失败/终态）；`conversations.deleted` 单列。
  - `attachments.total` 只计 `status != 'deleted'`；`storage_bytes` 为这些行的 `COALESCE(SUM(size_bytes),0)`；`deleted` 单独计数。
  - 所有状态键**始终存在**，无数据时为 `0`；`generated_at` 用 `datetime.now(UTC)`。
- SQL 要求：每张表最多一条聚合语句；`users` 一条 `GROUP BY status, role`（同时得到 active/disabled/admins）；`conversations`/`runs`/`a2a_tasks`/`knowledge_documents`/`attachments` 各一条 `GROUP BY status`（文档另带 `SUM(size_bytes)`，但本接口不返回文档存储）；`messages`/`knowledge_chunks` 各一条 `COUNT`；合计 8 条查询。禁止 `select(User)`/`select(Run)` 后 Python 过滤。

- [ ] **Step 1: 写失败测试**：
  - `test_overview_empty_counts_are_zero`：只请求 `admin_auth`（会创建 1 个管理员），断言 `users == {"total":1,"active":1,"disabled":0,"admins":1}`，其余所有叶子字段为 0，`generated_at` 可被 `datetime.fromisoformat` 解析。
  - `test_overview_counts_match_seeded_rows`：直接落库：
    - 1 个停用用户（+夹具 `user`、`admin` → total=3/active=2/disabled=1/admins=1）；
    - 4 个会话分别 `active/active/archived/deleted`；每个会话 1–2 条消息，共 5 条；
    - 6 个 run：`queued`/`running`/`interrupted` 各占一个不同会话（`uq_runs_active_conversation` 只约束活跃 run），`succeeded`/`failed`/`canceled` 复用剩余会话；
    - 1 个 `RemoteAgent` + 2 个 `A2aTask`（`completed`/`failed`）；
    - 2 个 `KnowledgeDocument`（`ready`/`failed`），ready 文档 3 个 `KnowledgeChunk`（`embedding=[0.0]*settings.embedding_dim`，checksum 唯一）；
    - 3 个 `Attachment`：ready 100、processing 200、deleted 999（`user_id=user.id`）。
    逐项断言上面的数字。
  - `test_overview_uses_bounded_queries`：用 SQLAlchemy 事件监听统计本次请求的 SELECT：
    ```python
    statements: list[str] = []
    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    sync_engine = get_engine().sync_engine
    event.listen(sync_engine, "before_cursor_execute", _capture)
    try:
        response = await client.get("/api/v1/admin/overview", headers=admin_auth)
    finally:
        event.remove(sync_engine, "before_cursor_execute", _capture)
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) <= 10
    ```
  - `test_overview_requires_admin`：普通用户 403 + `details.required_role`；无 token 401。
- [ ] **Step 2: 运行测试确认失败**（404）。
- [ ] **Step 3: 实现** `build_overview`：按上表执行 8 条聚合；用 `defaultdict(int)` 汇总后以固定状态元组补齐 0；`int(row.count or 0)` 防 `None`；不要缓存（每次实时）。
- [ ] **Step 4: 验证**：新测试通过；`pytest -q` 全绿；`ruff check app tests`（文档字段说明统一在 T7 补）。
- [ ] **Step 5: 性能口径记录**：在 `AdminOverviewOut` docstring 写明「精确计数，O(表大小)；管理端低频只读；表规模增大后再评估统计表，不在 M8」。

**Acceptance:** 空库/有数据时计数正确且键齐全；一次请求 SELECT ≤10；没有 ORM 行加载与按状态 N+1；普通用户 403；无迁移。

---

### Task 3: 平台默认指令 `GET/PUT /api/v1/admin/settings/instruction`

**Files:**
- Modify: `app/modules/instructions/service.py`
- Modify: `app/modules/admin/router.py` / `schemas.py` / `service.py`
- Test: `tests/integration/test_admin_instructions.py`

**Interfaces:**

- 复用键 `instructions.schemas.SYSTEM_KEY_INSTRUCTION == "instruction.default"` 与内置 `DEFAULT_INSTRUCTION`；值兼容既有两种历史形态：`{"content": "..."}` 与裸字符串。
- `app.modules.instructions.service` 新增：
  - `async def get_platform_instruction(db: AsyncSession) -> tuple[str, SystemSetting | None]`：无行/形态非法 → `(DEFAULT_INSTRUCTION, None)`；有行 → `(content, row)`。
  - `async def set_platform_instruction(db: AsyncSession, admin: User, content: str | None) -> SystemSetting | None`：
    - `content` 长度 > `settings.instruction_max_chars` → 422 `instruction_too_long`（文案沿用现有）；
    - `content is None`：删除该 key（无行则 no-op）；
    - 非空：upsert `value={"content": content}`、`updated_by=admin.id`、**手动 `updated_at=datetime.now(UTC)`**（`SystemSetting` 没有 `onupdate`，只改 `updated_by` 不会自动刷新时间）；
    - `write_audit(action="instruction.update", resource_type="system_setting", resource_id="instruction.default", detail={"scope": "platform", "length": len(content), "fingerprint": content_fingerprint(content)} 或 {"scope": "platform", "restored_default": True})`，commit（复用既有动作名，detail.scope 区分平台/个人）。
  - 保留 `_default_instruction` 作为 `get_platform_instruction` 的薄包装（返回 `(text, row.updated_at if row else None)`），避免影响既有 `get_effective`。
- `AdminInstructionOut`（GET/PUT 同一响应）：
  `content: str`、`is_default: bool`（无 `system_settings` 行=true）、`max_chars: int`、`user_override_count: int`（`user_settings.instructions IS NOT NULL` 的用户数）、`updated_by: UUID|None`、`updated_by_email: str|None`、`updated_at: datetime|None`。平台默认文本本身就是无个人覆盖用户实际注入的内容，不再额外返回同义的 `effective_preview`。
- `AdminInstructionUpdateRequest`：`content: str|None`（**必填字段**，可显式 null）；strip 后空串归一为 null；`extra="forbid"`。
- `admin/service.py` 新增：
  - `async def get_admin_instruction(db) -> AdminInstructionOut`
  - `async def update_admin_instruction(db, admin: User, content: str | None) -> AdminInstructionOut`
- 路由：`GET /api/v1/admin/settings/instruction` 200；`PUT` 200；401/403；422 `instruction_too_long`/`validation_error`；**不因 `INSTRUCTIONS_ENABLED=false` 返回 403**（R1）。

- [ ] **Step 1: 写失败测试**：
  - `test_get_platform_instruction_default`：无 `system_settings` 行时 `content == DEFAULT_INSTRUCTION`、`is_default=true`、`updated_by/updated_at=null`、`max_chars==get_settings().instruction_max_chars`、`user_override_count=0`。
  - `test_put_updates_default_and_user_effective`：`PUT {"content":"平台默认：先结论后依据"}` → 200、`is_default=false`、`updated_by==admin.id`、`updated_by_email==admin.email`、`updated_at` 非空；直查 `SystemSetting` 断言 `value=={"content": ...}`；注册普通用户后 `GET /api/v1/instructions` 的 `default_value` 等于新值。
  - `test_put_null_restores_builtin`：再 `PUT {"content": null}` → 200、`is_default=true`、`updated_at=null`；`SystemSetting` 行被删除；普通用户 `GET /instructions` 回退到内置默认。
  - `test_user_override_count`：普通用户 `PUT /api/v1/instructions` 后，管理员 GET 的 `user_override_count==1`。
  - `test_too_long_and_extra_field`：`"字"*4001` → 422 `instruction_too_long`；`{"content":"x","extra":1}` → 422。
  - `test_audit_has_no_content`：`GET /admin/audit-logs?action=instruction.update`，断言 `detail.scope=="platform"`、`detail.fingerprint` 是字符串、`detail.length` 正确、响应文本不含指令正文。
  - `test_non_admin_forbidden` + `test_capability_disabled_does_not_block_admin`（monkeypatch `settings.instructions_enabled=False`，GET/PUT 仍 200，运行态 `/instructions` 仍 403 `capability_disabled`）。
- [ ] **Step 2: 运行确认失败**。
- [ ] **Step 3: 实现领域函数 + admin 视图装配**（`updated_by_email` 用 `db.get(User, row.updated_by)`，用户已删则 null）。
- [ ] **Step 4: 验证**：新测试 + `pytest -q` + ruff（`docs/api.md` 统一在 T7 更新）。
- [ ] **Step 5: 复核现有指令测试**：`pytest tests/integration/test_instructions.py -q` 必须仍绿（`get_effective` 行为不变）。

**Acceptance:** 管理员可读/写/恢复平台默认指令；生效预览、`updated_by/updated_at`、个人覆盖计数完整；超长 422；审计不含正文；普通用户 403；能力关闭时管理端仍可配置；既有用户指令行为不变。

---

### Task 4: 系统技能 `GET /admin/skills` + 启停/强制停用

**Files:**
- Modify: `app/modules/skills/service.py`
- Modify: `app/modules/admin/router.py` / `schemas.py`
- Test: `tests/integration/test_admin_skills.py`

**Interfaces:**

- `app.modules.skills.service` 新增（不调用 `_require_enabled()`，R1）：
  - `async def list_system_skills(db, *, q: str|None = None) -> list[SkillOut]`：只查 `Skill.scope=="system"`，`q` ilike `name/description`（转义），按 `Skill.name.asc()` 排序；`effective_enabled = skill.enabled and not skill.force_disabled`（管理端展示全局有效态，不合并用户覆盖）。
  - `async def set_system_skill_enabled(db, admin: User, skill_id: uuid.UUID, enabled: bool) -> SkillOut`
  - `async def set_system_skill_force_disabled(db, admin: User, skill_id: uuid.UUID, force_disabled: bool) -> SkillOut`
  - 三者共用 `_load_system_skill`：不存在或 `scope != "system"` → `NotFoundError("系统技能不存在")`；写后 `write_audit`：
    - 启停 `action="skill.enable"`，`detail={"enabled": enabled, "scope": "system"}`；
    - 强制停用 `action="skill.force_disable"`，`detail={"force_disabled": force_disabled}`；
    commit + refresh 后返回 `_skill_out(...)`。
- `AdminSkillStats`：`total`、`enabled`、`force_disabled`；`AdminSkillListOut`：`items: list[SkillOut]`、`stats`。
- `AdminSkillForceDisableRequest`：`force_disabled: bool`，`extra="forbid"`。
- 路由：
  - `GET /api/v1/admin/skills?q=`（`q` 最长 100，匹配名称/描述）→ 200 `AdminSkillListOut`；系统技能数量小，**不分页**（R6）。
  - `PUT /api/v1/admin/skills/{skill_id}/enabled` body 复用 `SkillEnabledRequest` → 200 `SkillOut`。
  - `PUT /api/v1/admin/skills/{skill_id}/force-disable` body `{"force_disabled": bool}` → 200 `SkillOut`。
  - 404 `not_found`；401/403；422 `validation_error`。

- [ ] **Step 1: 写失败测试**：
  - `test_list_system_skills_only`：启动夹具会同步内置 `weekly-report`；管理员 GET 只含 `scope=="system"`；先以普通用户创建 1 个私有技能，再 GET 不包含它；`stats.total==len(items)`。
  - `test_admin_enable_disable`：GET 取得系统技能；`PUT enabled=false` → `enabled=false`、`effective_enabled=false`；普通用户 `GET /skills` 中该项 `effective_enabled=false`；`PUT enabled=true` 恢复。
  - `test_force_disable_beats_user_override`：普通用户先 `PUT /skills/{id}/enabled {"enabled":true}` 写 `UserCapabilityOverride(enabled=true)`；管理员 `PUT /admin/skills/{id}/force-disable {"force_disabled":true}` → 管理端 `effective_enabled=false`；普通用户 `/skills` 该项 `effective_enabled=false`，`/skills/catalog` 不包含；再 `force_disabled=false` → 普通用户 `effective_enabled=true`（证明用户覆盖未被破坏）。
  - `test_admin_skill_not_found_for_user_scope`：对普通用户的私有技能调用两个写端点 → 404 `not_found`。
  - `test_audit`：`GET /admin/audit-logs?action=skill.enable` 的 `detail` 含 `enabled` 与 `scope=="system"`；`?action=skill.force_disable` 的 `detail.force_disabled is True`。
  - `test_non_admin_forbidden`；`test_capability_disabled_does_not_block_admin`（monkeypatch `skills_enabled=False`，GET 200）。
- [ ] **Step 2: 运行确认失败**。
- [ ] **Step 3: 实现领域函数 + 路由**；复用 `_skill_out`，不要复制 `SkillOut` 组装。
- [ ] **Step 4: 验证**：新测试 + `pytest -q` + ruff（`docs/api.md` 统一在 T7 更新）。
- [ ] **Step 5: 回归技能**：`pytest tests/integration/test_skills.py -q` 全绿。

**Acceptance:** 管理端只读系统技能；启停与强制停用可写且有审计；`force_disabled` 在任何用户覆盖之上；私有技能 404；普通用户 403；无迁移。

---

### Task 5: 系统 MCP `GET/POST/PATCH/DELETE /admin/mcp/servers`

**Files:**
- Modify: `app/modules/mcp/service.py`
- Modify: `app/modules/mcp/schemas.py`（新增 `AdminMcpServerUpdateRequest`）
- Modify: `app/modules/admin/router.py`
- Test: `tests/integration/test_admin_mcp.py`

**Interfaces:**

- `app.modules.mcp.schemas` 新增：
  ```python
  class AdminMcpServerUpdateRequest(McpServerUpdateRequest):
      enabled: bool | None = None
      force_disabled: bool | None = None
      # model_config = ConfigDict(extra="forbid")；model_validator 至少一项非 None
  ```
  可编辑字段：`name`、`description`、`url`、`headers`、`auto_inject`、`enabled`、`force_disabled`；**slug/transport/command/args 不可编辑**（新建时确定）。
  > 这是 M8 对系统 MCP 的唯一字段扩展：现有 `PUT /mcp/servers/{id}/enabled` 只写**用户覆盖**，无法管理 `system.enabled/force_disabled`；若拆新路由会超出 spec 的四个方法，因此并入 PATCH。
- `app.modules.mcp.service` 新增（不调用 `_require_enabled()`，R1）：
  - `async def list_system_servers(db: AsyncSession) -> McpServerListOut`：`scope=="system"`，按 `created_at.desc()`；工具数用一次 `GROUP BY server_id`；`stats` 复用 `{"total","system","user","enabled","healthy"}`，其中 `user=0`、`total==system==len(items)`、`enabled` 按 `enabled and not force_disabled` 计。
  - `async def create_system_server(db: AsyncSession, admin: User, payload: McpServerCreateRequest) -> McpServerOut`：
    1. `_validate_create(payload, admin)`（复用 stdio 仅管理员、URL SSRF、command 必填）；
    2. slug 冲突（任意 scope 已存在同一 slug）→ 409 `conflict`；系统 MCP 会进入所有用户的工具目录，必须避免与任何用户自建服务器同名导致 `mcp__<slug>__*` 工具名重复；
    3. `normalize_headers(payload.headers, allow_platform_secret=True)`；不检查每人配额；
    4. 建 `McpServer(scope="system", owner_user_id=None, enabled=True, auto_inject=True, force_disabled=False, health_status="unknown", ...)`；
    5. 审计 `mcp.server.create`，detail 只含 `slug/transport/url/scope`；
    6. commit 后 `await _check_and_sync(db, server, force=True)`（**工具发现语义与用户侧一致**：可达则同步工具/healthy，不可达仍 201 且 `health_status="unhealthy"`、`last_error` 截断）。
  - `async def update_system_server(db: AsyncSession, admin: User, server_id: uuid.UUID, payload: AdminMcpServerUpdateRequest) -> McpServerOut`：`_load_system_server`（404）；`url` 用 `validate_mcp_url`；`headers` 用 `normalize_headers(..., allow_platform_secret=True)`；应用其余字段；审计 `mcp.server.update`（detail 只列字段名 + scope）；commit 后 `_check_and_sync(force=True)`；返回 `_system_server_out`。
  - `async def delete_system_server(db: AsyncSession, admin: User, server_id: uuid.UUID) -> None`：`_load_system_server`；先 `delete(UserCapabilityOverride).where(capability=="mcp_server", target_id==server.id)` 清孤儿覆盖；`await db.delete(server)`（`mcp_tools` 由 FK CASCADE）；审计 `mcp.server.delete`（`slug/scope`）；commit。
  - 辅助 `_load_system_server` / `_system_server_out`：后者复用 `_server_out(effective=server.enabled and not server.force_disabled, tool_count=...)`，不经过 `_load_visible`（避免 `_require_enabled`）。
- 路由：
  - `GET /api/v1/admin/mcp/servers` → 200 `McpServerListOut`（无分页）。
  - `POST /api/v1/admin/mcp/servers` → 201 `McpServerOut`；body 复用 `McpServerCreateRequest`：`name`(1–128)、`slug`(2–64，小写/数字/连字符)、`description`(≤2000)、`transport`(`streamable_http|sse|stdio`)、`url`(≤2048)、`headers`(dict)、`command`(stdio 必填)、`args`(≤32)。
  - `PATCH /api/v1/admin/mcp/servers/{server_id}` → 200 `McpServerOut`；body `AdminMcpServerUpdateRequest`。
  - `DELETE /api/v1/admin/mcp/servers/{server_id}` → 204。
  - 错误：404 `not_found`；409 `conflict`（slug 已被任意 scope 占用）；422 `mcp_ssrf_blocked` / `mcp_transport_not_allowed` / `secret_ref_not_allowed` / `secret_ref_invalid` / `validation_error`；503 `secret_store_unavailable`（敏感头明文且无 `CAPABILITY_SECRET_KEY`）；401/403。

- [ ] **Step 1: 写失败测试**：
  - `test_create_list_and_tool_discovery`：`register_test_transport("https://mcp.example.com/mcp", make_mcp_transport([MOCK_TOOL_READ, MOCK_TOOL_WRITE]))`；管理员 POST `{name,slug,transport:"streamable_http",url}` → 201、`scope=="system"`、`tool_count==2`、`health_status=="healthy"`；GET 列表含该项且 `stats.user==0`；普通用户 `GET /mcp/servers` 能看到 `scope=="system"`。
  - `test_system_mcp_secret_ref_never_echoed`：先 `POST /admin/secrets {"name":"MCP_KEY","value":"s3cr3t-value"}`；再 POST 服务器 `headers={"Authorization":"secret:MCP_KEY"}`；断言响应文本不含 `s3cr3t-value`、无 `headers`/`ciphertext` 字段、`credentials_hint` 含 `secret:MCP_KEY`；直查 `McpServer.headers["Authorization"] == {"secret_ref":"secret:MCP_KEY"}`；审计文本也不含明文。
  - `test_ssrf_duplicate_and_validation`：`url="http://169.254.169.254/mcp"` → 422 `mcp_ssrf_blocked`；同 slug 再建 → 409 `conflict`；普通用户先用同一 slug 建一个用户 MCP，再让管理员建系统 MCP → 409（跨 scope 冲突）；`transport="streamable_http"` 无 url → 422。
  - `test_patch_fields_force_disable_and_discovery`：用户先 `PUT /mcp/servers/{id}/enabled {"enabled":true}` 写覆盖；管理员 PATCH `{"force_disabled":true}` → `effective_enabled=false`，普通用户列表该项 `effective_enabled=false`；PATCH `{"force_disabled":false,"enabled":true}` 恢复；PATCH 改 URL 到第二个 mock（工具集不同）→ `tool_count` 变化；PATCH 空对象 → 422 `validation_error`（至少一项）。
  - `test_delete_cleans_tools_and_overrides`：DELETE 204；`GET /mcp/servers` 不含；直查 `McpTool` 无该 server 行、`UserCapabilityOverride` 无该 target；再 DELETE → 404。
  - `test_non_admin_forbidden`：普通用户对 4 个方法均 403 + `details.required_role`（POST/PATCH 带合法 body，避免先触发 422）；无 token 401。
  - `test_audit`：分别查 `mcp.server.create/update/delete`，`detail` 无 headers/凭据。
- [ ] **Step 2: 运行确认失败**。
- [ ] **Step 3: 实现** schemas/service/router；复用 `_validate_create`、`normalize_headers`、`_server_out`、`_check_and_sync`；不要另写 URL/凭据校验。
- [ ] **Step 4: 验证**：新测试 + `pytest -q` + ruff（`docs/api.md` 统一在 T7 更新）。
- [ ] **Step 5: 回归 MCP**：`pytest tests/integration/test_mcp.py tests/test_secret_ref_unit.py -q` 全绿。

**Acceptance:** 管理员可建/查/改/删系统 MCP；复用现有 SSRF/`secret:`/工具发现语义；系统 slug 冲突 409；响应与审计永不回显明文或密文；用户覆盖在 `force_disabled` 前失效；删除清理工具与用户覆盖；普通用户 403；无迁移。

---

### Task 6: 全局 Agent `GET/PATCH /admin/agents`

**Files:**
- Modify: `app/modules/agents/service.py`
- Modify: `app/modules/agents/schemas.py`（新增 `AdminAgentUpdateRequest`）
- Modify: `app/modules/admin/router.py`
- Test: `tests/integration/test_admin_agents.py`

**Interfaces:**

- `app.modules.agents.schemas` 新增：
  ```python
  class AdminAgentUpdateRequest(BaseModel):
      name: str | None = Field(default=None, min_length=1, max_length=128)
      enabled: bool | None = None
      requires_approval: bool | None = None
      auth: AgentAuthInput | None = None
      headers: dict[str, str] | None = None
      # ConfigDict(extra="forbid")；model_validator 至少一项
  ```
  **可编辑字段**：`name`（改后置 `name_edited=True`，`refresh` 不再覆盖）、`enabled`、`requires_approval`、`auth`、`headers`。
  **只读字段**：`slug`、`url`、`description`、`protocol_version`、`card`、`skills`、`capabilities`、`health_status`、`owner_user_id`；其中 `description` 由 Agent Card 派生并会被 `refresh` 覆盖，故不允许 PATCH（R7）。
- `app.modules.agents.service` 新增：
  - `async def admin_update_agent(db, admin: User, agent_id: uuid.UUID, payload: AdminAgentUpdateRequest) -> AgentDetailOut`：
    1. `_get_agent`（不存在/已软删 → 404）；
    2. `name` 变化 → 赋值并 `name_edited=True`；
    3. `enabled`、`requires_approval` 变化 → 赋值；
    4. `auth is not None` → `_apply_auth(agent, agent.card or {}, payload.auth, role="admin")`（允许 `secret:`）；
    5. `headers is not None` → `normalize_headers(..., allow_platform_secret=True)`，写 `headers/auth_hint`；
    6. 有变化才 `write_audit(action="agent.updated", resource_type="agent", resource_id=str(agent.id), detail={"fields": changed, "source": "admin"})` + commit + refresh；无变化直接返回详情；
    7. 返回 `_detail_with_recent(db, agent)`。
  - `GET /admin/agents` 直接复用既有 `list_agents(db, health_status=..., enabled=..., q=..., limit=..., cursor=...)`，**不加 owner 过滤**：v1 `remote_agents` 是全局注册表，`GET /agents` 本来就对所有登录用户返回全部未删除记录；`owner_user_id` 只作保留字段，不参与可见性（R8，见“假设与开放问题”）。
    **若评审坚持 spec 字面 `owner_user_id IS NULL`**：需同时把 `create_agent` 的 owner 写为 NULL，并对历史行做数据回填（脚本或迁移），会突破“无迁移”前提，应单独立项；本计划不采用。
- 路由：
  - `GET /api/v1/admin/agents` query 与 `GET /agents` 完全一致：`health_status`（max 16）、`enabled`、`q`（max 100）、`limit`（1–100，默认 20）、`cursor`（max 500）→ 200 `AgentPage`。
  - `PATCH /api/v1/admin/agents/{agent_id}` → 200 `AgentDetailOut`；404；401/403；422 `validation_error`。
- 审计动作沿用 `agent.updated`（M7 后 `PATCH /agents` 也仅管理员，动作语义一致），用 `detail.source="admin"` 区分管理台入口。

- [ ] **Step 1: 写失败测试**：
  - `test_admin_agents_covers_global_registry`：注册假 A2A（`FakeA2aAgent(url="http://a2a.test")` + `register_test_transport`），管理员 `POST /agents` 建 1 个（当前实现会写 `owner_user_id=admin.id`）；`GET /admin/agents` 必须返回它（证明不按 `owner_user_id IS NULL` 过滤）；`stats.total==1`；普通用户 GET → 403。
  - `test_patch_editable_fields_and_audit`：PATCH `{"name":"管理台改名","enabled":false,"requires_approval":true}` → 200，字段生效；再 `POST /agents/{id}/refresh`（管理员）后 `name` 不被 Card 覆盖；`GET /admin/audit-logs?action=agent.updated` 的 `detail.fields` 含三字段、`source=="admin"`。
  - `test_admin_agent_secret_ref_and_headers_not_echoed`：先建平台密钥 `AGENT_KEY`；PATCH `{"auth":{"type":"api_key","secret_ref":"secret:AGENT_KEY"},"headers":{"Authorization":"env:AGENT_TOKEN"}}` → 200，`auth_type=="api_key"`、`auth_hint` 含 `env:AGENT_TOKEN`；响应文本不含密钥明文；直查 DB 断言 `secret_ref=="secret:AGENT_KEY"`、`headers["Authorization"]=={"secret_ref":"env:AGENT_TOKEN"}`。
  - `test_patch_readonly_field_rejected`：PATCH `{"description":"想改"}` 与 `{"url":"http://x"}` → 422 `validation_error`（`extra="forbid"`），不静默忽略。
  - `test_patch_empty_and_not_found`：`{}` → 422；随机 UUID → 404。
  - `test_non_admin_forbidden`：普通用户 GET/PATCH 均 403 + `details.required_role`；无 token 401。
- [ ] **Step 2: 运行确认失败**。
- [ ] **Step 3: 实现** schema + `admin_update_agent` + 路由；复用 `_apply_auth`/`normalize_headers`/`_detail_with_recent`/`list_agents`，不要复制 Card/SSRF 逻辑。
- [ ] **Step 4: 验证**：新测试 + `pytest -q` + ruff（`docs/api.md` 统一在 T7 更新）。
- [ ] **Step 5: 回归 Agent**：`pytest tests/integration/test_agents.py -q` 全绿（M7 已把写接口改为 admin，测试使用管理员头）。

**Acceptance:** 管理端可列全局 Agent、PATCH 白名单字段；`name_edited` 保护生效；`secret:` 引用可用且不回显；只读字段显式 422；写审计；普通用户 403；无迁移。

---

### Task 7: 契约文档、OpenAPI 与前端 handoff

**Files:**
- Modify: `docs/api.md`
- Modify: `docs/openapi.json`（由脚本生成，不手改）
- Add: `docs/feedback/2026-11-11-m8-frontend-handoff.md`
- Modify: `README.md`、`docs/roadmap.md`、`docs/architecture.md`、`docs/database.md`
- Modify（文档性）：`app/api/schemas.py`（`ErrorBodyOut.code` 描述补 M8 新错误码）、`app/api/v1/router.py` 的 403 描述补 `details.required_role="admin"`（若 M7 已改则跳过）

**Interfaces / 文档必须写清：**

1. `docs/api.md` §1 错误码表：沿用已有 404 `not_found`/422 `validation_error`，新增：
   - 409 `last_admin_protected`：目标是最后一个活跃管理员；
   - 422 `cannot_change_own_role`：管理员不能修改自己的角色；
   - 422 `cannot_disable_self`：管理员不能停用自己的账号；
   - 422 `instruction_too_long`、`mcp_ssrf_blocked`、`mcp_transport_not_allowed`、`secret_ref_not_allowed`、`secret_ref_invalid` 在管理端也会出现（已有码，补交叉引用）；
   - 503 `secret_store_unavailable`（系统 MCP 敏感头明文且无 `CAPABILITY_SECRET_KEY`）。
2. `docs/api.md` §2.12 管理端：用 T1–T6 的实际路由替换“未实现”表，标注“全部要求 `role='admin'`；非管理员 403 `forbidden` + `details.required_role='admin'`；管理员由 `scripts/grant_admin.py` 设置”；保留 `/admin/audit-logs` 与 `/admin/secrets` 的现有说明（M8 不改审计过滤与密钥接口）。
3. `docs/api.md` §7.6 分页契约：加入 `GET /admin/users`（`created_at DESC, id DESC`，游标不透明，非法游标 422）。
4. `docs/api.md` 新增 `## 11. M8 管理台后端实现状态（2026-11-11）`，至少包含：
   - 端点总表（方法/路径/角色/成功码）；
   - 用户管理规则：自操作 422、最后活跃管理员 409、可改字段只有 `role/status`；
   - 概览 JSON 示例与计数口径（含 attachments 排除 `deleted`、状态键恒在、精确计数性能说明）；
   - 平台指令字段：`content/is_default/max_chars/user_override_count/updated_by/updated_by_email/updated_at`，`null` 恢复内置默认；
   - 系统技能：`force_disabled` 高于用户覆盖，`effective_enabled` 管理端口径；
   - 系统 MCP：`scope='system'`、`secret:NAME` 仅管理员、工具发现失败仍保存、PATCH 可改 `enabled/force_disabled`、响应不含 headers/密文；
   - 全局 Agent：可编辑字段白名单、`description` 只读（Card 派生）、列表为全局注册表；
   - 审计动作表：`admin.user.update`、`instruction.update`（detail.scope=platform）、`skill.enable`、`skill.force_disable`、`mcp.server.create/update/delete/check`、`agent.updated`；
   - “M8 无迁移；迁移 head 仍为 `2d8f6a1c3e70`”。
5. 前端 handoff（`docs/feedback/2026-11-11-m8-frontend-handoff.md`）：
   - 认证/403 处理：`error.code=="forbidden"` 且 `error.details.required_role=="admin"` 时展示“需要管理员权限”；401 走刷新/登录；
   - 用户页：列表查询参数/分页/`stats`；自己的行禁用“改角色/停用”控件；422/409 文案映射；
   - 概览页：字段树与示例、刷新频率建议 ≥30s（精确计数，不要秒级轮询）；
   - 系统指令页：GET/PUT 示例、`null` 恢复默认、`is_default`/`updated_by_email` 展示；
   - 系统技能页：启停与强制停用两个开关、`force_disabled` 对用户覆盖的压制说明；
   - 系统 MCP 页：表单字段、`secret:NAME` 需先在 `/admin/secrets` 建密钥、PATCH 字段集、保存后可能 `unhealthy` 的提示；
   - 全局 Agent 页：可编辑字段、只读字段、`secret:` 引用；
   - 错误码表与 OpenAPI 链接；明确“无迁移、无新环境变量”。
6. `README.md`：状态段 M8 改为“实施完成，待 T8 全量回归”；“明确未包含”去掉 M8；文档索引加 M8 计划/handoff/报告占位。
7. `docs/roadmap.md`：M8 状态改为“实施中（代码完成，待回归）”，勾选已交付项并写“无迁移”；T8 再改“已完成”并补证据。
8. `docs/architecture.md`：新增“管理面（M8）”小节：`/admin/*` 是配置面薄适配层，领域规则在各自服务，`AdminUser` 门禁，无迁移。
9. `docs/database.md`：在“迁移规范/表说明”附近注明 M8 未新增表/列，管理端复用 `users`/`system_settings`/`skills`/`mcp_servers`/`remote_agents`/`user_capability_overrides`。

- [ ] **Step 1: 导出 OpenAPI**：`.venv/bin/python scripts/export_openapi.py`；`git diff --stat docs/openapi.json` 只应包含 M7（若尚未导出）+ M8 新增路径/schemas。
- [ ] **Step 2: 写 `docs/api.md`** 上述 §1/§2.12/§7.6/§11；核对每个字段名与代码一致。
- [ ] **Step 3: 写 handoff**；包含 curl 示例（登录后取 token、GET 用户、PUT 指令、POST 系统 MCP、PATCH Agent、GET 概览），示例值不得使用真实密钥。
- [ ] **Step 4: 同步 README/roadmap/architecture/database**；更新 `app/api/schemas.py` 错误码描述（若改，重新导出 OpenAPI 并复核 diff）。
- [ ] **Step 5: 验证**：`git diff docs/openapi.json` 无意外；所有 Markdown 相对链接可达（可用 `grep -o` 抽链接人工核对）；`pytest -q` 仍全绿；`ruff check app tests`。

**Acceptance:** OpenAPI 与代码一致；`docs/api.md` 覆盖全部 M8 端点、字段、错误码与规则；handoff 能让前端独立接入；README/roadmap/architecture/database 状态与口径一致；无未记录的新配置/迁移。

---

### Task 8: 全量回归、独立审查与交付报告

**Files:**
- Add: `docs/feedback/2026-11-11-m8-backend-report.md`
- Modify: `README.md`、`docs/roadmap.md`（最终状态）
- Test: 全量

- [ ] **Step 1: 全量验证**

```bash
TEST_DATABASE_URL="${TEST_DATABASE_URL:?请先导出本机测试库 URL}" \
  .venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .

# 迁移基线：沿用 M6 报告 §2.1 的「空库 upgrade head」做法。
# 不要对 pytest 已 create_all 过的测试库直接 upgrade；在同一 PostgreSQL 上新建
# 一个空库（示例名 agent_hub_m8_mig），执行后可 drop。
MIG_DATABASE_URL="${MIG_DATABASE_URL:?请先导出空库 URL}"
DATABASE_URL="${MIG_DATABASE_URL}" .venv/bin/python -m alembic upgrade head
DATABASE_URL="${MIG_DATABASE_URL}" .venv/bin/python -m alembic check
DATABASE_URL="${MIG_DATABASE_URL}" .venv/bin/python -m alembic heads   # 期望仍为 2d8f6a1c3e70 (head)

.venv/bin/python scripts/export_openapi.py && git diff --exit-code docs/openapi.json
git status --short alembic/versions   # 必须为空（无新迁移文件）
```

Expected：pytest 全绿、数量 ≥ M7 交付后的基线（+ 本计划新增用例）；ruff `All checks passed!`；`alembic check` 输出 `No new upgrade operations detected.`；`alembic heads` 仍为 `2d8f6a1c3e70`；OpenAPI 无 diff；`alembic/versions/` 无新增。

- [ ] **Step 2: 独立审查**：另起子代理（不共享本会话上下文）对照 spec §4 + 本计划审查全部变更，重点 Review Focus 1–6；要求按 Critical/Important/Minor 分级；Critical/Important 修完复跑 Step 1。
- [ ] **Step 3: 写交付报告** `docs/feedback/2026-11-11-m8-backend-report.md`：
  - 日期/范围/设计/计划/前置 M7 依赖；
  - 交付表（文件 → 说明，按 T1–T7）；
  - 实测证据：pytest 数量、ruff、`alembic check`、OpenAPI diff、迁移 head；
  - 关键决策（R1–R10）与偏差（尤其是全局 Agent 口径、能力开关绕过）；
  - 未做项/非目标（密码重置、强制下线、多角色、导出/批量、前端页面）；
  - 风险与遗留（精确计数成本、全局 Agent owner 语义待确认、审计过滤未扩展）；
  - 前端 handoff 索引与验收对照表（spec §4.4 逐条勾选）。
- [ ] **Step 4: 最终状态**：README/roadmap 的 M8 改为“已完成（2026-11-11）”，补测试数量/证据链接。
- [ ] **Step 5: 本地提交**（只提交显式路径，不推送）：

```bash
git add app/modules/admin app/modules/instructions/service.py \
  app/modules/skills/service.py app/modules/mcp/service.py app/modules/mcp/schemas.py \
  app/modules/agents/service.py app/modules/agents/schemas.py app/api/v1/router.py app/api/schemas.py \
  tests/integration/test_admin_users.py tests/integration/test_admin_overview.py \
  tests/integration/test_admin_instructions.py tests/integration/test_admin_skills.py \
  tests/integration/test_admin_mcp.py tests/integration/test_admin_agents.py \
  docs/api.md docs/openapi.json docs/feedback/2026-11-11-m8-frontend-handoff.md \
  docs/feedback/2026-11-11-m8-backend-report.md README.md docs/roadmap.md \
  docs/architecture.md docs/database.md docs/superpowers/plans/2026-11-11-m8-admin-console.md
git commit -m "feat: M8 管理台后端（用户/概览/系统项/契约）"
```

**Acceptance:** 全量测试通过且不低于基线；ruff 全绿；无迁移漂移/新增；OpenAPI 与代码一致；独立审查无未解决 Critical/Important；报告可复现；本地提交完成。

---

## 任务顺序与依赖

```text
T1（路由骨架 + 用户管理）
 ├─► T2（概览）
 ├─► T3（平台指令）
 ├─► T4（系统技能）
 ├─► T5（系统 MCP）
 └─► T6（全局 Agent）
T1–T6 ─► T7（文档/OpenAPI/handoff）─► T8（回归/审查/报告）
```

- **默认串行执行**：T2–T6 都向同一个 `app/modules/admin/router.py` 追加路由；若要多代理并行，先由 T1 拆成 `users.py`/`overview.py`/… 子路由并由聚合器 include，否则同文件写冲突。
- T1 是唯一有外部前置的任务（M7-2）；T2–T6 彼此无代码依赖，但都依赖 T1 的包/路由骨架与 `AdminUser` 挂载方式。
- T7 依赖 T1–T6 的最终字段名；若 T7 发现代码与本文档不一致，**以本文档契约修正代码**（契约先行），不要反向改文档迁就实现。
- T8 依赖 T7 的 OpenAPI 导出结果；T8 不再新增功能。

## 风险与回滚

| 风险 | 触发/影响 | 缓解 |
|---|---|---|
| M7-2 未落地或 403 无 `details.required_role` | T1 直接失败，全站管理端 403 契约不一致 | T1 Step 1 硬性前置检查；缺失即停止并报告，M8 不重复实现 M7 |
| 最后管理员并发互降 | 两个管理员同时降级对方，可能 0 管理员 | 锁 `active admin` 行（`ORDER BY id FOR UPDATE`）+ 锁后 refresh 再计数；服务层测试守护 |
| 管理员自我锁死 | 误改自己 role/status 后无法进入管理台 | 自操作 422 双规则 + 前端 handoff 禁用控件；必要时运维 `grant_admin.py` 恢复 |
| 密钥泄露 | 系统 MCP/Agent 的 headers/密文进入响应、审计或日志 | 复用不含 headers 的输出 DTO；审计只写字段名；测试扫描响应/审计文本；永不记录请求头 |
| 401/403 混淆 | 前端把 403 当登录失效反复刷新 | handoff 明确 401 刷新、403 显示无权限；测试断言 `details.required_role` |
| 概览慢/扫全表 | 大表 `count(*)` 慢，前端高频刷新 | 单次 `GROUP BY`、不拉行、不 N+1；前端建议 ≥30s；测试 SELECT ≤10；未来再加统计表 |
| 全局 Agent 口径偏差 | spec 写 `owner_user_id IS NULL`，实际创建写 owner | R8 采用“全部未删除”的 v1 全局注册表口径，测试固定；需用户最终确认；若坚持 NULL 口径需数据回填/创建逻辑变更（超出无迁移前提） |
| 能力开关语义争议 | 关闭技能/MCP 后管理员无法改配置 | R1 配置面绕过开关，运行态仍受控；测试固定；若产品要一致，改为 `_require_enabled` 即可回滚 |
| 系统 MCP 删除留孤儿覆盖 | `user_capability_overrides` 无 FK | 删除同事务清理 `capability='mcp_server'` 的 target 行；测试断言 |
| 既有测试因 M7 改动失败 | agents/tasks 写接口收紧到 admin | M7 负责更新既有用例；M8 新用例统一用 `admin_auth`；T8 全量回归兜底 |
| OpenAPI 大 diff | M7 未导出就进入 M8，混入大量无关变更 | T7 Step 1 先核对 diff 来源；M7 的 OpenAPI 应由 M7 自己导出；T8 要求零 diff |
| 无迁移但环境漂移 | 目标库缺列/迁移落后 | T8 `alembic upgrade head` + `alembic check`；启动 `/readyz` 已检查迁移 |

## 假设与开放问题（决策优先）

| # | 问题 | 决定（Ruling） | 需用户确认 |
|---|---|---|---|
| R1 | 能力总开关关闭时，管理端配置接口是否也 403 `capability_disabled`？ | **不 403**：管理端是配置面，关闭能力后仍应能修正配置；运行态接口保持原行为。 | 是（低风险，可一行回退） |
| R2 | `PATCH /admin/users` 可改字段范围？ | 只改 `role`/`status`；`email/display_name/password` 不在 M8（密码重置是明确非目标）。 | 否 |
| R3 | 自己停用是否允许？spec 只禁止改自己 role | **禁止**：422 `cannot_disable_self`，避免自锁死；同时禁止改自己 role（422 `cannot_change_own_role`）。 | 是（若产品允许自我停用可去掉） |
| R4 | 最后活跃管理员规则用什么状态码/码值？ | 409 `last_admin_protected` + `details.active_admins=1`；自操作从前两条规则先拒绝。 | 是（前端接入前冻结命名） |
| R5 | 是否返回 `effective_preview`？ | **不返回**：平台默认文本本身就是无个人覆盖用户实际注入的内容，与 `content` 重复；后续要“按用户预览”再加 query 参数。 | 否（新端点，无兼容负担） |
| R6 | 系统技能/MCP 列表是否分页？用户详情是否带活动计数？ | 不分页（系统项数量有上界）；用户详情与列表同形、不带活动计数（概览给全局计数）。 | 否（低风险） |
| R7 | 全局 Agent 哪些字段可编辑？ | `name/enabled/requires_approval/auth/headers`；`description` 由 Card 派生且 `refresh` 会覆盖，只读；`extra="forbid"` 显式 422。 | 否（可用 refresh 维护 Card 字段） |
| R8 | 全局 Agent 用 `owner_user_id IS NULL` 还是全部未删除？ | **全部未删除**：v1 `GET /agents` 本就返回全局注册表，创建路径写 `owner_user_id=当前管理员`；按 NULL 过滤会让所有现存 Agent 不可见。测试固定“created by admin 仍可见”。 | **是（与 spec §4.2 字面表述不一致，优先确认）** |
| R9 | 审计是否新增过滤（spec 写“过滤扩展”）？ | M8 **不扩展**：现有 `actor_type/actor_id/action/resource_type/resource_id/start/end/cursor` 已覆盖管理页；`request_id`/全文搜索留后续。 | 是（若前端审计页需要 `request_id`/`q` 再加） |
| R10 | 审计动作用什么命名？ | `admin.user.update`、`instruction.update`（复用既有动作，detail.scope=platform）、`skill.enable`、`skill.force_disable`、`mcp.server.create/update/delete/check`、`agent.updated`（系统项写操作 detail 标 scope/source）；detail 只字段名/状态值。 | 是（前端筛选可能依赖 action 字符串） |

## 验收对照（spec §4.4）

- [ ] 管理员可查用户、改角色/状态；「不能改自己」「不能禁用/降级最后一个活跃 admin」有 HTTP/服务层测试。
- [ ] 平台指令、系统技能、系统 MCP、全局 Agent 可读可改；`force_disabled` 压过用户覆盖（技能与 MCP 各有测试）。
- [ ] 普通用户访问任何新增 `/admin/*` 返回 403 `forbidden` + `details.required_role="admin"`；未登录 401。
- [ ] 关键写操作全部写审计；`GET /admin/overview` 返回计数且无 N+1/ORM 行加载。
- [ ] OpenAPI / `docs/api.md` / 前端 handoff / README / roadmap 同步；无迁移；全量测试与 ruff 全绿。
