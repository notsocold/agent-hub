# 测试

M1.5 已落地：

```
tests/
├── conftest.py                    # 测试 PostgreSQL、httpx ASGI Client、假模型、表清理、MCP 白名单
├── helpers.py                     # ScriptedToolModel（脚本化工具调用模型）与 MCP mock transport
├── test_security_unit.py          # 密码、JWT、游标、schema 单元测试（M1）
├── test_cursor_unit.py            # 游标编解码单元测试（M1）
├── test_capability_unit.py        # M1.5：zip 校验、SSRF、MCP 响应解析、工具名、凭据、提示词装配
├── test_config_knowledge_unit.py  # M4：占位符密钥识别、知识库/存储配置默认值
├── test_storage_unit.py           # M4：local/S3 双后端存储、路径穿越防护、原子写
├── test_knowledge_embedding_unit.py  # M4：fake/OpenAI embedding、占位符 key 回退
├── test_knowledge_parse_unit.py   # M4：md/txt/pdf/csv/json 解析与错误文案
├── test_knowledge_split_unit.py   # M4：Markdown 分层切分、overlap、尾块合并、截断
└── integration/
    ├── test_auth.py               # 注册/登录/刷新轮换/登出/改密（M1）
    ├── test_conversations.py      # 会话 CRUD、搜索、消息历史与计数（M1）
    ├── test_chat.py               # SSE 顺序、幂等、并发 409、取消、事件回放、系统信息（M1 已按 M1.5 更新）
    ├── test_pagination.py         # 分页回归（M1）
    ├── test_attachments.py        # 上传/下载/去重/越权/软删/文本注入模型上下文
    ├── test_instructions.py       # 默认值/覆盖/超长/恢复默认/审计/注入
    ├── test_memories.py           # CRUD/stats/q/导出/清空/越权/save_memory 工具与 memory.saved
    ├── test_skills.py             # 手写/zip 导入/文件读取/系统项只读/越权/skill.activated/read_skill_file
    ├── test_mcp.py                # CRUD/check/工具发现/审批策略/凭据不回显/SSRF/stdio 禁用/聊天调用/HITL/超时/降级
    └── test_approvals.py          # 重复决定 409、取消清理 pending、列表 stats
```

运行：

```bash
# 用本机已有 PostgreSQL 创建独立测试库（示例，按实际用户/端口修改）
createdb -h localhost -p 5432 -U postgres agent_hub_test

# 指向本机测试库；不配置时集成测试自动 skip
TEST_DATABASE_URL=postgresql+psycopg://devuser:devpass@localhost:54321/agent_hub_test \
  uv run pytest -q
```

约定：

- 没有可用 PostgreSQL 时集成测试自动 skip，单元测试仍可运行；
- 测试默认不依赖真实 LLM/Embedding API key，使用 `FakeStreamingChatModel`；工具循环用例用 `ScriptedToolModel`；
- 集成测试使用独立测试库，每个用例前 `create_all`、用例后 `TRUNCATE`；
  **注意**：`create_all` 不执行迁移，所以「测试全绿」不代表开发库已迁移（M4 联调踩过）；
- 知识库默认 fake embedding + local 存储，零网络零 S3；语义评测组用 `KNOWLEDGE_EVAL_REAL=1` 手动开启；
- 耗时类断言依赖系统时钟；已用 `app/core/timeutil.py::elapsed_ms()` 夹紧到 0，避免 NTP 回退导致负值；
- SSE 断言事件顺序与 `event:` 名称，不只断言最终文本；
- **ASGITransport 会缓冲整个 SSE 响应**：HITL 审批的实时交互（审批决定、取消）绕过 HTTP 层，
  用服务层 `start_chat` + `collect_run_events_live`（`tests/conftest.py`）验证；真实 uvicorn 下 SSE 实时流式；
- MCP 集成测试用 `register_test_transport(url, httpx.MockTransport)` 注入假服务器，
  `MCP_ALLOWED_HOSTS` 由 conftest 预置（`mcp.example.com,mcp.test`）；审批超时由 `APPROVAL_TIMEOUT_SECONDS=3` 缩短。
