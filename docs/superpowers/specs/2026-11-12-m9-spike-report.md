# M9 Spike 报告：步骤模板注入执行模型验证

- 日期：2026-11-12
- 状态：**Spike 通过，原型代码已按约回滚**（本文档是 spike 的唯一保留产物）
- 依据：[M7–M9 立项](2026-11-11-m7-m9-backend-roadmap-design.md) §5.5「先做 0.5–1 天技术 spike」
- 基线：M8 收口后 master（`e1f958a`）；回归 825 passed（+1 skipped）；ruff 全绿

## 1. 结论

立项 §5.2 的「步骤模板注入 system prompt + 工具白名单 + 审批闸门」执行模型**机制上完全可行**，
主图一行不用改。3 步模板（检索 → A2A 强制审批 → 回答）端到端跑通：模板注入生效、白名单生效、
强制审批 interrupt/resume 生效、`workflow.step` 事件落库可回放、两表迁移可逆。

spike 验证的是机制（装配、过滤、事件、落库、迁移），不承诺真实模型的步骤服从性——
那属于正式实施期的提示词调优与评测问题。

## 2. 验证点与证据

| # | 立项 spike 目标 | 结果 | 证据（spike 测试，已回滚） |
|---|---|---|---|
| 1 | 模板注入能约束主图 | ✅ | `test_three_step_workflow_end_to_end`：模型第一轮 captured 的 system prompt 含 `<workflow name="退货处理流程">` 与步骤清单；`build_graph` 未改动 |
| 2 | 不破坏现有工具选择 | ✅ | 同测试：monkeypatch 捕获 `build_graph(tools=…)` 实际收到 `["search_knowledge", "send_message"]`（白名单并集），审批 gate 与 runner 正常 |
| 3 | `workflow.step` 事件 | ✅ | `s1`/`s2` 两条事件紧跟各自 `tool.result`，`event_type="workflow"`，seq 单调、可回放 |
| 4 | 迁移可逆 | ✅ | 临时库 `upgrade head → downgrade -1 → upgrade head` 全链通过；临时库已删除 |

回归与静态检查：spike 代码在库上时全量 `832 passed / 1 skipped`、`ruff check` 全绿；
验收语义测试覆盖 404 / 409 `workflow_disabled` / 409 `workflow_version_conflict` /
422 `workflow_steps_invalid` / 他人私有模板 404。

## 3. 关键发现（正式实施必须知道）

### 3.1 主图完全参数化，注入点唯一

`app/agents/graph.py` 的 `build_graph(model, checkpointer, *, system_prompt, tools,
tool_executor, approval_gate, max_tool_rounds)` 全部由 `modules/chat` 每轮装配传入。
**唯一注入点是 `prepare_runtime()`**（`app/modules/chat/service.py`）：

1. 从 `run.capabilities["workflow_id"]` 读回模板（恢复路径自动生效，同一 workflow 行）；
2. `tools` 构建后按白名单过滤；
3. `system_prompt` 末尾拼 workflow 段落（在 prompt_guard 附注之前）；
4. `ToolRunner` 增加两个可选参数：`workflow_steps`（事件映射）与 `force_approval_tools`。

`start_chat()` 增加 `_validate_workflow()`：存在/可见（system 或本人）/启用/版本匹配/steps 合法，
错误语义 404 / 409 `workflow_disabled` / 409 `workflow_version_conflict` / 422 `workflow_steps_invalid`。
`run.workflow_id` / `run.workflow_version` 列 M1 已预留，直接填；同时插一行 `workflow_runs` 映射。

### 3.2 迁移 head 是 `2d8f6a1c3e70`，不是文件名日期最新的那个

迁移链真实顺序：`45f5db481e8c(M1) → 7f3a9c21b4d6(M1.5) → 8a2f7c31e9d5(M2) →
c4e7a91b2f36(M4) → 9c1d4e7a2b60(M5 secrets) → 2d8f6a1c3e70(M5 approvals)`。
M4 的文件名日期（11-11）比 M5（11-10）晚是因为链后来 rebase 过，**以 `down_revision` 为准**。
spike 曾把新迁移接在 `c4e7a91b2f36` 上造成双 head（`alembic upgrade head` 直接报错），
正式迁移必须 `down_revision = "2d8f6a1c3e70"`，且合入前跑一次 `alembic heads` 确认单 head。

### 3.3 测试基建的三个接入点

