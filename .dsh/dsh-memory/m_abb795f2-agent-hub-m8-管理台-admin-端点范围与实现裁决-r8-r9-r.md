---
id: "m_abb795f2"
title: "agent-hub M8 管理台 /admin/* 端点范围与实现裁决（R8/R9/R1）"
summary: "M8 新增 15 个 AdminUser 门禁的 /admin/* 端点，并定下全局 Agent 口径、审计过滤、配置面与并发保护等裁决。"
kind: "decision"
importance: 4
tags: ["agent-hub","M8","admin","RBAC","裁决"]
pinned: false
status: "active"
created: "2026-11-11T16:01:51.211Z"
modified: "2026-11-11T16:01:51.211Z"
revision: 1
origin: "auto"
sessionId: "session-4c5efa4f-7308-4b93-a054-c5e148a65852"
sessionTitle: "实现M7和M8功能"
model: "deepseek-official/deepseek-flash"
---

M8 管理台（已实现并本地提交，未推送）：新增 15 个 /api/v1/admin/* 端点，全部 AdminUser 门禁。

覆盖范围：用户管理（列表/详情/PATCH）、只读概览计数、平台指令 GET/PUT、系统技能 enabled/force-disable、系统 MCP 服务 CRUD、全局 Agent GET/PATCH。

关键裁决：
- R8 全局 Agent 口径：采用「全部未软删」的 v1 全局注册表，而非 spec 字面的 owner_user_id IS NULL——因为创建路径会写 owner，按 NULL 过滤会让现存 Agent 全部不可见；此项仍需产品最终确认。
- R9：M8 不扩展 /admin/audit-logs 的过滤条件（现有字段够用）。
- R1 配置面：INSTRUCTIONS_ENABLED/SKILLS_ENABLED/MCP_ENABLED=false 时，管理端仍可读写持久化配置，运行态接口行为不变。
- 用户 PATCH 保护：固定顺序先锁活跃管理员集合与目标行，锁后重算变更与剩余管理员；不能改自己角色（422 cannot_change_own_role）、不能停用自己（422 cannot_disable_self）、不能移除最后一个活跃管理员（409 last_admin_protected）；不返回 password_hash；写审计 admin.user.update。
- 全局 Agent 编辑：可改 name/enabled/requires_approval/auth/headers，只读字段显式 422；name_edited 保护名称不被 refresh 覆盖；审计 agent.updated(source=admin)。
- 系统 MCP 管理：复用既有 SSRF/secret: 解析/工具发现逻辑，跨 scope slug 冲突 409，响应与审计不回显凭据，删除时清理工具与用户覆盖。
- secret_store_unavailable：统一了共享 normalize_headers 的 503 code（用户侧同路径从 service_unavailable 改为同一 code，HTTP 状态不变）。
- force_disabled 语义：压过用户级覆盖，私有技能一律 404。
