---
id: "m_5bd6d7c5"
title: "agent-hub 工作流 = 步骤模板数据 + 固定主图 + 参数注入，不是 LangGraph 节点编排"
summary: "M9 工作流不给 LangGraph 增加节点、也不编译新图，而是把 steps 模板在运行时翻译成 prompt 段、工具白名单、强制审批集合与事件映射四处注入到同一张主图上。"
kind: "decision"
importance: 4
tags: ["agent-hub","workflow","langgraph","architecture","M9"]
pinned: false
status: "active"
created: "2026-11-13T04:59:17.930Z"
modified: "2026-11-13T04:59:17.930Z"
revision: 1
origin: "auto"
sessionId: "session-74c36160-a838-434f-a4b7-bd962b97f89b"
sessionTitle: "工作流是否用LangGraph编排"
model: "deepseek-account/deepseek-flash"
---

agent-hub 的「工作流」实现口径（M9）：**不是 LangGraph 节点编排**，工作流是纯数据模板 + 一张固定不变的主图 + 多处参数注入。

1) 主图固定：START → model →（有 tool_calls）approval_gate → tools → model 循环，无 tool_calls 则 END；节点数固定（approval_gate/tools 可为 None 退化为无工具图），M9 未改动图结构。所有参数（system_prompt、tools、tool_executor、approval_gate、max_tool_rounds）由 modules/chat 每轮装配后传入，agents/ 不反向依赖 modules/。

2) 数据形态：workflows.steps 为 JSONB 步骤模板，五类 step type——knowledge_search（默认给 search_knowledge）、tool（必须显式 config.tools）、a2a（默认给 send_message）、approval（不提供工具，config.tools 表示强制审批集合）、answer（纯生成）。

3) 校验集中在装配器 validate_steps()：非空、≤12 步、id 唯一、instruction ≤500 字符、工具名 ≤200、全部文本总长 ≤8000、tool 步骤必须显式列工具、**模板至少含一个提供工具的步骤**（防止模板调控导致工具全放行）。

4) 四个落地的注入点（prepare_runtime）与消费：
- system prompt 段落 prompt_section()：软约束，让模型自觉按序推进；拼接时刻意排在 prompt_guard 附注之前，保证安全提醒在末尾；
- 工具白名单 tool_allowlist()/filter_tools()：在 bind_tools 前过滤，硬约束；
- 强制审批 force_approval() → approval_spec()：ToolRunner 执行期兜底强制走审批 gate；
- 事件映射 step_for_tool()：工具执行完按「工具名 → 模板中首个命中步骤」emit workflow.step，payload 固定 5 键 {step_id, step_type, step_title, tool, status}，event_type="workflow"，紧跟对应 tool.result，seq 连续可回放。

5) 快照与错误语义：发起时把 workflow_steps 等键冻结进 run.capabilities，恢复轮读快照、快照缺失才回退查表，再查不到即失败；失败语义 404 / 409 workflow_disabled / 409 workflow_version_conflict / 422。
