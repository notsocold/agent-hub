---
id: "m_080603aa"
title: "agent-hub M6 实现口径：健康探针、启动恢复巡检、流式增量过滤、span 取证"
summary: "M6 设计中由助手裁决的实现口径：启动依赖不可用快速失败、恢复一次性+周期巡检并排除本进程活跃 run、checkpoint 失效用 updated_at、工具 span 用脚本化假模型取证。"
kind: "decision"
importance: 4
tags: ["agent-hub","M6","可观测性","启动恢复","流式过滤"]
pinned: false
status: "active"
created: "2026-11-11T03:31:17.612Z"
modified: "2026-11-11T03:31:17.612Z"
revision: 1
origin: "auto"
sessionId: "session-cfda4151-8ee5-4b95-a6c9-6d2e450f10b3"
sessionTitle: "开始进行 M6"
model: "deepseek-official/deepseek-flash"
---

M6 设计里已写死的实现裁决（builder-check 提出但不需要用户再选）：
- 启动期依赖不可用 → 快速失败，而不是带病启动等 /readyz 变红。
- 启动恢复：running/queued → failed；interrupted 有 pending → 保留并立即过期巡检；无 pending 且 checkpoint 有挂起 → resume；无挂起 → canceled；采用「启动一次 + 每 5 分钟巡检」，且巡检必须排除本进程活跃 run 以免互锁。
- checkpoint 清理标记用 updated_at 比较失效。
- 工具 span 的取证方式：脚本化假模型（fake model）驱动，避免真实 LLM 调用。
- 健康检查语义：/healthz 纯存活；/readyz 在原有数据库检查上追加迁移版本、checkpoint、存储与 draining 状态，关键组件失败返回 503。
- 指标用独立 registry，标签白名单防基数爆炸，/metrics 可关闭且可选 Bearer；sink 覆盖 HTTP/SSE/LLM/工具/A2A/检索/审批/限流/清理。
- 流式过滤：新增增量 filter，保证密钥任意跨 chunk 切割都不泄漏、危险 HTML 整段丢弃，落库前仍跑完整 sanitize_output。
- 停机：RunRegistry.drain() 排水 + uvicorn graceful timeout；后台清理任务覆盖 checkpoint / run_events / audit / 过期附件 / refresh token，并提供 scripts/cleanup.py --dry-run。
