# M7–M9 后端立项：契约补齐、管理台与工作流

- 日期：2026-11-11
- 状态：**已立项，未开始**（本文只定义范围与设计，不代表已实施）
- 依据：[项目整理（2026-11-11）](../../feedback/2026-11-11-project-status-review.md) §3.2/§4.2、前端 M5/M6 交接记录、M2/M5 遗留项
- 前置基线：M6 后端已完成；582 passed / 1 skipped；迁移 head `2d8f6a1c3e70`
- 实施计划：[M7 实施计划](../plans/2026-11-11-m7-contract-security.md)（M8/M9 计划在各自启动时再拆）

## 0. 立项结论

| 编号 | 名称 | 优先级 | 依赖 | 粗略工作量 | 对应前端 |
|---|---|---|---|---|---|
| M7 | 契约补齐与安全收口 | 高（上线后立即） | M6 | 4–5 天 | M7 前端适配（续流/403/审批字段） |
| M8 | 管理台后端（原 X2） | 中 | M7-2 RBAC | 5–7 天 | X2 升级为 M8 前端 |
| M9 | 工作流后端（原 X1） | 中低（需 spike） | M7-1 事件流 | 10–15 天 | X1 升级为 M9 前端 |

> 编号约定：M7/M8/M9 是**后端里程碑编号**；前端 X1/X2 在后端立项后升级为同名 M 编号，
> 但只描述各自仓库的交付，不改变「同名同义」原则。多实例不占编号，见 §6。

## 1. 背景

M6 收尾后，后端主干能力已交付，但有三类「设计还没做、方向已明确」的工作被反复推迟：

1. **前端已经提出、后端契约没补**：断线逐字续流缺少 `event` 字段与 SSE 续流端点；
   agents/tasks 写接口没有 RBAC；`approval.required` 字段少于审批详情；A2A `authorization_url`
   没有后端白名单。
2. **前端已有骨架、后端没有契约**：完整管理控制台（X2）与工作流（X1）长期悬空；
   前端只能放 mock 或置灰入口。
3. **安全/运维遗留**：系统 MCP/A2A 的管理员配置仍靠运维脚本/DB；管理端写操作缺少统一审计。

本立项把第 1、2 类拆成三个可独立验收的里程碑；第 3 类分别并入 M7（安全）与 M8（管理台）。

## 2. 总体原则

- **契约先行**：每个里程碑先冻结 API 形状（OpenAPI + `docs/api.md`），再写实现；
- **不破坏既有契约**：M7 的新增字段/端点对旧前端兼容；M8/M9 只新增管理端/工作流端点；
- **单实例前提不变**：M7 的续流依赖进程内事件总线，跨实例续流仍留 M10+；
- **复用已有表**：M7/M8 预计不需要迁移；M9 新增工作流表，单独迁移；
- **每个里程碑独立可验收**：M7 可由前端逐项联调，M8/M9 不阻塞 M7 上线。

## 3. M7 契约补齐与安全收口

### 3.1 目标

让前端能「断线逐字续流」、写操作有真正的权限边界、审批事件自足、外部授权链接可控；
同时修正 M5/M6 留下的契约文档偏差。

### 3.2 范围

#### M7-1 运行事件回放补 `event` 字段 + SSE 续流端点

**现状**：`GET /runs/{id}/events` 返回的 `RunEventOut` 没有事件名；`payload._event` 被剔除；
`GET /runs/{id}/events` 只是分页查询，不跟随实时流；没有 SSE 续接端点。

**契约变更（新增，不破坏旧字段）**：

1. `RunEventOut` 新增 `event: str`：原始 SSE 事件名（如 `message.delta`、`approval.required`）；
   `type` 仍保留分类（run/message/tool/approval/...），`payload` 仍剔除 `_event`。
2. 新增 `GET /api/v1/runs/{run_id}/stream?after_seq=<int>`：
   - 鉴权 Bearer；校验 run 所有权（不存在/非本人 → 404，不泄露存在性）；
   - `text/event-stream`，帧格式与 `POST /chat/stream` 一致（`id: seq`、`event: 名称`、`data: JSON`、15s `: ping`）；
   - 先回放 `seq > after_seq` 的已落库事件，再订阅实时事件；终态事件后关闭；
   - run 已终态：回放完即关闭；`interrupted`：保持连接等审批恢复（沿用 `HITL_STREAM_TIMEOUT_SECONDS`）；
   - 回放上限 1000 条，超出返回 `400 replay_too_large`（提示先用分页接口或从更近的 `after_seq` 续）；
   - 排水期间行为与现有业务路由一致；当前没有全局业务层 draining 503 拦截，
     若需要「排水即拒绝新流」需单独加中间件（不在 M7 默认范围）；
   - 前端用 `fetch`/`@microsoft/fetch-event-source`（EventSource 不支持自定义 Authorization）。

