---
id: "m_5bd6d7c5"
title: "agent-hub 工作流 = 固定主图 + 模板数据注入；用户可编辑工作流走 C1 受控 DSL + 解释器"
summary: "M9 工作流仍是固定主图 + steps 参数注入；用户可自定义工作流的目标形态是 C1「受控 DSL + 确定性解释器」，2026-11-16 已出 v2 设计文档并定下三条架构裁决。"
kind: "decision"
importance: 4
tags: ["agent-hub","workflow","dsl","langgraph","design"]
pinned: false
status: "active"
created: "2026-11-13T04:59:17.930Z"
modified: "2026-11-16T15:15:24.216Z"
revision: 3
origin: "auto"
sessionId: "session-74c36160-a838-434f-a4b7-bd962b97f89b"
sessionTitle: "工作流是否用LangGraph编排"
model: "deepseek-account/deepseek-flash"
---

M9 工作流仍是固定主图 + steps 参数注入；用户要求可自定义（增/改）工作流后，目标形态定为 C1「受控 DSL + 确定性解释器」，LangGraph 只当执行内核，一张固定解释器图 + DSL 放 state。

2026-11-16 设计文档落在 docs/superpowers/specs/2026-11-16-workflow-dsl-v2-design.md（约 650 行、17 章），本轮只写文档未改代码。三条核心裁决：
1) DSL 顶层用 steps[] + 每步 next，不引入独立 edges：存量 v1 模板零迁移、next 缺省即数组顺序已能表达任意有向图、前端画布把连线映射成 next；条件分支用查表路由（routes: {值: 目标步 id}）而非表达式引擎，避免 eval 注入面，判断交给上游 llm/确定性节点。
2) 解释器图固定 4 节点（advance 纯计算 → run_node / gate(interrupt) → 回到 advance → END），模板编辑不触发重编译：checkpoint 与节点名绑定，图结构恒定则模板改动不影响恢复，符合 LangGraph「节点内按 config 分支而非动态改图」的工程建议。
3) 审批语义收紧为「不重复审批」：approval 节点 = 一次性人工闸门，通过后其 tools 记入本 run 的 approved 集合；工具节点执行前检查是否已覆盖，已覆盖直接执行不再二次挂起；未被覆盖但工具原生 requires_approval 时由 advance 路由到 gate 单独审批。
