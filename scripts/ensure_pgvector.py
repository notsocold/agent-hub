"""运维脚本：确保共享 PostgreSQL 容器具备 pgvector 扩展（幂等）。

背景：本机 ``dev_postgres`` 来自 ``docker_work/docker-compose.yaml``，镜像是官方
``postgres:latest``，不带 pgvector；扩展装在容器可写层，容器一旦重建就会丢失，
而数据卷里的业务库仍在。本脚本把「重建后恢复」收敛成一条命令：

1. 校验容器存在且运行中；
2. 检查容器内 ``pg_available_extensions`` 是否已有 ``vector``；
3. 没有则 ``docker exec`` 安装 ``postgresql-<major>-pgvector``（走容器已配置的 pgdg 源）；
4. 对业务库与测试库执行 ``CREATE EXTENSION IF NOT EXISTS vector``。

全程不改数据、不重启 PostgreSQL，重复执行是安全的。

用法：
    uv run python scripts/ensure_pgvector.py
    uv run python scripts/ensure_pgvector.py --container dev_postgres
    uv run python scripts/ensure_pgvector.py --databases agent_hub agent_hub_test

注意：
- 数据库账号需要超级用户权限才能 ``CREATE EXTENSION``（``devuser`` 已是 superuser）；
- 扩展是库级对象，必须对每个库分别创建；
- 长期方案是把容器镜像换成 ``pgvector/pgvector:pg18``，届时本脚本会直接跳过安装步骤。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# 支持 ``python scripts/ensure_pgvector.py`` 直接运行。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.engine import make_url

from app.core.config import get_settings


def _run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, check=False, text=True, capture_output=True)
    if check and result.returncode != 0:
        raise SystemExit(f"命令执行失败：{' '.join(argv)}\n{result.stderr.strip()}")
    return result


def _psql(container: str, user: str, password: str, database: str, sql: str) -> str:
    """在容器内以指定账号执行单条 SQL，返回去空白后的标准输出。"""
    result = _run(
        [
            "docker",
            "exec",
            "-e",
            f"PGPASSWORD={password}",
            container,
            "psql",
            "-U",
            user,
            "-d",
            database,
            "-tAc",
            sql,
        ]
    )
    return result.stdout.strip()


def _ensure_container(container: str) -> None:
    result = _run(["docker", "inspect", "-f", "{{.State.Running}}", container], check=False)
    if result.returncode != 0:
        raise SystemExit(f"容器 {container!r} 不存在：{result.stderr.strip()}")
    if result.stdout.strip() != "true":
        raise SystemExit(f"容器 {container!r} 未运行，请先 docker start。")


def _vector_available(container: str, user: str, password: str, database: str) -> bool:
    out = _psql(
        container,
        user,
        password,
        database,
        "select 1 from pg_available_extensions where name = 'vector'",
    )
    return out == "1"


def _pg_major(container: str) -> str:
    output = _run(["docker", "exec", container, "pg_config", "--version"]).stdout
    match = re.search(r"PostgreSQL\s+(\d+)", output)
    if match is None:
        raise SystemExit(f"无法从 {output.strip()!r} 解析 PostgreSQL 主版本")
    return match.group(1)


def _install_pgvector(container: str, major: str) -> None:
    print(f"[1/2] 容器内缺少 vector 扩展，安装 postgresql-{major}-pgvector ……")
    _run(
        [
            "docker",
            "exec",
            "-u",
            "root",
            container,
            "bash",
            "-lc",
            "export DEBIAN_FRONTEND=noninteractive; "
            "apt-get update -qq && "
            f"apt-get install -y --no-install-recommends postgresql-{major}-pgvector",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="确保指定 PostgreSQL 容器具备 pgvector 扩展")
    parser.add_argument("--container", default="dev_postgres", help="容器名，默认 dev_postgres")
    parser.add_argument(
        "--databases",
        nargs="+",
        default=None,
        help="需要建扩展的库；默认取 DATABASE_URL 的库名及其 _test 副本",
    )
    args = parser.parse_args()

    settings = get_settings()
    if not settings.database_url:
        raise SystemExit("DATABASE_URL 为空，无法确定数据库连接信息。")

    url = make_url(settings.database_url)
    user = url.username or ""
    password = url.password or ""
    if not (user and password and url.database):
        raise SystemExit("DATABASE_URL 必须包含用户名、密码与库名。")

    databases = args.databases or [url.database, f"{url.database}_test"]

    _ensure_container(args.container)

    if _vector_available(args.container, user, password, databases[0]):
        print("[1/2] 容器内已提供 vector 扩展，跳过安装。")
    else:
        _install_pgvector(args.container, _pg_major(args.container))

    print(f"[2/2] 为 {', '.join(databases)} 创建扩展 ……")
    for database in databases:
        version = _psql(
            args.container,
            user,
            password,
            database,
            "create extension if not exists vector; "
            "select extversion from pg_extension where extname = 'vector'",
        )
        print(f"  {database}: vector {version.splitlines()[-1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
