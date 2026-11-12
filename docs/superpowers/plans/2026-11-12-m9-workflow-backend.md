# M9 工作流后端（原 X1）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付轻量步骤模板工作流 MVP：`workflows`/`workflow_runs` 两表、步骤模板 CRUD 与启停、运行入口复用 `POST /chat/stream` 的 `workflow_id`、`workflow.step` 事件与运行历史查询，契约文档与前端 handoff 同步。

**Architecture:** 主图不改。执行模型为「步骤模板快照注入 system prompt + 工具白名单过滤 + `approval` 步骤强制审批闸门」：`start_chat` 校验并把 steps 快照进 `run.capabilities`，`prepare_runtime` 读快照装配（模板段落拼 prompt 末尾、`bind_tools` 只暴露白名单并集），`ToolRunner` 在工具完成后按「工具名→步骤」映射 emit `workflow.step`。新增 `app/modules/workflows/`（CRUD/启停/运行历史），全部裁决与注入点证据见 [Spike 报告](../specs/2026-11-12-m9-spike-report.md)。

**Tech Stack:** FastAPI、SQLAlchemy 2.x async、Pydantic v2、pytest-asyncio、httpx、PostgreSQL（复用本机 `dev_postgres`）；测试用 `ScriptedToolModel`/`FakeA2aAgent`/fake embedding，零网络零新容器。不新增第三方依赖。

**Spec:** [M7–M9 立项](../specs/2026-11-11-m7-m9-backend-roadmap-design.md) §5（M9 范围/验收/非目标）+ [M9 Spike 报告](../specs/2026-11-12-m9-spike-report.md)（执行语义裁决，本计划以其为输入）

## Global Constraints

- Python `>=3.13`；ruff `line-length = 100`；注释、docstring、API 描述、用户可见错误消息一律中文。
- **迁移纪律**：唯一新迁移 `down_revision = "2d8f6a1c3e70"`（真实 head；M4 文件名日期新但链在 M5 之前，**以 down_revision 为准**）。每个动迁移的任务合入前跑 `.venv/bin/alembic heads` 必须单 head。
- **测试库不跑 alembic**（`Base.metadata.create_all` 建表）；迁移可逆性一律用独立临时库验证（建库→预装 pgvector→upgrade/downgrade→删库）。
- `tests/conftest.py` 的 `_TABLES` TRUNCATE 清单是硬编码，新表 `workflow_runs, workflows` 追加在最前。
- 复用既有设施，不自造：错误用 `app.core.errors` 的 `DomainError` 子类；游标分页用 `app.core.cursor.encode_cursor/decode_cursor`（limit+1 探测）；审计用 `app.core.audit.write_audit`；A2A 测试传输 `app.modules.a2a.client.register_test_transport` + `FakeA2aAgent`；假模型 `tests/helpers.make_scripted_model`。
- 错误语义（全里程碑统一）：模板不存在/不可见 → 404 `not_found`；停用 → 409 `workflow_disabled`；版本不匹配 → 409 `workflow_version_conflict`；steps 非法 → 422 `workflow_steps_invalid`；他人模板写操作 → 404（不泄露存在性）。
- steps schema 约束（装配器 `validate_steps` 强制）：非空数组、≤12 步、`type ∈ {knowledge_search, tool, a2a, approval, answer}`、`id` 唯一非空、`instruction` ≤500 字符、单工具名 ≤200 字符、`tool` 步骤必须显式 `config.tools`、全部文本总长 ≤8000 字符。
- 快照原则：`start_chat` 把 `workflow_steps` 与 `workflow_id/workflow_version` 快照进 run（列 + `run.capabilities`），执行与恢复只读快照；快照缺失时回退查表（防御），查不到按 run 数据缺失失败。
- 版本规则：`PATCH /workflows/{id}` 成功必 `version += 1`；`PUT .../enabled` 不改 version。
- `workflows_enabled` 开关（默认 `True`）：关闭时 chat 带 `workflow_id` → 409 `workflow_disabled`，CRUD 仍可用（配置面不受开关影响，同 M8 R1 惯例）；`GET /system/info` 的 `capabilities.workflows` 反映开关。
- 系统模板（`scope='system'`、`owner_user_id IS NULL`）v1 由 DB/脚本创建，CRUD API 只管理 `scope='user'`；系统模板人人可读、任何人不可改删（管理端点留候选池）；`GET /workflows/{id}/runs` 仅模板 owner（系统模板 403，运行数据属各用户）。
- 不做非目标：DAG/并行/条件跳转、外部引擎、拖拽编排、跨用户共享、`/admin/workflows`、`WorkflowRun` 终态以外的状态机。
- 提交只在本地、不推送、不删分支；`git add` 用显式路径，禁止 `-A`。

