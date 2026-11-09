"""运维脚本：按邮箱授予/回收管理员身份。

设计约定：注册流程不自动提权，管理员只能由运维人员显式授权。

用法：
    uv run python scripts/grant_admin.py you@example.com
    uv run python scripts/grant_admin.py you@example.com --revoke
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import func, select

from app.db.models.user import User
from app.db.session import dispose_engine, get_session_factory


async def _grant(email: str, revoke: bool) -> int:
    async with get_session_factory()() as session:
        user = await session.scalar(
            select(User).where(func.lower(User.email) == email.strip().lower())
        )
        if user is None:
            print(f"用户不存在：{email}", file=sys.stderr)
            return 1
        user.role = "user" if revoke else "admin"
        await session.commit()
        action = "回收" if revoke else "授予"
        print(f"已{action}管理员身份：{user.email}（role={user.role}）")
        return 0


async def _main(email: str, revoke: bool) -> int:
    try:
        return await _grant(email, revoke)
    finally:
        await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(description="授予/回收管理员身份")
    parser.add_argument("email", help="目标用户邮箱")
    parser.add_argument("--revoke", action="store_true", help="回收管理员身份")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args.email, args.revoke)))


if __name__ == "__main__":
    main()
