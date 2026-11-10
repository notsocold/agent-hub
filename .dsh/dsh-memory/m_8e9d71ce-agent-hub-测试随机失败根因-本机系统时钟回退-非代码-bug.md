---
id: "m_8e9d71ce"
title: "agent-hub 测试随机失败根因：本机系统时钟回退（非代码 bug）"
summary: "本机 NTP 校正导致系统时钟回退（200 次采样 70 次回退，最差 -494ms），造成 JWT iat 落在未来返回 401 与耗时为负断言失败；已用 JWT leeway + elapsed_ms 夹紧消除。"
kind: "project"
importance: 5
tags: ["agent-hub","testing","flake","jwt","environment"]
pinned: false
status: "active"
created: "2026-11-10T14:00:29.905Z"
modified: "2026-11-10T14:00:29.905Z"
revision: 1
origin: "auto"
sessionId: "session-3bc6169d-3a64-443d-9cef-444da4c4c01a"
sessionTitle: "开始 M4 阶段"
model: "deepseek-official/deepseek-flash"
---

现象：全量测试随机失败，位置不固定；失败形态是 401「登录状态无效，请重新登录」以及 duration 为负数的断言（assert -7...）。单独重跑常通过，无随机顺序插件。

排除项：CLI 测试真实 dispose 全局 engine 的假设被实验推翻（不含该用例的子集同样 flake）；teardown 里 TRUNCATE 与未结束后台 run 竞争的假设也被推翻（8 次仍失败 2 次）。

定位方法：401 实际来自 JWT 解码失败，在解码处加诊断（异常类型、secret 指纹、token 时间戳）后抓到 ImmatureSignatureError —— token 的 iat 落在未来；再对系统时钟做 200 次采样，确认回退 70 次、单次最多 -494ms（NTP 持续校正）。

修复约定：
- JWT 解码加 5 秒 leeway，容忍轻微时钟回退；
- 新增 elapsed_ms() 并把所有时间差夹紧到 0（a2a/taskstate.py 早已用 max(0, ...)，说明这是反复踩过的坑，按同一模式统一收口）；
- 效果：修复前 8 次全量 2 次失败，修复后连续 10 次全量 0 失败。

后续：在该机器上写时间差相关代码或断言时，默认时间可能回退，需夹紧到 0 而非假设单调递增；遇到随机 401 优先怀疑时钟而不是鉴权逻辑。
