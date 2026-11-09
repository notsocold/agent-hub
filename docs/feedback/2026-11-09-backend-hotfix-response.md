# 后端对前端联调反馈的处理结果（M1 热修）

- 日期：2026-11-09
- 对照：`docs/feedback/2026-11-09-frontend-integration-response.md`（下称「前端反馈」）
- 后端状态：M1；本次只做缺陷修复、契约补齐与文档同步，不扩大功能范围
- 结论：**前端反馈第 5 节的 P0/P1/P2 已全部处理**；cursor 分页缺陷已修复并在真实 HTTP 上复验；OpenAPI 与 `docs/api.md` 已补齐；后端测试 27 个全部通过

---

## 1. 一句话结论

前端联调发现的唯一阻断项（`has_more=true` 时列表/历史/审批 500）已修复：根因是 cursor 编码把 ORM 的 `UUID` 对象直接交给 `json.dumps`；现在编码层统一做 JSON-safe 归一化，并新增会话、消息、审批三条分页回归测试。其余 OpenAPI/文档/可观测性/限流问题同步收口，前端可以按原计划复验。

---

## 2. P0：`has_more=true` 时 500（已修复）

### 2.1 根因确认

与前端反馈一致：

- `app/core/cursor.py::encode_cursor` 直接 `json.dumps(payload)`；
- `list_conversations`、`list_messages`、`list_approvals` 把 `last.id`（`UUID`）放进 payload；
- `json.dumps` 抛 `TypeError: Object of type UUID is not JSON serializable`，被全局兜底成 500；
- 只有 `has_more=true` 才会走到编码分支，所以 `limit >= total` 时正常。

### 2.2 修复方式

在编码层统一归一化，而不是只改三处调用点（避免后续新增分页接口再踩）：

- `app/core/cursor.py` 新增 `_json_safe`：
  - `UUID` → 标准字符串；
  - `datetime` / `date` → ISO 8601；
  - 其他未知类型仍抛 `TypeError`（不静默吞掉）；
- 解码侧不变，业务层继续用 `uuid.UUID(str(...))` / `datetime.fromisoformat(...)` 还原。

### 2.3 顺带修正的契约偏差

`GET /conversations/{id}/messages` 原来把 `before` 与 `cursor` 同时叠加为 `AND` 条件，与「同时传时以 `cursor` 为准」的契约不一致。现改为：

```python
if cursor:   # cursor 优先
    apply_cursor()
elif before:
    apply_before()
```

### 2.4 新增回归测试

`tests/integration/test_pagination.py`：

| 用例 | 覆盖 |
|---|---|
| `test_conversation_pagination_has_more` | 3 个会话、`limit=2` → `has_more=true`、`next_cursor` 非空；第二页 1 条、无重复、严格更旧；`stats` 为全量 |
| `test_message_pagination_has_more` | 一轮对话、`limit=1` → 翻两页；第二页严格更早；`before` + `cursor` 同时传时 cursor 优先 |
| `test_approval_pagination_has_more` | 直接落库 3 条审批、`limit=2` → 翻两页 |
| `test_invalid_cursor_returns_validation_error` | 非法 cursor 返回 422 `validation_error`，不是 500 |
| `test_internal_error_returns_real_request_id` | 500 响应体与响应头 `X-Request-ID` 一致 |

`tests/test_cursor_unit.py` 另有 5 条单元测试，覆盖 `UUID`/`datetime`/`int`、非法输入与未知类型。

### 2.5 真实 HTTP 复验

对本地实例执行（不是纯 ASGI 测试）：

```text
GET /api/v1/conversations?limit=2
→ 200 {"has_more":true,"next_cursor":"eyJ0IjpudWxsLCJpZCI6...","stats":{"total":3,...}}

GET /api/v1/conversations?limit=2&cursor=...
→ 200 {"has_more":false,"next_cursor":null,"items":[...1 条...]}

GET /api/v1/conversations/{id}/messages?limit=1
→ 200 {"has_more":true,"next_cursor":"..."}
GET .../messages?limit=1&cursor=...
→ 200 {"has_more":false,"items":[...1 条更旧...]}

GET /api/v1/approvals?status=all&limit=2
→ 200 {"has_more":true,"next_cursor":"...","stats":{"pending":3,...}}
GET /api/v1/approvals?status=all&limit=2&cursor=...
→ 200 {"has_more":false,"items":[...1 条...]}
```

> 运行中的 `127.0.0.1:8000` 实例已确认加载该修复（分页返回 200，且其 `/openapi.json` 与导出的 `docs/openapi.json` 完全一致）；若换成未开 reload 的实例，需要重启后端。

---

## 3. P1：OpenAPI 未覆盖 M1 真实行为（已补齐）

`docs/openapi.json` 已重新导出（`scripts/export_openapi.py`，脚本现在也支持直接运行）。

