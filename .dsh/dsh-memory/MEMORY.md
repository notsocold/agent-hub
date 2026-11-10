# dsh-memory · Project agent-hub

> Generated index. Topic files in this directory are the source of truth;
> edits here are overwritten. Edit a topic file or use Settings → Memory.

Project: `/home/zhaodan/work/PythonProjects/agent-hub`

Active topics: 31

- [agent-hub 测试随机失败根因：本机系统时钟回退（非代码 bug）](m_8e9d71ce-agent-hub-测试随机失败根因-本机系统时钟回退-非代码-bug.md) — 本机 NTP 校正导致系统时钟回退（200 次采样 70 次回退，最差 -494ms），造成 JWT iat 落在未来返回 401 与耗时为负断言失败；已用 JWT leeway + elapsed_ms 夹紧消除。 · `project` · importance 5 · 2026-11-10
- [agent-hub M4 知识库 RAG 范围与关键设计决策](m_63c9a26d-agent-hub-m4-知识库-rag-范围与关键设计决策.md) — M4 实现知识库 RAG：分类/owner/pgvector/双通道 embedding 基础上，v2 设计补齐原始文件落盘、存储双后端、fake 阈值置 0 与 scope 权限模型。 · `decision` · importance 5 · 2026-11-10
- [agent-hub 部署/联调前必须 alembic upgrade head，测试 create_all 会掩盖迁移缺失](m_4c883c07-agent-hub-部署-联调前必须-alembic-upgrade-head-.md) — 测试库用 create_all 建表，导致开发库未跑迁移时测试全绿但真实服务上传返回 500；部署前必须执行 alembic upgrade head。 · `project` · importance 4 · 2026-11-10
- [M4 知识库 RAG 已交付完成；剩余待办为语义评测证据与生产 Embedding 策略](m_a9aa6490-m4-知识库-rag-已交付完成-剩余待办为语义评测证据与生产-embeddin.md) — M4 知识库 RAG 后端（Task 9–14 + 全分支审查修复）已完成并停在可复看状态，剩余两项待用户裁决/补证据。 · `todo` · importance 4 · 2026-11-10
- [agent-hub M4 实施约定：不用工作树、沙箱依赖与错误语义 Ruling](m_0e1102ce-agent-hub-m4-实施约定-不用工作树-沙箱依赖与错误语义-ruling.md) — 用户关闭了工作树功能，后续直接在 master 实施；沙箱下用 UV_CACHE_DIR + .venv/bin/python 装依赖跑测试，并已定下错误语义、PDF/rebuild/fixture 等实施约定。 · `decision` · importance 4 · 2026-11-10
- [agent-hub 数据库环境：dev_postgres 容器已装 pgvector 0.8.7](m_67050ce2-agent-hub-数据库环境-dev-postgres-容器已装-pgvect.md) — agent-hub 用的 dev_postgres 容器是官方 postgres:latest（PG 18.4）原本没有 pgvector，已在容器内安装并在 agent_hub / agent_hub_test 两库建好扩展。 · `project` · importance 4 · 2026-11-10
- [agent-hub A2A：1.0 为主路径，0.3 兼容层有意保留，不清退](m_9591dea5-agent-hub-a2a-1-0-为主路径-0-3-兼容层有意保留-不清退.md) — 项目里残留的 0.3 相关内容属于显式、受测试覆盖的兼容能力（官方 a2a.compat.v0_3），目标是新功能默认走 1.0、0.3 仅做兼容，因此不做 1.0-only 清理。 · `decision` · importance 4 · 2026-11-10
- [agent-hub 选型准则：官方 SDK + 薄封装为默认，自研仅为例外](m_8346c8de-agent-hub-选型准则-官方-sdk-薄封装为默认-自研仅为例外.md) — A2A/MCP 一类协议选型默认采用官方 SDK 做协议层、自研只保留安全层薄封装；自研完整协议栈仅在 SDK 不满足硬约束时例外。 · `decision` · importance 4 · 2026-11-10
- [agent-hub A2A Card 拉取保留自研 httpx，SDK 仅做 schema 校验与传输](m_c5312293-agent-hub-a2a-card-拉取保留自研-httpx-sdk-仅做-s.md) — Card 拉取的安全边界（SSRF/同源/重定向/大小上限）由自有 httpx 实现负责，SDK 的 parse_agent_card 只用于规范 schema 校验。 · `decision` · importance 4 · 2026-11-10
- [agent-hub A2A 出站已迁移到 a2a-sdk 1.3.0 并消除版本偏离](m_d5e5b9b9-agent-hub-a2a-出站已迁移到-a2a-sdk-1-3-0-并消除版本.md) — 2026-11-10 出站协议层改用 a2a-sdk 1.3.0，并完成一轮全项目优化彻底清除文档/实现中的版本偏离残留。 · `decision` · importance 4 · 2026-11-10
- [agent-hub 仅做 A2A 出站：M3 入站取消、M4 顺延](m_f1716c7a-agent-hub-仅做-a2a-出站-m3-入站取消-m4-顺延.md) — 2026-11-10 需求变更：本服务只做 A2A 出站 Client，不发布 Agent Card、不提供 JSON-RPC 入站；M3 入站作废，M4 顺延；已完成收尾并提交。 · `decision` · importance 4 · 2026-11-10
- [agent-hub 前后端里程碑语义错位与跨仓库路线对照结论](m_e7a7259f-agent-hub-前后端里程碑语义错位与跨仓库路线对照结论.md) — 后端 agent-hub 与前端 agent-hub-web 的里程碑编号在 M3/M5 上语义冲突，工作流与管理台双方悬空，但接口契约本身已对齐。 · `project` · importance 4 · 2026-11-10
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
- [agent-hub M4 生产 Embedding 策略：缺 key 拒绝启动（偏离计划的 auto 回退 fake）](m_153cc36c-agent-hub-m4-生产-embedding-策略-缺-key-拒绝启动-.md) — 按设计 spec 实现为生产环境缺少 Embedding key 直接拒绝启动，与实施计划的「auto 一律回退 fake」冲突，属待用户裁决的显式偏离。 · `project` · importance 3 · 2026-11-10
- [agent-hub 前端仓库在 agent-hub-web，当前沙箱只能写后端](m_e8a8d720-agent-hub-前端仓库在-agent-hub-web-当前沙箱只能写后端.md) — 前端 Vue 代码位于 ~/work/NodeJsProjects/agent-hub-web，不在 agent-hub 工作区内，当前会话的文件写入权限只覆盖 agent-hub 后端仓库。 · `project` · importance 3 · 2026-11-10
- [agent-hub 路线图里程碑状态的标记规范与证据口径](m_1d16a11e-agent-hub-路线图里程碑状态的标记规范与证据口径.md) — docs/roadmap.md 用总览表+标题内联状态+checkbox 标记里程碑，勾选必须有实测证据，不虚报。 · `project` · importance 3 · 2026-11-10
- [agent-hub 仓库 .dsh 运行时文件被 Git 追踪的遗留状况](m_602c394b-agent-hub-仓库-dsh-运行时文件被-git-追踪的遗留状况.md) — agent-hub 曾把 .dsh 运行时数据（会话记录、项目记忆）提交进 Git，导致 .gitignore 对它们无效，且 .dsh/sessions/ 整目录未被忽略。 · `project` · importance 3 · 2026-11-10
- [agent-hub 500 request_id 走 request.state，注册/登录限流可配置并用 seed 脚本绕开](m_47794f6a-agent-hub-500-request-id-走-request-state.md) — 未处理异常会越过中间件的 contextvar 清理，因此 request_id 同时写入 request.state；注册/登录限流改为可配置，并用幂等脚本创建开发账号。 · `decision` · importance 3 · 2026-11-09
- [agent-hub 使用用户本机 PostgreSQL，不依赖 Docker](m_bf4efbaa-agent-hub-使用用户本机-postgresql-不依赖-docker.md) — agent-hub 后端默认使用用户本机已有 PostgreSQL、不自启任何容器；docker-compose 仅作为可选项保留。 · `project` · importance 3 · 2026-11-09
- [agent-hub 长度口径：API 用字符（指令 4000 字符），token 用保守系数做预算](m_0464a21e-agent-hub-长度口径-api-用字符-指令-4000-字符-token-.md) — 对外 API 一律以字符为单位（如指令上限 4000 字符），内部预算用保守系数估算 token，技能在导入时即校验 ≤8000 tokens。 · `decision` · importance 3 · 2026-11-05
