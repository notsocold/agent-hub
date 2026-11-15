---
id: "m_f6e904c1"
title: "agent-hub M5 HITL：独立 approval_gate 节点 + LangGraph interrupt()/Command(resume=)"
summary: "M5 把审批从进程内 waiter 改为独立 approval_gate 图节点上的 LangGraph interrupt()，并用 Command(resume=...) 恢复同一 run；三条实测结论决定实现形态。"
kind: "decision"
importance: 5
tags: ["agent-hub","M5","HITL","langgraph","interrupt"]
pinned: false
status: "active"
created: "2026-11-10T16:01:41.751Z"
modified: "2026-11-10T16:01:41.751Z"
revision: 1
origin: "auto"
sessionId: "session-a4bcdcb6-3e62-4ad3-8b77-2625ae552a80"
sessionTitle: "M5 implementation kickoff"
model: "deepseek-official/deepseek-flash"
---

M5 审批（HITL）实现形态（LangGraph 1.2，实测结论驱动）：
- resume 时只重跑 gate 节点（实测 runs={'gate': 2, 'tool': 1}），因此审批放在独立的 approval_gate 节点，工具节点不会被重复执行产生副作用。
- **同一节点内顺序调用两次 interrupt()，恢复轮次中第二个 interrupt 会错误消费上一次的 resume 值**（第二个审批被跳过）→ 约定「一个 gate 只 interrupt 一次，等全部决定到齐后再 resume」。
- 放弃/取消审批后，必须给该线程补 ToolMessage 才能继续对话；线程被 gate 抛异常后需专用 halt gate 清理（不依赖旧 run 上下文），execute_run 启动时兜底清理。
- 超时：approval → expired、run → canceled，发 message.done(reason=approval_timeout)。
- 删除进程内 asyncio waiter 阻塞方案；POST /runs/{id}/resume 不再是占位。
文档：设计 docs/superpowers/specs/2026-11-10-m5-approval-security-design.md、计划 docs/superpowers/plans/2026-11-10-m5-approval-security.md、交付报告（含前端契约变更）docs/feedback/2026-11-10-m5-backend-report.md。
