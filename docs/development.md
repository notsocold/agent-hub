# 开发指南

> **当前仓库处于 M0 结构初始化**：没有实现代码、未安装依赖、未启动数据库。
> 本文描述的是后续编码时的目标流程与规范，命令当前不能直接执行。

## 1. 环境要求

| 工具 | 版本 | 说明 |
|---|---|---|
| Python | 3.13 | `uv` 会自动下载/使用 `.python-version` 指定的版本 |
| uv | ≥ 0.11 | 依赖与虚拟环境管理 |
| Docker + Compose | 任意近期版本 | 本地 PostgreSQL + pgvector |
| Node.js | 24 LTS | 前端 |
| pnpm | ≥ 10 | 前端包管理（统一使用 pnpm） |

## 2. 首次初始化（实现代码后）

```bash
cd /home/zhaodan/work/PythonProjects/agent-hub

cp .env.example .env
# 必填：LLM_API_KEY、EMBEDDING_API_KEY、JWT_SECRET（openssl rand -hex 32）

uv sync          # 安装依赖
docker compose up -d   # 启动 PostgreSQL 16 + pgvector（宿主机 5433）
uv run alembic upgrade head

uv run uvicorn app.main:app --reload
curl http://localhost:8000/healthz
```

验证数据库与扩展：

```bash
docker compose exec postgres psql -U agent_hub -d agent_hub \
  -c "SELECT extname FROM pg_extension;" \
  -c "\dn"
# 应看到 vector 扩展与 langgraph schema
```

## 3. 常用命令

> 当前仓库没有 `Makefile`（初始化阶段未提供），以下为后续使用的原始命令。
> 命令在实现代码、补上 `alembic/env.py` 后才可执行。

| 命令 | 作用 |
|---|---|
| `uv sync` | 安装/同步依赖 |
| `docker compose up -d` / `docker compose down` | 启动/停止 PostgreSQL（保留数据卷） |
| `uv run alembic upgrade head` | 应用迁移到最新 |
| `uv run alembic revision --autogenerate -m "说明"` | 生成 autogenerate 迁移 |
| `uv run uvicorn app.main:app --reload` | 开发服务器 |
| `uv run ruff check .` / `uv run ruff format .` | ruff 检查 / 格式化 |
| `uv run pytest` | pytest |
| `uv run alembic current` | 当前迁移版本 |
| `uv run alembic history` | 迁移历史 |

## 4. 配置管理

- 唯一入口：`app/core/config.py` 的 `Settings`；
- 业务代码禁止 `os.getenv`、禁止散落默认值；
- 新增配置项时同步更新 `.env.example` 与文档；
- 测试覆盖配置时调用 `get_settings.cache_clear()` 后重新获取；
- 密钥只放 `.env`/密钥管理服务，数据库只存 `secret_ref`。

## 5. 编码规范

### Python

- 行宽 100，ruff 规则见 `pyproject.toml`；
- 全量类型标注（`from __future__ import annotations`）；
- 公共函数/类写中文 docstring，说明「为什么」而非复述代码；
- 异常只抛领域异常（`DomainError` 子类），HTTP 边界统一转换；
- 不在 import 阶段做 IO（引擎、客户端、图都懒加载）；
- 时间统一 UTC，`datetime.now(UTC)`，禁止 naive datetime。

### 目录与分层

- `router.py`：参数解析、鉴权、调 service、返回 DTO；
- `service.py`：业务规则、事务边界，不依赖 FastAPI 对象；
- `repo.py`：持久化与复杂查询；
- `models` 统一在 `app/db/models/`，按领域分文件；
- `schemas` 不复用 ORM 模型，入参/出参分开。

### 新增业务模块的步骤

1. 在 `app/db/models/<domain>.py` 定义 ORM 模型，并在 `models/__init__.py` 导入；
2. `uv run alembic revision --autogenerate -m "add <domain>"`，检查生成的迁移（类型、索引、外键、默认值）；
3. 在 `app/modules/<name>/` 创建 `schemas.py`、`repo.py`（可选）、`service.py`、`router.py`；
4. 在 `app/api/v1/router.py` 注册路由；
5. 补测试（unit + integration），更新 `docs/api.md`；
6. 跑 `uv run ruff check . && uv run ruff format . && uv run pytest`。

### 新增 A2A 工具

1. 在 `app/agents/tools/` 新建工具模块，用 `@tool` 装饰并写清适用/不适用场景；
2. 工具必须是幂等的，或显式声明 `requires_approval=True`；
3. 超时、错误转换为可读文本，不把堆栈抛给模型；
4. 在 `app/agents/tools/__init__.py` 汇总；
5. 补工具单测（mock 外部依赖）。

