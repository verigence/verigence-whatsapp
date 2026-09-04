"""001 bootstrap wa and doc schemas

Revision ID: 001
Revises:
Create Date: 2025-01-01
"""
from __future__ import annotations
from pathlib import Path
from alembic import op

revision = "001"
down_revision = None
branch_labels = None
depends_on = None

_SQL = (Path(__file__).parent.parent / "migrations" / "001_wa_schema.sql").read_text()


def upgrade() -> None:
    op.execute(_SQL)


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS wa CASCADE; DROP SCHEMA IF EXISTS doc CASCADE;")