## Review Focus

1. **resume 一致性**：审批挂起期间模板被 PATCH/停用/删除，恢复后必须仍按发起时的 steps 快照执行，不得改走新版本或报模板不存在。测试：T5 `test_resume_uses_snapshotted_steps_after_workflow_patch`。
2. **越权面**：他人私有模板的读（详情）与写（PATCH/DELETE/enabled/runs）必须与不存在同响应（404），系统模板的 runs 列表 403，不得泄露他人运行数据。测试：T3 `test_foreign_workflow_read_and_write_hidden_as_404`、T4 `test_system_workflow_runs_forbidden`。
3. **白名单是安全边界**：`bind_tools` 只能收到步骤白名单并集；空白名单不过滤仅是防御坏模板，正常路径禁止出现「模板存在但全工具放行」。测试：T6 端到端断言 `build_graph` 收到的工具名集合。
4. **`workflow.step` 事件契约**：必须紧跟对应 `tool.result`（seq 连续）、`event_type="workflow"`、payload 只含 `step_id/step_type/step_title/tool/status`，回放（M7 `event` 字段）可见。测试：T6 事件序列断言。
5. **steps 注入的信任边界**：`instruction` 是创建者内容（同技能正文信任级），直接注入 prompt 但受 500 字符约束；工具面由白名单硬约束，提示词注入不能放大可调用工具集。测试：T2 `test_validate_steps_length_budget`（总长 8000 上限）。

## 文件结构

```text
app/db/models/workflow.py            # 新增：Workflow / WorkflowRun
app/db/models/__init__.py            # 修改：注册两模型
alembic/versions/20261112_1400_<rev>_m9_workflow_tables.py   # 新增迁移（down=2d8f6a1c3e70）
app/modules/workflows/
├── __init__.py                      # 包声明
├── assembler.py                     # 纯函数装配器（validate/allowlist/force/step_for_tool/prompt_section/filter_tools）
├── schemas.py                       # Workflow CRUD DTO
├── service.py                       # CRUD/启停/运行历史（唯一新增业务 SQL）
└── router.py                        # /workflows* 路由
app/modules/chat/service.py          # 修改：_validate_workflow、start_chat 快照、prepare_runtime 注入、_finalize_* 终态同步
app/modules/chat/tools.py            # 修改：ToolRunner 两参数 + workflow.step 事件 + approval_spec 兜底
app/modules/chat/schemas.py          # 不动（workflow_id/workflow_version/RunWorkflowOut 已存在）
app/modules/system/service.py        # 修改：capabilities.workflows 读开关
app/core/config.py                   # 修改：workflows_enabled: bool = True
app/api/v1/router.py                 # 修改：include workflows_router
tests/conftest.py                    # 修改：_TABLES 追加两表
tests/integration/test_workflow_models.py / test_workflow_crud.py / test_workflow_runs_api.py / test_workflow_chat.py / test_workflow_execution.py
docs/api.md / docs/openapi.json / docs/roadmap.md / docs/feedback/2026-11-12-m9-frontend-handoff.md
```

---

### Task 1: 数据层——两表模型与迁移

**Files:**
- Create: `app/db/models/workflow.py`
- Modify: `app/db/models/__init__.py`
- Modify: `tests/conftest.py`（`_TABLES`）
- Create: `alembic/versions/20261112_1400_<自动生成或自定 rev>_m9_workflow_tables.py`
- Test: `tests/integration/test_workflow_models.py`

