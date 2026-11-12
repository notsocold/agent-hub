# M9 工作流后端交付报告

- 日期：2026-11-12
- 范围：M9 工作流后端（原 X1，Task 1–9，**不含前端页面**）
- 基线：M8 收尾 **825 passed, 1 skipped**，迁移 head `2d8f6a1c3e70`（M9 前单 head）
- 设计：[M7–M9 立项](../superpowers/specs/2026-11-11-m7-m9-backend-roadmap-design.md) §5
- Spike：[M9 Spike 报告](../superpowers/specs/2026-11-12-m9-spike-report.md)（机制验证与执行语义裁决）
- 计划：[M9 实施计划](../superpowers/plans/2026-11-12-m9-workflow-backend.md)
- 前端交接：[M9 前端交接](2026-11-12-m9-frontend-handoff.md)
- 终局收口：Task 9（2026-11-12，全量回归 + 迁移演练 + §5.4 验收对照 + 审批超时同步修复，即本报告）
- 状态：**后端已完成并收口（2026-11-12）**；M9 前端接入尚未开始
  （本文不构成前端已集成的声明）

**目标**：把技能、检索、A2A 与审批编排成轻量步骤模板（MVP：不做 DAG/并行/条件跳转/外部引擎），
交付 `workflows`/`workflow_runs` 两表与可逆迁移、五类步骤装配器、7 个 `/workflows` 端点、
`POST /chat/stream` 带 `workflow_id` 的快照执行、`workflow.step` 事件、
`WORKFLOWS_ENABLED` 能力开关，并同步 OpenAPI / `docs/api.md` / 前端交接。

> 位置说明：本报告落位 `docs/feedback/`，与 M2–M8 交付报告同目录同结构
> （立项/计划/spike 归 `docs/superpowers/`，验收证据报告归 `docs/feedback/`）。

## 1. 交付内容

