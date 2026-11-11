# M7 契约补齐与安全收口实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补上「事件回放事件名 + SSE 续流端点」，把 agents/tasks 写接口收紧到管理员，补齐审批事件字段，给 A2A `authorization_url` 加后端白名单，并同步契约文档。

**Architecture:** 回放在现有 `RunEventOut` 上新增 `event` 字段（`payload._event` 继续作为内部字段）；新增 `GET /runs/{id}/stream` 复用 `stream_run_events()` 的「回放 + 实时跟随」生成器，路由层先做所有权与回放上限校验。RBAC 复用已存在的 `AdminUser` 依赖（`app/core/deps.py`），只替换 agents/tasks 写接口的依赖并补拒绝审计。审批字段在 `_ensure_approval` 发射处补齐。授权 URL 校验抽独立函数，在 A2A `auth_required` 落库前调用。

**Tech Stack:** FastAPI、SQLAlchemy 2.x async、pytest-asyncio、httpx、sse-starlette 风格手写 SSE（现有 `format_sse`）。

**Spec:** `docs/superpowers/specs/2026-11-11-m7-m9-backend-roadmap-design.md` §3

## Global Constraints

- Python `>=3.13`；ruff `line-length = 100`；注释与用户可见错误消息一律中文。
- 测试零网络、零外部存储；不启动新容器（复用 `dev_postgres`）。
- 每个任务结束时 `pytest -q` 必须全绿；新测试先失败后实现。
- 错误走 `app/core/errors.py` 既有类 + `code` 参数，不自造信封。
- 新增端点必须声明 400/401/403/404/409/500 契约（沿用 `docs/api.md` 错误信封）。
- 提交只在本地，不推送、不删分支（用户偏好）。
- 在 `master` 内联实施，不建 worktree（用户偏好）。

## Review Focus

1. **续流所有权**：非本人 run 必须 404，不能 403（避免探测）；未带 token 401。
2. **续流边界**：`after_seq` 刚好等于最后一条、run 已终态、`interrupted` 等待恢复、回放超 1000 条。
3. **RBAC 不误伤**：只收紧写接口，`GET /agents*`、`GET /tasks*`、`POST /agents/preview` 保持登录用户可用。
4. **审批字段兼容**：新增 5 字段不改变旧字段；事件体积可控（preview 500 / target 2000）。
5. **URL 校验**：`javascript:`/`data:`/`file:`/`ftp:`、内网 IP、超长、大小写绕过都要拒。
6. **旧前端兼容**：M7 结束时旧前端不传新字段、不调新端点也必须正常工作。

---

### Task 1: 运行事件回放补 `event` 字段

**Files:**
- Modify: `app/modules/chat/schemas.py`（`RunEventOut`）
- Modify: `app/modules/chat/service.py`（`list_run_events` 填充）
- Test: `tests/integration/test_chat.py`（补断言）或新增 `tests/integration/test_run_events.py`

**Interfaces:**
- Produces: `RunEventOut.event: str`（必填，原始事件名；未知时 `""`）
- Consumes: `RunEvent.payload["_event"]`

- [ ] **Step 1: 写失败测试**：发一轮聊天，`GET /runs/{id}/events` 返回项的 `event` 等于 SSE 事件名（`run.started`/`message.done` 等）；`payload` 中不再出现 `_event`；`type` 保持分类不变。
- [ ] **Step 2: 运行测试确认失败**（`pytest tests/integration/test_chat.py -k events -v`）。
- [ ] **Step 3: 实现**：`RunEventOut` 加 `event: str`；`list_run_events` 循环里取 `event_name = str((row.payload or {}).get("_event") or "")` 传入，`payload` 仍剔除 `_event`。
- [ ] **Step 4: 验证**：该测试通过；`pytest -q` 全绿；`ruff check app/ tests/`。

**Acceptance:** 旧字段零变化；新增字段对所有事件类型可用。

---

### Task 2: 新增 SSE 续流端点 `GET /runs/{run_id}/stream`

**Files:**
- Modify: `app/modules/chat/service.py`（`stream_run_events` 增加 `max_replay`；新增只读所有权/回放上限检查）
- Modify: `app/modules/chat/router.py`（新增路由 + SSE 描述 + 响应声明）
- Test: `tests/integration/test_run_stream.py`（新增）

**Interfaces:**
- Produces: `GET /api/v1/runs/{run_id}/stream?after_seq=<int:0>` → `text/event-stream`
- Consumes: `service.stream_run_events(run_id, after_seq=..., max_replay=1000)`、`service.get_run(db, user, run_id)`（所有权）、`format_sse`
- Errors: 401 未登录；404 非本人/不存在；400 `replay_too_large`（回放 > 1000 条，提示用分页接口）；run 已终态且无事件时返回 200 空流后立即关闭（不引入额外错误码）

