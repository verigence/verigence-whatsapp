"""Session state machine and debounce timer.

State flow (VAC-WA-SD-001 §4.2):
  collecting → processing (debounce expired, worker flush)
  processing → confirming_deal (cold start, awaiting PC confirmation)
  confirming_deal → gaps_pending (PC confirmed, evidence linked)
  gaps_pending → complete (all blocking items satisfied)
  any → parked / escalated (SLA breach, idle, unresolvable)

The debounce timer lives on the session row (flush_at), never in memory,
so it survives pod restarts.
"""
from __future__ import annotations

from uuid import UUID

import structlog
from sqlalchemy import Connection, text

logger = structlog.get_logger(__name__)


def get_or_create_session(
    conn: Connection,
    *,
    tenant_id: UUID,
    contact_id: UUID,
    org_unit_id: UUID,
) -> dict:
    """Return the current open session or create a new one."""
    row = conn.execute(
        text("""
            SELECT id, state, deal_id, file_count, flush_at, created_at
            FROM wa.session
            WHERE contact_id = :cid
              AND state IN ('collecting','confirming_deal','processing')
        """),
        {"cid": contact_id},
    ).mappings().one_or_none()
    if row is not None:
        return dict(row)

    row = conn.execute(
        text("""
            INSERT INTO wa.session
                (tenant_id, contact_id, org_unit_id, state, flush_at, expires_at)
            VALUES
                (:tenant_id, :cid, :org_unit_id, 'collecting',
                 now() + interval '90 seconds',
                 now() + interval '7 days')
            RETURNING id, state, deal_id, file_count, flush_at, created_at
        """),
        {"tenant_id": tenant_id, "cid": contact_id, "org_unit_id": org_unit_id},
    ).mappings().one()
    logger.info("wa_session_created", session_id=str(row["id"]), contact_id=str(contact_id))
    return dict(row)


def record_file_arrival(conn: Connection, *, session_id: UUID, byte_size: int) -> None:
    """Push debounce timer forward and increment file counter."""
    conn.execute(
        text("""
            UPDATE wa.session
               SET flush_at    = now() + interval '90 seconds',
                   file_count  = file_count + 1,
                   bytes_total = bytes_total + :bytes
             WHERE id = :sid AND state = 'collecting'
        """),
        {"sid": session_id, "bytes": byte_size},
    )


def register_file(
    conn: Connection,
    *,
    tenant_id: UUID,
    session_id: UUID,
    wamid: str,
    media_id: str,
    received_seq: int,
    wa_timestamp,
    kind: str,
    fidelity: str,
    declared_mime: str | None,
    declared_name: str | None,
    caption: str | None,
    meta_sha256: str | None,
    media_expires_at,
) -> UUID:
    row = conn.execute(
        text("""
            INSERT INTO wa.file
                (tenant_id, session_id, wamid, media_id, received_seq, wa_timestamp,
                 kind, fidelity, declared_mime, declared_name, caption,
                 meta_sha256, media_expires_at)
            VALUES
                (:tenant_id, :session_id, :wamid, :media_id, :received_seq, :wa_ts,
                 :kind, :fidelity, :mime, :name, :caption, :meta_sha256, :expires_at)
            ON CONFLICT (wamid) DO NOTHING
            RETURNING id
        """),
        {
            "tenant_id": tenant_id, "session_id": session_id, "wamid": wamid,
            "media_id": media_id, "received_seq": received_seq, "wa_ts": wa_timestamp,
            "kind": kind, "fidelity": fidelity, "mime": declared_mime,
            "name": declared_name, "caption": caption,
            "meta_sha256": meta_sha256, "expires_at": media_expires_at,
        },
    ).scalar_one_or_none()
    if row is None:
        row = conn.execute(
            text("SELECT id FROM wa.file WHERE wamid = :w"), {"w": wamid}
        ).scalar_one()
    return UUID(str(row))


def claim_sessions_for_flush(conn: Connection, *, limit: int = 50) -> list[UUID]:
    """Atomically claim collecting sessions whose debounce has expired."""
    rows = conn.execute(
        text("""
            UPDATE wa.session SET state = 'processing', submitted_at = now()
             WHERE id IN (
                 SELECT id FROM wa.session
                  WHERE state = 'collecting' AND flush_at < now()
                  ORDER BY flush_at
                  FOR UPDATE SKIP LOCKED
                  LIMIT :limit
             )
            RETURNING id
        """),
        {"limit": limit},
    ).fetchall()
    ids = [UUID(str(r[0])) for r in rows]
    if ids:
        logger.info("wa_sessions_claimed_for_flush", count=len(ids))
    return ids


def transition_to_confirming(conn: Connection, *, session_id: UUID, deal_id: UUID) -> None:
    conn.execute(
        text("""
            UPDATE wa.session
               SET state = 'confirming_deal',
                   deal_id = :deal_id,
                   flush_at = now() + interval '24 hours'
             WHERE id = :sid AND state = 'processing'
        """),
        {"sid": session_id, "deal_id": deal_id},
    )
    logger.info("wa_session_confirming_deal", session_id=str(session_id), deal_id=str(deal_id))


def confirm_deal(conn: Connection, *, session_id: UUID) -> None:
    conn.execute(
        text("""
            UPDATE wa.session
               SET state = 'gaps_pending',
                   flush_at = now() + interval '24 hours'
             WHERE id = :sid AND state = 'confirming_deal'
        """),
        {"sid": session_id},
    )


def mark_complete(conn: Connection, *, session_id: UUID) -> None:
    conn.execute(
        text("UPDATE wa.session SET state = 'complete', completed_at = now() WHERE id = :sid"),
        {"sid": session_id},
    )


def park_session(conn: Connection, *, session_id: UUID, note: str) -> None:
    conn.execute(
        text("""
            UPDATE wa.session SET state = 'parked', note = :note
             WHERE id = :sid AND state NOT IN ('complete','cancelled')
        """),
        {"sid": session_id, "note": note},
    )
    logger.warning("wa_session_parked", session_id=str(session_id), reason=note)


def escalate_session(conn: Connection, *, session_id: UUID, note: str) -> None:
    conn.execute(
        text("""
            UPDATE wa.session SET state = 'escalated', note = :note
             WHERE id = :sid AND state NOT IN ('complete','cancelled','escalated')
        """),
        {"sid": session_id, "note": note},
    )
    logger.warning("wa_session_escalated", session_id=str(session_id), reason=note)


def get_session_files(conn: Connection, *, session_id: UUID) -> list[dict]:
    rows = conn.execute(
        text("""
            SELECT id, wamid, media_id, kind, fidelity, state,
                   declared_mime, declared_name, meta_sha256, local_sha256,
                   byte_size, storage_uri, error_code
              FROM wa.file
             WHERE session_id = :sid
             ORDER BY received_seq
        """),
        {"sid": session_id},
    ).mappings().all()
    return [dict(r) for r in rows]