| 任务 | 提交 | 文件 | 说明 |
|---|---|---|---|
| Task 1 两表模型与迁移 | `cc91cf7` | `app/db/models/workflow.py`、`app/db/models/__init__.py`、`alembic/versions/20261112_1400_e8b4f2a6c9d3_m9_workflow_tables.py`、`tests/conftest.py`、`tests/integration/test_workflow_models.py`（4 项） | `Workflow`（`scope` CHECK system/user、`steps` JSONB、`version` 默认 1、owner FK CASCADE）与 `WorkflowRun`（五态 CHECK、`uq_workflow_runs_run(run_id)` 唯一、`ix_workflow_runs_workflow_created(workflow_id, created_at DESC)`、双 FK CASCADE）；迁移 `e8b4f2a6c9d3`，`down_revision="2d8f6a1c3e70"`，可逆；conftest `_TABLES` 追加 `workflow_runs, workflows` |
| Task 2 步骤装配器（纯函数） | `1a3475d` | `app/modules/workflows/{__init__,assembler}.py`、`tests/integration/test_workflow_assembler.py`（17 项） | `STEP_TYPES` 五类（`knowledge_search/tool/a2a/approval/answer`）、`validate_steps`（非法形状/缺 id/重复 id/tool 缺 tools/instruction>500/tool 名>200/步骤>12/总文本>8000）、`tool_allowlist` 并集、`force_approval`（仅 approval 步骤）、`step_for_tool` 首个命中、`prompt_section`（`<workflow name=…>` + 按序约束句）、`filter_tools`（空白名单透传副本） |
| Task 3 workflows CRUD API | `55d8020` | `app/modules/workflows/{router,schemas,service}.py`、`app/api/v1/router.py`、`tests/integration/test_workflow_crud.py`（13 项） | `POST/GET 列表(游标)/GET 详情/PATCH/PUT enabled/DELETE` 共 7 端点：创建即本人 `scope='user'`；列表=系统模板+本人（`created_at DESC, id` 游标）；PATCH 成功 `version += 1`、enabled PUT 不改 version；写路径 `validate_steps` 422 `workflow_steps_invalid`；系统模板只读（PATCH/DELETE 404）；他人读写一律 404（不泄露存在性）；审计 `workflow.created/updated/enabled/deleted`（detail 只放字段名与布尔值）；未登录 401 |
| Task 4 运行历史查询与终态同步 | `1dc2f10` | `workflows/{router,schemas,service}.py`、`app/modules/chat/service.py`（三处 `_finalize_*` 挂 sync）、`tests/integration/test_workflow_runs_api.py`（7 项） | `GET /workflows/{id}/runs?limit=&cursor=`：owner-only（系统模板 403 `forbidden`、他人 user 模板 404），join `runs` 输出 `run_status/run_finished_at/run_duration_ms/run_error_code`，`created_at DESC` 游标分页不重不漏；`sync_workflow_run_status(db, run_id, status)`：终态同名直写、幂等、非终态（interrupted）不落库、无行 no-op、仅 execute 不 commit |
| Task 5 chat 链路接入与步骤快照 | `85cb5c3` | `app/modules/chat/{service,tools}.py`、`tests/integration/test_workflow_chat.py`（12 项） | `_validate_workflow`（404 / 409 `workflow_disabled` / 409 `workflow_version_conflict` / 422 `workflow_steps_invalid`，他人 user 模板同 404）；与手动能力上下文互斥保持 M1 语义（422 `workflow_context_conflict`）；`start_chat` 落 `run.workflow_id/workflow_version` 列 + `run.capabilities` 四键快照（`workflow_id/workflow_version/workflow_name/workflow_steps`）+ 插 `WorkflowRun(status="running")`；`prepare_runtime` 从快照读 steps（缺失回退查表、损坏转 `_raise_missing_run_data()`）、tools 白名单过滤、`prompt_section` 拼在 prompt_guard 附注之前；`ToolRunner` 增 `workflow_steps`/`force_approval_tools` 可选参 |
| Task 6 `workflow.step` 事件与强制审批 | `4687c8b` | `app/modules/chat/tools.py`、`tests/integration/test_workflow_execution.py`（2 项） | `workflow.step` 在对应 `tool.result` 之后同一次 `_execute_one` 内 emit，payload 恰 `{step_id, step_type, step_title, tool, status}`、`event_type="workflow"`、`title=f"工作流步骤 {step_title}"`、M7 回放契约（`event` 字段还原）一致；`approval_spec` 在原生判定（MCP/A2A `requires_approval`）之后兜底：工具 ∈ 强制审批集合即 `ApprovalSpec(risk="unknown", source=分类, executor=name)` |
| Task 7 能力开关与 run.workflow 输出 | `cb5d014` | `app/core/config.py`、`chat/service.py`、`system/service.py`、`tests/integration/test_workflow_chat.py`（追加 4 项） | `settings.workflows_enabled`（默认 `True`）；关闭时 chat 带 `workflow_id` → 409 `workflow_disabled`（检查最前，不做可见性/版本/steps 校验），`GET /system/info` 的 `capabilities.workflows` 同步；`GET /runs/{id}` 返回 `workflow={id,name,version}`（读快照构造，模板删除后仍可显示） |
| Task 8 契约文档 / OpenAPI / 交接 | `357b53e` + `10cd899`（修复轮 1） | `docs/api.md`（新增 §13）、`docs/openapi.json`、`docs/feedback/2026-11-12-m9-frontend-handoff.md`（新增）、`README.md`、`docs/roadmap.md`、`docs/architecture.md` | §13：API 形状、五类步骤语义与 steps schema、错误码表（含 `workflow_disabled` 二义消歧）、`workflow.step` 帧格式、快照与版本规则；OpenAPI 重导出（`git diff` 仅新增 workflows 路径与 schema，无既有契约漂移）；前端 handoff：端点清单、发起示例、错误映射、SSE 帧、联调自测清单 |
| Task 9 终局验收与收口 | 本次两个提交（见 §7） | `chat/service.py`（1 行 sync）、`chat/router.py`（409 描述）、`docs/openapi.json`、`test_workflow_runs_api.py`（+1）、`test_workflow_chat.py`（参数化 +1 形态）、`.env.example`、roadmap/spike/计划/本报告 | 全量回归 885 passed/1 skipped（修复后终态）、ruff 全绿；空库迁移演练 `upgrade → downgrade → upgrade` 全链通过 + 单 head；§5.4 五项验收逐一对照；修复审批超时取消链路的映射行同步缺口（§2.3）；chat 409 描述对齐 M9 语义并重导出 OpenAPI；spike 报告/实施计划入库消除 README §8 死链 |

