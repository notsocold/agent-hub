# 脚本目录

规划中的离线/运维脚本（不属于 Web 进程）：

| 脚本 | 用途 |
|---|---|
| `build_kb.py` | 扫描 `data/` 文档 → 切分 → Embedding → 写入 `knowledge_chunks`；支持 `--rebuild` |
| `register_agents.py` | 批量注册远程 A2A Agent（读取 YAML/JSON 清单） |
| `check_agents.py` | 巡检已注册 Agent 的 Agent Card 与健康状态 |
| `seed_demo.py` | 本地演示数据（演示用户、会话、示例 Agent） |
| `eval_rag.py` | 离线检索评测集：问题 → 期望来源，统计召回率 |
| `grant_admin.py` | 已实现：设置/撤销用户管理员角色（`users.role`），用于首个管理员引导 |
| `seed_dev_account.py` | 已实现：幂等创建/重置开发账号（默认 `dev@example.com`），绕开注册限流 |
| `export_openapi.py` | 已实现：导出 `docs/openapi.json`，作为前后端契约源 |

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
