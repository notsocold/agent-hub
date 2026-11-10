---
id: "m_8346c8de"
title: "agent-hub 选型准则：官方 SDK + 薄封装为默认，自研仅为例外"
summary: "A2A/MCP 一类协议选型默认采用官方 SDK 做协议层、自研只保留安全层薄封装；自研完整协议栈仅在 SDK 不满足硬约束时例外。"
kind: "decision"
importance: 4
tags: ["a2a","架构选型","sdk","mcp","分层"]
pinned: false
status: "active"
created: "2026-11-10T03:44:48.679Z"
modified: "2026-11-10T03:44:48.679Z"
revision: 1
origin: "auto"
sessionId: "session-e3aa0116-6e08-44e5-976d-c1fe6c910b8a"
sessionTitle: "M3 开发"
model: "deepseek-official/deepseek-flash"
---

结论（2026-11-10 讨论确认）：协议类集成（A2A、MCP 等）默认「官方 SDK 打底 + 薄封装」，自研完整协议栈只作为有明确约束时的例外，判断口径是「SDK 是否覆盖 80% 且能长期跟进规范演进」+「剩余 20%（SSRF/白名单/审计/测试用 httpx.MockTransport 注入）是否必须自己做」，两者都为是时选 SDK + 薄封装。

分层形态（迁移后的既定结构）：业务层(tool.py/tasks/agents，不感知 protobuf) → 领域层(A2aClient/A2aUpdate/A2aError，语义稳定) → 协议层(a2a-sdk 的 JsonRpcTransport / CompatJsonRpcTransport) → 安全层(card.py 负责 SSRF/同源/重定向/响应体 1MB 上限 + SDK parse_agent_card 做 schema 校验)。

当初自研的四条理由（M2 报告 docs/feedback/2026-11-09-m2-backend-report.md §2.3、§3 有记录）：1) a2a-sdk 1.x 以 protobuf 为中心，接入成本高于需求；2) 沿用 M1.5 MCP 自研 JSON-RPC 的项目路径；3) 只需 message/send、message/stream、tasks/get、tasks/cancel 四个方法；4) 需要精确控制 SSRF、同源、响应体上限、超时与错误码映射，测试用 httpx.MockTransport 注入。

迁移原因：用户要求 SDK 真正被使用而非只做声明依赖；SDK 1.3.0 已提供 CompatJsonRpcTransport(0.3)、JsonRpcTransport(1.0)、parse_agent_card 兼容 0.3 Card、ClientConfig.httpx_client 传输注入，原自研理由大部分消失；自研需持续跟随 0.3→1.0 规范演进，成本累积。对外契约(A2aClient、错误码、a2a_tasks)始终未变，迁移成本被限制在协议层。

后续 MCP、记忆、检索等类似选型可复用同一判断框架。
