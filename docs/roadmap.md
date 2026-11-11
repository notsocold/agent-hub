# 路线图

> 原则：每个里程碑都能独立演示、独立验收；先打通最小闭环，再扩展能力。

## 里程碑状态总览

> 状态口径：**已完成** = 范围项交付且验收/联调有可复查证据；**未开始** = 尚未进入编码；部分能力提前落地的，在说明中标注。
> 标记约定：范围与验收清单中 `[x]` 为已完成、`[ ]` 为未完成；标题中的状态是里程碑级结论。

| 里程碑 | 状态 | 证据 / 说明 |
|---|---|---|
| M0 项目骨架 | 已完成 | 目录、依赖声明、配置模板、文档与前端脚手架；交付物全部勾选 |
| M1 对话最小闭环 | 已完成（2026-11-09） | 认证/会话/SSE/运行/系统信息；前端真实联调 `smoke:real` 97 项；首 token P95 压测并入 M6 |
| M1.5 个人能力层 | 已完成（2026-11-09） | 指令/记忆/技能/MCP/附件/HITL；85 个测试 + `smoke:m15` 68 项；遗留项见 M1.5 报告 §6 |
| M2 A2A 出站 | 已完成（2026-11-10） | Agent Card/客户端/`send_message`/任务 API；101 个测试 + 假 Agent 真实联调；同日出站客户端迁移到 `a2a-sdk` 1.3.0（108 测试） |
| M3 A2A 入站 | 已取消（2026-11-10 需求变更） | 本服务只做 A2A 出站 Client，不发布 Agent Card、不提供 JSON-RPC 入站；原范围/验收不适用，M4 顺延 |
| M4 知识库 RAG | 后端已完成（2026-11-10）；前端已联调 | 模型/pgvector 迁移/解析/切分/双通道 Embedding/7 个 API/`search_knowledge`/引用落库/`build_kb.py`/离线评测；323 测试通过、修复时钟抖动后全量 10 连跑 0 失败；语义召回率因无真实 Embedding key **未取得证据**；前端知识库页与引用展示已联调（2026-11-10） |
| M5 审批与安全加固 | 后端已完成（2026-11-10） | HITL 迁移到 `interrupt()`/`Command(resume=...)`；超时取消、聊天限流、注入检测/输出打码、平台密钥、审计检索；395 测试通过；跨实例启动保活已并入 M6；前端 M5 已联调（2026-11-11） |
| M6 生产化与可观测 | 后端已完成（2026-11-11） | 日志/OTel/指标/readyz/Dockerfile+Compose/清理/启动恢复/流式过滤/压测/CI；582 测试通过；Grafana 与真实 LB 滚动未实测 |
| M7 契约补齐与安全收口 | 已完成（2026-11-11；后端契约与全量回归完成，前端待接入） | 事件回放 `event` + SSE 续流端点、agents/tasks 写操作 RBAC、审批事件字段、A2A 授权 URL 白名单、契约/OpenAPI/前端 handoff；无迁移；733 passed / 1 skipped、ruff 全绿、`alembic check` 无漂移、OpenAPI 无 diff |
| M8 管理台后端（原 X2） | 实施完成，待独立审查（2026-11-11；前端未接入） | 15 个 `/admin/*` 端点：用户/角色、只读概览、平台指令、系统技能、系统 MCP、全局 Agent；管理端配置面绕过能力开关（R1）、全局 Agent 全局注册表口径（R8）；无迁移；810 passed / 1 skipped、ruff 全绿、`alembic check` 无漂移、OpenAPI 无 diff |
| M9 工作流后端（原 X1） | 已立项（2026-11-11），未开始 | 轻量步骤模板 MVP（检索/工具/A2A/审批/回答）；先 spike，不引入外部引擎 |

> 注：M1.5 是追加的中间里程碑，排在 M1 之后、M2 之前，不改动原 M2–M6 的编号含义。

## M0 项目骨架（已完成）

**目标**：目录、依赖声明、配置模板、数据库编排配置、文档就绪；本阶段**不包含实现代码与运行验证**。

交付物：

