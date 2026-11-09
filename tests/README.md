# 测试

M1 已落地：

```
tests/
├── conftest.py                    # 测试 PostgreSQL、httpx ASGI Client、假模型、表清理
├── test_security_unit.py          # 密码、JWT、游标、schema 单元测试
└── integration/
    ├── test_auth.py               # 注册/登录/刷新轮换/登出/改密
    ├── test_conversations.py      # 会话 CRUD、搜索、消息历史与计数
    └── test_chat.py               # SSE 顺序、幂等、并发 409、取消、事件回放、系统信息
```

运行：

```bash
# 用本机已有 PostgreSQL 创建独立测试库（示例，按实际用户/端口修改）
createdb -h localhost -p 5432 -U postgres agent_hub_test

# 指向本机测试库；不配置时集成测试自动 skip
TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/agent_hub_test \
  uv run pytest -q
```

约定：

- 没有可用 PostgreSQL 时集成测试自动 skip，单元测试仍可运行；
- 测试默认不依赖真实 LLM/Embedding API key，使用 `FakeStreamingChatModel`；
- 集成测试使用独立测试库，每个用例前 `create_all`、用例后 `TRUNCATE`；
- SSE 断言事件顺序与 `event:` 名称，不只断言最终文本；
- 取消/并发等场景绕过 httpx `ASGITransport`（它会缓冲整个响应），用真实 uvicorn 或服务层验证。
