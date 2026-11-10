"""批量导入知识库：递归遍历目录，复用 service 层的同一入库路径。

用法：
    python scripts/build_kb.py --source docs/kb --category 售后
    python scripts/build_kb.py --source docs/kb --owner admin@example.com --rebuild
    python scripts/build_kb.py --source docs/kb --ext .md,.txt --dry-run

行为约定：

- 文件以 POSIX 相对路径作为 ``filename`` / ``source_uri``，``--rebuild`` 据此定位旧文档；
- ``--owner`` 指向的用户不存在是**致命错误**（中文提示 + 非零退出），绝不静默写入系统文档；
- 省略 ``--owner`` 时写入系统文档（``owner_user_id IS NULL``）；
- 内容级失败由 service 落成 ``status="failed"``，脚本计入「失败」并打印中文原因；
- ``--dry-run`` 只读：解析 + 切分并打印计划，不写数据库、不写对象存储。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import sys
import uuid
from pathlib import Path

# 支持 ``python scripts/build_kb.py ...`` 直接运行。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import DomainError
from app.core.storage import get_storage
from app.db.models.knowledge import KnowledgeDocument
from app.db.models.user import User
from app.db.session import dispose_engine, get_session_factory
from app.modules.knowledge import repository
from app.modules.knowledge.parsing import parse_document
from app.modules.knowledge.service import ingest_upload
from app.modules.knowledge.splitting import split_text

logger = logging.getLogger(__name__)

# 默认支持的知识库扩展名；与 parsing._SUPPORTED_SUFFIXES 保持一致。
_DEFAULT_EXTS = frozenset({".md", ".markdown", ".txt", ".pdf", ".csv", ".json"})
_GENERIC_FAILURE = "文档处理失败，请检查文件内容或稍后重试"
_EMPTY_CHUNKS = "文档切分后没有可入库的片段"


class BuildKBError(RuntimeError):
    """脚本级致命错误（目录、扩展名或归属参数不合法），CLI 打印中文提示并非零退出。"""


# --------------------------------------------------------------------- 参数处理
def _normalize_exts(exts: set[str] | None) -> set[str]:
    """把扩展名统一成小写且带前导点；``None`` 表示默认支持的全部类型。"""
    if exts is None:
        return set(_DEFAULT_EXTS)
    normalized: set[str] = set()
    for ext in exts:
        value = (ext or "").strip().lower()
        if not value:
            continue
        normalized.add(value if value.startswith(".") else f".{value}")
    if not normalized:
        raise BuildKBError("扩展名列表不能为空")
    return normalized


def _collect_files(source: Path, suffixes: set[str]) -> list[Path]:
    """递归收集匹配扩展名的文件，按相对路径排序保证导入顺序可复现。"""
    files = [
        path for path in source.rglob("*") if path.is_file() and path.suffix.lower() in suffixes
    ]
    return sorted(files, key=lambda path: path.relative_to(source).as_posix())


async def _resolve_owner(db: AsyncSession, email: str | None) -> User | None:
    """按邮箱解析归属用户；未指定返回 ``None``（系统文档），查不到直接致命报错。"""
    if email is None:
        return None
    normalized = email.strip().lower()
    if not normalized:
        raise BuildKBError("--owner 不能为空邮箱")
    user = await db.scalar(select(User).where(func.lower(User.email) == normalized))
    if user is None:
        raise BuildKBError(f"用户不存在：{email}")
    return user


# --------------------------------------------------------------------- 重建清理
async def _delete_for_rebuild(
    db: AsyncSession, *, source_uri: str, owner_user_id: uuid.UUID | None
) -> None:
    """删除同一归属、同一 ``source_uri`` 的旧文档（分块随外键级联），再删原文对象。

    只删除同一归属域内的文档：系统文档与用户文档的 checksum 本来就是两个唯一域，
    重建时也不应误删其他用户导入的同名路径文档。先提交库事务再删对象存储，
    与 ``service.delete_document`` 的顺序一致，避免「库没删掉但文件已删」。
    """
    conditions = [KnowledgeDocument.source_uri == source_uri]
    if owner_user_id is None:
        conditions.append(KnowledgeDocument.owner_user_id.is_(None))
    else:
        conditions.append(KnowledgeDocument.owner_user_id == owner_user_id)
    stmt = (
        delete(KnowledgeDocument)
        .where(*conditions)
        .returning(KnowledgeDocument.storage_key)
        .execution_options(synchronize_session=False)
    )
    storage_keys = [key for key in (await db.execute(stmt)).scalars() if key]
    await db.commit()
    if not storage_keys:
        return
    storage = get_storage()
    for key in storage_keys:
        try:
            await storage.delete(key)
        except Exception:
            # 与 service.delete_document 一致：孤儿对象不应让整批导入失败。
            logger.warning("重建时删除旧原文失败，留下孤儿对象 key=%s", key, exc_info=True)


# --------------------------------------------------------------------- 输出辅助
def _failure_reason(exc: Exception) -> str:
    """把内容级异常转成中文原因；解析层的 ``ValueError`` 文案本身就是中文。"""
    if isinstance(exc, ValueError):
        return str(exc) or _GENERIC_FAILURE
    logger.exception("批量导入处理文档时出现未预期异常")
    return _GENERIC_FAILURE


def _record_failure(
    failures: list[tuple[str, str]], counts: dict[str, int], rel: str, reason: str
) -> None:
    counts["failed"] += 1
    failures.append((rel, reason))
    print(f"失败：{rel} - {reason}")


def _print_summary(
    counts: dict[str, int], failures: list[tuple[str, str]], *, dry_run: bool
) -> None:
    print()
    if dry_run:
        print("（试运行，未写入数据库与存储）")
    print(f"新增 {counts['created']} / 跳过 {counts['skipped']} / 失败 {counts['failed']}")
    if failures:
        print("失败明细：")
        for rel, reason in failures:
            print(f"  - {rel}：{reason}")


# ----------------------------------------------------------------------- 试运行
async def _dry_run_all(
    db: AsyncSession,
    *,
    source: Path,
    files: list[Path],
    owner_user_id: uuid.UUID | None,
    rebuild: bool,
) -> tuple[dict[str, int], list[tuple[str, str]]]:
    """只解析 + 切分并打印计划；只做只读查重，不写数据库、不落盘。"""
    settings = get_settings()
    counts = {"created": 0, "skipped": 0, "failed": 0}
    failures: list[tuple[str, str]] = []
    for path in files:
        rel = path.relative_to(source).as_posix()
        try:
            raw = path.read_bytes()
            parsed = parse_document(rel, raw, max_pdf_pages=settings.knowledge_max_pdf_pages)
            chunks = split_text(
                parsed.text,
                size=settings.knowledge_chunk_size,
                overlap=settings.knowledge_chunk_overlap,
                max_chunks=settings.knowledge_max_chunks_per_document,
            )
        except Exception as exc:
            _record_failure(failures, counts, rel, _failure_reason(exc))
            continue
        if not chunks:
            _record_failure(failures, counts, rel, _EMPTY_CHUNKS)
            continue

        if not rebuild:
            checksum = hashlib.sha256(raw).hexdigest()
            existing = await repository.find_by_checksum(
                db, owner_user_id=owner_user_id, checksum=checksum
            )
            if existing is not None:
                counts["skipped"] += 1
                print(f"[试运行] {rel}：内容已存在，将跳过")
                continue
        counts["created"] += 1
        print(f"[试运行] {rel}：将导入，切分 {len(chunks)} 块")
    return counts, failures


# ----------------------------------------------------------------------- 导入
async def build(
    source: Path,
    *,
    category: str = "",
    owner_email: str | None = None,
    exts: set[str] | None = None,
    rebuild: bool = False,
    dry_run: bool = False,
) -> dict[str, int]:
    """批量导入 ``source`` 目录，返回 ``{"created", "skipped", "failed"}`` 计数。

    - 归属用户由 ``owner_email`` 解析；查不到时抛 :class:`BuildKBError`，绝不降级为
      系统文档；
    - 内容级失败（解析/切分/向量化）由 service 落成 ``status="failed"``，这里计入
      ``failed`` 而不是 ``created``；
    - ``rebuild=True`` 时先删除同归属同 ``source_uri`` 的旧文档（含原文对象）再入库，
      因此文件内容变化后能真正重建索引；
    - ``dry_run=True`` 时只解析切分并打印计划，数据库与对象存储都不会被写入。
    """
    source = Path(source)
    if not source.exists():
        raise BuildKBError(f"目录不存在：{source}")
    if not source.is_dir():
        raise BuildKBError(f"不是目录：{source}")
    suffixes = _normalize_exts(exts)
    files = _collect_files(source, suffixes)
    counts = {"created": 0, "skipped": 0, "failed": 0}
    failures: list[tuple[str, str]] = []

    async with get_session_factory()() as session:
        owner = await _resolve_owner(session, owner_email)
        owner_user_id = owner.id if owner is not None else None

        if dry_run:
            counts, failures = await _dry_run_all(
                session,
                source=source,
                files=files,
                owner_user_id=owner_user_id,
                rebuild=rebuild,
            )
        else:
            for path in files:
                rel = path.relative_to(source).as_posix()
                try:
                    raw = path.read_bytes()
                except OSError as exc:
                    _record_failure(failures, counts, rel, f"读取文件失败：{exc}")
                    continue
                try:
                    if rebuild:
                        await _delete_for_rebuild(
                            session, source_uri=rel, owner_user_id=owner_user_id
                        )
                    document, created = await ingest_upload(
                        session,
                        owner=owner,
                        title=path.stem,
                        filename=rel,
                        category=category,
                        raw=raw,
                    )
                except DomainError as exc:
                    # 请求级校验失败（如扩展名不受支持、文件超过大小上限）：
                    # service 不建行，这里按单文件失败处理并继续导入其余文件。
                    await session.rollback()
                    _record_failure(failures, counts, rel, exc.message)
                    continue

                if not created:
                    counts["skipped"] += 1
                    print(f"跳过（重复）：{rel}")
                elif document.status == "failed":
                    _record_failure(failures, counts, rel, document.error or _GENERIC_FAILURE)
                else:
                    counts["created"] += 1
                    print(f"新增：{rel}（{document.chunk_count} 块）")

    _print_summary(counts, failures, dry_run=dry_run)
    return counts


# ------------------------------------------------------------------------- CLI
def _parse_exts(value: str | None) -> set[str] | None:
    """``--ext .md,.txt`` → ``{".md", ".txt"}``；未传返回 ``None`` 走默认集合。"""
    if value is None:
        return None
    return {item.strip() for item in value.split(",") if item.strip()}


async def _main(args: argparse.Namespace) -> int:
    """CLI 主体：无论成功失败都释放数据库引擎，保证进程干净退出。"""
    try:
        result = await build(
            args.source,
            category=args.category,
            owner_email=args.owner,
            exts=_parse_exts(args.ext),
            rebuild=args.rebuild,
            dry_run=args.dry_run,
        )
    except BuildKBError as exc:
        print(f"导入失败：{exc}", file=sys.stderr)
        return 1
    finally:
        await dispose_engine()
    # 有失败文件时返回非零，便于运维脚本/CI 感知部分失败。
    return 1 if result["failed"] else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="批量导入知识库文档（递归目录）")
    parser.add_argument("--source", required=True, type=Path, help="必填，待导入的目录（递归）")
    parser.add_argument("--category", default="", help="文档分类（默认空字符串）")
    parser.add_argument("--owner", default=None, help="归属用户邮箱；缺省 = 系统文档")
    parser.add_argument(
        "--ext",
        default=None,
        help="逗号分隔的扩展名，如 .md,.txt（默认 .md/.markdown/.txt/.pdf/.csv/.json）",
    )
    parser.add_argument(
        "--rebuild", action="store_true", help="先删除同 source_uri 的旧文档（含原文）再入库"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只解析并切分、打印导入计划，不写库不落盘"
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
