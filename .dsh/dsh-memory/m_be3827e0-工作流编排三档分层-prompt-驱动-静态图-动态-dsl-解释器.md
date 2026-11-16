---
id: "m_be3827e0"
title: "工作流编排三档分层：prompt 驱动 / 静态图 / 动态 DSL 解释器"
summary: "业界“workflow”按流程定义位置与执行者分三档（prompt 驱动 / 静态图 / 动态 DSL 解释器），C 类平台型实现由 DSL、校验器、变量池、执行器注册表、调度器五部件构成。"
kind: "reference"
importance: 3
tags: ["workflow","dsl","dify","langgraph","reference"]
pinned: false
status: "active"
created: "2026-11-16T14:30:16.459Z"
modified: "2026-11-16T14:48:32.969Z"
revision: 2
origin: "auto"
sessionId: "session-74c36160-a838-434f-a4b7-bd962b97f89b"
sessionTitle: "工作流是否用LangGraph编排"
model: "deepseek-account/deepseek-flash"
---

业界“workflow”按流程定义位置与执行者分三档：prompt 驱动（把步骤写进提示词让模型自觉，不算主流）、静态图（code-first Agent，主流）、动态 DSL 解释器（Dify/n8n 等平台型产品）。

第三档（C 类，用户可自助编辑工作流）通常由五个部件组成：① DSL（nodes[] + edges[] + 变量选择器）；② 校验器（schema、环检测/可达性、入口唯一、变量只能引用拓扑上游、节点数与文本规模上限、工具白名单/敏感节点强制审批）；③ 变量池（节点间不直接传值，统一写进可持久化变量池，下游按选择器读回——这是并行调度、状态重放与断点恢复的前提）；④ 节点执行器注册表（type → executor）；⑤ 调度器/解释器。外层配节点级 checkpoint 与 node.started/finished 轨迹事件。

变量可见性语义：串行时下游可读任何前序节点；并行分支之间互不可读、汇合点可读全部分支；迭代/循环按子图执行（深拷贝变量池、注入 index/item 到选择器命名空间、跑完合并）。Dify 另有单执行路径节点数上限（MAX_TREE_DEPTH，默认约 50）作为防止不可运维巨图的硬约束。

调度器三种主流写法：拓扑排序 + 顺序执行（Dify 单路径，简单但分支要动态跳过）、队列/Pregel 超步（LangGraph、Dify graph engine，天然支持并行）、事件驱动（n8n、LlamaIndex Workflows，解耦好但难追踪）。

参考资料：Dify 变量系统分析 https://github.com/sandgardenhq/dh-library/blob/6e076f95ec7efe3f5ec84ab0622540ea460c3ff7/content/en/dify/03-the-variable-system.md ；Dify 企业版编排逻辑 https://enterprise-docs.dify.ai/zh/3.5.x/use/build/orchestrate-node ；LangChain Rebuild graph at runtime https://docs.langchain.com/langsmith/graph-rebuild 。
