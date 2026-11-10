---
id: "m_602c394b"
title: "agent-hub 仓库 .dsh 运行时文件被 Git 追踪的遗留状况"
summary: "agent-hub 曾把 .dsh 运行时数据（会话记录、项目记忆）提交进 Git，导致 .gitignore 对它们无效，且 .dsh/sessions/ 整目录未被忽略。"
kind: "project"
importance: 3
tags: ["git","gitignore","dsh","repo-hygiene"]
pinned: false
status: "active"
created: "2026-11-10T02:05:24.986Z"
modified: "2026-11-10T02:05:24.986Z"
revision: 1
origin: "auto"
sessionId: "session-f72adebe-dd2e-4cbc-8df8-e73189b3b24b"
sessionTitle: "Gitignore 忽略后仍显示更改"
model: "deepseek-official/deepseek-flash"
---

agent-hub 仓库的 .dsh/ 目录中曾有一批文件被 Git 追踪（早期某次 `git add -A` 导致），使 .gitignore 规则对它们完全失效——典型症状是「.gitignore 明明写了却仍在 VS Code 源代码管理里显示修改」。

已处理：`.dsh/workspace-session.json` 通过 `git rm --cached` + 提交取消追踪，本地文件保留，.gitignore 规则从此生效。

仍遗留（用户当时选择最小范围，未处理）：
1. 8 个 `.dsh/sessions/*/session.v4.jsonl` 完整对话流水、17 个 `.dsh/dsh-memory/*.md` 项目记忆仍在版本库中。会话流水可能含敏感信息，仅取消追踪不能清除历史，需 rewrite 才可彻底移除。
2. `.dsh/sessions/` 整目录没有被忽略（规则只挡 `session.lock`、`*.tmp`、`.backup/`），所以每开一个新会话就会出现新的未追踪目录，需要时补一条忽略规则。
3. `.gitignore` 中 `.dsh/workspace-session.json` 规则重复写了两遍（第 50 与 54 行），无害但冗余。
4. `.dsh/dsh-memory/` 是否入库是设计选择（跟仓库走以共享团队），不是 bug。

排查口诀：`git ls-files --error-unmatch <路径>` 有输出即「已追踪」，需 `git rm --cached`；无输出才去查规则写法。
