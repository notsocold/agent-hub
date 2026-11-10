# A2A 出站客户端迁移报告（a2a-sdk 1.3.0）

- 日期：2026-11-10
- 背景：此前 M2 出站协议层为自研 JSON-RPC 2.0 over httpx；用户要求真正使用 `a2a-sdk`，版本取最新稳定版。
- 版本结论：PyPI 当前最新稳定版为 **1.3.0**（1.0.0 → 1.1.5 → 1.2.3 → 1.3.0），依赖约束保持 `a2a-sdk[fastapi]>=1.3.0,<2.0`。
- 范围：仅出站 Client（本服务不提供 A2A 入站，见[需求变更记录](2026-11-10-requirement-change-outbound-only.md)）。

## 1. 实现方案

| 层 | 迁移前 | 迁移后 |
|---|---|---|
| Agent Card 解析 | 自研 dict 校验（`card.py`） | 出站客户端用 SDK `parse_agent_card`；`card.py` 的拉取/SSRF/同源校验保留，并额外调用 SDK 做 schema 校验 |
| JSON-RPC 传输 | 自研 httpx 手写 JSON-RPC | SDK `JsonRpcTransport`（1.x）与 `CompatJsonRpcTransport`（0.3） |
| 消息/任务类型 | 裸 dict | SDK protobuf 类型（`SendMessageRequest` / `Task` / `StreamResponse` 等） |
| 对外接口 | `A2aClient` / `A2aError` / `A2aUpdate` | 保持不变，业务层（`tool.py`、`tasks`、`agents`）无需感知 SDK |
| 测试注入 | `register_test_transport` + `httpx.ASGITransport` | 保持不变；SDK 传输共用同一 `httpx.AsyncClient` |

关键点：

- **版本协商**：按 Agent Card `supportedInterfaces` 的 `protocolVersion` 选择传输——0.3.x 走 `CompatJsonRpcTransport`，1.x 走 `JsonRpcTransport`；
- **Card 快照复用**：调用方（`tool.py`、`tasks.service`、`agents.health_check`）传入数据库中的 Card 快照，避免每次调用重新拉取；Card 缺失时才用 SDK `A2ACardResolver` 兜底；
- **不污染快照**：`parse_agent_card` 会原地 pop 0.3 字段（`url`/`protocolVersion` 等），客户端先 `deepcopy` 再解析；
- **兼容转换**：SDK protobuf 事件统一转换为 0.3 风格的 `A2aUpdate`（`status_message`/`artifact` 仍是 dict），`tool.py` 的既有解析逻辑零改动；
- **错误映射**：SDK `A2AClientTimeoutError` / `A2AClientError` / `A2AError` 与 httpx 异常统一映射为既有契约错误码（`a2a_timeout`、`a2a_unavailable`、`a2a_auth_failed`、`a2a_remote_error`、`a2a_invalid_response`）；
- **响应体上限**：保留 `A2A_MAX_RESPONSE_BYTES`，按文本字符数近似累计（流式逐事件累计）。

## 2. 改动文件

| 文件 | 改动 |
|---|---|
| `app/modules/a2a/client.py` | 重写为 SDK 传输实现；保留 `A2aClient`/`A2aError`/`A2aUpdate`/`register_test_transport`/`artifact_text` |
| `app/modules/a2a/card.py` | 新增 `validate_card_schema`：用 SDK `parse_agent_card` 做规范校验，保留原安全/业务校验 |
| `app/modules/a2a/tool.py` | `A2aClient(...)` 传入 `card=self.agent.card` |
| `app/modules/tasks/service.py` | 取消任务时传入 `card=agent.card` |
| `app/modules/agents/service.py` | 健康检查用刚拉取的 Card 快照探测，避免二次拉取 |
| `tests/fixtures/fake_a2a_agent.py` | Card 支持 `protocol_version`（默认 0.3.0，如实声明）；补 0.3 必填字段 `artifactId`、`messageId` |
| `tests/integration/test_agents.py` | 协议版本断言改为 0.3.0 |
| `tests/test_a2a_sdk_unit.py` | 新增 4 个单元测试：0.3/1.0 传输选择、1.0 Task 解析、JSON-RPC 错误映射、HTTP 401 映射 |

## 3. 验证

```text
pytest（TEST_DATABASE_URL → agent_hub_test）  108 passed
ruff check app tests                          All checks passed!
```

- 现有 M2 集成测试（注册/预览/健康检查/对话委派/任务查询/取消/HITL/流式）全部通过，说明 0.3 兼容传输在真实业务链路可用；
- 新增测试覆盖 1.0 传输路径（含流式 SSE）与错误映射，避免迁移后「只兼容 0.3」的隐性回归。

## 4. 已知差异与未做项

1. **Card 拉取仍为自研**：注册/刷新/健康检查使用 `card.py` 的 httpx 拉取，以保留 SSRF、同源、重定向、响应体上限等安全校验；SDK resolver 仅作为客户端兜底。
2. **响应体上限按 UTF-8 字节估算**：文本部分已覆盖；二进制 Artifact 未纳入统计。
3. **0.3/1.0 协商依赖 Card 声明**：远端 Card 若声明 1.0 但实际只接受 0.3 方法名，SDK 不会自动降级（这是远端 Card 与实际实现不一致）；本项目假 Agent 已如实声明 0.3.0。
4. **未接入 SDK 的高级能力**：push notifications、gRPC/REST 传输、扩展卡片、签名校验暂未使用（与 M2 范围一致）。
5. **1.0 流式为单元测试**：0.3 流式由 M2 集成测试覆盖，1.0 流式由 `tests/test_a2a_sdk_unit.py` 的 SSE 单元测试覆盖；真实 1.0 服务端的端到端联调留给后续。

## 5. 回滚方式

`client.py` 为单一协议层入口，回滚只需恢复上一提交的该文件与三处 Card 传参；数据库与 API 契约无变化。