**Interfaces:**
- Produces: `Workflow`（列：`id/owner_user_id/scope/name/description/steps/enabled/version/created_at/updated_at`，CHECK `scope IN ('system','user')`）与 `WorkflowRun`（列：`id/workflow_id/run_id/status/created_at/updated_at`，CHECK status 五态，唯一索引 `uq_workflow_runs_run(run_id)`，索引 `ix_workflow_runs_workflow_created(workflow_id, created_at DESC)`），后续所有任务从 `app.db.models.workflow` 导入。
- Produces: 迁移 revision id（记入 Task 8 文档），down_revision 固定 `2d8f6a1c3e70`。

- [ ] **Step 1: 写失败测试** `test_workflow_models.py`：直接向 DB 插 `scope='system'` 与 `scope='user'`（带 owner）各一行断言可查回；`scope='bogus'` 插入触发 CheckConstraint；`workflow_runs.run_id` 重复插入触发唯一索引。
- [ ] **Step 2: 跑测试确认失败**：`TEST_DATABASE_URL=postgresql+psycopg://devuser:devpass@localhost:54321/agent_hub_test .venv/bin/python -m pytest tests/integration/test_workflow_models.py -q` → FAIL（ImportError: Workflow）。
- [ ] **Step 3: 实现模型与注册**：模型列按 Interfaces；`steps` 用 `JSONB` default `[]`；参照 [Spike 报告 §3](../specs/2026-11-12-m9-spike-report.md)（spike 同构模型已验证）。`__init__.py` 注册两模型；`conftest.py` `_TABLES` 最前追加 `"workflow_runs, workflows, "`。
- [ ] **Step 4: 跑测试确认通过**：同 Step 2 命令 → PASS。
- [ ] **Step 5: 写迁移并验证可逆**：迁移内容 = spike 迁移（`workflows`/`workflow_runs` 建表 + 两索引 + CHECK），`down_revision="2d8f6a1c3e70"`。验证：`docker exec dev_postgres psql -U devuser -d postgres -c "DROP DATABASE IF EXISTS m9_mig_check" -c "CREATE DATABASE m9_mig_check"`；`CREATE EXTENSION vector` 用 `psql -U devuser -d m9_mig_check`；`DATABASE_URL=postgresql+psycopg://devuser:devpass@localhost:54321/m9_mig_check .venv/bin/alembic upgrade head` → `downgrade -1` → `upgrade head` 全部成功；`.venv/bin/alembic heads` 单 head；最后 DROP 该库。
- [ ] **Step 6: Commit**：`git add app/db/models/workflow.py app/db/models/__init__.py tests/conftest.py alembic/versions/<迁移文件> tests/integration/test_workflow_models.py && git commit -m "feat: M9 工作流两表模型与迁移（T1）"`。

### Task 2: 步骤装配器（纯函数）

**Files:**
- Create: `app/modules/workflows/__init__.py`、`app/modules/workflows/assembler.py`
- Test: `tests/integration/test_workflow_assembler.py`（纯函数，无需 DB）

**Interfaces:**
- Produces（后续 T5/T6 消费，签名固定）:
  - `STEP_TYPES: tuple[str, ...]`、`TYPE_DEFAULT_TOOLS: dict[str, tuple[str, ...]]`（`knowledge_search→("search_knowledge",)`、`a2a→("send_message",)`）
  - `validate_steps(steps: Any) -> list[str]`（错误列表，空=合法；含 Global Constraints 全部约束与 8000 字符总预算）
  - `tool_allowlist(steps) -> set[str]`（各步骤并集；`tool` 步骤取 `config.tools`）
  - `force_approval(steps) -> set[str]`（仅 `approval` 步骤的 `config.tools`）
  - `step_for_tool(steps, tool_name: str) -> dict | None`（按模板顺序首个命中）
  - `prompt_section(name: str, steps) -> str`（`<workflow name="…">` 段落，含「严格按序推进、禁 止步骤外工具调用」约束句）
  - `filter_tools(tools: list[Any], allowlist: set[str]) -> list[Any]`（按 `tool.name` 过滤；空 allowlist 原样返回副本）
- 语义基准：Spike 报告 §3.4/§3.5（裁决不变）。

