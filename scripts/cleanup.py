"""数据清理 CLI。

用法：
    .venv/bin/python scripts/cleanup.py --dry-run
    .venv/bin/python scripts/cleanup.py --only audit,run_events
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.db.session import dispose_engine
from app.modules.maintenance.cleanup import CLEANUP_KINDS, run_all


def _parse_only(raw: str | None) -> set[str] | None:
    if not raw:
        return None
    only = {item.strip() for item in raw.split(",") if item.strip()}
    unknown = only - set(CLEANUP_KINDS)
    if unknown:
        raise SystemExit(f"未知清理类型：{', '.join(sorted(unknown))}")
    return only


async def _main(args: argparse.Namespace) -> int:
    get_settings().validate_runtime()
    try:
        results = await run_all(only=args.only, dry_run=args.dry_run)
        print(f"{'kind':<16}{'deleted':>10}  status  detail")
        for result in results:
            print(f"{result.kind:<16}{result.deleted:>10}  {result.status:<7} {result.detail}")
        return 1 if any(result.status == "error" for result in results) else 0
    finally:
        await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(description="agent-hub 数据清理")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不删除")
    parser.add_argument(
        "--only",
        default="",
        help=f"逗号分隔，可选：{','.join(CLEANUP_KINDS)}",
    )
    args = parser.parse_args()
    args.only = _parse_only(args.only)
    raise SystemExit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
