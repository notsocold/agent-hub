---
id: "m_11494769"
title: "agent-hub 后端 M1 关键实现约束：SSE 解耦、幂等回放、并发 409"
summary: "SSE 与图执行解耦、client_message_id 幂等回放、未结束 run 用部分唯一索引兜底，是 M1 后端的既定实现约束。"
kind: "decision"
importance: 4
tags: ["agent-hub","implementation","sse"]
pinned: false
status: "active"
created: "2026-11-09T09:10:11.615Z"
modified: "2026-11-09T09:10:11.615Z"
revision: 1
origin: "auto"
sessionId: "session-ca2ef162-8077-4b24-bdde-e8e74222aa25"
sessionTitle: "后端开发与前端交接文档对接"
model: "deepseek-official/deepseek-flash"
---

1) 同一会话未结束 run 由数据库部分唯一索引兜底，并发发送返回 409 run_in_progress（details.run_id 供前端恢复/停止）。
2) client_message_id 重复请求返回同一 run 的完整事件回放，不新建消息。
3) SSE 与图执行解耦：客户端断开不取消 run，可通过 GET /runs/{id}/events?after_seq= 回放；事件 ID 形如 <run_id>:<seq>。
4) 无 LLM_API_KEY 时开发/测试环境使用假流式模型，避免阻塞前端联调。
5) 生成中的助手消息不进入历史接口，避免刷新看到空的“已取消”消息；历史消息倒序且不返回 streaming 状态。
6) 契约测试用 ASGITransport 会缓冲响应，真实流式须用 uvicorn + TCP 验证。
7) 审批接口已具备，但 M1 无工具触发 approval.required。
