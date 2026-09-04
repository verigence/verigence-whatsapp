"""HMAC-SHA256 signature verification and wa.inbox insert.

The webhook always returns 200. Invalid signatures are recorded in the
inbox row (signature_ok=false) and the worker decides whether to process
or ignore them. This keeps Meta's retry engine quiet.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import structlog
from sqlalchemy import Connection, text

logger = structlog.get_logger(__name__)


def verify_signature(*, raw_body: bytes, hub_signature: str | None, app_secret: str) -> bool:
    """Return True if X-Hub-Signature-256 matches HMAC-SHA256 over raw_body."""
    if not hub_signature or not hub_signature.startswith("sha256="):
        return False
    received = hub_signature.removeprefix("sha256=")
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)


def _extract_wamid(payload: dict[str, Any]) -> str | None:
    try:
        messages = payload["entry"][0]["changes"][0]["value"]["messages"]
        return messages[0].get("id") if messages else None
    except (KeyError, IndexError):
        return None


def _extract_phone_number_id(payload: dict[str, Any]) -> str | None:
    try:
        return payload["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"]
    except (KeyError, IndexError):
        return None


def insert_inbox(*, connection: Connection, raw_body: bytes, signature_ok: bool) -> int | None:
    """Write one row to wa.inbox. Returns the row id, or None if duplicate wamid."""
    try:
        payload: dict[str, Any] = json.loads(raw_body)
    except ValueError:
        logger.warning("wa_webhook_unparseable_body")
        return None

    wamid = _extract_wamid(payload)
    phone_number_id = _extract_phone_number_id(payload)

    if wamid is not None:
        exists = connection.execute(
            text("SELECT 1 FROM wa.inbox WHERE wamid = :wamid"), {"wamid": wamid}
        ).scalar_one_or_none()
        if exists is not None:
            logger.debug("wa_inbox_duplicate_skipped", wamid=wamid)
            return None

    row = connection.execute(
        text("""
            INSERT INTO wa.inbox (wamid, phone_number_id, payload, signature_ok)
            VALUES (:wamid, :pnid, CAST(:payload AS jsonb), :sig_ok)
            RETURNING id
        """),
        {
            "wamid": wamid,
            "pnid": phone_number_id,
            "payload": json.dumps(payload),
            "sig_ok": signature_ok,
        },
    ).scalar_one()

    logger.info(
        "wa_inbox_written",
        inbox_id=row,
        wamid=wamid,
        signature_ok=signature_ok,
        phone_number_id=phone_number_id,
    )
    return row