- [ ] **Step 1: 写失败测试**：非法形状六例（空数组/未知 type/缺 id/id 重复/tool 步骤缺 tools/instruction 超长）+ `test_validate_steps_length_budget`（steps 总文本 >8000 报错）+ 合法 THREE_STEPS 样例（同 spike：检索/a2a/approval）返回 `[]`；白名单并集与 `force_approval` 断言；`step_for_tool` 命中与未命中；`filter_tools` 过滤与空白名单防御；`prompt_section` 含 `<workflow name=` 与步骤行。
- [ ] **Step 2: 跑测试确认失败** → ImportError。
- [ ] **Step 3: 实现 assembler**：spike 报告 §3.5 即实现说明；唯一差异是 `validate_steps` 增加 8000 字符总预算检查。
- [ ] **Step 4: 跑测试确认通过**。
- [ ] **Step 5: Commit**：`git add app/modules/workflows tests/integration/test_workflow_assembler.py && git commit -m "feat: M9 步骤装配器纯函数（T2）"`。

### Task 3: workflows CRUD API

**Files:**
- Create: `app/modules/workflows/schemas.py`、`app/modules/workflows/service.py`、`app/modules/workflows/router.py`
- Modify: `app/api/v1/router.py`
- Test: `tests/integration/test_workflow_crud.py`

**Interfaces:**
- Consumes: T1 模型、T2 `validate_steps`。
- Produces（T4/T5/T7 消费）:
  - `POST /api/v1/workflows` body `{name, description?, steps}` → 201 `WorkflowOut{id, name, description, steps, enabled, scope:"user", version:1, created_at, updated_at}`；创建即本人 `scope='user'`。
  - `GET /api/v1/workflows?limit=&cursor=` → cursor 分页（系统模板 + 本人，按 `created_at DESC`），复用 `encode_cursor({"created_at":…, "id":…})` 模式。
  - `GET /api/v1/workflows/{id}`：系统或本人 → 200；他人/不存在 → 404。
  - `PATCH /api/v1/workflows/{id}` body `{name?, description?, steps?}`：仅本人 user 模板（系统/他人 404）；成功 `version += 1`；steps 变更走 `validate_steps`。
  - `PUT /api/v1/workflows/{id}/enabled` body `{enabled: bool}`：仅本人；不改 version。
  - `DELETE /api/v1/workflows/{id}`：仅本人硬删；`workflow_runs` 随 FK CASCADE。
  - `service.py` 提供 `create_workflow / list_workflows / get_visible_workflow(db, user, workflow_id) -> Workflow | None`（T5 复用做 chat 校验）/ `update_workflow / set_enabled / delete_workflow`。
  - schemas：`WorkflowCreate/WorkflowUpdate/WorkflowEnabledRequest/WorkflowOut/WorkflowListOut`；`name` strip 后 1–100 字符，`description` ≤500。

- [ ] **Step 1: 写失败测试**（`test_workflow_crud.py`，HTTP 层）：创建 201 且落库字段正确；steps 非法 422 `workflow_steps_invalid`；name 空白 422；列表含系统模板+本人且不含他人；详情他人 404；PATCH 他人 404、PATCH 本人成功且 version+1、PATCH 系统 404、PATCH steps 非法 422；enabled PUT 成功且 version 不变；DELETE 后 GET 404 且再次创建同 id 不可复用；未登录 401。
- [ ] **Step 2: 跑测试确认失败** → 404（路由不存在）。
- [ ] **Step 3: 实现 schemas/service/router** 并在 `router.py` 注册（`responses=COMMON_ERROR_RESPONSES`，同既有模块）。写操作 `write_audit`：action `workflow.created/updated/enabled/deleted`，`resource_id=workflow_id`，detail 只放字段名与布尔值。
- [ ] **Step 4: 跑测试确认通过**；`.venv/bin/ruff check app tests` 全绿。
- [ ] **Step 5: Commit**：显式路径 `git commit -m "feat: M9 workflows CRUD API（T3）"`。

### Task 4: 运行历史查询与终态同步

**Files:**
- Modify: `app/modules/workflows/service.py`（追加 `list_workflow_runs`）、`app/modules/workflows/schemas.py`（`WorkflowRunOut`）
- Modify: `app/modules/chat/service.py`（`_finalize_success`/`_finalize_failed`/`_finalize_canceled` 三处同步）
- Test: `tests/integration/test_workflow_runs_api.py`

