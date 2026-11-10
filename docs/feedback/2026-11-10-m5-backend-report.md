# M5 审批与安全加固后端交付报告

- 日期：2026-11-10
- 范围：M5 审批与安全加固后端全量（**不含前端**）
- 设计文档：[M5 审批与安全加固设计](../superpowers/specs/2026-11-10-m5-approval-security-design.md)
- 实施计划：[M5 审批与安全加固实施计划](../superpowers/plans/2026-11-10-m5-approval-security.md)
- 用户拍板：HITL 全量迁移、超时 run canceled、平台密钥建表、注入防护低误伤、审计双权限（2026-11-10）

## 1. 交付内容

| 层 | 文件 | 说明 |
|---|---|---|
| 图 | `app/agents/graph.py` | `GraphState` + `halted`；独立 `approval_gate` 节点；gate → tools/END 路由 |
| 审批 | `app/modules/chat/approvals.py` | 幂等创建审批、一次 `interrupt()`、halt gate、取消补 ToolMessage |
| 工具 | `app/modules/chat/tools.py` | 删除进程内 waiter；执行前查审批；外部结果 `<untrusted>` 包裹；`approval_spec` |
| 运行 | `app/modules/chat/service.py` | `prepare_runtime` / `_stream_graph` / `execute_run` / `resume_after_decision` / `halt_run`；`run.interrupted`/`run.resumed`；输出打码 |
| 注册表 | `app/modules/chat/registry.py` | 删除审批 waiter；并发 context 清理修正 |
| 审批服务 | `app/modules/approvals/service.py` | decide 调度恢复（无 pending 才恢复）；过期巡检取消 run + 审计 |
| 审计 | `app/modules/audit/{router,service,schemas}.py` | `GET /audit-logs`、`GET /admin/audit-logs` |
| 密钥 | `app/db/models/secret.py`、`app/modules/secrets/*`、迁移 `9c1d4e7a2b60` | `platform_secrets` + 管理员 CRUD + `secret:NAME` |
| 凭据解析 | `app/core/ssrf.py`、`app/modules/mcp/service.py`、`app/modules/agents/service.py`、`app/modules/a2a/tool.py` | `resolve_headers_async`；`secret:` 写入按角色、运行时无条件解析 |
| 防护 | `app/core/prompt_guard.py`、`app/modules/chat/service.py`、`app/agents/prompts.py` | 注入规则检测/审计/提示加固；输出密钥打码；`<untrusted>` 提示 |
| 限流 | `app/core/config.py`、`app/modules/chat/service.py` | 聊天 30 次/60s（可配）429 `rate_limited` |
| 迁移 | `2d8f6a1c3e70` | `approvals.tool_call_id` + 部分唯一索引 |
| 测试 | `tests/test_graph_gate_unit.py`、`test_prompt_guard_unit.py`、`test_secret_unit.py`、`test_secret_ref_unit.py`、`integration/test_hitl_interrupt.py`、`test_audit_api.py`、`test_chat_rate_limit.py`、`test_secrets_api.py`、`test_security_hardening.py`、`test_hitl_models.py` | 见 §2 |

## 2. 实测证据

### 2.1 全量测试

```
TEST_DATABASE_URL=postgresql+psycopg://devuser:devpass@localhost:54321/agent_hub_test \
  .venv/bin/python -m pytest -q
→ 389 passed, 1 skipped in ~66s
```

- 基线 M4 时期 339 项 → 现在 **389 项通过**（+50）；
- 唯一的 skip 仍是知识库语义评测组（需真实 Embedding key，与 M4 一致）；
- 新增/更新的关键用例：

| 用例 | 覆盖 |
|---|---|
| `test_graph_gate_unit.py`（3） | gate 恢复不重跑 tools；一轮两审批只挂起一次；halt 清理后新输入正常 |
| `test_hitl_interrupt.py`（8） | 批准恢复、拒绝不执行、决定先于挂起、部分内容落库、多审批逐条决定、超时取消、取消后继续聊天、取消后决定 409 |
| `test_hitl_models.py`（2） | `(run_id, tool_call_id)` 唯一；空 tool_call_id 允许多条 |
| `test_approvals.py`（3，更新） | 重复决定、取消、列表统计 |
| `test_mcp.py`（更新） | 审批超时 → expired + run canceled + 晚决定 409 |
| `test_audit_api.py`（4） | 用户自查范围、管理员全局/过滤、时间范围/游标、403 |
| `test_chat_rate_limit.py`（3） | 超限 429、用户隔离、开关关闭 |
| `test_secrets_api.py`（6） | CRUD、无明文、审计、403、503、`secret:` 解析 |
| `test_secret_ref_unit.py`（6） | 引用格式/角色校验、env/secret 解析 |
| `test_security_hardening.py`（4） | 注入审计、外部内容包裹、输出打码、越权 404 |
| `test_prompt_guard_unit.py`（8） | 规则正/负样例、包裹转义、打码 |

### 2.2 关键验收证据（对应 roadmap）

