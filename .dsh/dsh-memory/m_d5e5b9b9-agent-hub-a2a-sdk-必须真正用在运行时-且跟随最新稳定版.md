---
id: "m_d5e5b9b9"
title: "agent-hub A2A 出站已迁移到 a2a-sdk 1.3.0 并消除版本偏离"
summary: "2026-11-10 出站协议层改用 a2a-sdk 1.3.0，并完成一轮全项目优化彻底清除文档/实现中的版本偏离残留。"
kind: "decision"
importance: 4
tags: ["a2a","a2a-sdk","版本约束","文档一致性"]
pinned: false
status: "active"
created: "2026-11-10T03:19:19.847Z"
modified: "2026-11-10T03:40:29.537Z"
revision: 5
origin: "auto"
sessionId: "session-e3aa0116-6e08-44e5-976d-c1fe6c910b8a"
sessionTitle: "M3 开发"
model: "deepseek-official/deepseek-flash"
---

2026-11-10 完成：出站协议层改用 a2a-sdk 1.3.0（0.3 CompatJsonRpcTransport / 1.0 JsonRpcTransport）。随后又做了一轮全项目优化，彻底消除 SDK 版本偏离的残留：

- 依赖与文档口径统一为 `>=1.3.0,<2.0（当前锁定 1.3.0）`，涉及 README、docs/a2a.md、docs/api.md、docs/roadmap.md、设计基线文档；设计基线原先写的 `1.3.x` 已改正。
- docs/development.md 进度从「M1」更新为 M1/M1.5/M2 + SDK 迁移，联调说明改为「仅出站联调」，契约测试改为假 Agent + MockTransport（不再提 a2a-inspector 作本服务入站）。
- 历史文档（M2 报告、文档一致性审查）加注记指向迁移报告，不再声称「A2A 客户端为自研」。
- 补齐 1.0 流式 SSE 单元测试（statusUpdate / artifactUpdate / completed 事件转换），关闭迁移报告中的缺口。

验证：108 测试通过、ruff check 通过、uv lock --check 通过。仍未覆盖：1.0 流式只有 MockTransport 单测、无真实 1.0 服务端端到端；二进制 Artifact 未计入响应体大小统计。
