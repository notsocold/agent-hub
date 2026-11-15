---
id: "m_666bcdd2"
title: "agent-hub 测试不得依赖本地 .env：Pydantic Settings 会读取导致本地失败/CI 通过"
summary: "test_missing_capability_key_503 用 delenv 删环境变量后仍从本地 .env 读到 CAPABILITY_SECRET_KEY，属既有测试的环境依赖问题，应改成不依赖 .env。"
kind: "project"
importance: 3
tags: ["agent-hub","测试","配置",".env"]
pinned: false
status: "active"
created: "2026-11-11T03:31:17.617Z"
modified: "2026-11-11T03:31:17.617Z"
revision: 1
origin: "auto"
sessionId: "session-cfda4151-8ee5-4b95-a6c9-6d2e450f10b3"
sessionTitle: "开始进行 M6"
model: "deepseek-official/deepseek-flash"
---

M6 开工前的全量回归出现 1 个失败：test_missing_capability_key_503——用例 delenv 掉环境变量后，Pydantic Settings 仍会从仓库本地 .env 读回 CAPABILITY_SECRET_KEY，因此在有 .env 的开发机失败、在无 .env 的 CI 通过。这是既有测试的环境依赖问题（不是 M6 代码引入），修法是让该用例不依赖本地 .env（例如显式指向空 env_file 或覆盖 Settings 的 env 来源）。排查同类 503/配置类测试时都要留意这个陷阱。
