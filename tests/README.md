# 测试规划

骨架阶段暂无测试代码。后续测试按以下层次组织：

```
tests/
├── conftest.py              # 事件循环、测试数据库、httpx AsyncClient、假模型
├── unit/                    # 纯函数、schema、工具封装、状态机
├── integration/             # FastAPI + 测试 PostgreSQL + 假 LLM
├── contract/                # A2A Agent Card / JSON-RPC 契约、SSE 事件格式
└── e2e/                     # 真实链路（可选，CI 中用假远程 Agent 服务）
```

约定：

- 测试默认不依赖真实 LLM/Embedding API key：用 `FakeListChatModel`、确定性 embedding 或 mock `VectorStore`。
- 集成测试用独立 schema/数据库，`alembic upgrade head` 后跑；禁止连开发库。
- A2A 出站测试启动一个本地 FastAPI 假 Agent（返回固定 Agent Card 与 Task 结果），验证超时、重试、取消、错误映射。
- SSE 测试断言事件顺序与 `event:` 名称，不只断言最终文本。
