---
id: "m_47794f6a"
title: "agent-hub 500 request_id 走 request.state，注册/登录限流可配置并用 seed 脚本绕开"
summary: "未处理异常会越过中间件的 contextvar 清理，因此 request_id 同时写入 request.state；注册/登录限流改为可配置，并用幂等脚本创建开发账号。"
kind: "decision"
importance: 3
tags: ["agent-hub","errors","rate-limit","dev-account","scripts"]
pinned: false
status: "active"
created: "2026-11-09T13:50:20.250Z"
modified: "2026-11-09T13:50:20.250Z"
revision: 1
origin: "auto"
sessionId: "session-fb5c27ae-a636-43ea-8da2-2813dbe9f022"
sessionTitle: "Frontend contract integration feedback"
model: "deepseek-official/deepseek-flash"
---

500 错误的 request_id：未处理异常越过 RequestContextMiddleware，finally 已复位 contextvar，最外层 ServerErrorMiddleware 取不到 ID。修法是中间件同时写 request.state.request_id，错误处理器统一从 request.state 读取，500 显式回填 X-Request-ID 并用同一 ID 写日志，保证响应体 request_id 与响应头一致。

限流：新增 RATE_LIMIT_REGISTER_MAX / RATE_LIMIT_REGISTER_WINDOW_SECONDS / RATE_LIMIT_LOGIN_MAX / RATE_LIMIT_LOGIN_WINDOW_SECONDS 配置（默认值不变），.env.example 与 docs/development.md 已注明。

开发账号：scripts/seed_dev_account.py 幂等创建/重置固定开发账号（默认 dev@example.com），用于绕开注册限流做联调；已实测“创建 → 更新”幂等。
