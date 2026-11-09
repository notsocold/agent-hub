# dsh-memory · Project agent-hub

> Generated index. Topic files in this directory are the source of truth;
> edits here are overwritten. Edit a topic file or use Settings → Memory.

Project: `/home/zhaodan/work/PythonProjects/agent-hub`

Active topics: 15

- [agent-hub 分页契约：cursor 编码层做 JSON-safe 归一化，cursor 优先于 before](m_a0c1601b-agent-hub-分页契约-cursor-编码层做-json-safe-归一化.md) — 分页 500 的根因是 encode_cursor 直接 json.dumps ORM UUID，修复定在编码层统一归一化，并明确 cursor 与 before 同时传入时 cursor 优先。 · `decision` · importance 4 · 2026-11-09
- [agent-hub 前端交接/联调文档与契约修复要点](m_62b3826d-agent-hub-前端交接-联调文档与契约修复要点.md) — agent-hub 与 agent-hub-web 的交接/反馈/热修文档位置，以及已对齐的契约要点（cursor 分页、错误信封、seed 账号、OpenAPI 声明）。 · `reference` · importance 4 · 2026-11-09
- [agent-hub 后端 M1 关键实现约束：SSE 解耦、幂等回放、并发 409](m_11494769-agent-hub-后端-m1-关键实现约束-sse-解耦-幂等回放-并发-40.md) — SSE 与图执行解耦、client_message_id 幂等回放、未结束 run 用部分唯一索引兜底，是 M1 后端的既定实现约束。 · `decision` · importance 4 · 2026-11-09
- [agent-hub 本轮后端范围收敛到 M1 对话最小闭环](m_f0cacd8c-agent-hub-本轮后端范围收敛到-m1-对话最小闭环.md) — M1 是 M1.5、M2–M6 的硬前提，先与前端联调；工作流与管理控制台/RBAC 延后到 M5+。 · `decision` · importance 4 · 2026-11-09
- [agent-hub 系统 MCP 对普通用户只开放只读工具，写工具仅管理员](m_2ac1ede0-agent-hub-系统-mcp-对普通用户只开放只读工具-写工具仅管理员.md) — 按方案 A 定稿：系统 MCP 的写/破坏性工具仅管理员可用，普通用户只能看到并调用只读工具；用户自建 MCP 的写工具由本人审批。 · `decision` · importance 4 · 2026-11-05
- [agent-hub A2A 入站请求用 CapabilityContext(user_id=None) 隔离用户级上下文](m_e52f35a6-agent-hub-a2a-入站请求用-capabilitycontext-us.md) — A2A 入站调用构造无用户身份的 CapabilityContext，不注入任何用户级指令、记忆、技能与 MCP。 · `decision` · importance 4 · 2026-11-05
- [agent-hub 管理员引导用 scripts/grant_admin.py，系统项用 force_disabled 硬停用](m_ca134ea1-agent-hub-管理员引导用-scripts-grant-admin-py-.md) — 管理员身份改为运维脚本显式授权（不做注册邮箱自动提权），并新增 force_disabled 让管理员硬停用系统项、压过用户覆盖。 · `decision` · importance 4 · 2026-11-05
- [agent-hub 迭代计划基线：M1.5 以 M1 完成为前提，部分能力提前最小切片](m_40e181ee-agent-hub-迭代计划基线-m1-5-以-m1-完成为前提-部分能力提前最.md) — agent-hub 的 M1.5 里程碑依赖 M1 完成，并把 tool_invocations、audit_logs、approvals 与共享 SSRF 校验作为最小切片提前落地，M2/M5 范围不变。 · `decision` · importance 4 · 2026-11-05
- [agent-hub 长期记忆存储复用 LangGraph Store（AsyncPostgresStore），不自研存储](m_fb420a74-agent-hub-长期记忆存储复用-langgraph-store-async.md) — agent-hub 的长期记忆直接用 LangGraph Store 的 Postgres 实现做存储层，插件只管写入策略与 REST 管理，表结构由框架 setup() 管理，不走 Alembic。 · `decision` · importance 4 · 2026-11-05
- [agent-hub Skill v1 定为纯指令包，可执行脚本列为后续方案](m_638f250d-agent-hub-skill-v1-定为纯指令包-可执行脚本列为后续方案.md) — agent-hub 的 Skill 能力第一阶段只做纯指令包，可执行脚本（含沙箱、资源限制、网络隔离等前置条件）单列为后续第二方案，不混入 v1 范围。 · `decision` · importance 4 · 2026-11-05
- [agent-hub MCP 接入边界：内置可 stdio，用户自建仅远程 URL](m_c9d2ab0a-agent-hub-mcp-接入边界-内置可-stdio-用户自建仅远程-url.md) — agent-hub 的 MCP 支持定为：系统内置 MCP 允许 stdio 传输，用户自建 MCP 只允许远程 URL，stdio 开关默认关闭。 · `decision` · importance 4 · 2026-11-05
- [agent-hub 需求：MCP、Skill、长期记忆、Instruction 四大能力 + 两层配置模型](m_869f2af8-agent-hub-需求-mcp-skill-长期记忆-instruction-.md) — agent-hub 本阶段做四大能力（MCP、Skill、长期记忆、Instruction），配置为账号级并由「系统默认 + 用户自定义」两层在运行时合并，会话内可显式点名使用 Skill/MCP。 · `project` · importance 4 · 2026-11-05
- [agent-hub 500 request_id 走 request.state，注册/登录限流可配置并用 seed 脚本绕开](m_47794f6a-agent-hub-500-request-id-走-request-state.md) — 未处理异常会越过中间件的 contextvar 清理，因此 request_id 同时写入 request.state；注册/登录限流改为可配置，并用幂等脚本创建开发账号。 · `decision` · importance 3 · 2026-11-09
- [agent-hub 使用用户本机 PostgreSQL，不依赖 Docker](m_bf4efbaa-agent-hub-使用用户本机-postgresql-不依赖-docker.md) — agent-hub 后端默认使用用户本机已有 PostgreSQL、不自启任何容器；docker-compose 仅作为可选项保留。 · `project` · importance 3 · 2026-11-09
- [agent-hub 长度口径：API 用字符（指令 4000 字符），token 用保守系数做预算](m_0464a21e-agent-hub-长度口径-api-用字符-指令-4000-字符-token-.md) — 对外 API 一律以字符为单位（如指令上限 4000 字符），内部预算用保守系数估算 token，技能在导入时即校验 ≤8000 tokens。 · `decision` · importance 3 · 2026-11-05
