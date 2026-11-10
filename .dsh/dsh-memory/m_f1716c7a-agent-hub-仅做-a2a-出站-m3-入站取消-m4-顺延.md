---
id: "m_f1716c7a"
title: "agent-hub 仅做 A2A 出站：M3 入站取消、M4 顺延"
summary: "2026-11-10 需求变更：本服务只做 A2A 出站 Client，不发布 Agent Card、不提供 JSON-RPC 入站；M3 入站作废，M4 顺延；已完成收尾并提交。"
kind: "decision"
importance: 4
tags: ["a2a","roadmap","requirement-change","m3"]
pinned: false
status: "active"
created: "2026-11-10T03:12:18.628Z"
modified: "2026-11-10T03:19:19.851Z"
revision: 2
origin: "auto"
sessionId: "session-e3aa0116-6e08-44e5-976d-c1fe6c910b8a"
sessionTitle: "M3 开发"
---

决策（2026-11-10 需求变更）：agent-hub 只承担 A2A 出站 Client 角色，不发布 Agent Card、不暴露 JSON-RPC 入站端点。M3「A2A 入站」整体取消，M4 顺延为下一个里程碑；system/info.a2a 恒为无入站。

收尾落地（提交 1cfb024，19 files changed）：
- docs/roadmap.md：M3 标「已取消（2026-11-10 需求变更）」，原范围/验收用 [-] 保留作历史；M4 顺延。
- system/service.py + schemas.py：a2a 五字段保留（只增不删），enabled=false、card_url/jsonrpc_url=""、auth_type/card_security="none"，OpenAPI 描述写明不提供入站。
- config.py：A2A_ENABLED / A2A_PUBLIC_URL / A2A_AGENT_PATH 标为历史兼容字段，不参与运行逻辑。
- 新增 tests/integration/test_a2a_outbound_only.py：断言 system/info.a2a 恒无入站，/a2a 与两个 well-known 路径均 404。
- a2a.md / architecture.md / api.md / README.md / app/modules/README.md / 两份 spec 同步为「仅出站」；记录见 docs/feedback/2026-11-10-requirement-change-outbound-only.md。
- 无新增数据库迁移，a2a_tasks 不加 source 字段；M2 出站能力与拉取 Card 校验完全保留。

理由：M2 客户端、假 Agent、docs/a2a.md 的 curl 样例都是 0.3 风格自研契约；接 SDK 1.3.0（protobuf/1.0 模型）会同时推翻 M2 契约。
