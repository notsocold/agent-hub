"""附件路由。"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, Query, UploadFile
from fastapi.responses import FileResponse

from app.core.deps import CurrentUser, DbSession
from app.modules.attachments import service
from app.modules.attachments.schemas import AttachmentOut, AttachmentPage, AttachmentPurpose

router = APIRouter(prefix="/attachments", tags=["附件"])

ConversationForm = Annotated[uuid.UUID | None, Form()]
PurposeForm = Annotated[AttachmentPurpose, Form()]


@router.post("", response_model=AttachmentOut, status_code=201)
async def upload_attachment(
    user: CurrentUser,
    db: DbSession,
    file: UploadFile,
    conversation_id: ConversationForm = None,
    purpose: PurposeForm = "chat",
) -> AttachmentOut:
    return await service.create_attachment(
        db, user, file, conversation_id=conversation_id, purpose=purpose
    )


@router.get("", response_model=AttachmentPage)
async def list_attachments(
    user: CurrentUser,
    db: DbSession,
    conversation_id: uuid.UUID | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
) -> AttachmentPage:
    return await service.list_attachments(
        db, user, conversation_id=conversation_id, limit=limit, cursor=cursor
    )


@router.get("/{attachment_id}", response_model=AttachmentOut)
async def get_attachment(attachment_id: uuid.UUID, user: CurrentUser, db: DbSession) -> AttachmentOut:
    return await service.get_attachment(db, user, attachment_id)


@router.delete("/{attachment_id}", status_code=204)
async def delete_attachment(attachment_id: uuid.UUID, user: CurrentUser, db: DbSession) -> None:
    await service.delete_attachment(db, user, attachment_id)


@router.get("/{attachment_id}/download")
async def download_attachment(attachment_id: uuid.UUID, user: CurrentUser, db: DbSession) -> FileResponse:
    path: Path = await service.open_attachment_file(db, user, attachment_id)
    filename = path.name.split("-", 1)[-1] if "-" in path.name else path.name
    # FileResponse 会把 filename 编码为 Content-Disposition: attachment; filename*=utf-8''…
    return FileResponse(path, media_type="application/octet-stream", filename=filename)