| 前端反馈 | 处理 | 契约位置 |
|---|---|---|
| `chat/stream` 只声明 200/422 | 200 改为 `text/event-stream`，描述事件顺序、帧格式、幂等、断开恢复；补 `example` | `POST /api/v1/chat/stream` |
| 未声明 403/409/422 错误 | 明确 `capability_disabled`、`run_in_progress`（`details.run_id`）、`workflow_disabled`、`validation_error`、`model_not_allowed` | 同上 |
| 未声明 `X-Run-ID` / `X-Request-ID` | 200 响应增加两个 header 定义 | 同上 |
| `error.details` 未建模 | 新增 `ErrorBodyOut` / `ErrorEnvelopeOut`，`details` 为可选对象，描述已知字段 | `components.schemas` |
| 错误响应只覆盖少数状态码 | 所有业务端点统一声明 400/401/403/404/409/413/422/429/500 | 各 path |
| `RunEventOut.tokens/error` 非必填 | 改为 **required + nullable**，并补字段说明；`payload` 说明内部 `_event` 已剔除 | `RunEventOut` |
| `ApprovalDecisionResult.resume_error` 非必填 | 改为 **required + nullable**，说明 `resumed=true` 时为 `null`，不应视为失败 | `ApprovalDecisionResult` |
| `SystemCapabilitiesOut.agents/tasks` 可选 | 保留可选（向后兼容），但补描述：键始终返回、M1 恒为 `false` | `SystemCapabilitiesOut` |

前端可以重新生成类型；运行时值不变，只有 schema 更准确。

---

## 4. P1：文档与实时环境不一致（已修正）

前端反馈 5.3 的逐项处理：

| 项 | 说明 | 处理 |
|---|---|---|
| 默认模型 | 前端反馈文档写 `deepseek-chat`，实时实例是 `deepseek-flash` | 以 `/system/info.models` / `default_model` 为唯一事实来源；`.env.example` 的示例仍是 `deepseek-chat`，实际值由 `LLM_MODEL` 决定。已在前端反馈的后续沟通与 `docs/api.md` 强调 |
| 推理等级 | 反馈文档写「默认空」，实时返回 3 个等级 | 原因是该实例 `LLM_REASONING_EFFORT_SUPPORTED=true`。`.env.example` 已注明该开关语义与「改完需重启」；`docs/api.md` §7.4 同步说明 |
| `message.done.conversation_id` | 实际返回但未写入契约 | 写入 `docs/api.md` §7.9，列出 `message.done` 的完整字段 |
| `resume` 错误码 | 反馈只写「M1 返回 409」 | 明确为 `409 run_not_resumable`，写入 §7.8 |
| 运行事件细节 | `id=<run_id>:<seq>`、`payload` 去 `_event`、`seq` 跳号 | 写入 `docs/api.md` §7.7（字段表 + 过滤/分页说明） |

补充：`docs/api.md` 新增 §7.6 分页契约（排序、`has_more`、`stats`、`before`/`cursor` 优先级、UTC 时间），§7.12 热修记录。

---

## 5. P2：500 的 `request_id` 为 `"unknown"`（已修复）

### 5.1 根因

未处理异常会穿过 `RequestContextMiddleware`，其 `finally` 已复位 `contextvar`，随后才由最外层 `ServerErrorMiddleware` 处理，因此错误信封和日志都拿不到请求 ID。

### 5.2 修复

- 中间件同时把 ID 写入 `request.state.request_id`；
- 错误处理器统一从 `request.state`（缺失时回退 `contextvar`）读取；
- 500 响应显式回填 `X-Request-ID` 响应头，并用同一 ID 写日志；
- 极端情况下（中间件尚未执行）生成新 ID，保证不再是 `"unknown"`。

### 5.3 真实 HTTP 复验

```text
GET /api/v1/system/info（注入未处理异常）
→ 500
X-Request-ID: trace-500-check
{"error":{"code":"internal_error","message":"服务器内部错误，请稍后重试","request_id":"trace-500-check"}}
```

客户端自带 `X-Request-ID` 时会原样回显，便于前后端日志串联。

---

## 6. P2：注册限流对本地联调不友好（已处理）

### 6.1 限流配置化

新增环境变量（默认值保持生产建议值）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `RATE_LIMIT_ENABLED` | `true` | 已有的总开关 |
| `RATE_LIMIT_REGISTER_MAX` | `5` | 注册次数上限 |
| `RATE_LIMIT_REGISTER_WINDOW_SECONDS` | `3600` | 注册窗口 |
| `RATE_LIMIT_LOGIN_MAX` | `10` | 登录次数上限 |
| `RATE_LIMIT_LOGIN_WINDOW_SECONDS` | `300` | 登录窗口 |

