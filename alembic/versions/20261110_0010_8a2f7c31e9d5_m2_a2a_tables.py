"""m2 a2a outbound tables

Revision ID: 8a2f7c31e9d5
Revises: 7f3a9c21b4d6
Create Date: 2026-11-10 00:10:00

M2 A2A 出站：远程 Agent 注册表与 A2A 任务记录。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "8a2f7c31e9d5"
down_revision: str | None = "7f3a9c21b4d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "remote_agents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("protocol_version", sa.String(length=16), server_default="", nullable=False),
        sa.Column(
            "card",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "skills",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("auth_type", sa.String(length=16), server_default="none", nullable=False),
        sa.Column("auth_scheme", sa.String(length=32), server_default="none", nullable=False),
        sa.Column("auth_hint", sa.String(length=160), server_default="", nullable=False),
        sa.Column("secret_ref", sa.String(length=200), nullable=True),
        sa.Column(
            "headers",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "requires_approval", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "health_status", sa.String(length=16), server_default="unknown", nullable=False
        ),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_latency_ms", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("name_edited", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "auth_type IN ('none', 'api_key', 'oauth2')", name="auth_type"
        ),
        sa.CheckConstraint(
            "health_status IN ('unknown', 'healthy', 'degraded', 'unhealthy')",
            name="health_status",
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_remote_agents_owner", "remote_agents", ["owner_user_id"], unique=False
    )
    op.create_index(
        "ix_remote_agents_enabled_health",
        "remote_agents",
        ["enabled", "health_status"],
        unique=False,
    )
    op.create_index(
        "uq_remote_agents_slug_active",
        "remote_agents",
        ["slug"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "a2a_tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("a2a_task_id", sa.String(length=128), server_default="", nullable=False),
        sa.Column("remote_agent_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("tool_call_id", sa.String(length=64), server_default="", nullable=False),
        sa.Column("context_id", sa.String(length=128), nullable=True),
        sa.Column("state", sa.String(length=16), server_default="submitted", nullable=False),
        sa.Column("progress", sa.Float(), nullable=True),
        sa.Column(
            "request",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("input_request", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("auth_request", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "timeline",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('submitted', 'working', 'input_required', 'auth_required', "
            "'completed', 'failed', 'canceled', 'rejected')",
            name="state",
        ),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["remote_agent_id"], ["remote_agents.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_a2a_tasks_state_created",
        "a2a_tasks",
        ["state", sa.text("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_a2a_tasks_agent_created",
        "a2a_tasks",
        ["remote_agent_id", sa.text("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_a2a_tasks_run_created",
        "a2a_tasks",
        ["run_id", sa.text("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_a2a_tasks_conversation_created",
        "a2a_tasks",
        ["conversation_id", sa.text("created_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("a2a_tasks")
    op.drop_index("uq_remote_agents_slug_active", table_name="remote_agents")
    op.drop_index("ix_remote_agents_enabled_health", table_name="remote_agents")
    op.drop_index("ix_remote_agents_owner", table_name="remote_agents")
    op.drop_table("remote_agents")