- [x] `app/` 分层目录与模块约定
- [x] `pyproject.toml` 依赖声明（未安装、无锁文件）
- [x] `.env.example` 配置模板
- [x] 可选的 PostgreSQL + pgvector Docker Compose 与 initdb SQL（默认使用本机已有 PostgreSQL，不要求启动）
- [x] `app/db/` 目录化方案（空包，待实现）
- [x] 架构 / 数据库 / A2A / API / 开发文档
- [x] 前端 `agent-hub-web` 脚手架（create-vue bare，未安装依赖）

验收：

- [x] 目录结构与文档描述一致；
- [x] `pyproject.toml` / `package.json` 的依赖声明与技术选型文档一致；
- [x] 配置文件可审阅，不要求本次可启动/可构建；
- [x] 需求细化后再进入 M1 编码。

## M1 对话最小闭环（已完成，2026-11-09）

**目标**：注册登录 → 创建会话 → 发消息 → 流式回答 → 历史可查。

范围：

- [x] `users` / `conversations` / `messages` / `runs` 模型与迁移
- [x] JWT 注册、登录、刷新、`/auth/me`
- [x] 会话 CRUD、消息历史分页
- [x] LangGraph 主图（无工具版）+ PostgresSaver
- [x] `POST /chat/stream` SSE：`message.delta` / `message.done`
- [x] 前端聊天页接通

验收：

- [x] 新用户注册后能连续多轮对话，刷新页面历史仍在；
- [ ] SSE 首 token P95 < 3s（本地假模型可更短）——未单独做 P95 压测；联调单次实测 `first_token_ms=966ms`，P95 基线并入 M6 容量验收；
- [x] 同一会话并发发消息被串行化或返回 409；
- [x] 断线后重新查询 run 能拿到最终结果。

## M1.5 个人能力层 — MCP / Skill / 长期记忆 / Instruction（已完成，2026-11-09）

**目标**：账号级能力配置 + 会话内显式使用；系统默认与用户自定义在运行时合并。

> 编号 1.5 表示排在 M1 之后、M2 之前，不改动既有 M2–M6 编号。详细设计见 [个人能力层设计](superpowers/specs/2026-11-05-agent-capabilities-design.md)。

**前置与切片**：以 M1 完成为前提；`tool_invocations`、`audit_logs`、`approvals` 与共享 SSRF 主机校验以最小切片提前落地，M2/M5 的完整范围不变。

范围：

- [x] `users.role`、`system_settings`、`user_settings`、`user_capability_overrides`
- [x] `tool_invocations` / `audit_logs` / `approvals` 最小切片（M2/M5 复用）
- [x] 指令：平台默认 + 用户覆盖（覆盖式）、生效预览
- [x] 长期记忆：`memories` 业务表 + `save_memory` + 管理接口（原设计为 LangGraph `AsyncPostgresStore`，实现改为业务表；语义检索 M4）
- [x] 技能：`skills` / `skill_files`、zip 导入、`@` 显式激活、`read_skill_file`
- [x] MCP：`mcp_servers` / `mcp_tools`、远程 Streamable HTTP/SSE、工具发现、审批接入、系统 stdio
- [x] 前端：设置页四个区块、输入框 `@` 选择器、Skill/记忆/MCP 事件卡
- [x] 审计与降级事件（聊天限流不在本里程碑，随 M5）

验收：

- [x] 指令覆盖生效，点「恢复默认」回到平台默认；
- [x] 记忆可保存、跨会话检索、可编辑/归档/删除/清空；
- [x] 技能包可导入并在会话中显式使用，时间线出现 Skill 卡；
- [x] MCP 工具可发现可调用，写工具必须审批，拒绝后不执行；
- [x] 普通用户只能使用系统 MCP 的只读工具，写工具仅管理员可用；
- [x] 跨用户访问返回 404；
- [x] MCP 不可达时对话不中断，发 `notice` 降级；记忆改用 `memories` 业务表后，原「Store 故障」场景不再适用。

## M2 A2A 出站（已完成，2026-11-10）

**目标**：注册远程 Agent，主智能体能把任务委派出去并整合结果。

范围：

- [x] `remote_agents` / `a2a_tasks` / `tool_invocations` 模型与迁移
- [x] Agent Card 拉取、校验、SSRF 防护
- [x] A2A Client 封装（发送、流式、超时、取消、错误映射）
- [x] 远程 Agent → 统一 `send_message` 工具（按注册表枚举 Agent；未生成 per-agent 包装工具）
- [x] `agents` 管理接口、健康检查
- [x] SSE 增加 `tool.start` / `tool.result` / `a2a.task`（另含 `a2a.artifact` / `a2a.input_required` / `a2a.auth_required`）

