# M9 前端交接：工作流步骤模板接入（2026-11-12）

> 后端范围：M9「工作流后端」（Task 1–8 已完成）。契约真相以
> [`docs/api.md`](../api.md) §1 / §2.3 / §2.16 / §3.1 / §13 与
> [`docs/openapi.json`](../openapi.json) 为准；本文件是前端接入清单，不替代 OpenAPI。
>
> M9 **新增数据库迁移 `e8b4f2a6c9d3`**（`down_revision=2d8f6a1c3e70`，`workflows` 与
> `workflow_runs` 两表，可逆；两表结构以该迁移为准）。**部署前必须执行
> `alembic upgrade head`**，否则 `/workflows` 全部 500。新增配置项
> `WORKFLOWS_ENABLED`（默认 `true`）。
>
> 后端状态：**后端已交付（2026-11-12）；前端尚未接入**。后端不交付任何前端页面。
>
> **无前端 breaking change**：`POST /chat/stream` 只新增可选字段
> （`workflow_id`/`workflow_version`），不传时行为与 M8 完全一致；
> `GET /runs/{id}` 等既有响应只新增字段（`workflow`），不改既有字段；
> M1 占位的 409 `workflow_disabled` 语义恢复为真实现（开关关闭），错误信封结构不变。

## 1. 变更速览

| 变更 | 兼容性 | 前端动作 |
|---|---|---|
| 新增 7 个 `/workflows` 端点（CRUD 5 + 启停 1 + 运行历史 1） | 新能力（登录用户） | 模板列表/详情/编辑页；系统模板只读 |
| `POST /chat/stream` 新增可选 `workflow_id`/`workflow_version` | 兼容（新增可选字段） | 发起时带上所选模板；`workflow_version` 与手头列表缓存比对 |
| `GET /runs/{id}` 新增 `workflow` 字段 `{id, name, version}` | 兼容（新增字段） | run 详情可显示来源模板；**值来自发起时快照**（§6） |
| 新增 SSE 事件 `workflow.step`（落库可回放） | 兼容（新事件） | 时间线步骤卡；未携带模板的 run 不会出现该事件 |
| 409 `workflow_disabled` 从占位变为真实现 | 语义启用 | 按 §4 的消歧规则处理（`capabilities.workflows` 区分） |
| `GET /system/info` 的 `capabilities.workflows` 反映开关（默认 `true`） | 兼容 | 开关关闭时隐藏模板入口与发起选项 |
| 无管理端点（无 `/admin/workflows`）；runs 仅 owner 可查 | 权限面 | 系统模板全员只读；运行历史只在本人模板下展示 |

---

## 2. 端点清单

所有路径前缀 `/api/v1`；请求头 `Authorization: Bearer <access_token>`（与全站一致）。

| 方法 | 路径 | 成功 | 说明 |
|---|---|---|---|
| POST | `/workflows` | 201 | 创建本人模板（`scope=user`、`version=1`、`enabled=true`） |
| GET | `/workflows` | 200 | 系统模板 + 本人模板；`limit`（1–100，默认 20）+ `cursor` 游标分页 |
| GET | `/workflows/{workflow_id}` | 200 | 详情；系统或本人可读，他人/不存在统一 404 |
| PATCH | `/workflows/{workflow_id}` | 200 | 修改本人模板；**成功必 `version += 1`**（§6） |
| PUT | `/workflows/{workflow_id}/enabled` | 200 | 启停本人模板；**不改 `version`** |
| DELETE | `/workflows/{workflow_id}` | 204 | 硬删本人模板（运行历史条目随之删除） |
| GET | `/workflows/{workflow_id}/runs` | 200 | 运行历史；**仅模板 owner**（系统模板 403，他人/不存在 404） |

分页响应统一 `{items, next_cursor, has_more}`；`next_cursor` 为空表示没有更多页；
非法 `cursor` 422 `validation_error`。

### 2.1 模板对象（WorkflowOut，恰好 9 个字段）

```json
{
  "id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
  "scope": "user",
  "name": "退货处理流程",
  "description": "检索政策后委派远程助手复核",
  "steps": [
    {"id": "s1", "type": "knowledge_search", "title": "检索政策", "instruction": "查退货政策要点"},
    {"id": "s2", "type": "a2a", "title": "委派复核", "instruction": "让远程助手复核结论"}
  ],
  "enabled": true,
  "version": 3,
  "created_at": "2026-11-12T12:00:00Z",
  "updated_at": "2026-11-12T12:30:00Z"
}
```

