"""003 public /v1 API support: wa.contact.org_unit_id + wa.api_message

Revision ID: 003
Revises: 002
Create Date: 2026-10-24
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None

_SQL_FILE = Path(__file__).parent.parent / "003_wa_api.sql"


def upgrade() -> None:
    op.execute(_SQL_FILE.read_text())


def downgrade() -> None:
    op.execute("""
        DROP TABLE IF EXISTS wa.api_message;
        ALTER TABLE wa.contact DROP COLUMN IF EXISTS org_unit_id;
    """)