验收：

- [x] 注册本地假 Agent 后，下一轮对话工具列表包含其技能（实际为统一 `send_message` 工具的 Agent 枚举与技能描述）；
- [x] 主智能体能根据问题选择正确工具并返回远程结果；
- [x] 远程超时/500/非法响应均有可读错误，不导致整轮失败崩溃；
- [x] 任务记录可在 `GET /api/v1/tasks` 查询。

## M3 A2A 入站（已取消，2026-11-10 需求变更）

> **需求变更**：本服务只做 A2A 出站 Client，不发布 Agent Card、不暴露 JSON-RPC 入站端点。
> 本里程碑作废，M4 顺延；以下原范围与原验收保留作历史记录，均不适用（标记 `[-]`）。
> 代码侧已移除入站占位：`system/info.a2a.enabled` 恒 `false`、`card_url`/`jsonrpc_url` 恒空，
> 并新增契约测试固定「`/a2a` 与 well-known 不注册」。变更记录见
> [需求变更记录](feedback/2026-11-10-requirement-change-outbound-only.md)。

**原目标**：本服务可作为 A2A Server 被其他智能体调用。

原范围（不适用）：

- [-] Agent Card 构建与 well-known 路由
- [-] `LangGraphAgentExecutor` 实现 `execute()` / `cancel()`
- [-] 入站鉴权（API Key，OAuth2 预留）
- [-] contextId ↔ conversationId 映射
- [-] 任务状态、artifact 流式回传

原验收（不适用）：

- [-] `a2a-inspector` 能发现 Agent Card 并完成一次 `message/send`；
- [-] 入站任务在 `a2a_tasks` 可见，`source=a2a`；
- [-] 取消请求能停止图执行；
- [-] 未授权请求返回 401/403，不泄露内部信息。

## M4 知识库 RAG（后端已完成、前端已联调，2026-11-10）

**目标**：知识库入库、检索、回答带来源。

> 交付证据见 [M4 后端交付报告](feedback/2026-11-10-m4-backend-report.md)，设计见
> [知识库 RAG 设计](superpowers/specs/2026-11-10-m4-knowledge-rag-design.md)。
> 后端先交付，前端知识库页与引用展示已于同日联调完成（`smoke:m4` + `test:real`）。

范围：

- [x] `knowledge_documents` / `knowledge_chunks` 模型与 pgvector 迁移（`c4e7a91b2f36`，含两条部分唯一索引、HNSW、GIN）
- [x] 文档解析（Markdown/TXT/PDF/CSV/JSON）+ 切分 + Embedding（fake / OpenAI 兼容双通道）
- [x] `scripts/build_kb.py`（支持 `--dry-run` / `--rebuild` / `--owner`）
- [x] `search_knowledge` 工具（含分类默认过滤、去重重编号、失败降级）
- [x] `POST /knowledge/search` 调试接口（外加文档 CRUD/retry/rebuild 共 7 个端点）
- [x] 前端展示来源引用 —— 后端契约（`message.done.citations` + 落库 `messages.citations`）已就绪并实测；前端 M4 已联调

验收：

- [x] 上传/重建后能检索到正确片段：离线评测**链路组 15/15** 条 top-1 命中预期文档且含预期关键词；**语义组 5 条在 fake embedding 下全部未命中**（证明这组测的是语义而非词面）。**「语义召回率 ≥ 90%」未取得证据**——本机无可用 Embedding key，`KNOWLEDGE_EVAL_REAL=1` 逃生舱已就绪；
- [x] 回答包含来源文档与片段定位：真实 HTTP 抓包 `tool.result` / `message.done` 均带 `citations`，`GET /conversations/{id}/messages` 回读一致（刷新后仍在）；
- [x] 检索 P95 < 200ms：实测**服务端 P95 = 2ms、端到端 P95 = 7.8ms**（50 次采样）。**样本仅 3 文档 / 6 分块，百万级规模未验证**，容量基线并入 M6。

## M5 审批与安全加固（后端已完成，2026-11-10）

**目标**：高风险动作可控、可审计。