- `tests/conftest.py` 的 `_TABLES` 硬编码 TRUNCATE 清单，新表必须手工追加
  （`workflow_runs, workflows` 加在最前），否则用例间数据残留；
- 测试建表走 `Base.metadata.create_all`，模型注册进 `app/db/models/__init__.py` 即自动建表，
  迁移正确性必须另用**独立临时库**验证（测试库不跑 alembic，见 m_4c883c07）；
- `create_access_token(user.id).token`（不是 `.access_token`）+ `auth_headers(token)` 签请求头；
  user fixture 的对象不要跨 session `refresh`，在新 session 里 `session.get(User, user.id)`。

### 3.4 spike 内的执行语义裁决（供正式计划复核）

1. **白名单 = 各步骤允许工具的并集**：`knowledge_search→search_knowledge`、
   `a2a→send_message`、`tool→config.tools` 显式清单、`approval/answer` 不提供工具；
   严格白名单（`save_memory` 等基础工具也不默认放行）；空白名单不过滤（防御坏模板）。
2. **`approval` 步骤 = 强制审批集合**：`config.tools` 里的工具无论原生策略一律走审批 gate
   （在 `approval_spec` 的原生判定之后兜底）；不是独立调度点。
3. **`workflow.step` 事件 = 工具名→步骤映射**：在 `tool.result` 之后 emit
   `{step_id, step_type, step_title, tool, status}`，`event_type="workflow"`（≤16 字符无 CHECK 约束）；
   不做强制调度，模型自行按 prompt 推进。`approval`/`answer` 步骤本身不产生事件
   （正式实现可评估审批通过后补一条）。
4. **错误语义**：见 §3.1；`workflow_id` 与手动能力上下文互斥（M1 已有，保留）。

### 3.5 步骤 schema（spike 草案）

```json
[
  {"id": "s1", "type": "knowledge_search", "title": "检索政策", "instruction": "…"},
  {"id": "s2", "type": "a2a",              "title": "委派远程", "instruction": "…"},
  {"id": "s3", "type": "tool",             "config": {"tools": ["mcp__srv__tool"]}},
  {"id": "s4", "type": "approval",         "config": {"tools": ["send_message"]}},
  {"id": "s5", "type": "answer",           "title": "汇总回答"}
]
```

约束：steps 非空数组、≤12 步、id 唯一、instruction ≤500 字符、tool 步骤必须显式列工具。
装配器纯函数（spike 命名）：`validate_steps` / `tool_allowlist` / `force_approval` /
`step_for_tool` / `prompt_section` / `filter_tools`。

## 4. Spike 未覆盖、正式实现要做的

1. **workflows CRUD API**：`GET/POST/PATCH/DELETE /workflows`、`PUT /workflows/{id}/enabled`、
   `GET /workflows/{id}/runs`（立项 §5.2）；spike 只直插 DB 行；
2. **`WorkflowRun` 终态同步**：`_finalize_success/failed/canceled` 里把映射行状态追平；
3. **能力开关**：`workflows_enabled` 配置 + `GET /system/info` 的 `capabilities.workflows`
   （现为恒 False，M1 遗留）；
4. **迁移**：重写 spike 迁移（`down_revision="2d8f6a1c3e70"`），并评估给
   `runs.workflow_id` 补外键；
5. **恢复一致性**：resume 时模板被改/删的行为（快照 steps 进 run 或按 version 拒绝）；
6. **契约面**：`docs/api.md`、OpenAPI 导出、前端 handoff（X1 升级 M9）；
7. **system 模板管理**：谁能建 system 模板（建议 admin，走 M8 的 RBAC 惯例）。

## 5. 回滚记录

按约定，spike 原型已全部还原，master 无 spike 残留：

- 还原：`app/modules/chat/service.py`、`app/modules/chat/tools.py`、
  `app/db/models/__init__.py`、`tests/conftest.py`
- 删除：`app/db/models/workflow.py`、`app/modules/workflows/assembler.py`、
  `alembic/versions/20261112_1300_b7e2f9a4c1d8_m9_workflow_tables.py`、
  `tests/integration/test_workflow_spike.py`
- 回滚后基线复核：全量测试与 ruff 恢复 M8 收口状态

## 6. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-12 | 首次记录：spike 通过，四验证点证据与正式实现输入固定 |
| 2026-11-12 | 正式实现已交付（T1–T9），验收见 roadmap M9 |