字段要点：

- `scope`：`"system"`（系统模板，全员只读）| `"user"`（本人模板，可编辑）；
- `name`：strip 后 1–100 字符，**不能包含双引号或换行符**（422）——前端输入框直接禁用这两类字符；
- `description`：strip 后 ≤500 字符，允许空串；
- `steps`：形状校验由后端装配器做（422 `workflow_steps_invalid`，message 拼前 3 条）；
  前端编辑器建议直接按 §3 的步骤 schema 渲染与预校验；
- `version`：编辑保存后从响应读回**新版本号**，刷新本地缓存（§6）。

### 2.2 创建 / 修改 / 启停请求体

```json
// POST /workflows
{"name": "退货处理流程", "description": "检索政策后委派远程助手复核", "steps": [
  {"id": "s1", "type": "knowledge_search", "title": "检索政策", "instruction": "查退货政策要点"},
  {"id": "s2", "type": "answer", "title": "汇总回答"}
]}

// PATCH /workflows/{id} —— 只提交变更字段！
{"description": "更新后的描述"}

// PUT /workflows/{id}/enabled
{"enabled": false}
```

## 3. steps schema 与五类步骤语义

```json
[
  {"id": "s1", "type": "knowledge_search", "title": "检索政策", "instruction": "…"},
  {"id": "s2", "type": "tool",             "title": "调内部工具", "config": {"tools": ["mcp__srv__tool"]}},
  {"id": "s3", "type": "a2a",              "title": "委派远程", "instruction": "…"},
  {"id": "s4", "type": "approval",         "title": "外发审批", "config": {"tools": ["send_message"]}},
  {"id": "s5", "type": "answer",           "title": "汇总回答"}
]
```

| type | 提供的工具 | 语义 |
|---|---|---|
| `knowledge_search` | `search_knowledge` | 检索知识库（固定工具） |
| `tool` | `config.tools` 显式清单 | 通用工具步骤（如 MCP 工具） |
| `a2a` | `send_message` | 委派远程 Agent |
| `approval` | 无（不进白名单） | `config.tools` 里的工具**无论原生策略一律强制审批**；不是独立调度点；本身不产生事件 |
| `answer` | 无 | 汇总回答；不提供工具 |

约束（后端一次性收集全部错误，前端可照抄做预校验）：

- `steps` 非空数组、**≤12 步**；每步必须是对象；
- `id` 非空字符串且全表唯一；`type` ∈ 上表五类；
- `title`/`instruction` 可选字符串（`instruction` ≤500 字符）；`config` 可选对象；
- `tool` 步骤必须显式提供非空 `config.tools`（非空字符串列表，单个工具名 ≤200 字符）；
- 全部文本（所有 `id`/`title`/`instruction`/`config.tools` 工具名拼接）总长 ≤8000 字符。

## 4. 错误语义表

| 场景 | HTTP | code | message / 前端处理 |
|---|---|---|---|
| 未登录 / token 失效 | 401 | `unauthorized` | 走既有刷新逻辑 |
| 他人模板或不存在（读/写均同） | 404 | `not_found` | 「工作流不存在」；提示后刷新列表（不区分他人与不存在） |
| `name` 空/超 100 字符/含双引号或换行；`description` 超 500；`steps` 不是数组；`cursor` 非法 | 422 | `validation_error` | 按 message 提示 |
| steps 违反 §3 约束 | 422 | `workflow_steps_invalid` | message 含前 3 条装配器错误，逐条展示 |
| `GET /workflows/{id}/runs` 且模板是系统模板 | 403 | `forbidden` | 「系统模板的运行数据属各用户，不可查看」；隐藏运行历史入口 |
| 发起时 `WORKFLOWS_ENABLED=false` | 409 | `workflow_disabled` | 「工作流能力未启用」；见下方消歧 |
| 发起时模板已停用（`enabled=false`） | 409 | `workflow_disabled` | 「工作流已停用，无法在此轮使用」；提供「去启用」入口（`PUT enabled=true`） |
| 发起时 `workflow_version` 与当前版本不匹配 | 409 | `workflow_version_conflict` | 「工作流版本已更新，请刷新模板后重试」；`GET /workflows/{id}` 取新版本后重试 |
| 发起时模板 steps 不合法（存量坏模板） | 422 | `workflow_steps_invalid` | 同上 |
| 发起时 `workflow_id` 与 `skills`/`mcp_servers`/`mcp_tools`/`knowledge_categories` 同用 | 422 | `workflow_context_conflict` | 「工作流与手动能力上下文不能同时使用」；UI 上二者互斥选择 |
| 发起时 `workflow_version` 脱离 `workflow_id` 单独携带 | 422 | `validation_error` | 「workflow_version 不能脱离 workflow_id 使用」 |

