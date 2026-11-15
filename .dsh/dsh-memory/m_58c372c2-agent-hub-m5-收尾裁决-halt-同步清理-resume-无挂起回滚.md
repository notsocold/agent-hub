---
id: "m_58c372c2"
title: "agent-hub M5 收尾裁决：halt 同步清理、resume 无挂起回滚"
summary: "M5 两处显式裁决：检查点线程清理在取消接口内同步执行，resume 遇到无挂起时回滚 interrupted 并记 warning 而非自动取消。"
kind: "decision"
importance: 4
tags: ["agent-hub","M5","ruling","approval","checkpoint"]
pinned: false
status: "active"
created: "2026-11-10T16:01:41.756Z"
modified: "2026-11-10T16:01:41.756Z"
revision: 1
origin: "auto"
sessionId: "session-a4bcdcb6-3e62-4ad3-8b77-2625ae552a80"
sessionTitle: "M5 implementation kickoff"
model: "deepseek-official/deepseek-flash"
---

- **halt 清理同步执行**（不在取消接口内 create_task）：避免新 run 与旧线程并发写同一 checkpoint 线程导致死锁，代价是取消接口多几十毫秒。
- **resume 发现无挂起时回滚 interrupted 并记 warning**，不自动取消：保留「决定已到但图已完成」场景的人工处理空间，代价是该极端场景仍需用户手动取消。
- 配套修复（来自最终整分支审查）：resume 的 start_seq 必须在旧任务收尾后取 max(seq) 并加 DB 幂等（否则 seq 冲突/倒序、双终态）；A2A 取消进行中任务需 await _resolved_headers；工具异常路径同样包 <untrusted>。
