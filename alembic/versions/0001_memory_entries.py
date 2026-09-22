"""建立 `memory_entries` 表（Phase 4）。

Revision ID: 0001_memory_entries
Revises: （首版）
Create Date: 2026-09-21

维度固定 1024（spec D2）：换 embedding 模型必须**同时**改这里、改
`infrastructure/db/models.py` 的 `EMBEDDING_DIM`、改 `MEMORY_EMBEDDING_DIM`，并重算历史向量。
`tests/unit/test_memory_migration.py` 会检查三处不漂移。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "0001_memory_entries"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 必须最先执行：纯净的 pgvector 镜像里连 vector 类型都还不存在，后面建表会直接失败
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "memory_entries",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False, server_default="agent"),
        sa.Column(
            "meta", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("embedding", Vector(1024), nullable=True),
        sa.Column("session_id", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_index(
        "ix_memory_entries_created_at", "memory_entries", [sa.text("created_at DESC")]
    )
    op.create_index("ix_memory_entries_session", "memory_entries", ["session_id"])
    # ivfflat + 余弦距离：必须与 long_term.py 查询里的 `<=>` 一致，否则索引不被使用
    op.create_index(
        "ix_memory_entries_embedding",
        "memory_entries",
        ["embedding"],
        postgresql_using="ivfflat",
        postgresql_ops={"embedding": "vector_cosine_ops"},
        postgresql_with={"lists": 100},
    )


def downgrade() -> None:
    op.drop_index("ix_memory_entries_embedding", table_name="memory_entries")
    op.drop_index("ix_memory_entries_session", table_name="memory_entries")
    op.drop_index("ix_memory_entries_created_at", table_name="memory_entries")
    op.drop_table("memory_entries")
    # 故意不 DROP EXTENSION vector：它可能被其它表/索引共用，删掉影响面超出本迁移