| 验收项 | 证据 |
|---|---|
| 高风险工具调用前必须审批，拒绝后不执行 | `test_interrupt_then_reject_does_not_execute`：MCP mock `tools/call` 计数不变 + `tool.result.status=rejected` |
| 审批超时自动过期，run 进入 canceled | `test_approval_timeout_cancels_run_and_chat_continues` / `test_chat_mcp_approval_timeout_cancels_run`：approval expired、run canceled、`message.done(reason=approval_timeout)` |
| 审计日志可按用户/动作/时间检索 | `test_audit_api.py` 4 个用例（含游标与时间范围） |
| 安全用例（SSRF/越权/注入） | `test_security_hardening.py` + 既有 `test_security_unit.py` / MCP / A2A SSRF 用例全绿 |
| 聊天限流 | `test_chat_rate_limit.py` 429 + `retry_after_seconds` |

### 2.3 行为变化（实测）

- `interrupt()` 恢复只重跑 gate 节点，tools 只执行一次（probe：gate×2、tools×1）；
- 同一 gate 节点顺序调用两次 `interrupt()` 会**错配 resume 值**（probe5），因此一轮多审批只挂起一次；
- 挂起线程直接收新输入会继续停在旧 interrupt；取消/超时必须用 halt gate 清理，并补 ToolMessage 才能继续对话（probe3/4）。

## 3. 决策与偏离

| # | 决策 | 说明 |
|---|---|---|
| 1 | `approval_gate` 独立节点 | 避免工具节点重跑导致副作用重复（实测依据） |
| 2 | 幂等键 `(run_id, tool_call_id)` | gate 恢复重放不重复建审批；部分唯一索引兜底 |
| 3 | 一轮多审批一次 `interrupt()` | 全部 pending 决定后才恢复；中间决定返回 `resumed=false` |
| 4 | 决定先于挂起 | decide 也调度 resume；resume 任务先等旧任务结束再决定是否恢复 |
| 5 | 中断落库口径 | 内容/citations 落库、`messages.status` 保持 `streaming`、`message_count` 不 +1 |
| 6 | halt gate 只清理线程 | 终态事件与 run/message 由取消方（接口/巡检）统一写；避免重复终态 |
| 7 | 知识库固定文案不包裹 | 只有真实检索片段包 `<untrusted>`；「未找到/错误/未启用」保持原文 |
| 8 | `secret:` 写入按提交者角色 | 运行时无条件解析；系统 MCP 的管理员配置接口不在 M5（运维脚本/DB） |

## 4. 前端契约变更（需前端 M5 适配）

1. `approval.required` 后新增非终态 `run.interrupted`（run=`interrupted`），**不再发** `message.done(finish_reason="interrupted")`；SSE 保持连接；
2. 审批决定后 `run.resumed`，继续收到同一 run 的 `message.delta` 与最终 `message.done`；断线用
   `GET /runs/{id}/events?after_seq=` 回放；
3. 审批超时：`message.done.finish_reason="canceled"`、`reason="approval_timeout"`；
4. 聊天 429 `rate_limited`，`details.retry_after_seconds`；
5. 多审批时中间决定 `resumed=false` + `resume_error="本轮还有其他审批待处理"` 属正常；
6. 新端点：`GET /audit-logs`、`GET /admin/audit-logs`、`/admin/secrets` CRUD；
7. `POST /runs/{id}/resume` 仍为占位（409），审批恢复无需调用。

## 5. 未做 / 遗留

- 启动时扫描 interrupted run 并保活、跨实例恢复 → M6；
- 流式输出实时过滤（当前只过滤落库与 `message.done`，已发 delta 不回溯）→ M6；
- 管理控制台前端页面（概览/审计/用户只读/系统设置）→ 前端 M5；
- 平台系统 MCP/A2A 的管理员配置接口 → 后续（当前用运维脚本/DB 配置 `secret:`）；
- 多实例聊天限流（进程内滑动窗口）→ M6 换 Redis；
- 删除被引用的平台密钥不阻止（解析失败跳过，符合设计）。

## 6. 部署与迁移注意

1. **必须执行 `alembic upgrade head`**（新增 `9c1d4e7a2b60`、`2d8f6a1c3e70`）；测试库由 `create_all` 建表，
   若测试库已有旧 `approvals` 表，需要重建该表（本次已手动 DROP 后重建）；
2. 新增配置项已写入 `.env.example` / `.env`：`RATE_LIMIT_CHAT_MAX`、`RATE_LIMIT_CHAT_WINDOW_SECONDS`、
   `PROMPT_GUARD_ENABLED`、`PROMPT_GUARD_REDACT_OUTPUT`；
3. 平台密钥需要 `CAPABILITY_SECRET_KEY`；未配置时 `/admin/secrets` 写入返回 503 `secret_store_unavailable`；
4. `docs/openapi.json` 已重新导出（含审计/密钥端点）。

## 7. 提交记录

- `feat: M5 审批 interrupt 迁移与安全加固`（代码 + 测试）
- `docs: M5 设计、计划、文档与交付报告`（本报告所在提交）
