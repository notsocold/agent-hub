"""本地技能包（Skill）与参考文件。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Skill(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "skills"
    __table_args__ = (
        CheckConstraint("scope IN ('system', 'user')", name="scope"),
        CheckConstraint("source IN ('builtin', 'upload', 'manual')", name="source"),
        CheckConstraint("scope = 'user' OR owner_user_id IS NULL", name="owner_rule"),
        Index(
            "uq_skills_system_slug",
            "slug",
            unique=True,
            postgresql_where=text("scope = 'system'"),
        ),
        Index(
            "uq_skills_user_slug",
            "owner_user_id",
            "slug",
            unique=True,
            postgresql_where=text("scope = 'user'"),
        ),
        Index("ix_skills_owner_enabled", "owner_user_id", "enabled"),
        Index("ix_skills_scope_enabled", "scope", "enabled"),
    )

    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="1.0", server_default="1.0"
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="manual", server_default="manual"
    )
    enabled: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    force_disabled: Mapped[bool] = mapped_column(
        default=False, server_default=text("false"), nullable=False
    )
    estimated_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    size_bytes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    file_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )


class SkillFile(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "skill_files"
    __table_args__ = (Index("uq_skill_files_skill_path", "skill_id", "path", unique=True),)

    skill_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("skills.id", ondelete="CASCADE"), nullable=False
    )
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    size_bytes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
