# M7 契约补齐与安全收口后端交付报告

- 日期：2026-11-11
- 范围：M7 契约补齐与安全收口后端（Task 1–7，**不含前端**）
- 基线：M6 收尾 **582 passed, 1 skipped**，迁移 head `2d8f6a1c3e70`，OpenAPI 与代码一致
- 设计：[M7–M9 立项](../superpowers/specs/2026-11-11-m7-m9-backend-roadmap-design.md) §3
- 计划：[M7 实施计划](../superpowers/plans/2026-11-11-m7-contract-security.md)
- 前端交接：[M7 前端交接](2026-11-11-m7-frontend-handoff.md)

**目标**：让前端能「断线逐字续流」（本里程碑只交付后端契约），写操作有真正的权限边界，
审批事件自足，外部授权链接可控，并修正 M5/M6 留下的契约文档偏差；M7 不新增数据库迁移。

## 1. 交付内容

| 任务 | 文件 | 说明 |
|---|---|---|
| Task 1 事件回放补 `event` | `app/modules/chat/schemas.py`、`app/modules/chat/service.py`、`tests/integration/test_chat.py` | `RunEventOut` 新增必填 `event`（原始 SSE 事件名，未知/历史缺失为 `""`）；`list_run_events` 从 `payload._event` 映射；`payload` 仍剔除 `_event`，分类字段 `type` 语义不变 |
| Task 2 SSE 续流端点 | `app/modules/chat/service.py`、`app/modules/chat/router.py`、`tests/integration/test_run_stream.py`（新增） | 新增 `GET /runs/{run_id}/stream?after_seq=`：所有权校验（非本人/不存在统一 404）、回放 `seq > after_seq` 后跟随实时、回放上限 1000（`400 replay_too_large`）、终态关闭、`interrupted` 保持连接等审批恢复、15s `: ping`；旧 `POST /chat/stream` 参数化后默认语义不变 |
| Task 3 写操作 RBAC | `app/core/deps.py`、`app/modules/agents/router.py`、`app/modules/tasks/router.py`、`tests/integration/test_rbac.py`（新增）、`tests/integration/test_agents.py`、`tests/integration/test_tasks.py` | agents 写接口（注册/修改/删除/刷新/健康检查）与 tasks 动作接口（取消/补输入/授权/拒绝）改用 `AdminUser`；403 `forbidden` + `details.required_role="admin"`；拒绝写操作统一审计 `security.rbac_denied`；读接口与 `POST /agents/preview` 保持登录用户可用 |
| Task 4 `approval.required` 字段补齐 | `app/modules/chat/approvals.py`、`tests/integration/test_hitl_interrupt.py` | 事件 `data` 新增 `tool_display_name`、`executor`、`target`、`side_effect`、`payload_preview` 5 个展示字段，值与 `GET /approvals/{id}` 一致；原始 `payload` 仍脱敏，旧字段不变 |
| Task 5 A2A 授权 URL 白名单 | `app/core/urls.py`（新增）、`app/modules/a2a/tool.py`、`app/modules/tasks/service.py`、`app/core/config.py`、`.env.example`、`tests/test_auth_url_unit.py`（新增）、`tests/integration/test_tasks.py`、`tests/fixtures/fake_a2a_agent.py` | 落库前校验 `authorization_url`，违规置 `null`（任务继续）+ warning + 审计 `security.auth_url_rejected`；任务详情展示层复用同一校验过滤历史脏数据（不写审计）；新增 `A2A_AUTH_URL_ALLOWED_HOSTS`（默认空 = 不额外限制） |
| Task 6 契约/OpenAPI/前端交接 | `docs/api.md`、`docs/openapi.json`、`docs/feedback/2026-11-11-m7-frontend-handoff.md`（新增）、`docs/architecture.md`、`docs/database.md`、`README.md`、`docs/roadmap.md`、`app/modules/chat/router.py` | `event`/续流端点/403 权限/审批字段/授权 URL 契约；OpenAPI 重新导出并补 SSE 描述与 4xx/5xx；前端 handoff（续流示例、兼容性、自测清单）；文档状态同步 |
| Task 7 全量回归与交付报告 | `docs/feedback/2026-11-11-m7-backend-report.md`（本文件）、`README.md`、`docs/roadmap.md` | 全量回归、证据归档、里程碑状态收口；无业务代码/测试改动 |

