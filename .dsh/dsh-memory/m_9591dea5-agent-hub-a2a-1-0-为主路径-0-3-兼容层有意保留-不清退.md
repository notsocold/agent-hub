---
id: "m_9591dea5"
title: "agent-hub A2A：1.0 为主路径，0.3 兼容层有意保留，不清退"
summary: "项目里残留的 0.3 相关内容属于显式、受测试覆盖的兼容能力（官方 a2a.compat.v0_3），目标是新功能默认走 1.0、0.3 仅做兼容，因此不做 1.0-only 清理。"
kind: "decision"
importance: 4
tags: ["a2a","protocol-version","compatibility","a2a-sdk"]
pinned: false
status: "active"
created: "2026-11-10T03:49:42.506Z"
modified: "2026-11-10T03:49:42.506Z"
revision: 1
origin: "auto"
sessionId: "session-e3aa0116-6e08-44e5-976d-c1fe6c910b8a"
sessionTitle: "M3 开发"
model: "deepseek-official/deepseek-flash"
---

结论：agent-hub 中所有 A2A 0.3 相关内容都是有意保留的兼容能力，不构成版本偏离残留；当前主路径为 a2a-sdk 1.3.0 的 1.0 协议。

保留理由：A2A 0.3 仍是大量远程 Agent 的现行版本，删掉会直接失去对它们的注册与调用能力；0.3 支持本身是 SDK 官方能力（a2a.compat.v0_3）加一层分支。

0.3 出现在这些位置（均属兼容/测试资产）：
- app/modules/a2a/client.py：CompatJsonRpcTransport + 按 Card 的 protocolVersion 协商（0.3 走 Compat 传输，1.x 走 JsonRpcTransport）；_message_dict 输出 0.3 风格 dict 仅为内部 DTO 便利，非协议依赖。
- app/modules/a2a/card.py：接受 0.3.x Card，并用 parse_agent_card 做 schema 校验。
- remote_agents.protocol_version 数据库字段：记录每个远程 Agent 实际声明的版本。
- tests/fixtures/fake_a2a_agent.py 默认 protocol_version="0.3.0"，模拟真实 0.3 Agent；test_a2a_sdk_unit.py 验证 0.3→Compat、1.0→JsonRpcTransport 的分支。
- docs/a2a.md 与 docs/development.md 明确记录该双传输策略。

若将来目标环境确认全为 1.0、需改 1.0-only，改动清单：client.py 删除 Compat 分支与导入；card.py 的 _SUPPORTED_PROTOCOL_PREFIXES 只留 "1."；fake_a2a_agent.py 改为 1.0 方法名（SendMessage/SendStreamingMessage/GetTask/CancelTask）与 1.0 响应结构；测试删 0.3 传输断言、补 1.0 端到端；文档同步。代价是 0.3-only 远程 Agent 无法使用，收益是少一个分支与一个测试方向。
