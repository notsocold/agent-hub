# Alembic 迁移目录

已包含 `alembic.ini`、异步 `env.py`、`script.py.mako` 与 M1 初始迁移。

要求：

- 连接串从 `app.core.config.get_settings()` 读取，不写死在 `alembic.ini`；
- `target_metadata = Base.metadata`，并在 `env.py` 中导入 `app.db.models`；
- 迁移文件放 `versions/`，命名与破坏性变更规则见 `docs/database.md`。
