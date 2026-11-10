"""m5 approvals tool_call_id

Revision ID: 2d8f6a1c3e70
Revises: 9c1d4e7a2b60
Create Date: 2026-11-10 23:50:00

M5 HITL：审批增加模型 tool_call_id，并建部分唯一索引作为恢复重放的幂等键。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "2d8f6a1c3e70"
down_revision: str | None = "9c1d4e7a2b60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("approvals", sa.Column("tool_call_id", sa.String(length=128), nullable=True))
    op.create_index(
        "ux_approvals_run_tool_call",
        "approvals",
        ["run_id", "tool_call_id"],
        unique=True,
        postgresql_where=sa.text("tool_call_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ux_approvals_run_tool_call", table_name="approvals")
    op.drop_column("approvals", "tool_call_id")