数据库：新增 1 个迁移 `e8b4f2a6c9d3`（两表 + 2 索引 + 3 CHECK），可逆（§2.2）；
`runs.workflow_id/workflow_version` 为 M1 预留普通列（无外键，评估见 §6.1）；
`Settings` 新增 `workflows_enabled`（T9 已补 `.env.example` 的 `WORKFLOWS_ENABLED=true`）。

## 2. 实测证据

### 2.1 全量验证（T9 步骤 1）

2026-11-12 终局收口执行，复用 `dev_postgres`（localhost:54321），业务测试库 `agent_hub_test`
（pytest `create_all` 建表，不触碰开发库 `agent_hub`）：

```text
$ TEST_DATABASE_URL=postgresql+psycopg://devuser:devpass@localhost:54321/agent_hub_test \
    .venv/bin/python -m pytest -q
883 passed, 1 skipped in 168.37s (0:02:48)     ← 步骤 1 实测（收口修复前）
885 passed, 1 skipped in 109.20s (0:01:49)     ← 收口修复后终态（§2.3 三项修复 + 2 条新用例）

$ .venv/bin/ruff check app tests
All checks passed!
```

- 基线 M8 **825 项** → M9 终态 **885 项**（+60：T1–T8 六个 workflow 测试文件 58 条；
  T9 收口轮再补 2 条：审批超时终态 1 条 + 损坏快照参数化新增形态 1 条；
  唯一 skip 仍是知识库语义评测组，需真实 Embedding key，与 M4–M8 一致）；
- ruff 全绿（收口轮新增/修改代码含注释均通过 `line-length=100`）。

### 2.2 迁移演练（T9 步骤 2）

独立临时库 `m9_final_check`（`DROP IF EXISTS → CREATE → CREATE EXTENSION vector 0.8.7`）：

```text
$ DATABASE_URL="…/m9_final_check" .venv/bin/alembic upgrade head
Running upgrade  -> 45f5db481e8c, m1 core tables
…（M1→M1.5→M2→M4→M5 共 6 个既有迁移全部通过）…
Running upgrade 2d8f6a1c3e70 -> e8b4f2a6c9d3, m9 workflow tables

$ DATABASE_URL="…/m9_final_check" .venv/bin/alembic heads
e8b4f2a6c9d3 (head)          ← 单 head

$ DATABASE_URL="…/m9_final_check" .venv/bin/alembic downgrade -1
Running downgrade e8b4f2a6c9d3 -> 2d8f6a1c3e70, m9 workflow tables

$ DATABASE_URL="…/m9_final_check" .venv/bin/alembic upgrade head   （再升回成功）
$ DATABASE_URL="…/m9_final_check" .venv/bin/alembic current
e8b4f2a6c9d3 (head)
（验证后 DROP DATABASE m9_final_check）
```

`\d workflows` / `\d workflow_runs` 结构核对（与 T1 交付一致）：

- `workflows`：`id` UUID PK、`owner_user_id` FK users CASCADE（可空=系统）、
  `scope` varchar(16) CHECK in (system,user) 默认 user、`name` varchar(100)、
  `description` text 默认 ''、`steps` jsonb 默认 '[]'、`enabled` bool 默认 true、
  `version` int 默认 1、`created_at/updated_at` timestamptz 默认 now()；
- `workflow_runs`：`id` UUID PK、`workflow_id` FK workflows CASCADE、
  `run_id` FK runs CASCADE 且 **UNIQUE**（`uq_workflow_runs_run`）、
  `status` varchar(16) CHECK 五态（running/succeeded/failed/canceled/interrupted）默认 running、
  索引 `ix_workflow_runs_workflow_created (workflow_id, created_at DESC)`；
- 顺带清理 T1 环境观察遗留：测试库 `agent_hub_test` 残留的旧 `alembic_version` 表已
  `DROP TABLE IF EXISTS`（测试走 `create_all` 不读它，防止误在测试库跑 alembic）。

### 2.3 收口修复（T9）