**非目标**：跨实例续流；`message.delta` 落库；`Last-Event-ID` 请求头（用 query `after_seq`）。

#### M7-2 agents/tasks 写操作 RBAC

**现状**：`app/modules/agents/router.py`、`tasks/router.py` 全部只校验当前用户，普通用户可写；
`require_admin`/`AdminUser` 已在 `app/core/deps.py` 存在并被 knowledge/audit/secrets 使用。

**设计**：

- **复用**现有 `AdminUser` 依赖，只补 403 `forbidden` 的 `details.required_role="admin"`
  与「拒绝写审计」；不新建权限框架；
- 收口接口：
  - agents：`POST /agents`、`POST /agents/preview`（写网络请求，仍限登录用户？**保留登录用户**，只读预览）、
    `PATCH/DELETE /agents/{id}`、`POST /agents/{id}/refresh`、`POST /agents/{id}/health` 改为 admin；
  - tasks：`POST /tasks/{id}/cancel|resume|authorize|reject` 改为 admin；
  - `GET /agents*`、`GET /tasks*` 保持登录用户可读（全局注册表，v1 语义不变）。
- 审计：所有被拒绝的写操作记 `security.rbac_denied`（action/resource/actor），成功写操作沿用现有审计；
- 兼容：前端已隐藏管理员入口；普通用户此前可调用，收紧属于**行为变更**，需在 M7 handoff 标注。

#### M7-3 `approval.required` 事件字段补齐

**现状**：事件只有 8 字段；`ApprovalOut` 还有 `tool_display_name/executor/target/side_effect/payload_preview`。

**设计**：事件 payload 增加这 5 个字段（值来自已创建的 `Approval` 行，不新增查询）；
`payload_preview` 已是 500 字符截断、`target` 2000 字符截断；字段只增不减，旧前端忽略。

#### M7-4 A2A `authorization_url` 白名单

**现状**：A2A 授权信息在 `app/modules/a2a/tool.py` 的 `auth_required` 分支写入
`task.auth_request` 时只截断；`tasks/service.py` 的输出层同样只是截断透传，
没有协议/域名校验。前端只允许 https 与 localhost http，任意 https 钓鱼地址仍可展示。

**设计**：

- 保存时校验：只允许 `https`；`dev/test` 额外允许 `http://localhost`、`http://127.0.0.1`；
  拒绝 `javascript:`/`data:`/`file:`/`ftp:`、内网/回环（prod）、超长（>2000）；
- 违规处置：`authorization_url=None`（不阻断任务状态），记 warning + 审计 `security.auth_url_rejected`；
- 可选配置 `A2A_AUTH_URL_ALLOWED_HOSTS`（逗号分隔；空=任意 https 公网，按当前默认）；
- 前端无需改动（已有 https 限制），但会收到更少的可点击链接。

#### M7-5 契约文档与 OpenAPI 同步

- `docs/api.md`：§7.7 增 `event`；§7.8/§10.2/§10.4 改「续流端点」；§8.2 审批超时字段（已在本次修正）；
- `docs/openapi.json` 重新导出；`/runs/{id}/stream` 的 SSE 描述与响应头；
- 前端 handoff：新增字段/端点/403 行为/续流用法示例；
- 相关文档：`README.md`、`docs/roadmap.md`、`docs/architecture.md` 的 M7 状态。

### 3.3 关键设计决策（待实施时确认）

| # | 决策 | 理由 |
|---|---|---|
| 1 | `event` 用新增字段，不改 `payload` | 旧前端兼容；`_event` 继续作为内部字段 |
| 2 | 续流用 query `after_seq`，不用 `Last-Event-ID` | 与现有分页接口一致；前端重连逻辑已有 `lastSeq` |
| 3 | 回放上限 1000 条 | 防止超长 run 一次打满内存；超出让前端用分页接口 |
| 4 | 写接口收 admin，读接口保持登录用户 | v1 全局注册表语义不变，只收紧写 |
| 5 | `authorization_url` 违规置空而非任务失败 | 远程 Agent 返回的数据不应让整个 A2A 任务失败 |
| 6 | M7 不新增迁移 | `event` 是派生字段，RBAC 用现有 `users.role` |

