---
id: "m_602c394b"
title: "agent-hub 仓库 .dsh 运行时数据被 Git 追踪并已推送公开仓库"
summary: "agent-hub 的 .dsh 运行时数据被 Git 跟踪并已推到公开仓库；2026-11-11 用户明确「.dsh 不用管」，本轮不做处置。"
kind: "project"
importance: 3
tags: ["agent-hub","仓库卫生","git","隐私"]
pinned: false
status: "active"
created: "2026-11-10T02:05:24.986Z"
modified: "2026-11-11T07:10:10.073Z"
revision: 4
origin: "auto"
sessionId: "session-19ed3c95-61c4-4fdc-961b-e2e254f421cf"
sessionTitle: "项目待办与前端对齐排查"
model: "deepseek-official/deepseek-flash"
---

agent-hub 后端 81 个 .dsh 文件（最大 7.2 MiB）与前端 54 个（约 88.3 MiB）被 Git 跟踪并已推送到公开仓库，脱敏核查无密钥，但对话正文属隐私面。2026-11-11 用户明确裁决：.dsh 不用管，后续改动/提交时也不对其做清理或移出版本控制。