1. **审批超时取消链路的映射行同步缺口（收口裁决 A，1 行修复）**：补验收用例时实证
   `expire_pending → cancel_interrupted_run → halt_run` 链路全程不经过任何 `_finalize_*`
   （`halt_run` 用 `Command(resume={"decision":"cancel"})` 恢复图但丢弃输出），run 正确置
   `canceled`、审批置 `expired`，但 `workflow_runs` 映射行**永久停留 running**，污染 T4
   runs 查询对外契约。修复：在 `cancel_interrupted_run` 条件 UPDATE 命中后的既有事务、
   commit 前补 `await sync_workflow_run_status(db, run_id, "canceled")`（幂等、无行 no-op），
   与 cancel 接口（走 `_finalize_canceled`）语义完全对齐；覆盖该函数全部 2 个调用方
   （审批超时巡检、恢复轮 user_canceled）。验证：
   `test_workflow_runs_api.py` 8/8（含新用例
   `test_workflow_run_approval_timeout_syncs_status`：改 `expires_at` + `expire_pending()`
   → 断言 run canceled + 映射行 canceled + 审批 expired，先红后绿），
   `test_hitl_interrupt.py` 9/9 无回归。
2. **chat 409 描述对齐 M9 语义**：`app/modules/chat/router.py` 的 `POST /chat/stream` 409
   响应描述原为「`workflow_disabled`（M1 未实现工作流）」，改为「全局工作流开关关闭，或
   模板已停用；用 `GET /system/info` 的 `capabilities.workflows` 与 message 可区分两者」，
   与 `docs/api.md` §13.4 消歧一致；重导出 OpenAPI 后 `git diff docs/openapi.json`
   **仅 1 处 description 字段变化，无 path/schema 结构变化**。
3. **损坏快照参数化（T7 遗留评估）**：`test_corrupted_workflow_snapshot_fails_run` 原为
   单形态断言（`[1, 2, 3]` 纯非 dict 项），用 `@pytest.mark.parametrize` 补混合形态
   `[合法 dict 项, 1]`（`ids=["all_non_dict_items","mixed_dict_and_non_dict"]`），
   验证逐项校验对混合形态同样拦下，2/2 通过。

### 2.4 契约要点与 OpenAPI

- **端点**：`POST/GET /api/v1/workflows`、`GET/PATCH/DELETE /api/v1/workflows/{id}`、
  `PUT /api/v1/workflows/{id}/enabled`、`GET /api/v1/workflows/{id}/runs` 共 7 个；
  运行入口复用 `POST /chat/stream` 的 `workflow_id`/`workflow_version`（不新增端点）；
- **错误语义**（按检查顺序）：422 `workflow_context_conflict`（与手动能力上下文互斥，M1
  既有）→ 422 `validation_error`（version 脱离 workflow_id）→ 409 `workflow_disabled`
  （开关关闭，检查最前）→ 404 `not_found`（不存在/不可见/他人，同响应）→ 409
  `workflow_disabled`（模板停用）→ 409 `workflow_version_conflict` → 422
  `workflow_steps_invalid`（存量坏模板，消息拼前 3 条）；
- **事件**：`workflow.step` 帧格式与全站一致（§3.2 SSE），`event_type="workflow"` 落库，
  `GET /runs/{id}/events` 回放还原 `event` 字段（M7 契约）；
- **快照原则对前端可见**：`GET /runs/{id}` 的 `workflow` 字段读 `run.capabilities` 快照，
  模板 PATCH/DELETE 后历史 run 仍显示发起时的 `{id, name, version}`；
- T9 之后的 OpenAPI 与代码一致（重导出仅 1 处 description 变化，§2.3.2）。

### 2.5 关键用例（M9 六个测试文件，60 条）