**Interfaces:**
- Consumes: T3 的 service/router；T1 `WorkflowRun`。
- Produces: `GET /api/v1/workflows/{id}/runs?limit=&cursor=` → `WorkflowRunOut{id, run_id, status, created_at, run_status, run_finished_at, run_duration_ms, run_error_code}`（join `runs`，`created_at DESC` 分页）；`sync_workflow_run_status(db, run_id, status)`——在 `_finalize_success/failed/canceled` 的既有事务内调用（`UPDATE workflow_runs SET status=:s WHERE run_id=:run_id`，无行不计）。`interrupted` 不同步（保持 running，等待恢复）。

- [ ] **Step 1: 写失败测试**：造 workflow + 两次 start_chat（脚本模型直落终态：一次成功、一次失败模型异常），`GET runs` 返回 2 行、终态与 run 一致、排序正确、cursor 翻页不重不漏；系统模板 403 `forbidden`；他人 user 模板 404；无 runs 返回空 items；`_finalize_canceled` 路径用 cancel 接口验证 canceled 同步。
- [ ] **Step 2: 跑测试确认失败**。
- [ ] **Step 3: 实现**：service join 查询（`select(WorkflowRun, Run).join(Run, …)`  limit+1）；router 加端点（owner-only）；三处 `_finalize_*` 在既有 db 事务里调 `sync_workflow_run_status`。
- [ ] **Step 4: 跑测试确认通过**；`pytest tests/integration/test_hitl_interrupt.py -q` 回归（确认 interrupt 路径无 workflow_runs 误同步）。
- [ ] **Step 5: Commit**：`git commit -m "feat: M9 运行历史查询与终态同步（T4）"`。

### Task 5: chat 链路接入（校验、快照、装配注入）

**Files:**
- Modify: `app/modules/chat/service.py`
- Modify: `app/modules/chat/tools.py`（`ToolRunner.__init__` 仅加参数，事件与审批在 T6）
- Test: `tests/integration/test_workflow_chat.py`

**Interfaces:**
- Consumes: T2 装配器、T3 `get_visible_workflow`。
- Produces（T6 消费）: `ToolRunner` 新可选参 `workflow_steps: list[dict] | None = None`、`force_approval_tools: set[str] | None = None`（存 `self.workflow_steps`/`self.force_approval_tools`）；`start_chat` 落 `run.workflow_id/workflow_version` 列 + `run.capabilities["workflow_id"/"workflow_version"/"workflow_steps"]` 快照 + 插入 `WorkflowRun(status="running")`；`prepare_runtime` 从快照读 steps（缺失回退查表，仍无则 `_raise_missing_run_data()`），tools 按白名单过滤、system_prompt 拼 `prompt_section`。
- 校验语义：`_validate_workflow(db, user, payload) -> Workflow | None`（404/409/409/422 见 Global Constraints；`workflow_version` 缺省用当前 version）；与手动能力上下文互斥保持 M1 现状（422 `workflow_context_conflict`）。

- [ ] **Step 1: 写失败测试**（服务层为主，模式同 spike 端到端与 `test_tasks.py` 的 `_start_chat`）：不存在 404；停用 409 `workflow_disabled`；version 不匹配 409 `workflow_version_conflict`；steps 非法 422；他人 user 模板 404；合法请求：run 列与 capabilities 快照落库、`WorkflowRun` 行生成、monkeypatch 捕获 `build_graph` 的 tools 名单=白名单并集、模型 captured 首轮 system prompt 含 `<workflow name=`；`test_resume_uses_snapshotted_steps_after_workflow_patch`：模板挂起审批期间 PATCH steps，approve 恢复后模型首轮 captured（恢复轮）的 system prompt 仍是旧步骤文本。
- [ ] **Step 2: 跑测试确认失败**（当前 `workflow_id` 恒 409 `workflow_disabled`）。
- [ ] **Step 3: 实现**：按 Interfaces 与 spike 报告 §3.1 注入点（`_check_capabilities` 移除 409 → `_validate_workflow`；`start_chat` 快照；`prepare_runtime` 读快照注入）。`prepare_runtime` 中 workflow 段拼在 prompt_guard 附注之前。
- [ ] **Step 4: 跑测试确认通过**；跑 `pytest tests/integration/test_chat.py tests/integration/test_hitl_interrupt.py -q` 回归（`test_chat.py` 的 `workflow_context_conflict` 用例不受影响）。
- [ ] **Step 5: Commit**：`git commit -m "feat: M9 chat 链路接入与步骤快照（T5）"`。