数据库：M7 **无迁移**，迁移 head 保持 `2d8f6a1c3e70`。

## 2. 实测证据

### 2.1 全量测试与静态检查

2026-11-11 本机执行，复用 `dev_postgres`（localhost:54321），测试库为 `agent_hub_test`
（由 `.env` 的开发库 `agent_hub` 派生，**不触碰开发库**）：

```text
$ TEST_DATABASE_URL="$(grep '^DATABASE_URL=' .env | cut -d= -f2- | sed 's#/agent_hub$#/agent_hub_test#')" \
    .venv/bin/python -m pytest -q
733 passed, 1 skipped in 85.03s (0:01:25)

$ .venv/bin/python -m ruff check .
All checks passed!

$ .venv/bin/python -m alembic check
No new upgrade operations detected.

$ .venv/bin/python -m alembic current
2d8f6a1c3e70 (head)

$ .venv/bin/python scripts/export_openapi.py && git diff --exit-code docs/openapi.json
已导出 /home/zhaodan/work/PythonProjects/agent-hub/docs/openapi.json
（git diff 退出码 0：无差异）
```

- 基线 M6 **582 项** → M7 **733 项**（+151，含 Task 1–6 新增用例与审查修复回归）；
  唯一 skip 仍是知识库语义评测组（`tests/integration/test_knowledge_eval.py`，需真实
  Embedding key，与 M4/M5/M6 一致）；
- ruff 全绿；`alembic check` 无漂移、head 不变（无迁移）；OpenAPI 重新导出后无 diff
  （契约与代码一致，CI contract 口径可复查）；
- 测试库 `agent_hub_test` 与开发库 `agent_hub` 分离，未向开发库写入测试数据。

### 2.2 关键用例

| 用例文件 | 覆盖 |
|---|---|
| `tests/integration/test_chat.py` | `GET /runs/{id}/events` 每项 `event` 与 SSE 事件名一致；`payload` 不再含 `_event`；`type` 分类不变；未知/历史缺失事件为 `""`；旧字段零变化 |
| `tests/integration/test_run_stream.py`（14 项） | 回放 + 实时跟随；`after_seq` 跳过前 N 条；终态 run 回放完立即关闭（空回放也不空挂）；未登录 401、非法令牌 401、非本人/不存在统一 404（含回放超限前先 404）；`after_seq` 负数/超 int32 422；回放 1000 边界与超限 400 `replay_too_large`；生成器内截断兜底；旧聊天流默认不截断、终态不早关；`interrupted` 回放后保持连接并在审批恢复后转发实时事件；15s 心跳（缩短周期断言 `: ping`）；`X-Run-ID`/`X-Request-ID` 响应头 |
| `tests/integration/test_rbac.py`（17 项参数化用例） | 普通用户调用 agents 写接口与 tasks 动作接口全部 403 `forbidden` + `details.required_role="admin"`；拒绝写操作落审计 `security.rbac_denied`（actor/method/path/required_role）；管理员 agents 写路径成功；管理员 tasks 动作通过权限门禁（落到业务 404，证明未被 403 拦截）；`GET /agents*`、`GET /tasks*`、`POST /agents/preview` 普通用户仍 200；未登录 401 |
| `tests/integration/test_hitl_interrupt.py` | `approval.required` 的 `data` 含 5 个新字段，且与 `GET /approvals/{id}` 一致；原始参数仍脱敏 |
| `tests/test_auth_url_unit.py`（10 个表驱动函数，共 112 项） | 合法 https / dev-test 本地 http；拒绝 `javascript:`/`data:`/`file:`/`ftp:`/无 scheme/超长；userinfo、反斜杠、百分号编码、数字写法 IP、IPv4-mapped IPv6、私网/回环/链路本地/保留地址、`localhost`/`*.local`/`*.internal`/单标签主机；非 ASCII/IDN 主机；`allowed_hosts` 只收窄不放宽；审计前缀脱敏（凭据、控制字符、截断） |
| `tests/integration/test_tasks.py` | A2A `auth_required` 落库时恶意 `authorization_url` 置 `null` 且任务继续；审计 `security.auth_url_rejected` 不泄漏完整 URL/凭据；历史脏数据在任务详情展示时被过滤为 `null`，且不重复写审计 |
| `tests/integration/test_agents.py` | agents 写接口依赖切换后的管理员成功路径与普通用户 403 回归 |