> 交付证据见 [M5 后端交付报告](feedback/2026-11-10-m5-backend-report.md)，设计见
> [M5 审批与安全加固设计](superpowers/specs/2026-11-10-m5-approval-security-design.md)。
> M1.5 已提前落地 `audit_logs` / `approvals` 最小切片与审批端点，M5 在其上完成 interrupt 迁移与安全加固。
> 遗留：启动时扫描 interrupted run 保活、流式输出实时过滤已并入 M6；前端 M5 审批中心与安全 UX 已联调（2026-11-11）。

范围：

- [x] `approvals` / `audit_logs` 模型与迁移（M1.5 已提前落地）
- [x] 工具 `requires_approval` 声明与 `interrupt()`（独立 `approval_gate` 节点，一轮多审批只挂起一次）
- [x] 审批端点（M1.5 已落地 `GET /approvals`、`POST /approvals/{id}/decision`）
- [x] 审批恢复改走 `Command(resume=...)`（决定接口自动调度；同进程/同线程 checkpoint 均可恢复）
- [x] 登录/注册/聊天限流（聊天 30 次/60s 可配，429 `rate_limited`）
- [x] 提示词注入防护、输出过滤（检测+审计+系统提示加固；外部内容 `<untrusted>`；输出密钥打码）
- [x] 密钥引用管理（`platform_secrets` + `/admin/secrets` + `secret:NAME` 引用）
- [x] 审计检索（`GET /audit-logs`、`GET /admin/audit-logs`）

验收：

- [x] 高风险工具调用前必须审批，拒绝后不执行（`test_hitl_interrupt.py`：拒绝后 MCP 未收到调用）；
- [x] 审批超时自动过期，run 进入 canceled（`test_hitl_interrupt` / `test_mcp`：expired + canceled + `message.done.reason=approval_timeout`）；
- [x] 审计日志可按用户/动作/时间检索（`test_audit_api.py`：范围、过滤、游标）；
- [x] 安全用例（SSRF、越权、注入）测试通过（`test_security_hardening.py` + 既有 SSRF/越权用例，全量 395 通过）。

## M6 生产化与可观测（后端已完成，2026-11-11）

**目标**：可部署、可观测、可运维。

> 交付证据见 [M6 后端交付报告](feedback/2026-11-11-m6-backend-report.md)，设计/计划见
> [M6 设计](superpowers/specs/2026-11-11-m6-production-observability-design.md) /
> [M6 实施计划](superpowers/plans/2026-11-11-m6-production-observability.md)。
> 本轮只做后端与运维产物；前端无 M6 契约变更。

范围：

- [x] 结构化日志 + OpenTelemetry Trace（JSON 日志、OTLP/HTTP、未配置 no-op）
- [x] Prometheus 指标（HTTP、SSE、LLM、工具、A2A、检索、审批、限流、清理）
- [x] 健康检查 `/readyz`（DB/迁移/checkpoint/存储/排水；`/healthz` 纯存活）
- [x] Dockerfile + 生产 Compose（非 root、健康检查、Prometheus/Grafana/Nginx profile；不含 Helm）
- [x] checkpoint/审计/事件/附件/token 清理任务（CLI + 后台调度）
- [x] 压测与容量基线（`scripts/load_test_sse.py`）
- [x] CI：lint + test + 迁移检查 + OpenAPI diff + 镜像构建
- [x] 承接 M5：启动恢复、流式输出实时过滤

验收：

- [x] 单实例 50 并发 SSE 稳定，错误率 < 1%：实测 50/50 成功、错误率 0、P95 首 token 2270ms
  （本地 fake LLM、单实例；样本与脚本见交付报告）；
- [x] 关键链路有 trace：`http.request → chat.run → llm.chat / tool.execute` 由
  `test_observability_chat.py` 的 InMemorySpanExporter 断言；Grafana 看板与 PromQL 已交付，
  **未实际启动 Grafana/Tempo 验收**（用户约定不新起容器）；
- [ ] 滚动发布不中断服务：已实现优雅停机排水，`scripts/graceful_shutdown_drill.py` 双进程演练
  通过（在跑 SSE 全部收到终态、另一实例 3/3 成功、重启就绪）；Compose 本身不做零停机滚动，
  真实 LB 下的滚动发布**未演练**；
- [x] 故障演练：数据库短时不可用后自动恢复：TCP 代理切断后 `/readyz` 503、业务请求 503
  `database_unavailable`，恢复后 `/readyz` 200 且聊天成功，API 进程未重启。

