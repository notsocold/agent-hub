"""m15 personal capability tables

Revision ID: 7f3a9c21b4d6
Revises: 45f5db481e8c
Create Date: 2026-11-09 23:00:00

M1.5 个人能力层：附件、技能、MCP、记忆、用户覆盖与工具调用轨迹。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "7f3a9c21b4d6"
down_revision: str | None = "45f5db481e8c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "attachments",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=128), server_default="", nullable=False),
        sa.Column("size_bytes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("checksum", sa.String(length=64), server_default="", nullable=False),
        sa.Column("storage_path", sa.String(length=512), server_default="", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="ready", nullable=False),
        sa.Column("purpose", sa.String(length=32), server_default="chat", nullable=False),
        sa.Column("text_excerpt", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "status IN ('processing', 'ready', 'failed', 'deleted')",
            name=op.f("ck_attachments_status"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_attachments_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_attachments_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_attachments")),
    )
    op.create_index(
        "ix_attachments_conversation_created", "attachments", ["conversation_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_attachments_user_checksum", "attachments", ["user_id", "checksum"], unique=False
    )
    op.create_index(
        "ix_attachments_user_created", "attachments", ["user_id", "created_at"], unique=False
    )

    op.create_table(
        "skills",
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("content", sa.Text(), server_default="", nullable=False),
        sa.Column("version", sa.String(length=32), server_default="1.0", nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=16), server_default="manual", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("force_disabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("estimated_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("size_bytes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("file_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
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
        sa.CheckConstraint("scope IN ('system', 'user')", name=op.f("ck_skills_scope")),
        sa.CheckConstraint(
            "scope = 'user' OR owner_user_id IS NULL", name=op.f("ck_skills_owner_rule")
        ),
        sa.CheckConstraint(
            "source IN ('builtin', 'upload', 'manual')", name=op.f("ck_skills_source")
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], name=op.f("fk_skills_owner_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skills")),
    )
    op.create_index("ix_skills_owner_enabled", "skills", ["owner_user_id", "enabled"], unique=False)
    op.create_index("ix_skills_scope_enabled", "skills", ["scope", "enabled"], unique=False)
    op.create_index(
        "uq_skills_system_slug",
        "skills",
        ["slug"],
        unique=True,
        postgresql_where=sa.text("scope = 'system'"),
    )
    op.create_index(
        "uq_skills_user_slug",
        "skills",
        ["owner_user_id", "slug"],
        unique=True,
        postgresql_where=sa.text("scope = 'user'"),
    )

    op.create_table(
        "skill_files",
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column("path", sa.String(length=512), nullable=False),
        sa.Column("content", sa.Text(), server_default="", nullable=False),
        sa.Column("size_bytes", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["skill_id"], ["skills.id"], name=op.f("fk_skill_files_skill_id_skills"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skill_files")),
    )
    op.create_index(
        "uq_skill_files_skill_path", "skill_files", ["skill_id", "path"], unique=True
    )

    op.create_table(
        "mcp_servers",
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("transport", sa.String(length=24), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("command", sa.String(length=512), nullable=True),
        sa.Column(
            "args",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "env_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "headers",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("credentials_hint", sa.String(length=160), server_default="", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("auto_inject", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("force_disabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("health_status", sa.String(length=16), server_default="unknown", nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("tools_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
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
        sa.CheckConstraint("scope IN ('system', 'user')", name=op.f("ck_mcp_servers_scope")),
        sa.CheckConstraint(
            "scope = 'user' OR owner_user_id IS NULL", name=op.f("ck_mcp_servers_owner_rule")
        ),
        sa.CheckConstraint(
            "transport IN ('streamable_http', 'sse', 'stdio')",
            name=op.f("ck_mcp_servers_transport"),
        ),
        sa.CheckConstraint(
            "(transport = 'stdio' AND command IS NOT NULL) OR "
            "(transport <> 'stdio' AND url IS NOT NULL)",
            name=op.f("ck_mcp_servers_transport_required"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], name=op.f("fk_mcp_servers_owner_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mcp_servers")),
    )
    op.create_index(
        "ix_mcp_servers_owner_enabled", "mcp_servers", ["owner_user_id", "enabled"], unique=False
    )
    op.create_index(
        "ix_mcp_servers_scope_enabled", "mcp_servers", ["scope", "enabled"], unique=False
    )
    op.create_index(
        "uq_mcp_servers_system_slug",
        "mcp_servers",
        ["slug"],
        unique=True,
        postgresql_where=sa.text("scope = 'system'"),
    )
    op.create_index(
        "uq_mcp_servers_user_slug",
        "mcp_servers",
        ["owner_user_id", "slug"],
        unique=True,
        postgresql_where=sa.text("scope = 'user'"),
    )

    op.create_table(
        "mcp_tools",
        sa.Column("server_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("exposed_name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "input_schema",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "annotations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("risk", sa.String(length=16), server_default="unknown", nullable=False),
        sa.Column("requires_approval", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("admin_only", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("user_allowed", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "discovered_at",
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
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "risk IN ('read', 'write', 'destructive', 'unknown')",
            name=op.f("ck_mcp_tools_risk"),
        ),
        sa.ForeignKeyConstraint(
            ["server_id"], ["mcp_servers.id"], name=op.f("fk_mcp_tools_server_id_mcp_servers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mcp_tools")),
    )
    op.create_index(
        "ix_mcp_tools_server_enabled", "mcp_tools", ["server_id", "enabled"], unique=False
    )
    op.create_index(
        "uq_mcp_tools_server_name", "mcp_tools", ["server_id", "name"], unique=True
    )

    op.create_table(
        "user_capability_overrides",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("capability", sa.String(length=16), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
            "capability IN ('mcp_server', 'skill')", name=op.f("ck_user_capability_overrides_capability")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_user_capability_overrides_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "capability", "target_id", name=op.f("pk_user_capability_overrides")),
    )

    op.create_table(
        "memories",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), server_default="fact", nullable=False),
        sa.Column("title", sa.String(length=200), server_default="", nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "source",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("pinned", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
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
        sa.CheckConstraint("kind IN ('profile', 'preference', 'fact')", name=op.f("ck_memories_kind")),
        sa.CheckConstraint(
            "status IN ('active', 'archived')", name=op.f("ck_memories_status")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_memories_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memories")),
    )
    op.create_index("ix_memories_user_kind", "memories", ["user_id", "kind"], unique=False)
    op.create_index(
        "ix_memories_user_status_updated",
        "memories",
        ["user_id", "status", sa.literal_column("updated_at DESC")],
        unique=False,
    )

    op.create_table(
        "tool_invocations",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("tool_call_id", sa.String(length=64), server_default="", nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("source", sa.String(length=16), server_default="native", nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column(
            "args",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.String(length=512), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "source IN ('native', 'mcp', 'a2a', 'knowledge', 'skill')",
            name=op.f("ck_tool_invocations_source"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'ok', 'error', 'rejected', 'canceled')",
            name=op.f("ck_tool_invocations_status"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_tool_invocations_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_invocations")),
    )
    op.create_index(
        "ix_tool_invocations_run_created",
        "tool_invocations",
        ["run_id", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_tool_invocations_source_created",
        "tool_invocations",
        ["source", sa.literal_column("created_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_tool_invocations_source_created", table_name="tool_invocations")
    op.drop_index("ix_tool_invocations_run_created", table_name="tool_invocations")
    op.drop_table("tool_invocations")
    op.drop_index("ix_memories_user_status_updated", table_name="memories")
    op.drop_index("ix_memories_user_kind", table_name="memories")
    op.drop_table("memories")
    op.drop_table("user_capability_overrides")
    op.drop_index("uq_mcp_tools_server_name", table_name="mcp_tools")
    op.drop_index("ix_mcp_tools_server_enabled", table_name="mcp_tools")
    op.drop_table("mcp_tools")
    op.drop_index("uq_mcp_servers_user_slug", table_name="mcp_servers")
    op.drop_index("uq_mcp_servers_system_slug", table_name="mcp_servers")
    op.drop_index("ix_mcp_servers_scope_enabled", table_name="mcp_servers")
    op.drop_index("ix_mcp_servers_owner_enabled", table_name="mcp_servers")
    op.drop_table("mcp_servers")
    op.drop_index("uq_skill_files_skill_path", table_name="skill_files")
    op.drop_table("skill_files")
    op.drop_index("uq_skills_user_slug", table_name="skills")
    op.drop_index("uq_skills_system_slug", table_name="skills")
    op.drop_index("ix_skills_scope_enabled", table_name="skills")
    op.drop_index("ix_skills_owner_enabled", table_name="skills")
    op.drop_table("skills")
    op.drop_index("ix_attachments_user_created", table_name="attachments")
    op.drop_index("ix_attachments_user_checksum", table_name="attachments")
    op.drop_index("ix_attachments_conversation_created", table_name="attachments")
    op.drop_table("attachments")