### 2.3 契约文档

- `docs/openapi.json` 与代码一致（§2.1 第 4 条）；`/runs/{id}/stream` 的 SSE 描述、
  400/401/403/404/422/500 响应与 `X-Run-ID`/`X-Request-ID` 响应头已导出；
- `docs/api.md`：§7.7 `event`、§7.8 续流端点与生命周期、§10.2/§10.4、
  §2.9/§2.12 权限、§8.2 审批超时字段已按实际实现同步；
- 前端 handoff `docs/feedback/2026-11-11-m7-frontend-handoff.md` 覆盖新字段、新端点、
  403 行为、`authorization_url=null`、兼容性清单与联调自测清单。

## 3. 关键决策与裁定

| # | 决策 | 说明 |
|---|---|---|
| 1 | `event` 用新增字段，不改 `payload` | 旧前端零破坏；`payload._event` 继续作为内部字段并从返回值中剔除；未知/历史缺失回退 `""` |
| 2 | 续流用 query `after_seq`，不用 `Last-Event-ID` | 与既有事件分页接口一致，前端已有 `lastSeq` 重连逻辑；`after_seq` 默认 0，范围 `0..2147483647` |
| 3 | 回放上限 1000 条 | `SSE_MAX_REPLAY = 1000`；建流前用 count 检查，超限返回 400 `replay_too_large`（提示改用分页接口或更近的 `after_seq`）；生成器内 `limit(max_replay+1)` 兜底 |
| 4 | 终态/`interrupted` 语义 | 回放含终态事件：回放完关闭；run 已终态但回放无终态事件：回放完直接关闭（避免空挂，前端 EOF 后查 `GET /runs/{id}`）；`interrupted` 不在终态集合，保持连接等审批恢复（受 `HITL_STREAM_TIMEOUT_SECONDS` 限制，15s `: ping`） |
| 5 | 旧 `POST /chat/stream` 语义不变 | `stream_run_events` / `_stream_run_events_inner` 新增参数默认 `max_replay=None`、`close_terminal_when_replayed=False`；旧路径不截断、终态后不提前关闭，新端点显式开启 |
| 6 | RBAC 复用既有 `AdminUser` 依赖 | 不新建权限框架；403 信封与全站一致，补 `details.required_role="admin"`；拒绝审计集中在 `app/core/deps.py` 的 `security.rbac_denied`（`actor`/`method`/`path`/`required_role`，沿用请求上下文）；成功写操作沿用原有审计；只收紧写接口，读与预览不变 |
| 7 | `approval.required` 只增字段 | 5 个字段源自已加载的 `Approval` 行，不新增查询；`payload_preview` 500 字符截断、`target` 2000 字符截断，控制事件体积；原始 `payload` 仍脱敏 |
| 8 | `authorization_url` 违规置空而非任务失败 | 远程 Agent 返回的数据不应让整个 A2A 任务失败；落库前校验 + 审计 `security.auth_url_rejected`（`detail` 只存脱敏截断值）；展示层对历史数据只过滤、不写审计 |
| 9 | URL 白名单规则 | 仅 `https` 公网主机；`dev`/`test` 额外允许 `http://localhost`、`http://127.0.0.1`（精确字面量）；拒绝非 http(s) scheme、userinfo、反斜杠、百分号编码混淆、浏览器等价数字 IP、回环/私网/链路本地/保留/组播/未指定 IP、`localhost`/`*.local`/`*.internal`/单标签主机、非 ASCII/IDN、超长（>2000）；`A2A_AUTH_URL_ALLOWED_HOSTS` 非空时只收窄不放宽（命中白名单也不能放行私网/回环）；校验纯离线（不做 DNS 解析） |
| 10 | M7 不新增迁移 | `event` 是派生字段，RBAC 用现有 `users.role`，5 个审批字段与白名单均为已有列/配置；`alembic check` 无漂移 |