### 3.4 验收

- [ ] `GET /runs/{id}/events` 每条事件带 `event`，旧字段不变；
- [ ] `GET /runs/{id}/stream?after_seq=`：未授权 401、非本人 404、回放+实时、终态关闭、心跳、超限 400；
- [ ] 普通用户调用 agents/tasks 写接口 403 `forbidden`，管理员成功且写审计；
- [ ] `approval.required` 含 5 个新字段，前端可不再额外拉取审批详情；
- [ ] `authorization_url` 恶意协议/内网地址被拒并留审计；
- [ ] `docs/openapi.json` 与代码一致（CI contract 绿）；全量测试 ≥ 582 passed；ruff 全绿；
- [ ] 前端可按 handoff 接续流与 403 文案。

### 3.5 依赖与风险

- 依赖：无外部依赖；单实例事件总线（跨实例续流不在 M7）；
- 风险：续流端点的所有权校验遗漏会泄露他人 run（用例必须覆盖）；GET SSE 在前端需要 fetch 实现；
  回放上限过小会让长 run 需要分页拼接（先用 1000，实测后调整）。

## 4. M8 管理台后端（原 X2）

### 4.1 目标

给前端管理控制台提供真实契约：用户与角色管理、系统项（指令/技能/MCP/A2A）管理、
只读概览；替代当前的运维脚本/DB 配置。

### 4.2 范围

| 子项 | 端点（草案） | 说明 |
|---|---|---|
| 用户管理 | `GET /admin/users`、`GET /admin/users/{id}`、`PATCH /admin/users/{id}` | 分页/搜索/status/role；可改 role/status；不能改自己的 role、不能禁用最后一个 admin |
| 概览 | `GET /admin/overview` | 用户/会话/run/任务/知识库/存储用量计数 |
| 平台指令 | `GET/PUT /admin/settings/instruction` | 复用 `system_settings` |
| 系统技能 | `GET /admin/skills`、`PUT /admin/skills/{id}/enabled`、`PUT /admin/skills/{id}/force-disable` | 复用 `skills.scope='system'` |
| 系统 MCP | `GET/POST/PATCH/DELETE /admin/mcp/servers` | 复用 `mcp_servers.scope='system'`；`secret:` 引用 |
| 全局 Agent | `GET/PATCH /admin/agents` | 复用 `remote_agents.owner_user_id IS NULL` 的全局注册表 |
| 审计 | `GET /admin/audit-logs`（已有）+ 过滤扩展 | 已有 M5 基础 |

### 4.3 非目标

- 用户密码重置/强制下线（可后续单独做）；
- 多角色 RBAC（v1 仍只有 `user`/`admin`）；
- 数据导出/批量操作；
- 前端页面本身（在前端 M8 交付）。

### 4.4 验收

- [ ] 管理员可查用户、改角色/状态，规则（不能改自己、不能禁用最后一个 admin）有测试；
- [ ] 系统指令/技能/MCP/Agent 可读可改，`force_disabled` 压过用户覆盖；
- [ ] 普通用户访问任何 `/admin/*` 返回 403；
- [ ] 关键写操作写审计；`GET /admin/overview` 返回计数；
- [ ] OpenAPI/文档/前端 handoff 同步。

### 4.5 依赖与风险

- 依赖 M7-2 的 `require_admin`；
- 预计无迁移（`users.status`、`system_settings`、`scope/force_disabled` 已存在）；
- 风险：管理端写操作影响面大，需要「不能把自己锁死」的规则与审计。

## 5. M9 工作流后端（原 X1）

### 5.1 目标

把技能、检索、A2A、MCP 与审批编排成**轻量步骤模板**，可保存、启停、运行、回放；
先做 MVP，不引入外部工作流引擎。

### 5.2 范围（MVP）

- 表：`workflows`（id、owner_user_id、scope、name、description、steps JSONB、enabled、时间戳）、
  `workflow_runs`（id、workflow_id、run_id、status、时间戳）；
