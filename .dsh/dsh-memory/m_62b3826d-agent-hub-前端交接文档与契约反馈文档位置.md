---
id: "m_62b3826d"
title: "agent-hub 前端交接文档与契约反馈文档位置"
summary: "前端仓库 /home/zhaodan/work/NodeJsProjects/agent-hub-web 的 docs/handoff 为交接包来源，后端反馈文档与 OpenAPI 导出物落在 agent-hub 仓库。"
kind: "reference"
importance: 4
tags: ["agent-hub","frontend-contract","docs"]
pinned: false
status: "active"
created: "2026-11-09T09:10:11.591Z"
modified: "2026-11-09T09:10:11.591Z"
revision: 1
origin: "auto"
sessionId: "session-ca2ef162-8077-4b24-bdde-e8e74222aa25"
sessionTitle: "后端开发与前端交接文档对接"
model: "deepseek-official/deepseek-flash"
---

前端仓库：/home/zhaodan/work/NodeJsProjects/agent-hub-web，交接文档在 docs/handoff（contract-delta.md、workflows.md、work-packages.md、admin-console.md、fixtures.md），前端类型在 src/types。
后端产出：契约反馈文档 docs/feedback/2026-11-09-frontend-contract-feedback.md；机器可读契约 docs/openapi.json（用 scripts/export_openapi.py 导出）。
最需先对齐的 P0 点：① 错误信封新增 error.details（409 run_in_progress 的 details.run_id 是恢复/停止按钮关键）；② 同 conversation_id+client_message_id 幂等重试返回事件回放而非 409，前端重试不要换 client_message_id；③ reasoning_efforts 可为空（DeepSeek Chat 不支持推理等级，前端需隐藏菜单）；④ 能力开关新增 capabilities.agents / capabilities.tasks。
原则：后端不必照搬前端原型，遇到不现实处写成反馈文档，待与前端同步修改。
