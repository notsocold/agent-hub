# M2 A2A 出站后端交接报告

- 日期：2026-11-10
- 仓库：`agent-hub`（FastAPI + SQLAlchemy async + Alembic + LangGraph）
- 范围：远程 Agent 注册表 / Agent Card 拉取校验 / A2A 出站客户端 / 统一 send_message 工具 / 任务查询与动作 / 聊天与系统集成
- 结论：**M2 全部交付并验证通过**；前端智能体页/任务页可按契约直接联调。

---

## 1. 一句话结论

M2 按任务口径完整落地：2 张新表 + 可逆迁移、8 个 agents 端点、6 个 tasks 端点、自研 A2A JSON-RPC 2.0 客户端（兼容 0.3/1.0 方法名）、统一 `send_message` 委派工具（含 HITL 式暂停/恢复）、4 个新 SSE 事件与 runs pending/agents 扩展。**101 个测试全绿（85 基线 + 16 新增）、ruff 通过、迁移 upgrade/downgrade/upgrade 通过、8000 实例真实联调全链路跑通。**

## 2. 实现清单

### 2.1 数据模型与迁移（revision `8a2f7c31e9d5`，可逆，`alembic check` 无漂移）

| 表 | 关键字段 |
|---|---|
| `remote_agents` | owner_user_id(可空，v1 全局注册表)/slug(部分唯一索引，deleted_at IS NULL)/name/description/url/protocol_version/card(JSONB 原样)/skills/capabilities/auth_type(none\|api_key\|oauth2)/auth_scheme(bearer\|header\|oauth2\|none)/auth_hint/secret_ref/headers(敏感头 AES-GCM 或 secret_ref)/requires_approval/enabled/health_status(unknown\|healthy\|degraded\|unhealthy)/consecutive_failures/last_checked_at/last_latency_ms/last_error/deleted_at/name_edited |
| `a2a_tasks` | a2a_task_id/remote_agent_id/run_id/conversation_id/tool_call_id/context_id/state(8 态)/progress/request/result/error/input_request/auth_request/timeline/started_at/finished_at；索引 state/agent/run/conversation |

### 2.2 端点（14 个新端点）

- `POST /agents/preview`：拉卡校验不落库 → `{valid, card, normalized, warnings}`；失败 422 `agent_card_invalid` / `agent_url_not_allowed`
- `POST /agents`：幂等（同 url+slug 返回已有；slug 冲突 409 `agent_slug_conflict`）；`GET /agents`（health_status/enabled/q/limit/cursor + stats）；`GET /agents/{id}`（card/skills/auth_hint/last_*/recent_tasks×3）；`PATCH`（enabled/name/auth{type,secret_ref}/headers）；`DELETE`（软删，有活动任务 409 `agent_has_active_tasks`）；`POST /{id}/refresh`（`{agent,changed,changed_fields}`，失败保留旧卡标 unhealthy）；`POST /{id}/health`（拉卡 + tasks/get 探测；<2s healthy、≥2s degraded、连败≥3 unhealthy）
- `GET /tasks`：state（可重复）/agent_id/run_id/conversation_id/q/from/to/limit/cursor + 九键 stats；`GET /tasks/{id}` 详情含 timeline/artifact；`POST /{id}/cancel|resume|authorize|reject`，错误码 `task_not_cancelable/resumable/authorizable/rejectable`（409）；等待态 expires_at 到期懒过期置 canceled + timeline「等待超时」+ 审计 `task.expired`

### 2.3 A2A 客户端与工具

- `app/modules/a2a/client.py`：自研 JSON-RPC 2.0 over httpx（SDK 1.x 为 protobuf 抽象，接入成本高，沿用 M1.5 MCP 先例）；支持 `message/send`（非流式，非终态自动轮询 `tasks/get`）、`message/stream`（SSE）、`tasks/get`、`tasks/cancel`；超时/连接/鉴权/非法响应映射 `a2a_timeout`/`a2a_unavailable`/`a2a_auth_failed`/`a2a_invalid_response`/`a2a_remote_error`；禁跨源重定向、响应体上限 1MB、`trust_env=False`（规避本机 NO_PROXY 含 `[::1]` 导致 httpx 解析崩溃）
- `app/modules/a2a/card.py`：well-known 双路径拉取（agent-card.json 兼容 agent.json）、SSRF/同源/协议版本（1.x、0.3.x）/技能数量≤50/描述≤2000 校验；10s 超时
- **工具策略（按任务决策）**：仅注入统一工具 `send_message(agent, task, context?)`，agent 为注册 slug 枚举，描述列出各 Agent/技能；未生成任何 per-agent 包装工具；无可选 Agent 时不注入
- `app/modules/a2a/tool.py`：a2a_tasks 落库（submitted→working→终态）、SSE 事件、input_required/auth_required 挂起复用 M1.5 进程内 waiter（`RunContext.a2a_task_*`），resume/authorize 唤醒后以同一 a2a_task_id/context_id 继续，SSE 流保持；远程返回按不可信数据截断处理
- Agent 声明 `requiresApproval`（Card skill metadata）时 send_message 走现有 HITL 审批链路