## 6. 数据库迁移流程

```bash
# 1. 改模型
# 2. 生成迁移
uv run alembic revision --autogenerate -m "add conversations"

# 3. 必读生成文件，确认：
#    - 没有误删表/列
#    - 索引、外键命名符合约定
#    - server_default 与 NULL 约束正确
#    - 向量列类型为 vector(N)，不是 Text

# 4. 本地应用
uv run alembic upgrade head

# 5. 回滚验证（可选）
uv run alembic downgrade -1 && uv run alembic upgrade head

# 6. 提交迁移文件
```

约束：

- 已提交的迁移禁止修改；
- 破坏性变更拆成「加新列 → 双写 → 切读 → 删旧列」多个迁移；
- 大表索引使用 `CONCURRENTLY`（Alembic `autocommit_block()`）；
- 向量维度变更必须走新表方案（见 `docs/database.md` 5.3）。

## 7. A2A 本地联调

```bash
# 启动本服务（需先实现 app/main.py）
uv run uvicorn app.main:app --reload

# 查看 Agent Card（路径以实际路由为准）
curl -s http://localhost:8000/a2a/.well-known/agent-card.json | python3 -m json.tool

# 用官方 inspector 调试
# https://github.com/a2aproject/a2a-inspector
```

出站联调建议：

- 先起一个本地假 Agent（`tests/fixtures/fake_agent.py`，M2 提供），验证注册、工具生成、任务状态；
- 用 `A2A_ALLOWED_HOSTS=localhost,127.0.0.1` 放行本地地址（仅开发环境）；
- 远程调用日志包含 `a2a_task_id`、`context_id`、耗时、状态码。

## 8. 测试策略

| 层次 | 范围 | 依赖 |
|---|---|---|
| 单元 | schema、工具封装、状态映射、权限函数 | 无外部服务 |
| 集成 | FastAPI + 测试 PG + 假 LLM | Docker PG |
| 契约 | Agent Card schema、JSON-RPC 方法、SSE 事件 | 固定样本/假 Agent |
| E2E | 真实链路（可选） | 真模型 + 假远程 Agent |

约定：

- 默认不依赖真实 LLM/Embedding；用 `FakeListChatModel` 或 mock；
- 测试数据库独立 schema，跑完清理；
- SSE 断言事件序列，不只断言文本；
- 远程调用测试覆盖超时、5xx、非法响应、取消。

## 9. 调试

- 请求追踪：响应头 `X-Request-ID`，日志中同一 ID 可串起整条链路；
- 图调试：LangGraph Studio（可选）或 `graph.get_graph().draw_mermaid()`；
- SQL：`DB_ECHO=true` 打印语句（仅本地）；
- SSE：`curl -N -H "Accept: text/event-stream" ...` 观察原始帧；
- A2A：`a2a-inspector` + 保存原始 JSON-RPC 报文到 `tests/fixtures/`。

## 10. 常见问题

| 问题 | 原因 | 处理 |
|---|---|---|
| `psycopg.OperationalError` 连接失败 | 容器未启动/端口不对 | `docker compose up -d`；确认 5433 映射 |
| `extension "vector" is not available` | 镜像不是 pgvector 版 | 使用 `pgvector/pgvector:pg16` |
| `type "vector" does not exist` | 未执行 initdb 或未建扩展 | 重建数据卷或手动 `CREATE EXTENSION vector` |
| `psycopg` 连接串报 driver 错误 | 把 `postgresql+psycopg://` 直接给了 psycopg | 用 `settings.checkpoint_dsn`（已去掉驱动后缀） |
| checkpoint 表找不到 | search_path 未指向 `langgraph` | 连接参数 `options=-csearch_path=langgraph` |
| embedding 维度不匹配 | 模型维度与 `EMBEDDING_DIM` 不一致 | 改配置并重建向量表，不能原地 ALTER |
| `429 Too Many Requests` | 限流 | 调整限流配置或等待窗口 |
| SSE 无输出 | 反向代理缓冲 | Nginx `proxy_buffering off`、`X-Accel-Buffering: no` |

## 11. Git 约定

- 分支：`main` 保护，功能分支 `feat/<scope>`、修复 `fix/<scope>`；
- 提交信息：`<type>(<scope>): <summary>`，type 用 `feat/fix/docs/refactor/test/chore`；
- 一个提交只做一件事；迁移文件与模型改动放同一提交；
- 不提交 `.env`、密钥、数据库文件、生成物；
- 合并前必须通过 `uv run ruff check . && uv run pytest`。
