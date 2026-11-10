---
id: "m_63c9a26d"
title: "agent-hub M4 知识库 RAG 范围与关键设计决策"
summary: "M4 实现知识库 RAG：分类/owner/pgvector/双通道 embedding 基础上，v2 设计补齐原始文件落盘、存储双后端、fake 阈值置 0 与 scope 权限模型。"
kind: "decision"
importance: 5
tags: ["agent-hub","M4","rag","pgvector","embedding","storage"]
pinned: false
status: "active"
created: "2026-11-10T06:29:28.074Z"
modified: "2026-11-10T08:36:51.018Z"
revision: 2
origin: "auto"
sessionId: "session-3bc6169d-3a64-443d-9cef-444da4c4c01a"
sessionTitle: "开始 M4 阶段"
model: "deepseek-official/deepseek-flash"
---

沿用决策：分类沿用、owner 归属、pgvector 存储、双通道 embedding（fake/OpenAI）、引用落库、分域唯一索引。

2026-11-10 设计评审 v2 新增/修正（docs/superpowers/specs/2026-11-10-m4-knowledge-rag-design.md）：
1. 原始文件必须落盘（data/knowledge/），knowledge_documents 增 storage_key，直接复用 attachments 的 storage_key+目录模式；retry/rebuild 靠它取原文；一致性规则为先落库后写文件、删除先库后文件。
2. 存储做抽象 + 双后端：默认 local，生产用 S3 兼容（对象存储才是生产正解：多实例共享、容器销毁不丢、生命周期规则现成，与前端交接包 D5 一致）；开发/测试/CI 不依赖 S3。本机 minio 可用（health=200）。
3. 词袋 fake embedding 余弦有数学上界 sqrt(短 token 数/长 token 数)：实测 9 字 query vs 183 字 chunk 仅 0.1120，低于 0.30 阈值，会导致 fake 模式检索全空、CI 门禁跑不过。故 fake 模式阈值自动置 0（只排序不过滤），真实 embedding 仍用 0.30，评测断言改为 top-k 命中预期文档、不依赖阈值。
4. embedding 用显式 EMBEDDING_PROVIDER 选择 + 占位符（sk-xxx）识别 + conftest 强制 fake，避免测试走网络 401。
5. 权限：上传加 scope（system 文档仅管理员可写）；检索恒为「自己 + 系统」，管理员也不越权；管理接口管理员可用 owner_scope=all。
6. hit.updated_at 取 document.updated_at，不给 chunk 加列。

已核实的仓库契约事实：ServiceUnavailableError(503) 早已存在（MCP 在用），上传超限应为 413；system/info 的 embedding 字段已存在且必填 {model,dim}；单测平铺在 tests/，没有 tests/unit/。