| 用例文件 | 数量 | 覆盖 |
|---|---|---|
| `test_workflow_models.py` | 4 | 两表插入回读、scope CHECK 违例、`workflow_runs.run_id` 唯一索引 |
| `test_workflow_assembler.py` | 17 | 五类步骤默认工具、非法形状六例、8000 字符预算、步骤数上限、合法 3 步样例、白名单并集、force_approval、首个命中、过滤与空白名单透传、prompt 段落 |
| `test_workflow_crud.py` | 13 | 创建落库、steps 非法 422、name 校验、列表系统+本人（不含他人）、游标翻页、他人读写 404、系统模板只读、PATCH version+1、enabled 不改 version、删除后 404 且 id 不可复用、401、审计 |
| `test_workflow_runs_api.py` | 8 | 成功/失败终态同步、游标翻页不重不漏、系统模板 403、他人 404、空列表、cancel 接口同步、**审批超时同步（T9 补）**、sync 规则直测（终态/幂等/非终态/无行） |
| `test_workflow_chat.py` | 16 | 校验语义 404/409×2/422、快照落库与白名单捕获、快照恢复（挂起期间 PATCH）、早退同步、开关 409、system/info、run.workflow 快照（删除后仍显示）、**损坏快照双形态（T9 参数化）** |
| `test_workflow_execution.py` | 2 | approve 全链路（事件序列/payload 键集/强制审批/回放契约/run succeeded）、reject 路径（step 状态 rejected、远程不调用、run 正常收尾） |

## 3. 关键决策与裁定

| # | 决策 | M9 实现 |
|---|---|---|
| D1 | **快照原则** | 一次 run 的执行语义在 `start_chat` 时冻结：`run.capabilities` 存 `workflow_id/workflow_version/workflow_name/workflow_steps` 四键；恢复轮一律读快照（模板 PATCH/DELETE 不影响已发起 run）；快照缺失（异常数据）回退查表兜底，查不到或 steps 已不合法转 `_raise_missing_run_data()`；快照为非空 list 但混入非 dict 项属损坏快照，**不得静默过滤**（否则退化成空白名单放行全部工具），同样转运行数据缺失（T5 审查交接，T9 参数化双形态固定） |
| D2 | **工具白名单** | `tool_allowlist` = 各步骤默认工具并集（`knowledge_search→search_knowledge`、`a2a→send_message`、`tool→config.tools`），每轮 `build_graph` 前过滤；空白名单透传副本（防御非工作流轮次误伤）；模型 prompt 段明确「严格按序推进、禁止步骤外工具调用」 |
| D3 | **事件语义** | `workflow.step` 紧跟对应 `tool.result`（同一次 `_execute_one` 内、emit 与 return ToolMessage 之间）；payload 恰 5 键 `{step_id, step_type, step_title, tool, status}`；status 复用工具执行状态（ok/rejected/…）；`event_type="workflow"` 落库与 M7 回放契约一致 |
| D4 | **强制审批** | `approval_spec` 在原生判定（MCP/A2A `requires_approval`）之后兜底：工具名 ∈ `force_approval(steps)`（仅 approval 步骤的 `config.tools`）即返回 `ApprovalSpec(risk="unknown", source=分类, executor=name)`；审批闸门复用 M5 语义（interrupt/approve/reject/超时），reject 后步骤状态 rejected、run 正常收尾 |
| D5 | **能力开关** | `WORKFLOWS_ENABLED=false`：chat 带 `workflow_id` 一律 409 `workflow_disabled`（检查最前，不做可见性/版本/steps 校验），`GET /system/info` 上报 false；模板 CRUD 配置面不受开关影响（M8 R1 惯例）；默认 `true` |
| D6 | **审批超时同步（收口裁决 A）** | `cancel_interrupted_run` 在条件 UPDATE 命中后的既有事务内补 `sync_workflow_run_status(db, run_id, "canceled")`——审批超时/恢复轮取消与 cancel 接口同语义，映射行不再停留 running；sync 幂等、无行 no-op，风险极低（lead 2026-11-12 拍板） |

## 5. 规格 §5.4 验收清单（T9 逐项对照）

