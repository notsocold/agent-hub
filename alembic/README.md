# Alembic 迁移目录（待初始化）

当前只有 `versions/.gitkeep`，**未包含** `env.py`、`script.py.mako`、`alembic.ini`。

后续开始编码时二选一：

1. 运行 `uv run alembic init alembic` 生成官方骨架，再改为异步引擎并接入 `app.core.config`；
2. 按 [数据库设计第 8 节](../docs/database.md) 的约定手写 `env.py`。

要求：

- 连接串从 `app.core.config.get_settings()` 读取，不写死在 `alembic.ini`；
- `target_metadata = Base.metadata`，并在 `env.py` 中导入 `app.db.models`；
- 迁移文件放 `versions/`，命名与破坏性变更规则见 `docs/database.md`。
