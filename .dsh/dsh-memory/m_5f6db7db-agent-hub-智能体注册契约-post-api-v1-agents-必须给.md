---
id: "m_5f6db7db"
title: "agent-hub 智能体注册契约：POST /api/v1/agents 必须给真实 A2A 服务"
summary: "注册智能体靠 POST URL + 后端拉 agent-card 校验，因此模拟智能体必须是真正讲 A2A 协议的服务；注册幂等但不刷新 Card。"
kind: "reference"
importance: 4
tags: ["agent-hub","a2a","agent-card","api-contract"]
pinned: false
status: "active"
created: "2026-11-13T05:22:13.269Z"
modified: "2026-11-13T05:22:13.269Z"
revision: 1
origin: "auto"
sessionId: "session-0067153c-38a9-43b4-b15a-87f4ad2f2263"
sessionTitle: "LangChain 多智能体注册脚本"
model: "deepseek-account/deepseek-flash"
---

agent-hub 的「注册智能体」= 调 `POST /api/v1/agents` 传一个 URL（管理员门禁），后端去拉 `{url}/.well-known/agent-card.json` 做同源、协议版本（0.3.x / 1.x）、skills 校验后落库。之后主 LangGraph 把它暴露成 `send_message(agent=<slug>, task=...)` 工具，聊天时真实走 A2A JSON-RPC / SSE 调用。

推论：任何 mock/模拟智能体都必须是**真正讲 A2A 协议的服务**，只往数据库里插记录是不行的。

其它要点：
- 注册幂等（重复启动返回同一批 id），但**不会刷新已有 Card**；改了 name/skills 后需先 `--unregister` 再启动，或调 `POST /api/v1/agents/{id}/refresh`。
- 注册接口是管理员门禁，需先 `uv run python scripts/seed_dev_account.py --admin` 准备账号。
- 注销按 slug + URL 双匹配（避免误删用户的同名 Agent），并需遍历分页。
