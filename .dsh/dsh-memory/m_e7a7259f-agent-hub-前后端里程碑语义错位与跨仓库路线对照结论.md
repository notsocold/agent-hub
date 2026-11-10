---
id: "m_e7a7259f"
title: "agent-hub 前后端里程碑语义错位与跨仓库路线对照结论"
summary: "后端 agent-hub 与前端 agent-hub-web 的里程碑编号在 M3/M5 上语义冲突，工作流与管理台双方悬空，但接口契约本身已对齐。"
kind: "project"
importance: 4
tags: ["roadmap","跨仓库","agent-hub-web","里程碑","契约"]
pinned: false
status: "active"
created: "2026-11-10T02:27:10.296Z"
modified: "2026-11-10T02:27:10.296Z"
revision: 1
origin: "auto"
sessionId: "session-3be98e63-25fa-4f4c-8d7e-0f004db39e5f"
sessionTitle: "前端开发路线匹配分析"
model: "deepseek-official/deepseek-flash"
---

跨仓库路线对照结论（后端 agent-hub，前端仓库位于 /home/zhaodan/work/NodeJsProjects/agent-hub-web）：

一致部分：M0、M1、M1.5、M2 双端编号与内容一致且已完成真实联调；M4 均为知识库 RAG，编号与时序匹配；M6 均为生产化/可观测。

错位部分：
- M3：后端定义为 A2A 入站（发布 Agent Card + JSON-RPC 端点 + 入站鉴权，纯服务端），前端 M3 写的是「审批完善」，而审批卡能力已在 M1.5/M2 落地，导致进入 M3 时前端没有可验收的入站联调交付物。
- M5：后端为「审批与安全加固」，前端为「体验与可访问性」，同名不同义。
- 工作流与管理台：后端仅写「M5 后按复杂度评估」「M5+」且无任何端点（无 /admin/*），前端已有入口骨架（置灰）与完整交接包 BE-12/BE-13，双方都未立项、长期悬空。

总体判断：问题主要在里程碑语义与文档状态（两侧 roadmap/README/api.md 的完成标记滞后），不在接口契约；M1.5/M2 的字段差异已逐条归一化并记录在前端 docs/handoff 对齐记录中。

前端门控约定：导航与路由读 system/info.capabilities（如 knowledge/workflows=false 时入口置灰或退回对话页），但 A2A 的 card_url/jsonrpc_url 未按 system.a2a.enabled 门控，M3 前设置页仍会展示可复制的未启用地址。
