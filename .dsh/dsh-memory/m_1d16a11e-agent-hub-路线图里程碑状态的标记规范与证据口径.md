---
id: "m_1d16a11e"
title: "agent-hub 路线图里程碑状态的标记规范与证据口径"
summary: "docs/roadmap.md 用总览表+标题内联状态+checkbox 标记里程碑，勾选必须有实测证据，不虚报。"
kind: "project"
importance: 3
tags: ["roadmap","docs","milestones","agent-hub"]
pinned: false
status: "active"
created: "2026-11-10T02:49:10.137Z"
modified: "2026-11-10T02:49:10.137Z"
revision: 1
origin: "auto"
sessionId: "session-3be98e63-25fa-4f4c-8d7e-0f004db39e5f"
sessionTitle: "前端开发路线匹配分析"
model: "deepseek-official/deepseek-flash"
---

在 agent-hub 标记里程碑完成情况时的既定做法（docs/roadmap.md）：
1) 顶部放「里程碑状态总览」表，一行给出状态、完成日期、证据（如测试数、smoke 脚本项数）；
2) 每个里程碑标题内联状态，如 `## M2 A2A 出站（已完成，2026-11-10）`、`## M3 A2A 入站（未开始）`；
3) 范围项与验收项统一加 checkbox，完成 `[x]`、未完成 `[ ]`，便于逐项推进与 review。
证据口径：只有实测/交付物支撑才打 [x]，避免虚报——例如 M1「首 token P95 < 3s」因只有单次联调实测、无 P95 压测数据，保留 `[ ]` 并注明「P95 基线并入 M6 容量验收」；M5 的 approvals/audit_logs 模型与迁移、审批端点标为 [x] 但注明是 M1.5 提前落地。
已知遗留：README.md §9「下一步」与 docs/api.md §7.3/§7.4 的事件/开关描述已过时，前端 agent-hub-web/docs/roadmap.md 在仓库工作区之外，均未同步，需用户确认后再改。