- [ ] **Step 1: 写失败测试**（用 `httpx.ASGITransport` 会缓冲 SSE，实时部分用服务层 `collect_run_events_live` 或直接消费生成器）：
  - 已完成的 run：`GET .../stream?after_seq=0` 依次收到已落库事件，终态后流结束；
  - `after_seq` 跳过前 N 条；
  - 他人 run：404；未登录：401；
  - 回放 > 1000：400 `replay_too_large`；
  - `interrupted` run：回放后保持连接，审批恢复后继续收到事件（可用服务层测试）。
- [ ] **Step 2: 运行测试确认失败**。
- [ ] **Step 3: 实现路由**：
  - `@router.get("/runs/{run_id}/stream", response_class=StreamingResponse, responses=_SSE_RESPONSES)`
  - 路由内先 `await service.get_run(db, user, run_id)` 做所有权；再 `await service.ensure_replay_size(db, run_id, after_seq, max_replay=1000)`；
  - `StreamingResponse(service.stream_run_events(run_id, after_seq=after_seq), media_type="text/event-stream", headers={...})`；
  - 响应头 `X-Run-ID`、`X-Request-ID`；不重复 `_SSE_HEADERS` 之外的 buffer 头。
- [ ] **Step 4: 实现服务**：`ensure_replay_size` 用 `select(count())` 或 `limit(max_replay+1)`；`stream_run_events` 透传 `max_replay`（生成器内部仍按 seq 回放，超限已在路由拦截）。
- [ ] **Step 5: 补 OpenAPI 描述**（§7.8/§10.2 口径：回放 + 实时、终态关闭、15s 心跳、单实例）。
- [ ] **Step 6: 验证**：新测试 + `pytest -q` + ruff。

**Acceptance:** 前端可用 `fetch-event-source` 带 Bearer 从 `lastSeq` 续流；非本人不可见；旧 `POST /chat/stream` 行为不变。

---

### Task 3: agents/tasks 写操作收紧到管理员

**Files:**
- Modify: `app/modules/agents/router.py`（写接口 `CurrentUser` → `AdminUser`）
- Modify: `app/modules/tasks/router.py`（动作接口 `CurrentUser` → `AdminUser`）
- Modify: `app/core/deps.py`（如需 403 `details.required_role` 则调整 `ForbiddenError` 调用）
- Test: `tests/integration/test_rbac.py`（新增）

**Interfaces:**
- Produces: `AdminUser` 依赖用于 agents/tasks 写接口；403 `forbidden` + `details.required_role="admin"`
- Consumes: `app.core.deps.AdminUser`（已存在）、`write_audit`（拒绝时 `security.rbac_denied`）

- [ ] **Step 1: 写失败测试**：普通用户 `POST /agents`、`PATCH/DELETE /agents/{id}`、`POST /agents/{id}/refresh|health`、`POST /tasks/{id}/cancel|resume|authorize|reject` 均 403；管理员同操作成功；`GET /agents`、`GET /tasks`、`POST /agents/preview` 普通用户仍 200。
- [ ] **Step 2: 运行确认失败**。
- [ ] **Step 3: 改依赖**：写接口签名换成 `admin: AdminUser`；保留 `user` 仅读接口。
- [ ] **Step 4: 审计**：`require_admin` 失败路径或路由内捕获写 `security.rbac_denied`（actor/action/resource）；成功写操作沿用现有审计。
- [ ] **Step 5: 契约**：`docs/api.md` 在 agents/tasks 章节标注「写操作仅管理员」；OpenAPI 403 描述补 `forbidden`。
- [ ] **Step 6: 验证**：RBAC 测试 + `pytest -q` + ruff。

**Acceptance:** 普通用户无法写；读与预览不受影响；403 信封与全站一致。

---

### Task 4: `approval.required` 事件字段补齐

**Files:**
- Modify: `app/modules/chat/approvals.py`（emit payload）
- Test: `tests/integration/test_hitl_interrupt.py`（补断言）

**Interfaces:**
- Produces: 事件 payload 新增 `tool_display_name`、`executor`、`target`、`side_effect`、`payload_preview`
- Consumes: `Approval` 行已加载字段

- [ ] **Step 1: 写失败测试**：触发审批后，`approval.required` 的 `data` 含 5 个新字段，值与 `GET /approvals/{id}` 一致；`payload`（原始参数）仍脱敏。
- [ ] **Step 2: 运行确认失败**。
- [ ] **Step 3: 实现**：在 `_ensure_approval` 的 `runner.publisher.emit(...)` 字典中补 5 个字段（`approval.tool_display_name`、`executor`、`target`、`side_effect`、`payload_preview`）。
- [ ] **Step 4: 验证**：HITL 测试 + `pytest -q` + ruff。

**Acceptance:** 前端可直接从事件渲染审批卡；无需再拉 `GET /approvals?run_id=` 补字段（仍保留该路径作为兜底）。

---

### Task 5: A2A `authorization_url` 后端白名单