> **`workflow_disabled` 消歧（重要）**：全局开关关闭与单个模板停用**共用此 code**。
> 前端可从 `GET /system/info` 的 `capabilities.workflows` 区分——该键为 `false` 时，
> 任何 `workflow_disabled` 都来自全局开关（直接隐藏工作流入口）；该键为 `true` 时的
> `workflow_disabled` 即模板停用（message 也会不同：「工作流能力未启用」vs「工作流已停用，
> 无法在此轮使用」）。

## 5. 发起工作流（`POST /chat/stream`）

```json
{
  "conversation_id": "0f1e2d3c-4b5a-4968-87a6-5f4e3d2c1b0a",
  "message": "按退货流程处理：用户订单 2024001，商品已签收 7 天",
  "client_message_id": "c-20261112-0001",
  "workflow_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
  "workflow_version": 3
}
```

- `workflow_version` **可省略**（按模板当前版本发起）；省略则不会撞 409
  `workflow_version_conflict`，适合「直接发起」场景；
- 若前端缓存了列表时的 `version`（如模板卡片角标），建议带上 `workflow_version` 做
  乐观校验：409 `workflow_version_conflict` 时重新拉详情，用新版本重试或提示用户；
- `workflow_id` 与手动能力上下文互斥：选择模板时清空 `@技能`/MCP/知识分类选择，反之亦然；
- 发起成功后照常消费 SSE 事件流；携带模板的 run 会额外出现 `workflow.step` 事件（§7）。

## 6. 快照与版本规则（前端必须理解的三件事）

1. **PATCH 必递增 `version`**：任何成功的 PATCH（含仅改 `name`/`description`）都
   `version += 1`；**空 body `{}` 也会递增**并写审计。因此前端 PATCH **只提交变更字段**，
   避免「读-改-全量 PATCH」的习惯在无实际修改时意外抬版本——版本一旦上抬，用户手头
   的 `workflow_version` 发起就会 409。前端无变更时不要发 PATCH。
2. **run 用发起时快照，恢复不受后续 PATCH 影响**：发起时 steps/name/version 冻结进
   run；审批挂起期间模板被改/停用/删除，恢复后仍按旧步骤执行（仅快照缺失的异常数据
   会回退查表，正常路径恒用快照），前端不要做「模板已变，中断恢复」的推断。
3. **`GET /runs/{id}` 的 `workflow` 来自快照**：`{id, name, version}` 由发起时快照构造，
   **模板删除后仍返回快照 name/version**；前端可放心用它展示「来源模板」，不要在拿不到
   模板详情时把 run 的 `workflow` 判空（仅 `workflow: null` 表示该 run 不带模板）。

补充：`PUT enabled` 不改 `version`（启停不影响版本号）；PATCH 提交 `steps` 时整组替换，
不做步骤级合并。

## 7. SSE `workflow.step` 事件帧

帧格式与全站一致（`id`/`event`/`data`，data 为单行 JSON）：

```text
id: 7
event: workflow.step
data: {"run_id":"6f1c2b3a-4d5e-4f60-8a9b-0c1d2e3f4a5b","ts":"2026-11-12T12:00:05.123456Z","seq":7,"step_id":"s1","step_type":"knowledge_search","step_title":"检索政策","tool":"search_knowledge","status":"ok"}
```

- `data` 恰好 8 个键：公共 `run_id`/`ts`/`seq` + payload `step_id`/`step_type`/
  `step_title`/`tool`/`status`；`step_*` 取步骤对象对应值（模板缺该键时为空串 `""`）；
- `status` 复用工具执行状态（`ok`/`error`/`rejected`）；`rejected` 表示工具被审批拒绝；
- **触发时机**：每个工具执行完成后、紧跟对应 `tool.result` 发出；按「工具名 → 模板顺序
  首个提供该工具的步骤」映射。未携带模板的 run 没有该事件；`approval`/`answer` 步骤
  不提供工具，不会命中；
