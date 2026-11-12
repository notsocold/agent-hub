"""M9 步骤模板工作流：模板表与运行映射表。

- ``workflows``：步骤模板。系统模板（``scope='system'``、``owner_user_id IS NULL``）
  人人可读、由 DB/脚本创建；用户模板（``scope='user'``）归 owner 所有。
- ``workflow_runs``：一次模板运行与 chat run 的映射行（``run_id`` 全局唯一），
  创建时 ``running``，由聊天终态回调同步为终态（挂起审批期间保持 running）。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Workflow(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """步骤模板工作流；``PATCH`` 保存成功时 version 自增（``PUT`` 启停不改）。"""

    __tablename__ = "workflows"
    __table_args__ = (CheckConstraint("scope IN ('system', 'user')", name="scope"),)

    # 系统模板无归属（NULL）；用户模板随用户删除级联删除。
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    scope: Mapped[str] = mapped_column(
        String(16), nullable=False, default="user", server_default="user"
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    # 步骤模板数组（结构由装配器 validate_steps 校验，落库前已保证合法）。
    steps: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")


class WorkflowRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """工作流运行记录；终态同步见 ``sync_workflow_run_status``（T4）。"""

    __tablename__ = "workflow_runs"
    __table_args__ = (
        # 与 runs 状态机对齐（创建即 running，无 queued）；挂起审批期间映射行保持 running。
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'canceled', 'interrupted')",
            name="status",
        ),
        # 一次 chat run 至多映射一条工作流运行记录。
        Index("uq_workflow_runs_run", "run_id", unique=True),
        Index("ix_workflow_runs_workflow_created", "workflow_id", text("created_at DESC")),
    )

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False
    )
    # run 随会话/用户删除时映射行级联删除，避免孤儿记录。
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="running", server_default="running"
    )
