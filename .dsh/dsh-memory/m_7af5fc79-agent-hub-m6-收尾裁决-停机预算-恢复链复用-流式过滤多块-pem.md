---
id: "m_7af5fc79"
title: "agent-hub M6 收尾裁决：停机预算、恢复链复用、流式过滤多块 PEM"
summary: "M6 生产化收尾定下的停机预算、恢复链复用、崩溃 run 处置与流式过滤必须覆盖的泄漏面。"
kind: "decision"
importance: 4
tags: ["agent-hub","M6","observability","streaming","shutdown","security"]
pinned: false
status: "active"
created: "2026-11-11T05:48:58.025Z"
modified: "2026-11-11T05:48:58.025Z"
revision: 1
origin: "auto"
sessionId: "session-cfda4151-8ee5-4b95-a6c9-6d2e450f10b3"
sessionTitle: "开始进行 M6"
model: "deepseek-official/deepseek-flash"
---

2026-11-11 M6 生产化收尾的裁决与硬约束：
- 停机预算：uvicorn 90s + registry.drain 30s + finalize 10s + 余量，必须 < Compose stop_grace_period: 160s，避免 SIGKILL 落在 finalize/tracing 收尾前（代价是发布窗口变长）。
- 恢复复用 M5 恢复链：schedule_resume(recovery=True)，由任务内部判断 restart_lost；restart_lost 直接 _finalize_canceled，不调用 cancel_interrupted_run（后者等待当前 resume 任务会自锁，无挂起时也无意义）。
- 崩溃的 running run 一律标 failed，不尝试恢复 LLM 流；只有 interrupted 走 resume / restart_lost。
- 流式过滤必须两层（含 flush 尾部判定），覆盖这些泄漏面：PEM 私钥跨 chunk 与流末尾；同一 feed 内多块 PEM（用「最后一个完整块」判定尾部破折号）；Bearer 后任意空白（\n、\t、多空格）；未闭合 <script>/<iframe>/<style>；无引号/斜杠前缀事件属性（如 <svg/onload=...>、<img src="x"onerror=...>）。
- 观测 span 用 wrapper（如 sse.stream）实现，不重排 chat 主流程大函数以降低回归。
- 在 master 内联实施，不建 worktree（沿用用户既有偏好）。
边界：真实 Grafana/Tempo 可视化与告警、真实 LB 下滚动发布未实测，仅交付配置/PromQL/dashboard JSON 与 span 断言；M6 明确单实例，多实例/Helm/跨实例 resume 留后续；M6 只清理运维类数据（checkpoint/事件/审计/附件/token），不含消息正文保留策略。
