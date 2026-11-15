---
id: "m_5f490db0"
title: "agent-hub M7 契约：事件回放 event 字段、可续流 SSE、写操作 RBAC、A2A 授权 URL 白名单"
summary: "M7 定下的对外契约与安全口径：事件回放补 event、新增可续流 SSE（只回放已落库事件）、写接口收紧到管理员、A2A 授权 URL 严格白名单。"
kind: "decision"
importance: 4
tags: ["agent-hub","M7","SSE","RBAC","A2A","契约"]
pinned: false
status: "active"
created: "2026-11-11T16:01:51.207Z"
modified: "2026-11-11T16:01:51.207Z"
revision: 1
origin: "auto"
sessionId: "session-4c5efa4f-7308-4b93-a054-c5e148a65852"
sessionTitle: "实现M7和M8功能"
model: "deepseek-official/deepseek-flash"
---

M7 生产化契约（已实现并本地提交，未推送）：

1) 事件回放：GET /runs/{id}/events 新增必填 event 字段（原始 SSE 事件名，未知为空串），payload 继续剔除 _event，既有字段零变化。

2) SSE 续流：新增 GET /runs/{id}/stream?after_seq=，Bearer 鉴权；非本人/不存在统一 404；只回放 seq > after_seq 的已落库事件；单次上限 1000（超出 400 replay_too_large）；终态关闭连接；interrupted 状态等待审批恢复；15s 心跳；不读 Last-Event-ID。旧 POST /chat/stream 行为保持不变（用显式默认参数锁定，含 1001 帧回归测试）。

3) 续流语义边界：message.delta 不落库、断线不补；interrupted 期间消息历史不返回，前端应在 run 终态后再拉消息终稿。

4) 写操作 RBAC：agents 的 5 个写接口、tasks 的 4 个动作接口收紧到 AdminUser；非管理员 403 forbidden 且 details.required_role=admin；拒绝写审计 security.rbac_denied 集中在 require_admin 内用独立会话写，审计失败不影响 403；读接口与 POST /agents/preview 保持所有登录用户可用。

5) A2A 授权 URL 白名单：仅允许 https 公网（dev/test 额外允许 http://localhost、127.0.0.1）；拒绝非 https、带凭据、私网/回环、十进制/十六进制/八进制/混合写法混淆 IP、百分号编码、反斜杠、非 ASCII/IDN、单标签、.local/.internal/.localdomain、超长；A2A_AUTH_URL_ALLOWED_HOSTS 只能收窄不能放宽；违规置 None 并审计 security.auth_url_rejected，历史任务详情与 run 事件回放都会再过滤一次。

6) approval.required 事件补齐 tool_display_name/executor/target/side_effect/payload_preview，与 GET /approvals/{id} 字段一致。