| 验收项 | 状态 | 证据 |
|---|---|---|
| 一个 3 步模板（检索 → A2A → 审批）可保存并跑通，事件可见 | **已验证（后端）** | `test_workflow_step_events_and_force_approval`：`WORKFLOW_STEPS` 三步（s1 检索/s2 委派/s3 人工确认）经 `POST /workflows` 保存 → chat 发起 → 强制审批 interrupt/approve → `workflow.step` 恰 2 条、紧跟各自 `tool.result`、payload 键集精确、`GET /runs/{id}/events` 回放还原；run succeeded |
| 取消/失败/审批超时语义与现有 run 一致 | **已验证（后端）** | 取消 `test_cancel_syncs_workflow_run`（cancel 接口 → `_finalize_canceled` → 映射行 canceled）；失败 `test_runs_after_success_and_failure_synced`（模型异常 → `_finalize_failed`）；审批超时 `test_workflow_run_approval_timeout_syncs_status`（**T9 补**，先红后绿：`expires_at` 过期 + `expire_pending()` → run canceled + 映射行 canceled + 审批 expired）——三者终态均与 run 同名同步 |
| 步骤类型/顺序/工具白名单校验有测试 | **已验证（后端）** | T2 `test_workflow_assembler.py` 17 项（类型/顺序约束句/白名单并集/预算）；T3 `test_create_rejects_invalid_steps`、`test_patch_invalid_steps_rejected`（422 `workflow_steps_invalid`）；T5 `test_workflow_invalid_steps_returns_422`（chat 入口拒绝存量坏模板） |
| 工作流运行可在 `GET /workflows/{id}/runs` 查询 | **已验证（后端）** | `test_workflow_runs_api.py` 8 项：成功/失败终态一致、`created_at DESC` 游标翻页不重不漏、系统模板 403、他人 404、空列表、cancel/审批超时同步；join runs 输出运行状态字段 |
| 迁移可逆（upgrade/downgrade） | **已验证（后端）** | §2.2：空库 `upgrade head`（7 迁移全过）→ `downgrade -1` → `upgrade head` 全链成功、`alembic heads` 单 head `e8b4f2a6c9d3`、`\d` 两表结构与 T1 一致 |

## 6. 已知缺口、遗留与延后项

1. **`runs.workflow_id` 无外键**：M1 预留列，M9 未补 FK（spike §4.4 遗留项评估结果：
   加 FK 需要处理历史 run 引用已删模板的数据迁移，突破 MVP 范围）；一致性由应用层
   `workflow_runs` 双 FK + 快照保证，跨表悬空引用仅影响 `GET /runs/{id}` 显示
   （快照兜底 name）。需要时单独立项补迁移。
2. **步骤服从性依赖提示词**：spike 验证的是机制（装配/过滤/事件/迁移），模型对
   「严格按序、禁步骤外工具」的实际服从率属提示词调优与评测问题，未在本里程碑取证。
3. **模板执行无并发上限**：同一模板可被多用户并发发起，`workflow_runs` 仅作映射无配额；
   限流复用 chat 既有 `429` 语义（每用户），模板维度配额不在 MVP。
4. **`workflow_context_conflict` 交互**：工作流与手动能力上下文（skills/mcp_servers 等）
   互斥为 M1 既有语义，前端需在模板发起时清空手动上下文（handoff §2 已注明）。
5. **前端尚未集成**：M9 只交付后端契约与 handoff，模板管理页、发起交互、`workflow.step`
   帧渲染与联调均未开始；本文不声明前端已完成，接入见
   [M9 前端交接](2026-11-12-m9-frontend-handoff.md)。
6. **T9 补记**：`.env.example` 能力开关清单补 `WORKFLOWS_ENABLED=true`（T7 新增配置项
   漏同步示例文件，T9 收口补齐）。

## 7. 提交记录

```text
cc91cf7 feat: M9 工作流两表模型与迁移（T1）
1a3475d feat: M9 步骤装配器纯函数（T2）
55d8020 feat: M9 workflows CRUD API（T3）
1dc2f10 feat: M9 运行历史查询与终态同步（T4）
85cb5c3 feat: M9 chat 链路接入与步骤快照（T5）
4687c8b feat: M9 workflow.step 事件与强制审批（T6）
cb5d014 feat: M9 能力开关与 run.workflow 输出（T7）
357b53e docs: M9 契约文档、OpenAPI 与前端交接（T8）
10cd899 docs: M9 文档审查修复（T8 修复轮 1）
（T9 收口提交）fix: M9 收口（审批超时取消链路同步、chat 409 描述与 OpenAPI 重导出）
（T9 收口提交）docs: M9 验收收口与文档勾选（T9）
```

均已本地提交、**未推送**；所有提交使用显式路径，未触碰 `.dsh/` 等运行时文件。
