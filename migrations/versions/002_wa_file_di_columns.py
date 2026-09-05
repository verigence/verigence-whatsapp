"""002 add DI result columns to wa.file and intake_journey_id to wa.route

Revision ID: 002
Revises: 001
Create Date: 2025-01-01
"""
from __future__ import annotations
from pathlib import Path
from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None

_SQL_FILE = Path(__file__).parent.parent / "migrations" / "002_wa_file_di_columns.sql"


def upgrade() -> None:
    op.execute(_SQL_FILE.read_text())


def downgrade() -> None:
    op.execute("""
        ALTER TABLE wa.file
            DROP COLUMN IF EXISTS di_document_id,
            DROP COLUMN IF EXISTS document_type_key,
            DROP COLUMN IF EXISTS di_facts;
        ALTER TABLE wa.route
            DROP COLUMN IF EXISTS intake_journey_id;
        DROP INDEX IF EXISTS wa_file_needs_di_poll;
    """)
