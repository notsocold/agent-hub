---
id: "m_a0c1601b"
title: "agent-hub 分页契约：cursor 编码层做 JSON-safe 归一化，cursor 优先于 before"
summary: "分页 500 的根因是 encode_cursor 直接 json.dumps ORM UUID，修复定在编码层统一归一化，并明确 cursor 与 before 同时传入时 cursor 优先。"
kind: "decision"
importance: 4
tags: ["agent-hub","pagination","cursor","contract"]
pinned: false
status: "active"
created: "2026-11-09T13:50:20.249Z"
modified: "2026-11-09T13:50:20.249Z"
revision: 1
origin: "auto"
sessionId: "session-fb5c27ae-a636-43ea-8da2-2813dbe9f022"
sessionTitle: "Frontend contract integration feedback"
model: "deepseek-official/deepseek-flash"
---

问题：列表/历史/审批在 has_more=true 时返回 500，根因是 app/core/cursor.py 的 encode_cursor 直接 json.dumps(payload)，而三处调用点传入 ORM 的 UUID 对象导致 TypeError 被兜底成 500。

既定修法与约定：
- 在编码层统一做 JSON-safe 归一化，而不是逐个调用点转字符串：UUID → 标准字符串，datetime/date → ISO 8601；未知类型仍抛 TypeError，不静默吞掉。
- 消息历史的 before 与 cursor 同时传入时以 cursor 为准（原实现是两者 AND），与契约一致。
- 回归覆盖：三处列表 has_more=true 翻页、第二页严格更旧且无重复、stats 为全量计数、非法 cursor 返回 422、500 的 request_id 与 X-Request-ID 一致。

意义：任何新的分页端点只要走 encode_cursor 就自动获得该保障，不需要额外转类型。
