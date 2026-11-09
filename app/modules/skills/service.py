"""技能业务：创建/导入/启停/文件读取、系统技能同步与聊天注入。"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import content_fingerprint, write_audit
from app.core.config import get_settings
from app.core.errors import (
    CapabilityDisabledError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationFailedError,
)
from app.db.models.mcp import UserCapabilityOverride
from app.db.models.skill import Skill, SkillFile
from app.db.models.user import User
from app.modules.skills.importer import estimate_tokens, parse_skill_zip
from app.modules.skills.schemas import (
    SkillCatalogListOut,
    SkillCatalogOut,
    SkillDetailOut,
    SkillFileContentOut,
    SkillFileOut,
    SkillImportResult,
    SkillListOut,
    SkillOut,
)

logger = logging.getLogger(__name__)

_SLUGIFY_PATTERN = re.compile(r"[^a-z0-9-]+")
_BUILTIN_DIR = Path(__file__).resolve().parents[2] / "agents" / "skills"


def _now() -> datetime:
    return datetime.now(UTC)


def _slugify(name: str) -> str:
    slug = _SLUGIFY_PATTERN.sub("-", name.lower()).strip("-")
    return (slug or "skill")[:64]


def _skill_out(skill: Skill, *, effective: bool) -> SkillOut:
    return SkillOut(
        id=skill.id,
        scope=skill.scope,  # type: ignore[arg-type]
        slug=skill.slug,
        name=skill.name,
        description=skill.description,
        version=skill.version,
        source=skill.source,
        enabled=skill.enabled,
        force_disabled=skill.force_disabled,
        effective_enabled=effective,
        estimated_tokens=skill.estimated_tokens,
        size_bytes=skill.size_bytes,
        file_count=skill.file_count,
        created_at=skill.created_at,
        updated_at=skill.updated_at,
    )


def _require_enabled() -> None:
    if not get_settings().skills_enabled:
        raise CapabilityDisabledError("技能能力未启用")


async def _effective_enabled(db: AsyncSession, skill: Skill, user: User | None) -> bool:
    if skill.force_disabled:
        return False
    if skill.scope == "user":
        return skill.enabled
    if user is None:
        return skill.enabled
    override = await db.scalar(
        select(UserCapabilityOverride.enabled).where(
            UserCapabilityOverride.user_id == user.id,
            UserCapabilityOverride.capability == "skill",
            UserCapabilityOverride.target_id == skill.id,
        )
    )
    return skill.enabled if override is None else override


async def list_skills(
    db: AsyncSession, user: User, *, q: str | None = None
) -> SkillListOut:
    _require_enabled()
    stmt = select(Skill).where(
        (Skill.scope == "system") | (Skill.owner_user_id == user.id)
    )
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(Skill.name.ilike(pattern) | Skill.description.ilike(pattern))
    rows = list((await db.execute(stmt.order_by(Skill.created_at.desc()))).scalars())
    stats = {"total": len(rows), "system": 0, "user": 0, "enabled": 0}
    items: list[SkillOut] = []
    for row in rows:
        effective = await _effective_enabled(db, row, user)
        stats[row.scope] += 1
        if effective:
            stats["enabled"] += 1
        items.append(_skill_out(row, effective=effective))
    return SkillListOut(items=items, stats=stats)


async def catalog(db: AsyncSession, user: User) -> SkillCatalogListOut:
    """输入框 @ 选择器用的精简目录（仅有效启用项）。"""
    settings = get_settings()
    if not settings.skills_enabled:
        raise CapabilityDisabledError("技能能力未启用")
    rows = list(
        (
            await db.execute(
                select(Skill)
                .where((Skill.scope == "system") | (Skill.owner_user_id == user.id))
                .order_by(Skill.scope.desc(), Skill.created_at.desc())
                .limit(settings.skill_catalog_max_items)
            )
        ).scalars()
    )
    items: list[SkillCatalogOut] = []
    for row in rows:
        effective = await _effective_enabled(db, row, user)
        if not effective:
            continue
        items.append(
            SkillCatalogOut(
                id=row.id,
                scope=row.scope,  # type: ignore[arg-type]
                slug=row.slug,
                name=row.name,
                description=row.description,
                effective_enabled=True,
                estimated_tokens=row.estimated_tokens,
            )
        )
    return SkillCatalogListOut(items=items)


async def _load_visible(db: AsyncSession, user: User, skill_id: uuid.UUID) -> Skill:
    _require_enabled()
    skill = await db.get(Skill, skill_id)
    if skill is None or (skill.scope == "user" and skill.owner_user_id != user.id):
        raise NotFoundError("技能不存在")
    return skill


async def _detail(db: AsyncSession, skill: Skill, user: User) -> SkillDetailOut:
    files = list(
        (
            await db.execute(
                select(SkillFile)
                .where(SkillFile.skill_id == skill.id)
                .order_by(SkillFile.path.asc())
            )
        ).scalars()
    )
    effective = await _effective_enabled(db, skill, user)
    return SkillDetailOut(
        **_skill_out(skill, effective=effective).model_dump(),
        content=skill.content,
        metadata=skill.metadata_ or {},
        files=[SkillFileOut(path=item.path, size_bytes=item.size_bytes) for item in files],
    )


async def get_skill(db: AsyncSession, user: User, skill_id: uuid.UUID) -> SkillDetailOut:
    skill = await _load_visible(db, user, skill_id)
    return await _detail(db, skill, user)


async def create_skill(
    db: AsyncSession,
    user: User,
    *,
    name: str,
    description: str,
    content: str,
) -> SkillDetailOut:
    _require_enabled()
    settings = get_settings()
    tokens = estimate_tokens(content)
    if tokens > settings.skill_max_body_tokens:
        raise ValidationFailedError(
            f"技能正文约 {tokens} tokens，超过 {settings.skill_max_body_tokens} 上限",
            code="skill_invalid",
        )
    slug = _slugify(name)
    base = slug
    for attempt in range(5):
        exists = await db.scalar(
            select(Skill.id).where(
                Skill.scope == "user", Skill.owner_user_id == user.id, Skill.slug == slug
            )
        )
        if exists is None:
            break
        suffix = hashlib.sha256(f"{attempt}".encode()).hexdigest()[:6]
        slug = f"{base[:56]}-{suffix}"
    skill = Skill(
        scope="user",
        owner_user_id=user.id,
        slug=slug,
        name=name,
        description=description,
        content=content,
        version="1.0",
        source="manual",
        enabled=True,
        estimated_tokens=tokens,
        size_bytes=len(content.encode()),
        file_count=0,
        metadata_={},
    )
    db.add(skill)
    await write_audit(
        db,
        actor_type="user",
        actor_id=str(user.id),
        action="skill.create",
        resource_type="skill",
        resource_id=str(skill.id),
        detail={"slug": slug, "length": len(content)},
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("技能 slug 已存在") from exc
    await db.refresh(skill)
    return await get_skill(db, user, skill.id)


async def import_skill(db: AsyncSession, user: User, file: UploadFile) -> SkillImportResult:
    _require_enabled()
    raw = await file.read(get_settings().skill_max_upload_bytes + 1)
    parsed = parse_skill_zip(raw)
    exists = await db.scalar(
        select(Skill.id).where(
            Skill.scope == "user", Skill.owner_user_id == user.id, Skill.slug == parsed.slug
        )
    )
    if exists is not None:
        raise ConflictError("同名技能已存在", details={"slug": parsed.slug})
    skill = Skill(
        scope="user",
        owner_user_id=user.id,
        slug=parsed.slug,
        name=parsed.name,
        description=parsed.description,
        content=parsed.content,
        version=parsed.version,
        source="upload",
        enabled=True,
        estimated_tokens=parsed.estimated_tokens,
        size_bytes=parsed.size_bytes,
        file_count=len(parsed.files),
        metadata_=parsed.metadata,
    )
    db.add(skill)
    await db.flush()
    for item in parsed.files:
        db.add(
            SkillFile(
                skill_id=skill.id,
                path=item.path,
                content=item.content,
                size_bytes=item.size_bytes,
            )
        )
    await write_audit(
        db,
        actor_type="user",
        actor_id=str(user.id),
        action="skill.import",
        resource_type="skill",
        resource_id=str(skill.id),
        detail={"slug": parsed.slug, "files": len(parsed.files), "ignored": parsed.ignored},
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("技能 slug 已存在") from exc
    await db.refresh(skill)
    detail = await get_skill(db, user, skill.id)
    return SkillImportResult(**detail.model_dump(), imported=True, ignored=parsed.ignored)


async def update_skill(
    db: AsyncSession,
    user: User,
    skill_id: uuid.UUID,
    *,
    name: str | None,
    description: str | None,
    content: str | None,
) -> SkillDetailOut:
    skill = await _load_visible(db, user, skill_id)
    if skill.scope != "user":
        raise ForbiddenError("系统技能不可修改")
    settings = get_settings()
    if content is not None:
        tokens = estimate_tokens(content)
        if tokens > settings.skill_max_body_tokens:
            raise ValidationFailedError(
                f"技能正文约 {tokens} tokens，超过 {settings.skill_max_body_tokens} 上限",
                code="skill_invalid",
            )
        skill.content = content
        skill.estimated_tokens = tokens
        skill.size_bytes = len(content.encode())
    if name is not None:
        skill.name = name.strip()
    if description is not None:
        skill.description = description.strip()
    await write_audit(
        db,
        actor_type="user",
        actor_id=str(user.id),
        action="skill.update",
        resource_type="skill",
        resource_id=str(skill_id),
        detail={"length": len(skill.content), "fingerprint": content_fingerprint(skill.content)},
    )
    await db.commit()
    await db.refresh(skill)
    return await get_skill(db, user, skill_id)


async def delete_skill(db: AsyncSession, user: User, skill_id: uuid.UUID) -> None:
    skill = await _load_visible(db, user, skill_id)
    if skill.scope != "user":
        raise ForbiddenError("系统技能不可删除")
    await db.delete(skill)
    await write_audit(
        db,
        actor_type="user",
        actor_id=str(user.id),
        action="skill.delete",
        resource_type="skill",
        resource_id=str(skill_id),
        detail={"slug": skill.slug},
    )
    await db.commit()


async def set_skill_enabled(
    db: AsyncSession, user: User, skill_id: uuid.UUID, enabled: bool
) -> SkillOut:
    skill = await _load_visible(db, user, skill_id)
    if skill.scope == "user":
        skill.enabled = enabled
        await write_audit(
            db,
            actor_type="user",
            actor_id=str(user.id),
            action="skill.enable",
            resource_type="skill",
            resource_id=str(skill_id),
            detail={"enabled": enabled},
        )
    else:
        override = await db.get(UserCapabilityOverride, (user.id, "skill", skill.id))
        if override is None:
            override = UserCapabilityOverride(
                user_id=user.id, capability="skill", target_id=skill.id, enabled=enabled
            )
            db.add(override)
        else:
            override.enabled = enabled
        await write_audit(
            db,
            actor_type="user",
            actor_id=str(user.id),
            action="capability.override",
            resource_type="skill",
            resource_id=str(skill_id),
            detail={"capability": "skill", "enabled": enabled},
        )
    await db.commit()
    await db.refresh(skill)
    effective = await _effective_enabled(db, skill, user)
    return _skill_out(skill, effective=effective)


async def list_files(
    db: AsyncSession, user: User, skill_id: uuid.UUID
) -> list[SkillFileOut]:
    skill = await _load_visible(db, user, skill_id)
    rows = list(
        (
            await db.execute(
                select(SkillFile)
                .where(SkillFile.skill_id == skill.id)
                .order_by(SkillFile.path.asc())
            )
        ).scalars()
    )
    return [SkillFileOut(path=item.path, size_bytes=item.size_bytes) for item in rows]


async def read_file(
    db: AsyncSession, user: User, skill_id: uuid.UUID, path: str
) -> SkillFileContentOut:
    skill = await _load_visible(db, user, skill_id)
    normalized = _normalize_skill_path(path)
    row = await db.scalar(
        select(SkillFile).where(SkillFile.skill_id == skill.id, SkillFile.path == normalized)
    )
    if row is None:
        raise NotFoundError("技能文件不存在")
    settings = get_settings()
    if row.size_bytes > settings.skill_file_read_max_bytes:
        raise ValidationFailedError(
            f"文件超过 {settings.skill_file_read_max_bytes // 1024}KB 读取上限",
            code="skill_file_too_large",
        )
    return SkillFileContentOut(
        skill_id=skill.id, path=row.path, content=row.content, size_bytes=row.size_bytes
    )


def _normalize_skill_path(path: str) -> str:
    path = path.replace("\\", "/")
    parts = [part for part in path.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ValidationFailedError("非法文件路径")
    return "/".join(parts)


# --------------------------------------------------------------------------- 系统技能同步


def _zip_from_dir(skill_dir: Path) -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(skill_dir.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(skill_dir).as_posix())
    return buffer.getvalue()


async def sync_builtin_skills(db: AsyncSession) -> int:
    """启动时把仓库内 ``app/agents/skills/<slug>/`` 幂等同步为系统技能。

    仓库中已删除的内置技能标记 ``force_disabled=true``（不物理删除，
    防止用户覆盖把下架技能复活）。
    """
    if not _BUILTIN_DIR.exists():
        return 0
    slugs_on_disk: set[str] = set()
    changed = 0
    for skill_dir in sorted(_BUILTIN_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            logger.warning("内置技能缺少 SKILL.md：%s", skill_dir.name)
            continue
        # 复用导入器解析，保证内置技能与用户上传同一条校验链。
        try:
            parsed = parse_skill_zip(_zip_from_dir(skill_dir))
        except Exception:
            logger.exception("内置技能解析失败：%s", skill_dir.name)
            continue
        slugs_on_disk.add(parsed.slug)
        existing = await db.scalar(
            select(Skill).where(Skill.scope == "system", Skill.slug == parsed.slug)
        )
        if existing is None:
            existing = Skill(
                scope="system",
                owner_user_id=None,
                slug=parsed.slug,
                name=parsed.name,
                description=parsed.description,
                content=parsed.content,
                version=parsed.version,
                source="builtin",
                enabled=True,
                force_disabled=False,
                estimated_tokens=parsed.estimated_tokens,
                size_bytes=parsed.size_bytes,
                file_count=len(parsed.files),
                metadata_=parsed.metadata,
            )
            db.add(existing)
            await db.flush()
            changed += 1
        else:
            updated = False
            for field_name, value in (
                ("name", parsed.name),
                ("description", parsed.description),
                ("content", parsed.content),
                ("version", parsed.version),
                ("estimated_tokens", parsed.estimated_tokens),
                ("size_bytes", parsed.size_bytes),
                ("file_count", len(parsed.files)),
            ):
                if getattr(existing, field_name) != value:
                    setattr(existing, field_name, value)
                    updated = True
            if updated:
                changed += 1
        await db.execute(delete(SkillFile).where(SkillFile.skill_id == existing.id))
        for item in parsed.files:
            db.add(
                SkillFile(
                    skill_id=existing.id,
                    path=item.path,
                    content=item.content,
                    size_bytes=item.size_bytes,
                )
            )
    rows = list((await db.execute(select(Skill).where(Skill.scope == "system"))).scalars())
    for row in rows:
        if row.slug not in slugs_on_disk and not row.force_disabled:
            row.force_disabled = True
            changed += 1
    await db.commit()
    return changed
