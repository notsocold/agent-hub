# 脚本目录

规划中的离线/运维脚本（不属于 Web 进程）：

| 脚本 | 用途 |
|---|---|
| `build_kb.py` | 扫描 `data/` 文档 → 切分 → Embedding → 写入 `knowledge_chunks`；支持 `--rebuild` |
| `register_agents.py` | 规划中：批量注册远程 A2A Agent（读取 YAML/JSON 清单）；`mock_langchain_agents.py` 已内置自动注册 |
| `check_agents.py` | 规划中：巡检已注册 Agent 的 Agent Card 与健康状态 |
| `seed_demo.py` | 规划中：本地演示数据（演示用户、会话、示例 Agent） |
| `eval_rag.py` | 规划中：离线检索评测集：问题 → 期望来源，统计召回率 |
| `mock_a2a_agent.py` | 已实现：本地假 A2A Agent（默认 9100），按任务文本触发 success/slow/fail/input_required/auth_required/streaming 场景 |
| `mock_langchain_agents.py` | 已实现：本地 LangChain 模拟智能体集群（默认 9101），一键启动并自动注册进 agent-hub |
| `grant_admin.py` | 已实现：设置/撤销用户管理员角色（`users.role`），用于首个管理员引导 |
| `seed_dev_account.py` | 已实现：幂等创建/重置开发账号（默认 `dev@example.com`），绕开注册限流 |
| `export_openapi.py` | 已实现：导出 `docs/openapi.json`，作为前后端契约源 |
| `cleanup.py` | 已实现：checkpoint/run_events/审计/附件/token 清理（支持 `--dry-run`/`--only`） |
| `load_test_sse.py` | 已实现：50 并发 SSE 压测 |
| `db_outage_drill.py` | 已实现：数据库短时不可用演练 |
| `graceful_shutdown_drill.py` | 已实现：优雅停机/双实例排水演练 |

脚本必须通过 `app.core.config.get_settings()` 读取配置，不重复实现环境变量解析。

## M6 压测与演练

```bash
# 50 并发 SSE（服务端需 RATE_LIMIT_ENABLED=false、LLM_PROVIDER=fake）
python scripts/load_test_sse.py --concurrency 50 --rounds 1 --json-out .tmp/m6-load.json

# 数据库短时不可用：TCP 代理切断后恢复，服务进程不重启
python scripts/db_outage_drill.py

# 优雅停机：两个 uvicorn 实例，先摘流量再 SIGTERM
python scripts/graceful_shutdown_drill.py
```

## LangChain 模拟智能体集群

`mock_langchain_agents.py` 在一个进程内启动多个用 LangChain 搭建的智能体，覆盖
agent-hub 出站客户端所需的 4 个 A2A 0.3 JSON-RPC 方法（`message/send`、
`message/stream`、`tasks/get`、`tasks/cancel`），供 agent-hub 真实调用；服务就绪后
自动登录并注册进 agent-hub。

```bash
# 前置：运行中的 agent-hub + 管理员账号（注册接口是管理员门禁）
uv run python scripts/seed_dev_account.py --admin

# 启动 5 个内置角色（代码审查/数据分析/翻译/技术文档/运维排障）并自动注册
uv run python scripts/mock_langchain_agents.py

# 不花 LLM 额度、纯离线跑通协议
uv run python scripts/mock_langchain_agents.py --fake-llm

# 自定义清单（复制示例改 slug/prompt）
uv run python scripts/mock_langchain_agents.py --config scripts/mock_agents.example.yaml

# 只注册（服务已在别的终端运行）、只启动（不注册）、注销清单里的智能体
uv run python scripts/mock_langchain_agents.py --register-only
uv run python scripts/mock_langchain_agents.py --no-register
uv run python scripts/mock_langchain_agents.py --unregister
```

- 默认监听 `127.0.0.1:9101`，每个智能体一个 path：`http://127.0.0.1:9101/agents/<slug>`；
- 智能体内部是 `langchain.agents.create_agent` + `ChatOpenAI`（读 `.env` 的
  `LLM_*`），可选内置工具 `calculator` / `current_time`；
- 任务文本里写 `#slow` 延迟 `--slow-seconds`（默认 3 秒）、写 `#fail` 返回 failed 任务，
  用于验证超时/失败链路；标记执行前会从提示词里剥离；
- 注册是幂等的（同 url+slug 返回已有记录），服务退出后注册记录保留；
- 注销按「slug + URL」双匹配，不会误删你自己注册的同名 Agent；换过端口的旧记录
  需要到管理台或 `DELETE /api/v1/agents/{id}` 手动删除；
- 幂等注册不会刷新已有 Card：改了 YAML 的 name/skills 后，先 `--unregister` 再启动，
  或调 `POST /api/v1/agents/{id}/refresh`；
- `--public-url` 用于容器/代理场景覆盖注册地址；`--token` 可跳过登录；
  `--no-health-check` 跳过注册后的逐个健康检查；
- 依赖：`PyYAML` 来自项目依赖链；清单也支持 JSON。

> 安全提示：mock 服务默认只绑定 `127.0.0.1` 且不带鉴权，仅用于本地联调；
> 不要用 `--host 0.0.0.0` 把它暴露到公网。