### Task 6: 执行语义——workflow.step 事件与强制审批

**Files:**
- Modify: `app/modules/chat/tools.py`（`approval_spec` 兜底 + `_execute_one` 尾部事件）
- Test: `tests/integration/test_workflow_execution.py`

**Interfaces:**
- Consumes: T5 的 runner 参数与装配注入。
- Produces: `workflow.step` 事件——时机在对应 `tool.result` 之后、同一次 `_execute_one` 内 emit；payload 恰为 `{step_id, step_type, step_title, tool, status}`；`event_type="workflow"`；`title=f"工作流步骤 {step_title 或 step_id}"`。`approval_spec` 在原生判定（MCP `requires_approval`/A2A `requires_approval`）之后兜底：工具名 ∈ `force_approval_tools` 时返回 `ApprovalSpec(risk="unknown", source=self._classify(name)[0], executor=name)`。

- [ ] **Step 1: 写失败测试**（全链路，`seeded_docs` + `FakeA2aAgent(requires_approval=False)` + admin 建 agent + 3 步模板，模式同 spike `test_three_step_workflow_end_to_end`）：`workflow.step` 恰 2 条且 `step_id` 序列 `["s1","s2"]`、每条紧跟 `tool.result` 且 tool 名一致；强制审批生效（agent 不要求审批仍出现 `run.interrupted`）→ approve → run succeeded、`fake.call_count == 1`；回放接口 `GET /runs/{id}/events` 中两条 `workflow.step` 的 `event` 字段正确（M7 契约）；拒绝路径（reject）后 `workflow.step` 的 `status=="rejected"` 且 run 正常收尾。
- [ ] **Step 2: 跑测试确认失败**。
- [ ] **Step 3: 实现**：spike 报告 §3.4 裁决 2/3 即实现说明（`approval_spec` 兜底放原生判定之后；emit 放 `tool.result` emit 与 `return ToolMessage` 之间，读 `self.workflow_steps`）。
- [ ] **Step 4: 跑测试确认通过**；`pytest tests/integration/test_approvals.py tests/integration/test_run_stream.py -q` 回归。
- [ ] **Step 5: Commit**：`git commit -m "feat: M9 workflow.step 事件与强制审批（T6）"`。

### Task 7: 能力开关与 run 查询输出补全

**Files:**
- Modify: `app/core/config.py`（`workflows_enabled: bool = True`）
- Modify: `app/modules/chat/service.py`（`_validate_workflow` 开关门禁、`get_run` 填 `workflow=RunWorkflowOut`）
- Modify: `app/modules/system/service.py`（`capabilities.workflows=settings.workflows_enabled`）
- Test: `tests/integration/test_workflow_chat.py`（追加）与 `tests/integration/test_chat.py` 不动

**Interfaces:**
- Produces: `settings.workflows_enabled`；开关关闭时 chat 带 `workflow_id` → 409 `workflow_disabled`（恢复 M1 占位语义，位置在 `_validate_workflow` 最前）；`GET /runs/{id}` 的 `workflow` 字段：`{id: workflow_id, name, version}`（快照查询不到已删模板时 `name` 用 `run.capabilities` 里无名字段的 `""`？——否：**快照同时存 `workflow_name` 进 `run.capabilities`**，`get_run` 从快照构造，模板删除后仍可显示）。
- 裁决：`RunWorkflowOut` 三字段（id/name/version）已有，不动 schema；T5 的 `start_chat` 快照追加 `workflow_name`（T5 实现时一并写入，本任务测试断言）。

- [ ] **Step 1: 写失败测试**：monkeypatch `Settings.workflows_enabled=False`（`get_settings.cache_clear()` 模式同既有配置测试）→ chat 带 workflow_id 409 `workflow_disabled`、`GET /system/info` 的 `capabilities.workflows is False`；开关开（默认）→ info 为 True；`GET /runs/{id}` 返回 `workflow={id,name,version}` 与发起时一致；DELETE 模板后 run 查询仍返回快照 name。
- [ ] **Step 2: 跑测试确认失败**。
- [ ] **Step 3: 实现**（T5 已把快照写全，这里只接开关与 get_run 输出）。
- [ ] **Step 4: 跑测试确认通过**；`pytest tests/integration/test_chat.py tests/integration/test_workflow_crud.py -q` 回归。
- [ ] **Step 5: Commit**：`git commit -m "feat: M9 能力开关与 run.workflow 输出（T7）"`。