- **seq 顺序**：`seq`（即 SSE `id`）是 run 内单调递增序号。`workflow.step` 与
  `tool.result` 都落库、可回放（`GET /runs/{id}/events` 中 `type="workflow"`、
  `event="workflow.step"`）；`message.delta`/`reasoning.delta` 不落库，回放时 `seq`
  **允许跳号**——前端不要假设 seq 连续，只保证按 seq 升序渲染与断点续传
  （`after_seq`）；审批挂起（`run.interrupted`）期间工具不执行，恢复后才依次出现
  `tool.result` 与 `workflow.step`；
- 步骤卡渲染建议：`step_title` 为空串时回退 `step_id`；同一 `step_id` 可出现多次
  （该步骤提供多个工具或被多次调用），按事件流顺序追加，不按 step_id 去重合并。

## 8. 系统模板只读口径与 runs 权限

- **系统模板（`scope="system"`）人人可读**：出现在 `GET /workflows` 列表与详情中，
  所有登录用户可见、可发起；**任何人不可改删**——PATCH/PUT enabled/DELETE 对系统模板
  返回 404（按不存在处理，不特殊报错）；
- **无管理端点**：v1 没有 `/admin/workflows`；系统模板由运维/种子数据写入，前端不需要
  管理界面；
- **运行历史 owner-only**：`GET /workflows/{id}/runs` 仅模板 owner 可查；系统模板
  403 `forbidden`（运行数据属各用户）；他人模板/不存在 404。前端只在「本人模板」的
  详情页展示运行历史入口；系统模板页不展示；
- 运行历史条目（`WorkflowRunOut`）：`{id, run_id, status, created_at, run_status,
  run_finished_at, run_duration_ms, run_error_code}`；`status` 是映射行状态（创建即
  `running`，终态由后端追平），`run_*` 是 chat run 的终态快照（可为 `null`）；
  `run_id` 可跳转 `GET /runs/{run_id}` 查详情。

## 9. 环境与回归注意

- **部署**：先 `alembic upgrade head`（迁移 `e8b4f2a6c9d3`），再重启服务；
- **CI / 本地测试**：集成测试需 `TEST_DATABASE_URL` 指向带 pgvector 的 PostgreSQL；
  未设置或库不可用时相关用例**静默 skip**——核对回归结果时注意 skip 数，不要误判全绿；
- **能力开关**：`WORKFLOWS_ENABLED=false` 时模板 CRUD 仍可用（配置面不受开关影响），
  仅运行入口被门禁；前端以 `capabilities.workflows` 决定 UI 可见性。

## 10. 联调自测清单

- [ ] 创建模板（含五类步骤各一）→ 201，`version=1`、`enabled=true`、`scope="user"`；
- [ ] `name` 含双引号或换行 → 422 `validation_error`；
- [ ] `tool` 步骤缺 `config.tools` → 422 `workflow_steps_invalid`，message 含「必须显式提供非空 config.tools」；
- [ ] PATCH 只改 description → 200 且 `version+1`；PUT enabled → 200 且 `version` 不变；
- [ ] PATCH 空 body `{}` → 200 且 `version+1`（验证 §6.1，避免前端踩坑）；
- [ ] 带模板发起 → 时间线出现 `tool.result` 与紧跟的 `workflow.step`，步骤卡字段齐全；
- [ ] 断线后用 `after_seq` 续流，`workflow.step` 事件可回放、seq 升序（允许跳号）；
- [ ] 强制审批步骤触发 `run.interrupted` → 批准恢复后模型按旧步骤继续（挂起期间改模板不影响）；
- [ ] 发起时传过期的 `workflow_version` → 409 `workflow_version_conflict`，重拉详情后用新版本成功；
- [ ] 停用模板（`PUT enabled=false`）后发起 → 409 `workflow_disabled`（message「工作流已停用…」）；
- [ ] `WORKFLOWS_ENABLED=false` 环境发起 → 409 `workflow_disabled`（message「工作流能力未启用」），且 `capabilities.workflows=false`；
- [ ] 系统模板：普通用户可读可发起；PATCH/DELETE → 404；`GET /workflows/{id}/runs` → 403 `forbidden`；
- [ ] 删除模板后：`GET /runs/{id}` 仍返回快照 `workflow`（name/version 为发起时值）；
      `GET /workflows/{id}` → 404；`GET /workflows/{id}/runs` → 404。

## 11. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-12 | 首次交付：M9 后端（Task 1–8）契约与接入清单 |
