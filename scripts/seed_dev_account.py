"""运维脚本：幂等地创建/重置本地开发账号。

用途：前端联调与自动化脚本复用固定账号，避免反复注册触发 ``/auth/register``
限流（默认 5 次/小时/IP）。

行为：
- 账号不存在 → 创建（密码 argon2 哈希）；
- 账号已存在 → 默认重置密码并确保 ``status=active``；加 ``--no-reset`` 则只读；
- ``--admin`` 同时把 ``role`` 设为 ``admin``；``--no-admin`` 保持/恢复为 ``user``。

用法：
    uv run python scripts/seed_dev_account.py
    uv run python scripts/seed_dev_account.py --email dev@example.com --password dev-password-123
    uv run python scripts/seed_dev_account.py --admin
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 支持 ``python scripts/seed_dev_account.py`` 直接运行。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select

from app.core.security import hash_password
from app.db.models.user import User
from app.db.session import dispose_engine, get_session_factory


async def _seed(
    email: str,
    password: str | None,
    display_name: str,
    *,
    reset_password: bool,
    admin: bool | None,
) -> int:
    normalized = email.strip().lower()
    async with get_session_factory()() as session:
        user = await session.scalar(select(User).where(func.lower(User.email) == normalized))
        created = user is None
        if user is None:
            if not password:
                print("账号不存在时必须提供 --password", file=sys.stderr)
                return 1
            user = User(
                email=normalized,
                password_hash=hash_password(password),
                display_name=display_name,
                status="active",
                role="admin" if admin else "user",
            )
            session.add(user)
        else:
            if reset_password and password:
                user.password_hash = hash_password(password)
            user.display_name = display_name
            user.status = "active"
            if admin is not None:
                user.role = "admin" if admin else "user"
        await session.commit()
        await session.refresh(user)

    action = "已创建" if created else "已更新"
    print(f"{action}开发账号：{user.email}（id={user.id}, role={user.role}, status={user.status}）")
    if reset_password and not created:
        print("密码已重置；已签发的 refresh token 不受影响，重新登录即可。")
    return 0


async def _main(args: argparse.Namespace) -> int:
    try:
        admin: bool | None = True if args.admin else (False if args.no_admin else None)
        return await _seed(
            args.email,
            args.password,
            args.display_name,
            reset_password=not args.no_reset,
            admin=admin,
        )
    finally:
        await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(description="幂等创建/重置本地开发账号")
    parser.add_argument(
        "--email", default="dev@example.com", help="账号邮箱（默认 dev@example.com）"
    )
    parser.add_argument(
        "--password", default="dev-password-123", help="账号密码（默认 dev-password-123）"
    )
    parser.add_argument("--display-name", default="本地开发账号", help="展示名")
    parser.add_argument("--no-reset", action="store_true", help="账号已存在时不重置密码")
    parser.add_argument("--admin", action="store_true", help="授予 admin 角色")
    parser.add_argument("--no-admin", action="store_true", help="确保为普通用户角色")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
