"""M4 知识库批量导入脚本（``scripts/build_kb.py``）集成测试。

用例使用真实数据库与真实本地存储（``data/test-knowledge``），向量为 conftest
强制的确定性 fake provider；断言覆盖计数、幂等、试运行、重建、归属与失败分层。
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.db.session as db_session
from app.db.models.knowledge import KnowledgeDocument
from scripts.build_kb import BuildKBError, _main, build

STORAGE_ROOT = Path("data/test-knowledge")


# --------------------------------------------------------------------- 测试辅助
async def _documents(db: AsyncSession) -> list[KnowledgeDocument]:
    rows = await db.execute(select(KnowledgeDocument).order_by(KnowledgeDocument.source_uri))
    return list(rows.scalars())


async def _document_count(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(KnowledgeDocument)) or 0)


def _stored_files() -> list[Path]:
    """本地存储里的原文文件；dry-run 后必须为空。"""
    if not STORAGE_ROOT.exists():
        return []
    return [path for path in STORAGE_ROOT.rglob("*") if path.is_file()]


def _cli_args(
    source: Path,
    *,
    category: str = "",
    owner: str | None = None,
    ext: str | None = None,
    rebuild: bool = False,
    dry_run: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        source=source,
        category=category,
        owner=owner,
        ext=ext,
        rebuild=rebuild,
        dry_run=dry_run,
    )


# ------------------------------------------------------------------- 导入与计数
async def test_build_imports_files_and_counts(db: AsyncSession, tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("# 退货\n\n退货需要保留吊牌。", encoding="utf-8")
    (tmp_path / "子目录").mkdir()
    (tmp_path / "子目录" / "b.txt").write_text("发货时效：24 小时内。", encoding="utf-8")
    # 未在 exts 中的文件不应被导入。
    (tmp_path / "忽略.csv").write_text("列名,值\n", encoding="utf-8")

    result = await build(tmp_path, category="售后", exts={".md", ".txt"})

    assert result == {"created": 2, "skipped": 0, "failed": 0}
    documents = await _documents(db)
    assert {document.source_uri for document in documents} == {"a.md", "子目录/b.txt"}
    assert all(document.owner_user_id is None for document in documents)
    assert all(document.status == "ready" for document in documents)
    assert all(document.category == "售后" for document in documents)
    assert all(document.chunk_count >= 1 for document in documents)
    assert {document.title for document in documents} == {"a", "b"}
    assert len(_stored_files()) == 2


async def test_build_skips_duplicate_content(db: AsyncSession, tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("# 标题\n\n内容。", encoding="utf-8")

    first = await build(tmp_path)
    second = await build(tmp_path)

    assert first == {"created": 1, "skipped": 0, "failed": 0}
    assert second == {"created": 0, "skipped": 1, "failed": 0}
    assert await _document_count(db) == 1
    assert len(_stored_files()) == 1


async def test_build_imports_for_owner(db: AsyncSession, tmp_path: Path, user: Any) -> None:
    (tmp_path / "a.md").write_text("# 标题\n\n内容。", encoding="utf-8")

    # 邮箱大小写不敏感，与 HTTP 注册/登录口径一致。
    result = await build(tmp_path, owner_email=user.email.upper())

    assert result["created"] == 1
    document = (await _documents(db))[0]
    assert document.owner_user_id == user.id


# ----------------------------------------------------------------------- 试运行
async def test_build_dry_run_writes_nothing(
    db: AsyncSession, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "a.md").write_text("# 标题\n\n内容。", encoding="utf-8")

    result = await build(tmp_path, dry_run=True)

    assert result == {"created": 1, "skipped": 0, "failed": 0}
    assert await _document_count(db) == 0
    assert _stored_files() == []
    out = capsys.readouterr().out
    assert "试运行" in out
    assert "a.md" in out
    assert "切分" in out


# ------------------------------------------------------------------------- 重建
async def test_build_rebuild_reimports_same_source_uri(db: AsyncSession, tmp_path: Path) -> None:
    path = tmp_path / "a.md"
    path.write_text("# 标题\n\n短内容。", encoding="utf-8")
    await build(tmp_path)
    old = (await _documents(db))[0]
    old_id, old_chunks, old_updated = old.id, old.chunk_count, old.updated_at

    # 改写文件内容与长度：重建后应换成一个新文档并保留同一条 source_uri。
    path.write_text(
        "# 第一节\n\n" + "第一节正文。" * 10 + "\n\n"
        "# 第二节\n\n" + "第二节正文。" * 10 + "\n\n"
        "# 第三节\n\n" + "第三节正文。" * 10,
        encoding="utf-8",
    )
    result = await build(tmp_path, rebuild=True)

    assert result == {"created": 1, "skipped": 0, "failed": 0}
    assert await _document_count(db) == 1
    new = (await _documents(db))[0]
    assert new.source_uri == "a.md"
    assert new.id != old_id
    assert new.chunk_count != old_chunks
    assert new.updated_at > old_updated
    # 旧原文对象已删除，不残留孤儿文件。
    assert len(_stored_files()) == 1


# ------------------------------------------------------------------- 归属错误
async def test_build_unknown_owner_raises_without_importing(
    db: AsyncSession, tmp_path: Path
) -> None:
    (tmp_path / "a.md").write_text("# 标题\n\n内容。", encoding="utf-8")

    with pytest.raises(BuildKBError, match="用户不存在"):
        await build(tmp_path, owner_email="nobody@example.com")

    # 绝不能静默退化成系统文档。
    assert await _document_count(db) == 0
    assert _stored_files() == []


# ------------------------------------------------------------------- 失败分层
async def test_build_parse_failure_counts_as_failed(
    db: AsyncSession, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "broken.md").write_bytes(b"\xff\xfe\x00bad")

    result = await build(tmp_path)

    assert result == {"created": 0, "skipped": 0, "failed": 1}
    documents = await _documents(db)
    assert len(documents) == 1
    document = documents[0]
    assert document.status == "failed"
    assert document.error is not None
    # 失败原因必须是可读中文（解析层文案），不能是英文异常原文。
    assert "UTF-8" in document.error
    assert any("\u4e00" <= char <= "\u9fff" for char in document.error)

    out = capsys.readouterr().out
    assert "失败 1" in out
    assert "broken.md" in out


# ----------------------------------------------------------------------- CLI
async def test_cli_unknown_owner_prints_chinese_and_disposes_engine(
    db: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    disposed: list[bool] = []
    original = db_session.dispose_engine

    async def _spy() -> None:
        disposed.append(True)
        await original()

    monkeypatch.setattr("scripts.build_kb.dispose_engine", _spy)

    code = await _main(_cli_args(tmp_path, owner="nobody@example.com", dry_run=True))

    assert code == 1
    # CLI 必须释放引擎，进程才能干净退出。
    assert disposed == [True]
    assert "用户不存在" in capsys.readouterr().err