### 2.4 聊天/系统集成

- SSE 新事件：`a2a.task`（task_id/a2a_task_id/agent/agent_detail/state/progress/request_summary/result_summary/duration_ms/error）、`a2a.artifact`、`a2a.input_required`、`a2a.auth_required`（字段与契约 4.4 逐字对齐）
- `system/info.capabilities.agents=true, tasks=true`（与入站 A2A_ENABLED 无关）；`GET /runs/{id}` pending.kind 支持 input_required/auth_required（带 task_id）、agents[] 含 a2a Agent（kind=a2a/slug/state）
- 顺带修复（lead 反馈）：user Message 也落 capabilities（历史时间线「本轮：技能 ×N · MCP ×N」刷新不丢）

## 3. 与契约的偏差

1. **写操作未做 admin 限制**：v1 全局注册表所有登录用户可读可写（写审计）；契约 7.1 只要求「受角色或权限控制」，收紧留 M5 RBAC；
2. **resume 内部实现为进程内 waiter 唤醒**（同审批），不复用 `POST /runs/{run_id}/resume`（该端点仍 409 run_not_resumable，未放开）；语义与契约一致：同一 a2a_task_id 继续、run 保持 running；
3. **A2A 客户端为自研**而非官方 a2a-sdk（见 §2.3），仅使用 4 个 0.3/1.0 通用方法名；
4. **健康探测**为「拉 Card + tasks/get 探测」：业务错误（task not found）视为可达；
5. `recent_task` 为最新一条任务（不限状态）；`stats` 按过滤条件统计（不含分页）；
6. 出站不跟随重定向（包括同源），Card 拉取允许同源重定向 ≤3 次；
7. 非流式 message/send 返回非终态 Task 时客户端轮询 tasks/get（间隔 0.2s）直至终态或 A2A_CLIENT_TIMEOUT_SECONDS 超时。

## 4. 测试与验证结果（实际跑过）

| 项目 | 命令 | 结果 |
|---|---|---|
| 全量测试 | `TEST_DATABASE_URL=…agent_hub_test uv run pytest -q` | **101 passed**（85 基线 + 6 agents + 9 tasks + 1 回归修复） |
| 静态检查 | `uv run ruff check .` | All checks passed |
| 迁移 | 测试库 `upgrade head → downgrade -1 → upgrade head` | 通过（`8a2f7c31e9d5`） |
| 迁移一致性 | `alembic check` | No new upgrade operations detected |
| 生产库 | agent_hub `upgrade head` | `8a2f7c31e9d5 (head)` |
| 8000 实测 | `scripts/verify_m2_live.py`（真实 DeepSeek + 9100 假 Agent） | 注册→preview→health→对话委派→SSE a2a 事件→任务列表/详情→cancel 409，全部通过 |
| OpenAPI | `scripts/export_openapi.py` | docs/openapi.json 已更新 |

## 5. 前端联调注意事项

- **假 Agent**：`uv run python scripts/mock_a2a_agent.py`（127.0.0.1:9100，`--streaming` 可开流式）；注册 `POST /api/v1/agents {"url":"http://127.0.0.1:9100"}`；dev/test 默认放行回环（`A2A_ALLOW_LOOPBACK`），prod 需 `A2A_ALLOWED_HOSTS=127.0.0.1` 白名单；task 文本含 success/slow/fail/input_required/auth_required/streaming 触发场景；
- **HITL 时序**：`a2a.input_required`/`a2a.auth_required` 事件后 SSE 保持连接（15s 心跳）；在任务页调 `POST /tasks/{id}/resume|authorize|reject`，同一流随后出现 `a2a.task(working→completed)`、`tool.result` 与 `message.done`；
- **expires_at 到期**后前端隐藏动作按钮，后端懒过期置 canceled；
- **事件字段**：`a2a.task.agent` 是 slug 字符串、`agent_detail` 是对象；`available_actions` 只含后端动作；task 状态枚举一律下划线；
- 任务列表/详情字段与 `agent.ts`/`task.ts` 逐字对齐，`result.artifact` 形状 `{type,summary,parts,data}`；
- 8000 实例热重载已生效（代码改动自动加载，新表已迁移）。

## 6. 遗留项 / 风险

- M3 入站 Server 未做（`A2A_ENABLED=false`，system/info.a2a 仍为占位）；
- agents/tasks 写操作无 admin 限制（M5 RBAC 收紧）；auth_required 的 `authorization_url` 未做后端白名单校验（前端按契约自行控制）；
- 进程内 waiter 与 M1.5 审批一致：多实例部署时跨实例动作无法唤醒（单实例前提，M6 Redis 总线）；
- 轮询 tasks/get 无指数退避（固定 0.2s）；未做定时健康巡检（仅有手动/懒触发）；
- M1.5 基线迁移 `7f3a9c21b4d6` 的 downgrade 自身存在 drop 顺序问题（tool_invocations 依赖 runs），本次未修（不影响 upgrade 链；测试库 version 表已人工校正，M2 迁移可逆性已独立验证）。