## M7 契约补齐与安全收口（已完成，2026-11-11）

**目标**：补齐前端已提出的契约缺口，给写操作真正的权限边界。

> 立项设计见 [M7–M9 立项](superpowers/specs/2026-11-11-m7-m9-backend-roadmap-design.md) §3，
> 实施计划见 [M7 实施计划](superpowers/plans/2026-11-11-m7-contract-security.md)，
> 前端接入见 [M7 前端交接](feedback/2026-11-11-m7-frontend-handoff.md)，
> 回归证据与已知缺口见 [M7 后端交付报告](feedback/2026-11-11-m7-backend-report.md)。
> 状态口径：Task 1–6 已交付，Task 7 全量回归已完成（2026-11-11）；Task 7 的独立审查由
> 独立 reviewer 并行执行，审查发现由后续修复轮处理；前端「逐字续流」等 M7 前端接入尚未验收。

范围：

- [x] `GET /runs/{id}/events` 新增 `event` 字段；新增 `GET /runs/{id}/stream?after_seq=` SSE 续流端点
      （续流只回放已落库事件；`message.delta`/`reasoning.delta` 不落库、断线不补）
- [x] agents/tasks 写接口收紧到 `AdminUser`（403 `forbidden` + `details.required_role`），读接口不变
- [x] `approval.required` 补 `tool_display_name/executor/target/side_effect/payload_preview`
- [x] A2A `authorization_url` 协议/域名白名单，违规置空并审计
- [x] 契约文档、OpenAPI、前端 handoff 同步（`docs/api.md` §11、`docs/openapi.json`、
      [M7 前端交接](feedback/2026-11-11-m7-frontend-handoff.md)）

验收：

- [ ] 前端断线后可从 `lastSeq` 续流已落库事件；非本人 run 404、未登录 401
      （后端契约已交付并有 `tests/integration/test_run_stream.py`；前端待按 handoff 接入；
      断线窗口内的 delta 不补发，终态后需拉消息终稿）
- [x] `GET /runs/{id}/events` 每条带 `event` 且旧字段不变（`tests/integration/test_chat.py`）
- [x] 普通用户写 agents/tasks 返回 403；管理员成功且写审计
      （`tests/integration/test_rbac.py`；拒绝审计 `security.rbac_denied`）
- [x] `approval.required` 含 5 个新字段，值与 `GET /approvals/{id}` 一致
      （`tests/integration/test_hitl_interrupt.py`）
- [x] `authorization_url` 恶意协议/内网地址被拒并留审计，历史脏数据展示被过滤
      （`tests/test_auth_url_unit.py`、`tests/integration/test_tasks.py`）
- [x] `openapi.json` 与代码一致；全量测试 ≥ 582 passed；ruff 全绿；无迁移
      （Task 7 回归：733 passed, 1 skipped；ruff 全绿；`alembic check` 无漂移，head `2d8f6a1c3e70`；
      OpenAPI `git diff --exit-code` 干净）
- [x] Task 7 全量回归完成（2026-11-11，证据见 [M7 后端交付报告](feedback/2026-11-11-m7-backend-report.md)）
- [ ] Task 7 独立审查：由独立 reviewer 并行执行，发现项由后续修复轮处理
      （本里程碑状态收口不等待该结论；审查完成后另行勾选）

## M8 管理台后端（原 X2，实施完成 2026-11-11，待独立审查）

**目标**：给前端管理控制台提供真实契约，替代运维脚本/DB 配置。

> 立项设计见 [M7–M9 立项](superpowers/specs/2026-11-11-m7-m9-backend-roadmap-design.md) §4，
> 实施计划见 [M8 实施计划](superpowers/plans/2026-11-11-m8-admin-console.md)，
> 前端接入见 [M8 前端交接](feedback/2026-11-11-m8-frontend-handoff.md)，
> 回归证据与已知缺口见 [M8 后端交付报告](feedback/2026-11-11-m8-backend-report.md)。
> 状态口径：Task 1–7 已交付，全量回归与交付报告完成（2026-11-11）；**独立审查进行中**，
> 审查结论由控制器确认后再翻为「已完成」；M8 前端页面与联调尚未开始。

范围：

