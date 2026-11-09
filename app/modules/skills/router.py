"""技能路由。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, UploadFile

from app.core.deps import CurrentUser, DbSession
from app.modules.skills import service
from app.modules.skills.schemas import (
    SkillCatalogListOut,
    SkillCreateRequest,
    SkillDetailOut,
    SkillEnabledRequest,
    SkillFileContentOut,
    SkillFileOut,
    SkillImportResult,
    SkillListOut,
    SkillOut,
    SkillUpdateRequest,
)

router = APIRouter(prefix="/skills", tags=["技能"])


@router.get("", response_model=SkillListOut)
async def list_skills(user: CurrentUser, db: DbSession, q: str | None = None) -> SkillListOut:
    return await service.list_skills(db, user, q=q)


@router.get("/catalog", response_model=SkillCatalogListOut)
async def skill_catalog(user: CurrentUser, db: DbSession) -> SkillCatalogListOut:
    return await service.catalog(db, user)


@router.post("", response_model=SkillDetailOut, status_code=201)
async def create_skill(
    payload: SkillCreateRequest, user: CurrentUser, db: DbSession
) -> SkillDetailOut:
    return await service.create_skill(
        db, user, name=payload.name, description=payload.description, content=payload.content
    )


@router.post("/import", response_model=SkillImportResult, status_code=201)
async def import_skill(user: CurrentUser, db: DbSession, file: UploadFile) -> SkillImportResult:
    return await service.import_skill(db, user, file)


@router.get("/{skill_id}", response_model=SkillDetailOut)
async def get_skill(skill_id: uuid.UUID, user: CurrentUser, db: DbSession) -> SkillDetailOut:
    return await service.get_skill(db, user, skill_id)


@router.patch("/{skill_id}", response_model=SkillDetailOut)
async def update_skill(
    skill_id: uuid.UUID, payload: SkillUpdateRequest, user: CurrentUser, db: DbSession
) -> SkillDetailOut:
    return await service.update_skill(
        db,
        user,
        skill_id,
        name=payload.name,
        description=payload.description,
        content=payload.content,
    )


@router.delete("/{skill_id}", status_code=204)
async def delete_skill(skill_id: uuid.UUID, user: CurrentUser, db: DbSession) -> None:
    await service.delete_skill(db, user, skill_id)


@router.put("/{skill_id}/enabled", response_model=SkillOut)
async def set_skill_enabled(
    skill_id: uuid.UUID, payload: SkillEnabledRequest, user: CurrentUser, db: DbSession
) -> SkillOut:
    return await service.set_skill_enabled(db, user, skill_id, payload.enabled)


@router.get("/{skill_id}/files", response_model=list[SkillFileOut])
async def list_skill_files(
    skill_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> list[SkillFileOut]:
    return await service.list_files(db, user, skill_id)


@router.get("/{skill_id}/files/{path:path}", response_model=SkillFileContentOut)
async def read_skill_file(
    skill_id: uuid.UUID, path: str, user: CurrentUser, db: DbSession
) -> SkillFileContentOut:
    return await service.read_file(db, user, skill_id, path)
