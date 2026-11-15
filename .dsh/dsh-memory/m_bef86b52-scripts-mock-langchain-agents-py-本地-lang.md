---
id: "m_bef86b52"
title: "scripts/mock_langchain_agents.py：本地 LangChain mock 智能体集群"
summary: "一条命令在本机起 N 个 LangChain(A2A 0.3) 模拟智能体并自动注册进 agent-hub，供开发联调使用。"
kind: "reference"
importance: 3
tags: ["agent-hub","mock","langchain","a2a","dev-testing"]
pinned: false
status: "active"
created: "2026-11-13T05:22:13.272Z"
modified: "2026-11-13T05:22:13.272Z"
revision: 1
origin: "auto"
sessionId: "session-0067153c-38a9-43b4-b15a-87f4ad2f2263"
sessionTitle: "LangChain 多智能体注册脚本"
model: "deepseek-account/deepseek-flash"
---

交付物：`scripts/mock_langchain_agents.py`（主体）、`scripts/mock_agents.example.yaml`（清单示例，内置 code-reviewer / data-analyst / translator / tech-writer / ops-troubleshooter 五个角色）、`tests/test_mock_langchain_agents_unit.py`（离线单测）、`scripts/README.md` 与根 README 的「本地假 Agent 联调」段落。

形态：单进程单端口（默认 `127.0.0.1:9101`），每个智能体一个 path（如 `/agents/coder`），Agent Card 的 url 与注册地址同源以通过 SSRF/同源校验；每个智能体内部用 LangChain 1.x `create_agent` + `ChatOpenAI`(DeepSeek)。协议层自实现 `message/send`、`message/stream`(SSE)、`tasks/get`、`tasks/cancel`，`protocolVersion` 固定 `0.3.0`；`tasks/get` 查不存在任务返回 JSON-RPC error，保证健康检查为 healthy。

常用命令：
- `uv run python scripts/mock_langchain_agents.py`（真实 LLM + 自动注册）
- `--fake-llm` 离线不花额度；`--config <yaml>` 自定义清单；`--dump-config` 打印内置清单
- `--register-only`（服务在别处起，只补注册）/ `--no-register` / `--unregister`（清理）

任务文本控制标记：`#slow` 延迟 `--slow-seconds`（默认 3s）后执行，测超时；`#fail` 直接返回 failed，测失败与重试。

限制：mock 服务默认只绑 127.0.0.1 且无鉴权，不要用 `--host 0.0.0.0` 暴露到公网；端口占用时脚本给友好提示并非零退出。