- 步骤类型（MVP 五类）：`knowledge_search`、`tool`（原生/技能/MCP）、`a2a`、`approval`、`answer`；
- API：`GET/POST/PATCH/DELETE /workflows`、`PUT /workflows/{id}/enabled`、
  `GET /workflows/{id}/runs`；运行入口复用 `POST /chat/stream` 的 `workflow_id` 字段；
- 执行：MVP 用「步骤模板注入 system prompt + 工具白名单 + 审批闸门」，不做强制 DAG 调度；
  运行事件复用 `run_events`（`workflow.step` 事件在 M9 补齐）；
- 前端：X1 升级为 M9 前端，骨架 + mock 升级为真实页面。

### 5.3 非目标

- planner-executor 子图、并行分支、条件跳转；
- 外部工作流引擎（Temporal/Camunda 等）；
- 可视化拖拽编排器（前端后续 F 系列）；
- 跨用户共享工作流（v1 系统模板 + 用户自建）。

### 5.4 验收

- [ ] 一个 3 步模板（检索 → A2A → 审批）可保存并跑通，事件可见；
- [ ] 取消/失败/审批超时语义与现有 run 一致；
- [ ] 步骤类型/顺序/工具白名单校验有测试；
- [ ] 工作流运行可在 `GET /workflows/{id}/runs` 查询；
- [ ] 迁移可逆（upgrade/downgrade）。

### 5.5 依赖与风险

- 依赖 M7-1 的事件流（工作流运行态需要续流）；
- 先做 0.5–1 天技术 spike：确认「模板注入」能约束主图而不破坏现有工具选择；
- 风险：工作流是最容易范围膨胀的项，MVP 必须坚持「不做 DAG/不做外部引擎」；
  与主图耦合深，建议独立分支 + 独立审查。

## 6. 候选池（不占里程碑编号）

| 项 | 触发条件 | 备注 |
|---|---|---|
| 多实例（Redis 事件总线/共享限流/跨实例 resume） | 出现扩容/多副本需求 | 原「M7 决策」改为需求触发；候选 M10+ |
| 通用 `POST /runs/{id}/resume`（ask_user 类） | 需要「向用户追问并恢复同一 run」的能力时 | 当前恒 409 占位；可复用审批的 `Command(resume=...)` 机制 |
| 对象存储真实 MinIO 端到端联调 | 启用 `STORAGE_BACKEND=s3` 前 | 当前只有单元测试与注入 client，未对真实 MinIO 验证 |
| 知识库语义召回评测取证 | 拿到真实 Embedding key 后 | `KNOWLEDGE_EVAL_REAL=1` 逃生舱已就绪；≥90% 目标未取证 |
| 对象存储孤儿文件巡检 | 启用 S3/MinIO 或附件量增大 | M4 遗留，先做对账脚本 |
| 上传异步入库 | 大文件 + 真实 Embedding 超时频发 | 状态机四态已保留 |
| MCP 连接池 / 复杂 JSON Schema | 高频工具调用或复杂 MCP 工具 | M1.5 遗留 |
| A2A 健康巡检 / 轮询退避 | 远程 Agent 数量增大 | M2 遗留 |
| MCP stdio 沙箱 | 要开放用户自建 stdio | 默认关闭，启用即信任 |
| 消息正文保留策略 | 合规/存储成本要求 | 产品决策，不属技术里程碑 |

## 7. 排期建议

```
M7（4–5 天）→ M8（5–7 天）→ M9 spike（1 天）→ M9（10–15 天）
```

- M7 与 M8 可部分并行（M8 依赖 M7-2 先落地）；
- M9 启动前先做 spike，spike 不通过则回到方案讨论，不硬推；
- 每个里程碑独立提交/独立报告，不合并发布。

## 8. 与前端对齐

| 里程碑 | 前端动作 | 契约来源 |
|---|---|---|
| M7 | 接 `GET /runs/{id}/stream` 逐字续流；处理写接口 403；审批事件可直接用 5 个新字段 | M7 handoff + OpenAPI |
| M8 | X2 升级为 M8：用户/角色、系统项、概览页 | `docs/api.md` §2.12 + M8 handoff |
| M9 | X1 升级为 M9：工作流 CRUD/运行/事件；骨架/mock 替换 | M9 handoff + OpenAPI |

> 前端仓库的 roadmap 状态更新由前端会话完成（跨仓库），本立项只保证后端契约与文档。

## 9. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-11 | 首次立项：M7 契约补齐与安全收口、M8 管理台后端、M9 工作流后端；多实例移入候选池 |