`.env.example` 已注明：前端反复注册时可临时放宽，或直接用固定账号。

### 6.2 开发账号 seed 脚本

新增 `scripts/seed_dev_account.py`，幂等创建/重置开发账号：

```bash
uv run python scripts/seed_dev_account.py
# 默认：dev@example.com / dev-password-123
uv run python scripts/seed_dev_account.py --email qa@example.com --password qa-pass-123 --admin
```

- 账号不存在 → 创建；
- 已存在 → 重置密码、确保 `status=active`（`--no-reset` 可跳过）；
- `--admin` / `--no-admin` 控制角色；
- 不经过 `/auth/register`，因此不受注册限流影响。

前端复验建议直接用该账号登录，避免每次跑自动化都注册新用户。

---

## 7. 验证记录

| 项 | 结果 |
|---|---|
| 后端测试 | `27 passed`（原有 17 条 + 新增 10 条：5 条集成、5 条单元） |
| Lint / 格式 | `ruff check` 通过；`ruff format --check` 69 文件已格式化 |
| OpenAPI 导出 | `docs/openapi.json` 已更新；`chat/stream` 200 仅 `text/event-stream`，错误响应完整 |
| 真实 HTTP 分页 | 会话/消息/审批三处 `has_more=true` 均 200，`next_cursor` 可翻页 |
| 真实 HTTP 500 | 响应体 `request_id` 与响应头 `X-Request-ID` 一致（`trace-500-check`） |
| 8000 实例 | 已加载修复；复验后清理了测试会话 |
| 数据库 | 使用本机 PostgreSQL `agent_hub_test` 做完整复验，未启容器；开发库 `agent_hub` 仅创建了一个 `dev@example.com` 账号 |

复验命令（后端）：

```bash
cd /home/zhaodan/work/PythonProjects/agent-hub
TEST_DATABASE_URL='postgresql+psycopg://devuser:devpass@localhost:54321/agent_hub_test' \
  .venv/bin/python -m pytest -q
.venv/bin/python scripts/export_openapi.py
```

---

## 8. 前端复验清单（对应前端反馈第 10 节）

| # | 复验项 | 状态 | 说明 |
|---|---|---|---|
| 1 | `GET /conversations?limit=2` 返回 200、`has_more=true`、`next_cursor` 可翻页 | ✅ 已验证 | 8000 实例与 8010 测试实例均通过 |
| 2 | `GET /conversations/{id}/messages?limit=1` 同上，第二页严格更旧 | ✅ 已验证 | 第二页 `created_at` 严格更早 |
| 3 | `GET /approvals?limit=1`（>1 条审批时）不再 500 | ✅ 已验证 | 测试库落库 3 条审批后翻页通过 |
| 4 | `POST /chat/stream` 的 403/409 在 OpenAPI 可见 | ✅ 已补 | 另有 422/500/429 等完整错误响应 |
| 5 | 500 响应 `request_id` 与 `X-Request-ID` 一致 | ✅ 已验证 | 真实 HTTP 注入异常验证 |
| 6 | 默认模型/推理等级与 `.env.example`、反馈文档一致 | ✅ 已说明 | 以 `/system/info` 为准；`.env.example` 注明开关语义 |

> 审批分页的复验需要至少 2 条审批数据。M1 没有会触发 `interrupt()` 的工具，正常联调不会产生；可用测试夹具直接落库（见 `tests/integration/test_pagination.py`），或等 M2 接入真实审批事件。

---

## 9. 兼容性与注意事项

1. **运行时值不变**：本次只改 schema 描述与必填标记，枚举、字段名、错误码均未变化；前端 TS 类型无需回滚。
2. **`RunEventOut.tokens/error` 变为 required**：实际一直返回这两个键，前端原类型是可选，仍兼容。
3. **`ApprovalDecisionResult.resume_error` 变为 required**：实际一直返回该键（`resumed=true` 时 `null`），前端已按可选消费，仍兼容。
4. **cursor 不可解析**：修复后 `next_cursor` 内容仍是 urlsafe base64，前端继续原样回传即可。
5. **运行实例需要最新代码**：`127.0.0.1:8000` 已确认生效；其他部署需重启后端。
6. **未处理项**：多实例 SSE（M6）、`message.delta` 部分文本恢复（M6）、真实审批事件（M2）、附件受控下载（M1.5）不在本次范围。

---

## 10. 后续

- 前端按第 8 节逐项复验，如有新差异继续记入 `agent-hub-web/docs/handoff/contract-delta.md`；
- 后端 M1.5 优先补：附件上传/受控下载、技能/MCP 上下文的最小切片；
- M2 接入 interrupt 后，统一确认 `approval.required` 事件、HITL SSE 生命周期与 D8「新消息自动放弃旧 interrupted run」策略。
