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
| M2 A2A 出站 | 已完成（2026-11-10） | Agent Card/客户端/`send_message`/任务 API；101 个测试 + 假 Agent 真实联调 |
| M3 A2A 入站 | 已取消（2026-11-10 需求变更） | 本服务只做 A2A 出站 Client，不发布 Agent Card、不提供 JSON-RPC 入站；原范围/验收不适用，M4 顺延 |
| M4 知识库 RAG | 未开始 | `capabilities.knowledge=false`；pgvector 与 `search_knowledge` 未接入 |
| M5 审批与安全加固 | 未开始（部分切片已提前落地） | `approvals`/`audit_logs` 与审批端点已在 M1.5 落地；聊天限流、`interrupt()`/`Command(resume=...)`、注入防护、密钥管理未做 |
| M6 生产化与可观测 | 未开始 | OTel/指标/CI/Dockerfile/容量基线未做 |

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

## M4 知识库 RAG（未开始）

**目标**：知识库入库、检索、回答带来源。

范围：

- [ ] `knowledge_documents` / `knowledge_chunks` 模型与 pgvector 迁移
- [ ] 文档解析（Markdown/TXT/PDF）+ 切分 + Embedding
- [ ] `scripts/build_kb.py`
- [ ] `search_knowledge` 工具
- [ ] `POST /knowledge/search` 调试接口
- [ ] 前端展示来源引用

验收：

- [ ] 上传/重建后能检索到正确片段（离线评测集召回率 ≥ 90%）；
- [ ] 回答包含来源文档与片段定位；
- [ ] 检索 P95 < 200ms（百万级 chunk 以内，本地基准）。

## M5 审批与安全加固（未开始，部分切片已提前落地）

**目标**：高风险动作可控、可审计。

> M1.5 已提前落地 `audit_logs` / `approvals` 最小切片与审批端点，M5 在其上补全管理/审计界面与安全加固。

范围：

- [x] `approvals` / `audit_logs` 模型与迁移（M1.5 已提前落地）
- [ ] 工具 `requires_approval` 声明与 `interrupt()`——当前 HITL 用进程内 waiter，未接 `interrupt()`
- [x] 审批端点（M1.5 已落地 `GET /approvals`、`POST /approvals/{id}/decision`）
- [ ] 审批恢复改走 `Command(resume=...)`（当前为进程内 waiter；跨实例恢复留 M6）
- [ ] 登录/注册/聊天限流——登录/注册限流已在 M1 落地；聊天限流未做
- [ ] 提示词注入防护、输出过滤
- [ ] 密钥引用管理——MCP 凭据已支持 AES-GCM / `env:VAR`；平台级密钥管理未做

验收：

- [ ] 高风险工具调用前必须审批，拒绝后不执行（对话内审批最小闭环已在 M1.5 覆盖）；
- [ ] 审批超时自动过期，run 进入 canceled；
- [ ] 审计日志可按用户/动作/时间检索；
- [ ] 安全用例（SSRF、越权、注入）测试通过。

## M6 生产化与可观测（未开始）

**目标**：可部署、可观测、可运维。

范围：

- [ ] 结构化日志 + OpenTelemetry Trace
- [ ] Prometheus 指标（HTTP、SSE、LLM、A2A、检索）
- [ ] 健康检查 `/readyz`
- [ ] Dockerfile + 生产 Compose/Helm
- [ ] checkpoint/审计数据清理任务
- [ ] 压测与容量基线
- [ ] CI：lint + test + 迁移检查

验收：

- [ ] 单实例 50 并发 SSE 稳定，错误率 < 1%；
- [ ] 关键链路有 trace，P95 可在 Grafana 查看；
- [ ] 滚动发布不中断服务；
- [ ] 故障演练：数据库短时不可用后自动恢复。

## 待决策 / 开放问题

> 以下问题不随里程碑完成状态变化，仍需单独拍板。

| 问题 | 选项 | 当前倾向 |
|---|---|---|
| 多实例 SSE | 粘性会话 / Redis 事件总线 / 前端轮询 | 先单实例，M6 评估 Redis Stream |
| 复杂任务编排 | 单主图 / planner-executor 子图 / 外部工作流引擎 | M5 后按实际任务复杂度评估 |
| 远程 Agent 鉴权（出站） | API Key / OAuth2 / mTLS | M2 已实现 `none` / `api_key`；OAuth2 出站预留，mTLS 交给反向代理 |
| 文件与多模态 | 对象存储 + 签名 URL / 直传 | M4 后 |
| 计费与配额 | 按 token / 按调用次数 | 暂不做 |
| 前端组件库 | Element Plus / Naive UI | 已选 Element Plus，若视觉不满足可替换 |
| 管理员读取用户记忆正文 | 提供审计接口 / 不提供 | v1 不提供，仅数据库运维可访问 |
| 系统 MCP 的按用户凭据 | 平台凭据 / 用户凭据 | v1 平台凭据，按用户凭据 v2 |
| 技能自动触发 | 仅显式 `@` / 模型自动选择 | v1 仅显式，v2 增加 `load_skill` |
