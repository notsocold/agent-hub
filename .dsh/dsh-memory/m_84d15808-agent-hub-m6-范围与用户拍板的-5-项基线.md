---
id: "m_84d15808"
title: "agent-hub M6 范围与用户拍板的 5 项基线"
summary: "M6 做路线图 7 项生产化能力并承接 M5 遗留的启动恢复/流式过滤，用户定调：复用已有服务、单实例、Dockerfile+Compose、可观测走 JSON+Prometheus+OTel。"
kind: "decision"
importance: 5
tags: ["agent-hub","M6","范围","决策"]
pinned: false
status: "active"
created: "2026-11-11T03:31:17.604Z"
modified: "2026-11-11T03:31:17.604Z"
revision: 1
origin: "auto"
sessionId: "session-cfda4151-8ee5-4b95-a6c9-6d2e450f10b3"
sessionTitle: "开始进行 M6"
model: "deepseek-official/deepseek-flash"
---

M6 范围 = 路线图上的 7 项生产化能力，外加承接 M5 遗留的启动恢复、流式实时过滤、多实例限流。用户在节点上做了 5 项拍板：① 只复用本机已在运行的 dev_postgres / dev_redis / dev_minio 做本地故障注入与压测，不新起容器；② 单实例生产化——不做多实例、不做零停机滚动（需 LB 摘流量）；③ 交付多阶段 Dockerfile + 生产 Compose（Prometheus/Grafana/Nginx 作为可选 profile），不做 Helm；④ M5 的两项承接（启动恢复、流式实时过滤）都纳入 M6；⑤ 可观测按生产常规定稿：stdlib JSON 日志 + Prometheus 指标 + OTel/OTLP，未配置时整体 no-op。设计文档位于 docs/superpowers/specs/2026-11-11-m6-production-observability-design.md，工作量约为 M5 的 1.5 倍，拆成 12–14 个可并行任务，最后做整分支独立审查。
