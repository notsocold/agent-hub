---
id: "m_f0cacd8c"
title: "agent-hub 本轮后端范围收敛到 M1 对话最小闭环"
summary: "M1 是 M1.5、M2–M6 的硬前提，先与前端联调；工作流与管理控制台/RBAC 延后到 M5+。"
kind: "decision"
importance: 4
tags: ["agent-hub","milestone","scope"]
pinned: false
status: "active"
created: "2026-11-09T09:10:11.594Z"
modified: "2026-11-09T09:10:11.594Z"
revision: 1
origin: "auto"
sessionId: "session-ca2ef162-8077-4b24-bdde-e8e74222aa25"
sessionTitle: "后端开发与前端交接文档对接"
model: "deepseek-official/deepseek-flash"
---

本轮结合前端交接文档与后端设计，把范围收敛为 M1 对话最小闭环（认证、会话/消息、聊天 SSE、运行轨迹、取消、事件回放、审批最小接口、系统信息），因为它是一切后续里程碑的硬前提且最适合先联调。
明确不按原型实现的部分：工作流延后 M5+（倾向轻量步骤模板，M1 capabilities.workflows=false）；管理控制台/RBAC 延后 M5+（M1 只有 users.role）；A2A/智能体/任务/知识库分别在 M2/M3/M4，未注册端点返回 404；MCP/Skill/记忆/Instruction/附件属 M1.5，能力开关为 false；用户级 A2A 授权 v1 不做。
本轮未修改前端仓库。
