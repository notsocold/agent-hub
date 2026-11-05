# LangGraph 领域组件约定

`app/agents/` 存放与 HTTP 无关的智能体编排逻辑，可被 `modules/chat`（用户对话）与 `modules/a2a`（A2A 服务端）复用。

## 规划文件

| 文件 | 职责 |
|---|---|
| `state.py` | 图状态定义：`MessagesState` 扩展 `user_id`、`conversation_id`、`run_id`、`pending_tasks`、`interrupt` 等字段 |
| `graph.py` | `build_graph()`：装配模型、工具、checkpointer、中断策略，返回编译后的图 |
| `prompts.py` | 基础 SYSTEM_PROMPT + 各能力区块装配：用户指令、记忆、技能目录/正文、工具说明 |
| `checkpointer.py` | 创建 `PostgresSaver`（生产）/`InMemorySaver`（测试），负责 schema 与连接 |
| `memory.py` | 长期记忆检索与 `save_memory` 工具封装（`AsyncPostgresStore`） |
| `skills.py` | 技能目录、本轮激活正文、`read_skill_file` 工具 |
| `tools/knowledge.py` | `search_knowledge`：调用 `VectorStore.search`，返回带来源的片段 |
| `tools/remote_agents.py` | 根据远程 Agent 注册表动态生成 LangChain 工具，内部走 A2A 客户端 |
| `tools/mcp.py` | MCP 工具桥接：`mcp__<slug>__<tool>`，含审批与超时 |
| `tools/__init__.py` | 汇总工具列表，供 `graph.py` 使用 |

## 状态与持久化

- 图状态通过 `thread_id = conversation_id` 与 LangGraph checkpointer 绑定，实现多轮记忆。
- 长期记忆走 `AsyncPostgresStore`（与 checkpoint 同 schema），按 `("memories", user_id)` 命名空间隔离；写入策略与检索由 `memory.py` 封装。
- 一次用户请求对应一条 `runs` 记录；图内的工具调用写入 `tool_invocations`，远程 A2A 调用写入 `a2a_tasks`，显式选择的技能/MCP 写入 `runs.capabilities`。
- 高风险动作（如远程 Agent 声明 `requires_approval`）通过 `interrupt()` 暂停，前端收到 `approval.required` 事件，由 `modules/approvals` 恢复执行。

## 工具设计原则

1. **工具描述即路由**：工具 docstring 写清楚适用场景与不适用场景，模型据此选择。
2. **幂等**：写操作工具必须支持幂等键（`run_id + tool_call_id`），避免重试造成重复副作用。
3. **超时与取消**：远程调用必须带超时、可取消，并把 A2A Task 状态映射为图内可读结果。
4. **可观测**：每次工具调用记录耗时、输入摘要、输出摘要、错误类型，敏感字段脱敏。
5. **不可信数据**：技能正文、记忆、MCP 输出与检索片段一律按数据包裹，禁止在其中执行指令或绕过审批。