## 4. 前端交接索引

| 主题 | 交接位置 |
|---|---|
| 变更速览 / 兼容性开关 | [handoff §1](2026-11-11-m7-frontend-handoff.md#1-变更速览) |
| SSE 续流端点、鉴权、`fetch-event-source`/原生 fetch 示例、生命周期、错误处理 | [handoff §2](2026-11-11-m7-frontend-handoff.md#2-运行事件续流新端点) |
| `/runs/{id}/events` 新增 `event` 字段 | [handoff §3](2026-11-11-m7-frontend-handoff.md#3-事件回放新增-event-字段) |
| `approval.required` 5 个展示字段 | [handoff §4](2026-11-11-m7-frontend-handoff.md#4-approvalrequired-新增字段) |
| agents/tasks 写接口 403 与按角色隐藏入口 | [handoff §5](2026-11-11-m7-frontend-handoff.md#5-agents--tasks-写接口改为管理员) |
| `authorization_url` 可为 `null` | [handoff §6](2026-11-11-m7-frontend-handoff.md#6-a2a-authorization_url-可能为-null) |
| 兼容性清单 / 联调自测清单 | [handoff §7](2026-11-11-m7-frontend-handoff.md#7-兼容性清单)、[§8](2026-11-11-m7-frontend-handoff.md#8-联调自测清单) |

## 5. 规格 §3.4 验收清单

| 验收项 | 证据状态 |
|---|---|
| `GET /runs/{id}/events` 每条事件带 `event`，旧字段不变 | **已验证**：`tests/integration/test_chat.py`；OpenAPI 导出无 diff |
| `GET /runs/{id}/stream?after_seq=`：401、非本人 404、回放+实时、终态关闭、心跳、超限 400 | **已验证**：`tests/integration/test_run_stream.py`（14 项，含 1000 边界、`interrupted` 保活、`X-Run-ID`/`X-Request-ID`）；422 参数校验一并覆盖 |
| 普通用户调用 agents/tasks 写接口 403 `forbidden`，管理员成功且写审计 | **已验证**：`tests/integration/test_rbac.py`、`tests/integration/test_agents.py`；成功写操作沿用既有审计 |
| `approval.required` 含 5 个新字段，前端可不再额外拉审批详情 | **已验证**：`tests/integration/test_hitl_interrupt.py` |
| `authorization_url` 恶意协议/内网地址被拒并留审计 | **已验证**：`tests/test_auth_url_unit.py`、`tests/integration/test_tasks.py`（含历史脏数据过滤） |
| `docs/openapi.json` 与代码一致；全量测试 ≥ 582 passed；ruff 全绿 | **已验证**：本报告 §2.1（733 passed, 1 skipped；ruff 全绿；OpenAPI 无 diff；`alembic check` 无漂移） |
| 前端可按 handoff 接续流与 403 文案 | **后端契约已交付，前端待接入**：交接文档见 §4；本里程碑不含前端实现，**「断线逐字续流」未做前端验收**，不能宣称已完成 |

## 6. 已知缺口与遗留 minor

**范围外（明确不做）**

1. **跨实例续流**仍不在 M7：事件总线为进程内实现，多实例/重部署时实时帧可能丢失；
   重连只能回放**已落库**事件。扩容前需单独设计 Redis 事件总线（M6 报告已列为后续项）。
2. **`message.delta`/`reasoning.delta` 不落库、不回放**：断线窗口内的增量文本无法补齐；
   前端必须在 run 进入终态后拉 `GET /conversations/{id}/messages` 终稿覆盖本地累积文本；
   `interrupted` 期间消息 `status=streaming`，历史接口不返回，此阶段不要拉终稿。
3. **前端「逐字续流」未验收**：M7 只交付后端契约与 handoff，前端接入/联调/自测清单仍待执行。

**遗留 minor（已知、未在 M7 修复）**

4. **历史 `run_events.payload` 中的旧脏 `authorization_url` 未回溯清洗**：M7 只在任务详情
   展示层过滤（新事件在落库前已校验）；历史事件回放（含续流回放）仍可能带出旧值，
   如需彻底清除需一次性数据清洗（不在 M7 范围）。
5. **`A2A_AUTH_URL_ALLOWED_HOSTS` 只收窄、不能放宽**：无法用它放行私网/回环主机；
   若远程 Agent 的合法授权回调部署在私网，需要调整部署或后续扩展策略（当前仅
   `dev`/`test` 的 `localhost`/`127.0.0.1` 例外）。
6. **管理员 `POST /tasks/{id}/cancel` 正向 HTTP 成功用例未覆盖**：`test_rbac.py` 对
   cancel 只验证了普通用户 403、未登录 401 与管理员通过权限门禁（业务 404），
   未构造真实可取消任务断言 200。
7. **审批创建既有风险（M5 遗留，M7 未改）**：`_compact` 在 `limit=500` 时可能返回
   501 字符（500 + `…`），而 `approvals.payload_preview` 列为 `String(500)`，极端超长
   参数可能导致审批创建失败；`_sanitize_args` 对 `>4000` 字符的参数先截断再
   `json.loads`，截断可能产生非法 JSON 抛 `JSONDecodeError`。
8. **IDN/非 ASCII 授权 URL 一律拒绝（有意为之）**：合法但未转 punycode 的国际化域名
   链接会显示为 `null`；如需支持，应先转 punycode 并配置白名单。
9. **DNS 别名指向回环（如 `nip.io`）离线无法识别**：校验只做字面主机与 IP 判断、
   不做 DNS 解析（测试零网络约束），这类域名在 `https` 下可能通过；不能替代出站网络层防护。
10. **回放上限 1000 为经验值**：未对超长 run（>1000 条已落库事件）做压测调优，
    前端需按 handoff 用分页接口兜底。
11. **里程碑级独立审查另行进行**：Task 1–6 已完成任务级审查与修复；Task 7 的独立审查
    由独立 reviewer 并行执行，其发现项由后续修复轮处理，本报告不包含该审查结论。

## 7. 提交记录

```text
9cf8f19 feat: 运行事件回放新增 event 字段（M7 Task 1）
620227a feat: 新增运行事件 SSE 续流端点（M7 Task 2）
50b2336 fix: SSE 续流参数化以保持旧聊天流语义（M7 Task 2 审查修复）
0fa139e feat: agents/tasks 写操作收紧为管理员（M7 Task 3）
6ba7cff feat: approval.required 事件补齐审批卡字段（M7 Task 4）
79ff26a feat: A2A authorization_url 后端白名单（M7 Task 5）
e122037 fix: 加固 authorization_url 审计脱敏与浏览器等价主机绕过（M7 Task 5 审查修复）
bb4501d docs: M7 契约文档、OpenAPI 与前端 handoff 同步（M7 Task 6）
0735d1d fix: 修正 M7 续流回放边界与 OpenAPI 403 口径（M7 Task 6 审查修复）
2c64e27 fix: 修正 interrupted 期间消息终稿契约与 handoff 重复行（M7 Task 6 复审修复）
（本报告提交：docs: M7 交付报告与里程碑状态收口（M7 Task 7））
```