**Files:**
- Add: `app/core/urls.py`（或 `app/modules/a2a/urls.py`，避免与 SSRF 模块耦合过深）
- Modify: `app/modules/a2a/tool.py`（`auth_required` 落库前校验）
- Modify: `app/modules/tasks/service.py`（展示层复用同一校验，防历史脏数据）
- Modify: `app/core/config.py` / `.env.example`（`A2A_AUTH_URL_ALLOWED_HOSTS`，可选）
- Test: `tests/test_auth_url_unit.py`（新增）

**Interfaces:**
- Produces: `validate_authorization_url(value: str | None, *, env: str, allowed_hosts: list[str]) -> str | None`
- 规则：`https` 一律允许（host 不在私网/回环）；`dev/test` 允许 `http://localhost`、`http://127.0.0.1`；拒绝 `javascript:`/`data:`/`file:`/`ftp:`/无 scheme/超长（>2000）；`allowed_hosts` 非空时只放行白名单。
- 违规：返回 `None`，调用方记 warning + 审计 `security.auth_url_rejected`（`detail` 只存截断后的原值前 200 字符）。

- [ ] **Step 1: 写失败测试**：表驱动覆盖合法/非法/大小写/空白/内网/超长/白名单。
- [ ] **Step 2: 运行确认失败**。
- [ ] **Step 3: 实现校验函数**（`urllib.parse.urlsplit`，`ipaddress` 判私网；不联网）。
- [ ] **Step 4: 接入**：`tool.py` 写 `task.auth_request` 时调用；`tasks/service.py::_waiting_out` 对历史数据再校验一次（不写审计）。
- [ ] **Step 5: 配置**：`A2A_AUTH_URL_ALLOWED_HOSTS` 默认空；`.env.example` 与 `Settings` 同步新增，数量保持一致（144 → 145）。
- [ ] **Step 6: 验证**：单测 + `pytest -q` + ruff。

**Acceptance:** 恶意链接不会出现在 API/SSE 里；合法 https 链接行为不变；历史脏数据展示时也被过滤。

---

### Task 6: 契约文档、OpenAPI 与前端 handoff

**Files:**
- Modify: `docs/api.md`（§7.7 `event`、§7.8 续流端点、§10.2/§10.4、§2.9/§2.12 权限）
- Modify: `docs/openapi.json`（重新导出）
- Add: `docs/feedback/2026-11-11-m7-frontend-handoff.md`（或实施时按日期命名）
- Modify: `README.md`、`docs/roadmap.md`（M7 状态）
- Modify: `docs/architecture.md`（SSE 回放/续流一节）

- [ ] **Step 1: 导出 OpenAPI**：`.venv/bin/python scripts/export_openapi.py`；确认只有预期新增。
- [ ] **Step 2: 写 handoff**：新字段/新端点/403 行为/续流示例（fetch-event-source 代码片段）/旧前端兼容说明。
- [ ] **Step 3: 同步 roadmap/README**：M7 状态从「已立项」改为「进行中/已完成」按实际。
- [ ] **Step 4: 验证**：`git diff docs/openapi.json` 只含预期；链接可达。

---

### Task 7: 全量回归与独立审查

**Files:**
- Add: `docs/feedback/2026-11-11-m7-backend-report.md`
- Test: 全量

- [ ] **Step 1: 全量验证**：

```bash
TEST_DATABASE_URL=postgresql+psycopg://devuser:***@localhost:54321/agent_hub_test \
  .venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
.venv/bin/python -m alembic check
.venv/bin/python scripts/export_openapi.py && git diff --exit-code docs/openapi.json
```

- [ ] **Step 2: 独立审查**：另起子代理对照 spec/计划审查变更（重点 Review Focus 1–6），Critical/Important 修完复跑。
- [ ] **Step 3: 交付报告**：实测证据、决策、未做项、前端 handoff 索引。
- [ ] **Step 4: 提交**：本地提交，不推送。

**Acceptance:** 全量 ≥582 passed；ruff 全绿；`alembic check` 无漂移；OpenAPI 无意外 diff；报告可复现。

---

## 任务顺序与依赖

```
Task 1（event 字段）→ Task 2（续流端点，依赖 Task 1）
Task 3（RBAC）可与 Task 1/2 并行
Task 4（审批字段）独立
Task 5（URL 校验）独立
Task 6（文档）依赖 1–5
Task 7（回归/审查）最后
```

## 风险与回滚

- 续流端点若出现 SSE 泄漏/鉴权遗漏，直接下线该路由（旧 `GET /runs/{id}/events` 仍在），不影响主聊天；
- RBAC 若误伤前端，回滚路由依赖即可（`AdminUser` → `CurrentUser`），无迁移；
- `authorization_url` 校验过严会让合法回调不可点，可通过 `A2A_AUTH_URL_ALLOWED_HOSTS` 放宽；
- M7 不新增迁移，回滚不涉及数据库。
