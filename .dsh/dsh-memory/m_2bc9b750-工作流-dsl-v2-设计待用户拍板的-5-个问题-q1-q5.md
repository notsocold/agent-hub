---
id: "m_2bc9b750"
title: "工作流 DSL v2 设计待用户拍板的 5 个问题（Q1–Q5）"
summary: "docs/superpowers/specs/2026-11-16-workflow-dsl-v2-design.md 已交付，需用户评审 §4.2 五个开放问题、§5.1 DSL 示例、§8.3.1 审批语义后拆实施计划。"
kind: "todo"
importance: 4
tags: ["agent-hub","workflow","dsl","todo","decision-pending"]
pinned: false
status: "active"
created: "2026-11-16T15:15:24.217Z"
modified: "2026-11-16T15:15:24.217Z"
revision: 1
origin: "auto"
sessionId: "session-74c36160-a838-434f-a4b7-bd962b97f89b"
sessionTitle: "工作流是否用LangGraph编排"
model: "deepseek-account/deepseek-flash"
---

工作流 DSL v2 设计文档已交付，下一步等用户评审后再拆实施计划、并在 docs/roadmap.md 登记里程碑。待拍板的 5 个问题（含助手建议）：
- Q1 里程碑编号：M10 / M9.5 / 不占编号 —— 建议记为 M10。
- Q2 v1 执行路径保留多久：永久双路径 / 演进期后下线 —— 建议保留 2 个里程碑后下线（双路径测试矩阵翻倍是真实成本）。
- Q3 节点类型范围：C1 只做确定性+llm / 三类都做 —— 建议三类都做，agent 节点显式标记「不确定」。
- Q4 v2 默认是否对外开启：新模板默认 v2 / 配置开关灰度 —— 建议配置开关 WORKFLOW_DSL_V2_ENABLED 默认关，联调后再开。
- Q5 前端编辑器形态：线性表单先行 / 直接画布 —— 建议线性表单先行（E1→E2→E3），画布属 C4。
评审重点：§4.2 五个问题、§5.1 DSL 示例、§8.3.1 审批语义；定稿后更新 roadmap「待决策/开放问题」表，再按项目惯例出实施计划（Task 1–N + Review Focus + 验收清单）。
