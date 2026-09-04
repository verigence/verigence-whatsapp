"""Outbound message queue with 24-hour Meta window enforcement and backoff.

Meta's 24-hour messaging window: a business can only send free-form
messages to a user within 24 hours of their last inbound message.
After that, only pre-approved templates can be sent.

outbox rows that miss their window are moved to 'skipped_window' so
the operator can decide whether to send a template instead.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

import structlog
from sqlalchemy import Connection, text

logger = structlog.get_logger(__name__)
_MAX_ATTEMPTS = 3
_WINDOW_HOURS = 23  # 1-hour safety margin before Meta's 24-hour cutoff


def enqueue_reply(
    conn: Connection,
    *,
    tenant_id: UUID,
    contact_id: UUID,
    session_id: UUID | None,
    kind: str,
    payload: dict,
    dedup_key: str | None = None,
    send_after=None,
) -> int:
    """Enqueue a message. Returns outbox row id."""
    if dedup_key and session_id:
        existing = conn.execute(
            text("""
                SELECT id FROM wa.outbox
                 WHERE session_id = :sid
                   AND kind = :kind
                   AND state = 'pending'
                   AND payload->>'_dedup' = :dedup
            """),
            {"sid": session_id, "kind": kind, "dedup": dedup_key},
        ).scalar_one_or_none()
        if existing is not None:
            return existing

    stamped = {**payload}
    if dedup_key:
        stamped["_dedup"] = dedup_key

    row = conn.execute(
        text("""
            INSERT INTO wa.outbox
                (tenant_id, contact_id, session_id, kind, payload, send_after)
            VALUES
                (:tenant_id, :contact_id, :session_id, :kind,
                 CAST(:payload AS jsonb), COALESCE(:send_after, now()))
            RETURNING id
        """),
        {
            "tenant_id": tenant_id, "contact_id": contact_id, "session_id": session_id,
            "kind": kind, "payload": json.dumps(stamped), "send_after": send_after,
        },
    ).scalar_one()
    logger.info("wa_outbox_enqueued", outbox_id=row, kind=kind)
    return row


def claim_pending(conn: Connection, *, limit: int = 20) -> list[dict]:
    """Expire window-missed rows then claim ready ones with SKIP LOCKED."""
    conn.execute(
        text("""
            UPDATE wa.outbox SET state = 'skipped_window'
             WHERE state = 'pending'
               AND send_after < now() - interval '23 hours'
        """)
    )
    rows = conn.execute(
        text("""
            UPDATE wa.outbox SET attempts = attempts + 1
             WHERE id IN (
                 SELECT id FROM wa.outbox
                  WHERE state = 'pending'
                    AND send_after <= now()
                    AND attempts < :max
                  ORDER BY send_after
                  FOR UPDATE SKIP LOCKED
                  LIMIT :limit
             )
            RETURNING id, contact_id, session_id, kind, payload, attempts
        """),
        {"limit": limit, "max": _MAX_ATTEMPTS},
    ).mappings().all()
    return [dict(r) for r in rows]


def mark_sent(conn: Connection, *, outbox_id: int, wamid: str) -> None:
    conn.execute(
        text("UPDATE wa.outbox SET state = 'sent', sent_at = now(), wamid = :wamid WHERE id = :oid"),
        {"oid": outbox_id, "wamid": wamid},
    )


def mark_failed(conn: Connection, *, outbox_id: int, error: str, permanent: bool = False) -> None:
    if permanent:
        conn.execute(
            text("UPDATE wa.outbox SET state = 'failed', last_error = :err WHERE id = :oid"),
            {"oid": outbox_id, "err": error},
        )
    else:
        conn.execute(
            text("""
                UPDATE wa.outbox
                   SET state = 'pending', last_error = :err,
                       send_after = now() + (interval '30 seconds' * power(2, attempts))
                 WHERE id = :oid
            """),
            {"oid": outbox_id, "err": error},
        )


def within_window(*, last_at: datetime | None) -> bool:
    """True if last_at is within the 23-hour safe window."""
    if last_at is None:
        return False
    return (datetime.now(tz=timezone.utc) - last_at) < timedelta(hours=_WINDOW_HOURS)