### Task 8: 契约文档与前端 handoff

**Files:**
- Modify: `docs/api.md`（§2 端点总表追加 workflows 组；新增「## 13. M9 工作流实现状态（2026-11-12）」：API 形状、steps schema 与五类步骤语义、错误码表、`workflow.step` 事件帧、快照与版本规则）
- Regenerate: `docs/openapi.json`（`.venv/bin/python scripts/export_openapi.py`）
- Modify: `docs/roadmap.md`（M9 状态 → 已完成后端）、`README.md`/`docs/architecture.md` 若有里程碑表则同步
- Create: `docs/feedback/2026-11-12-m9-frontend-handoff.md`（X1 升级 M9：端点清单与示例、错误语义、SSE `workflow.step` 帧示例、`workflow_id/workflow_version` 发起示例、系统模板只读口径、无前端 breaking change 声明）

- [ ] **Step 1: 写文档**：按上述文件逐一更新；api.md 示例请求/响应用真实字段（与 schemas 一致）。
- [ ] **Step 2: 导出 OpenAPI 并核对**：`git diff docs/openapi.json` 仅新增 workflows 路径与 `WorkflowOut` 等 schema，无既有契约漂移；`.venv/bin/python tests/contract/` 相关检查（若为目录则跑 `pytest tests/contract -q`）。
- [ ] **Step 3: Commit**：`git commit -m "docs: M9 契约文档、OpenAPI 与前端交接（T8）"`。

### Task 9: 终局验收与收口

**Files:**
- Modify: `docs/superpowers/specs/2026-11-11-m7-m9-backend-roadmap-design.md`（§5.4 验收清单勾选）、`docs/superpowers/specs/2026-11-12-m9-spike-report.md`（追加「正式实现已交付」一行）

- [ ] **Step 1: 全量回归**：`TEST_DATABASE_URL=postgresql+psycopg://devuser:devpass@localhost:54321/agent_hub_test .venv/bin/python -m pytest -q` → 全绿（基线 825 passed + M9 新增），`ruff check app tests` 全绿。
- [ ] **Step 2: 迁移演练**：临时库 `upgrade head` 后 `\d workflows`/`\d workflow_runs` 与 T1 输出一致；`alembic heads` 单 head；删临时库。
- [ ] **Step 3: 验收清单核对**（立项 §5.4 逐项）：3 步模板可保存并跑通且事件可见（T6）；取消/失败/审批超时语义与现有 run 一致（取消用例 T4、超时复用既有 `expire_pending` 测试模式补一条 workflow run 的超时终态到 `test_workflow_runs_api.py`）；步骤校验有测试（T2/T3/T5）；`GET /workflows/{id}/runs` 可查（T4）；迁移可逆（T1/T9）。若超时用例缺失，在 T4 测试文件补 `test_workflow_run_approval_timeout_syncs_status` 后重跑。
- [ ] **Step 4: 勾选文档并提交**：`git commit -m "chore: M9 验收收口与文档勾选（T9）"`。
- [ ] **Step 5: 全分支审查**：dispatch 独立子代理对照本计划 Global Constraints + Review Focus 审查全部 diff，问题按序修复后重跑 Step 1。

## Self-Review 结论

- 立项 §5.2 五项范围 → T1（表）/T2+T5+T6（步骤类型与执行）/T3（CRUD/启停/runs）/T5+T6（运行入口与 `workflow.step`）；§5.4 验收 → T9 逐项挂钩；非目标未引入任务。
- 类型一致性：`get_visible_workflow`/`sync_workflow_run_status`/`workflow_steps`/`force_approval_tools` 在 T3–T6 的 Interfaces 中签名一致；`RunWorkflowOut` 不改 schema，靠 `workflow_name` 快照。
- Review Focus 五条均有归属测试（T2/T3/T4/T5/T6）。
- 计划长度 ≈ 实现关键决策的 2 倍以内，无实现正文转录（装配器实现由 spike 报告 §3 携带）。