- [x] 用户列表/详情/角色与状态调整（不能改自己、不能停用自己、不能禁用最后一个活跃管理员）
- [x] 平台指令、系统技能、系统 MCP、全局 Agent 的查询与管理（复用 `scope`/`force_disabled`/`system_settings`）
- [x] `GET /admin/overview` 只读统计（8 条聚合 SQL，不加载 ORM 行）
- [x] 管理端审计与 403 契约（`forbidden` + `details.required_role="admin"`，拒绝写审计）
- [x] 管理端配置面绕过能力总开关（R1）；全局 Agent 采用「全部未软删」注册表口径（R8，与 spec 字面偏差已裁定）
- [x] 契约文档、OpenAPI、前端 handoff 同步（`docs/api.md` §12、`docs/openapi.json`）
- [x] **无迁移**：未新增表/列/索引；迁移 head 仍 `2d8f6a1c3e70`

验收：

- [x] 管理员可查用户、改角色/状态；「不能改自己」「不能禁用自己的账号」「不能移除最后一个
      活跃管理员」有 HTTP 与服务层测试（`test_admin_users.py`）
- [x] 平台指令、系统技能、系统 MCP、全局 Agent 可读可改；`force_disabled` 压过用户覆盖
      （技能与 MCP 各有测试）；敏感头/密钥引用不在响应与审计回显
- [x] 普通用户访问任何新增 `/admin/*` 返回 403 `forbidden` + `details.required_role="admin"`；
      未登录 401；关键写操作全部写审计（`test_admin_*` 六个文件共 50 条用例）
- [x] OpenAPI / `docs/api.md` / 前端 handoff / README / roadmap 同步；无迁移
      （810 passed, 1 skipped；ruff 全绿；`alembic check` 无漂移；OpenAPI `git diff --exit-code` 干净）
- [ ] 前端管理台页面接入与联调（前端 M8 范围；后端契约与 handoff 已交付）
- [ ] 独立审查收口（审查进行中，结论由控制器确认）

## M9 工作流后端（原 X1，已立项 2026-11-11；未开始）

**目标**：把技能、检索、A2A、MCP 与审批编排成轻量步骤模板。

> 设计见 [M7–M9 立项](superpowers/specs/2026-11-11-m7-m9-backend-roadmap-design.md) §5。
> 启动前先做 1 天技术 spike，确认模板注入不破坏主图工具选择。

范围（MVP）：

- [ ] `workflows` / `workflow_runs` 表与迁移（可逆）
- [ ] 步骤类型：`knowledge_search` / `tool` / `a2a` / `approval` / `answer`
- [ ] CRUD/启停/运行入口（`POST /chat/stream` 的 `workflow_id`）/运行历史
- [ ] `workflow.step` 事件与前端 handoff

验收：

- [ ] 3 步模板（检索 → A2A → 审批）跑通，取消/失败/审批超时语义一致
- [ ] 不做 DAG/并行/外部引擎；迁移可逆；全量测试通过

## 待决策 / 开放问题

> 以下问题不随里程碑完成状态变化，仍需单独拍板。

| 问题 | 选项 | 当前倾向 |
|---|---|---|
| 多实例 SSE | 粘性会话 / Redis 事件总线 / 前端轮询 | 先单实例；扩容需求触发后再立项（候选 M10+） |
| 复杂任务编排 | 单主图 / planner-executor 子图 / 外部工作流引擎 | 已立项为 M9（轻量步骤模板 MVP，不引入外部引擎） |
| 远程 Agent 鉴权（出站） | API Key / OAuth2 / mTLS | M2 已实现 `none` / `api_key`；OAuth2 出站预留，mTLS 交给反向代理 |
| 文件与多模态 | 对象存储 + 签名 URL / 直传 | M4 后 |
| 计费与配额 | 按 token / 按调用次数 | 暂不做 |
| 前端组件库 | Element Plus / Naive UI | 已选 Element Plus，若视觉不满足可替换 |
| 管理员读取用户记忆正文 | 提供审计接口 / 不提供 | v1 不提供，仅数据库运维可访问 |
| 系统 MCP 的按用户凭据 | 平台凭据 / 用户凭据 | v1 平台凭据，按用户凭据 v2 |
| 技能自动触发 | 仅显式 `@` / 模型自动选择 | v1 仅显式，v2 增加 `load_skill` |
