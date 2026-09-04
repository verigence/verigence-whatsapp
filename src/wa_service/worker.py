"""Procrastinate task worker — all background processing for the WhatsApp service.

Two process commands from this repo (railway.worker.toml):
  python -m wa_service.worker

Task flow for the booking + delivery cycle:

  wa.inbox row created (webhook)
       ↓
  dispatch_inbox_processing (Procrastinate, async-dispatched from webhook)
       ↓
  process_inbox_row
    ├─ resolve_full_context → tenant / contact / session
    └─ for each media message in payload:
         register_file (wa.file row)
         fetch_and_store_file task (one per file)
       ↓
  fetch_and_store_file
    ├─ fetch_media (streaming, SHA-256)
    ├─ redact (if enabled + Aadhaar)
    └─ store_via_audit_core (HTTP POST to audit-core /internal/evidence)
       ↓
  flush_ready_sessions (scheduler, every 60 s)
    └─ claim_sessions_for_flush → process_session task per session
       ↓
  process_session
    ├─ all files stored? → find/create deal (cold start)
    ├─ enqueue deal-confirm interactive button reply
    └─ on PC confirmation → confirm_deal, initialise_checklist, gap message
       ↓
  send_outbox (scheduler, every 30 s)
    └─ claim_pending → WaClient.send_* → mark_sent / mark_failed
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import structlog

from wa_service.config import get_settings
from wa_service.db import get_engine
from wa_service.deal.builder import (
    DealMatch,
    confirm_deal,
    create_provisional_deal,
    find_existing_deal,
    raise_ambiguous_review,
)
from wa_service.deal.checklist import (
    build_checklist,
    format_gap_message,
    initialise_checklist,
)
from wa_service.media.fetch import MediaFetchError, fetch_media
from wa_service.media.redact import redact_image_bytes
from wa_service.media.store import StoreError, store_via_audit_core
from wa_service.wa.client import WaApiError, get_wa_client
from wa_service.wa.copy import load_copy
from wa_service.wa.outbox import claim_pending, enqueue_reply, mark_failed, mark_sent
from wa_service.wa.router import RoutingError, resolve_full_context
from wa_service.wa.session import (
    claim_sessions_for_flush,
    get_or_create_session,
    get_session_files,
    mark_complete,
    park_session,
    record_file_arrival,
    register_file,
    transition_to_confirming,
)

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Procrastinate app (import here to avoid circular imports with main.py)
# ---------------------------------------------------------------------------
from wa_service.procrastinate_app import app as proc_app  # noqa: E402


# ---------------------------------------------------------------------------
# Dispatch helper — called from webhook_api.py
# ---------------------------------------------------------------------------
async def dispatch_inbox_processing(*, inbox_id: int) -> None:
    """Enqueue a process_inbox_row task for the given inbox row id."""
    async with proc_app.open_async():
        await proc_app.tasks["process_inbox_row"].defer_async(inbox_id=inbox_id)


# ---------------------------------------------------------------------------
# Task: process one wa.inbox row
# ---------------------------------------------------------------------------
@proc_app.task(name="process_inbox_row", retry=3, pass_context=False)
def process_inbox_row(inbox_id: int) -> None:
    settings = get_settings()
    engine = get_engine()

    with engine.begin() as conn:
        row = conn.execute(
            __import__("sqlalchemy", fromlist=["text"]).text(
                "SELECT id, payload, signature_ok, phone_number_id"
                " FROM wa.inbox WHERE id = :id"
            ),
            {"id": inbox_id},
        ).mappings().one_or_none()

    if row is None:
        logger.warning("inbox_row_not_found", inbox_id=inbox_id)
        return

    if not row["signature_ok"]:
        logger.warning("inbox_signature_invalid_ignored", inbox_id=inbox_id)
        _mark_inbox(engine, inbox_id, "ignored")
        return

    payload: dict[str, Any] = (
        row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
    )

    try:
        with engine.begin() as conn:
            ctx = resolve_full_context(
                conn,
                phone_number_id=row["phone_number_id"],
                payload=payload,
            )
            session = get_or_create_session(
                conn,
                tenant_id=ctx.tenant_id,
                contact_id=ctx.contact_id,
                org_unit_id=ctx.org_unit_id,
            )
            session_id = UUID(str(session["id"]))
            _process_messages(conn, payload, ctx, session_id, settings)

    except RoutingError as exc:
        if not exc.ignore:
            logger.warning("inbox_routing_error", reason=exc.reason, inbox_id=inbox_id)
        _mark_inbox(engine, inbox_id, "ignored")
        return
    except Exception as exc:  # noqa: BLE001
        logger.error("inbox_processing_error", inbox_id=inbox_id, error=str(exc))
        _mark_inbox(engine, inbox_id, "failed")
        return

    _mark_inbox(engine, inbox_id, "done")


def _process_messages(
    conn, payload: dict[str, Any], ctx, session_id: UUID, settings
) -> None:
    """Extract media messages from the payload and register / enqueue each file."""
    from sqlalchemy import text

    try:
        messages = payload["entry"][0]["changes"][0]["value"].get("messages", [])
    except (KeyError, IndexError):
        return

    seq = conn.execute(
        text("SELECT COALESCE(MAX(received_seq), 0) FROM wa.file WHERE session_id = :sid"),
        {"sid": session_id},
    ).scalar_one()

    for msg in messages:
        msg_type = msg.get("type", "")
        if msg_type not in ("image", "document"):
            continue

        seq += 1
        media_block = msg.get(msg_type, {})
        wamid = msg.get("id", "")
        ts = datetime.fromtimestamp(int(msg.get("timestamp", 0)), tz=timezone.utc)

        # image sent as Photo = recompressed; sent as Document = original
        fidelity = "recompressed" if msg_type == "image" else "original"

        file_id = register_file(
            conn,
            tenant_id=ctx.tenant_id,
            session_id=session_id,
            wamid=wamid,
            media_id=media_block.get("id", ""),
            received_seq=seq,
            wa_timestamp=ts,
            kind=msg_type,
            fidelity=fidelity,
            declared_mime=media_block.get("mime_type"),
            declared_name=media_block.get("filename"),
            caption=msg.get("caption"),
            meta_sha256=media_block.get("sha256"),
            media_expires_at=None,
        )
        record_file_arrival(conn, session_id=session_id, byte_size=0)

        # Defer per-file download task
        asyncio.get_event_loop().run_until_complete(
            proc_app.tasks["fetch_and_store_file"].defer_async(
                file_id=str(file_id),
                session_id=str(session_id),
                tenant_id=str(ctx.tenant_id),
                user_id=str(ctx.user_id),
                org_unit_id=str(ctx.org_unit_id),
                locale=ctx.locale,
            )
        )


# ---------------------------------------------------------------------------
# Task: fetch + redact + store one file
# ---------------------------------------------------------------------------
@proc_app.task(name="fetch_and_store_file", retry=3, pass_context=False)
def fetch_and_store_file(
    file_id: str,
    session_id: str,
    tenant_id: str,
    user_id: str,
    org_unit_id: str,
    locale: str = "en",
) -> None:
    from sqlalchemy import text

    settings = get_settings()
    engine = get_engine()
    _file_id = UUID(file_id)
    _tenant_id = UUID(tenant_id)
    _user_id = UUID(user_id)
    _org_unit_id = UUID(org_unit_id)
    _session_id = UUID(session_id)

    with engine.begin() as conn:
        frow = conn.execute(
            text("SELECT wamid, media_id, declared_mime, declared_name, fidelity,"
                 " meta_sha256, attempts FROM wa.file WHERE id = :fid"),
            {"fid": _file_id},
        ).mappings().one_or_none()
        if frow is None:
            return
        conn.execute(
            text("UPDATE wa.file SET state = 'downloading' WHERE id = :fid"),
            {"fid": _file_id},
        )

    try:
        result = fetch_media(
            media_id=frow["media_id"],
            access_token=settings.wa_access_token.get_secret_value(),
            declared_sha256=frow["meta_sha256"],
            attempt=frow["attempts"],
        )
    except MediaFetchError as exc:
        state = "quarantined" if exc.quarantine else "failed"
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE wa.file SET state = :state, error_code = :code,"
                     " attempts = attempts + 1 WHERE id = :fid"),
                {"state": state, "code": exc.code, "fid": _file_id},
            )
        logger.error("wa_file_fetch_failed", file_id=file_id, code=exc.code)
        if not exc.retryable:
            raise  # Procrastinate will not retry on re-raise
        return

    # Redact if enabled
    content = result.content
    if settings.wa_redaction_enabled:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE wa.file SET state = 'redacting' WHERE id = :fid"),
                {"fid": _file_id},
            )
        try:
            content, _ = redact_image_bytes(
                content, mime=result.mime, document_type_key="AADHAAR"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("wa_redact_skipped", file_id=file_id, error=str(exc))

    # Store via audit-core HTTP
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE wa.file SET state = 'storing' WHERE id = :fid"),
            {"fid": _file_id},
        )
    try:
        store_result = store_via_audit_core(
            audit_core_base_url=settings.audit_core_base_url,
            audit_core_internal_token=(
                settings.audit_core_internal_token.get_secret_value()
            ),
            tenant_id=_tenant_id,
            journey_id=None,  # cold start — linked after deal confirmation
            wamid=frow["wamid"],
            session_correlation_id=str(_session_id),
            user_id=_user_id,
            org_unit_id=_org_unit_id,
            content=content,
            declared_name=frow["declared_name"],
            mime=result.mime,
            sha256=result.sha256,
            fidelity=frow["fidelity"],
        )
    except StoreError as exc:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE wa.file SET state = 'failed', error_code = :code,"
                     " attempts = attempts + 1 WHERE id = :fid"),
                {"code": exc.code, "fid": _file_id},
            )
        logger.error("wa_file_store_failed", file_id=file_id, code=exc.code)
        return

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE wa.file SET state = 'stored', local_sha256 = :sha,"
                 " byte_size = :size, stored_at = now(),"
                 " storage_uri = :uri WHERE id = :fid"),
            {
                "sha": result.sha256,
                "size": result.byte_size,
                "uri": str(store_result.evidence_id),
                "fid": _file_id,
            },
        )
    logger.info("wa_file_stored", file_id=file_id, evidence_id=str(store_result.evidence_id))


# ---------------------------------------------------------------------------
# Scheduler: flush sessions whose debounce timer has expired
# ---------------------------------------------------------------------------
@proc_app.periodic(cron="* * * * *")  # every minute
def flush_ready_sessions(timestamp: datetime) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        session_ids = claim_sessions_for_flush(conn)
    for sid in session_ids:
        asyncio.get_event_loop().run_until_complete(
            proc_app.tasks["process_session"].defer_async(session_id=str(sid))
        )


# ---------------------------------------------------------------------------
# Task: process one flushed session — deal builder + gap message
# ---------------------------------------------------------------------------
@proc_app.task(name="process_session", retry=2, pass_context=False)
def process_session(session_id: str) -> None:
    from sqlalchemy import text

    _session_id = UUID(session_id)
    settings = get_settings()
    engine = get_engine()

    with engine.begin() as conn:
        srow = conn.execute(
            text("SELECT tenant_id, contact_id, org_unit_id, deal_id"
                 " FROM wa.session WHERE id = :sid"),
            {"sid": _session_id},
        ).mappings().one_or_none()
        if srow is None:
            return

        _tenant_id = UUID(str(srow["tenant_id"]))
        _contact_id = UUID(str(srow["contact_id"]))
        _org_unit_id = UUID(str(srow["org_unit_id"]))
        conn.execute(
            text("SET LOCAL app.tenant_id = :tid"), {"tid": str(_tenant_id)}
        )

        files = get_session_files(conn, session_id=_session_id)
        all_stored = all(f["state"] == "stored" for f in files)
        if not all_stored:
            # Not all files are ready — re-park, retry later
            park_session(conn, session_id=_session_id, note="files_not_ready")
            return

        # Cold-start deal discovery
        match = find_existing_deal(
            conn,
            tenant_id=_tenant_id,
            booking_number="UNKNOWN",  # real booking_number extracted by DI
            customer_name=None,
            model=None,
            booking_date=None,
        )

        if match is None:
            # Create provisional deal — PC must confirm
            deal = create_provisional_deal(
                conn,
                tenant_id=_tenant_id,
                org_unit_id=_org_unit_id,
                booking_number=f"WA-{str(_session_id)[:8].upper()}",
                customer_name="(pending)",
                customer_mobile=None,
                model=None,
                variant=None,
                booking_date=None,
                deal_type="retail",
                is_financed=False,
                has_exchange=False,
                is_corporate=False,
                created_by=_contact_id,
            )
            initialise_checklist(conn, deal_id=deal.deal_id, deal_type="retail")
            transition_to_confirming(conn, session_id=_session_id, deal_id=deal.deal_id)

            # Fetch contact locale + phone for reply
            crow = conn.execute(
                text("SELECT locale FROM wa.contact WHERE id = :cid"),
                {"cid": _contact_id},
            ).mappings().one()
            locale = crow["locale"]
            copy = load_copy(locale)

            enqueue_reply(
                conn,
                tenant_id=_tenant_id,
                contact_id=_contact_id,
                session_id=_session_id,
                kind="interactive",
                payload={
                    "type": "buttons",
                    "header": copy.get("deal_confirm_header", "Confirm Deal"),
                    "body": copy.get("deal_confirm_body", "").format(
                        booking_number=deal.booking_number,
                        model=deal.model or "(pending)",
                        customer_name=deal.customer_name,
                    ),
                    "buttons": [
                        {"id": f"confirm_{_session_id}",
                         "title": copy.get("deal_confirm_yes", "Confirm")},
                        {"id": f"resubmit_{_session_id}",
                         "title": copy.get("deal_confirm_no", "Re-submit")},
                    ],
                },
                dedup_key=f"deal_confirm_{_session_id}",
            )

        elif not match.is_exact:
            raise_ambiguous_review(
                conn,
                tenant_id=_tenant_id,
                match=match,
                detail=f"session:{_session_id}",
            )
            park_session(conn, session_id=_session_id, note="deal_ambiguous")
        else:
            # Exact match — confirm and send gap message
            confirm_deal(conn, tenant_id=_tenant_id,
                         deal_id=match.deal_id, confirmed_by=_contact_id)
            _send_gap_message(conn, _session_id, _tenant_id, _contact_id,
                              match.deal_id, settings)


def _send_gap_message(
    conn, session_id: UUID, tenant_id: UUID, contact_id: UUID, deal_id: UUID, settings
) -> None:
    from sqlalchemy import text

    crow = conn.execute(
        text("SELECT locale FROM wa.contact WHERE id = :cid"), {"cid": contact_id}
    ).mappings().one()
    locale = crow["locale"]
    copy = load_copy(locale)
    checklist = build_checklist(conn, tenant_id=tenant_id, deal_id=deal_id)
    _, body = format_gap_message(checklist, locale=locale, copy=copy)

    enqueue_reply(
        conn,
        tenant_id=tenant_id,
        contact_id=contact_id,
        session_id=session_id,
        kind="text",
        payload={"body": body},
        dedup_key=f"gap_{session_id}",
    )
    if checklist.is_complete:
        mark_complete(conn, session_id=session_id)


# ---------------------------------------------------------------------------
# Scheduler: drain outbox
# ---------------------------------------------------------------------------
@proc_app.periodic(cron="*/1 * * * *")
def send_outbox(timestamp: datetime) -> None:
    engine = get_engine()
    wa_client = get_wa_client()

    with engine.begin() as conn:
        rows = claim_pending(conn)

    for row in rows:
        outbox_id = row["id"]
        kind = row["kind"]
        payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])

        contact_id = UUID(str(row["contact_id"]))
        with engine.begin() as conn:
            crow = conn.execute(
                __import__("sqlalchemy", fromlist=["text"]).text(
                    "SELECT phone_e164, tenant_id FROM wa.contact WHERE id = :cid"
                ),
                {"cid": contact_id},
            ).mappings().one_or_none()
            if crow is None:
                continue
            # Get route phone_number_id for this tenant
            rrow = conn.execute(
                __import__("sqlalchemy", fromlist=["text"]).text(
                    "SELECT phone_number_id FROM wa.route"
                    " WHERE tenant_id = :tid AND active = true LIMIT 1"
                ),
                {"tid": UUID(str(crow["tenant_id"]))},
            ).mappings().one_or_none()

        if rrow is None:
            logger.error("outbox_no_route", outbox_id=outbox_id, contact_id=str(contact_id))
            with engine.begin() as conn:
                mark_failed(conn, outbox_id=outbox_id, error="NO_ROUTE", permanent=True)
            continue

        phone_number_id = rrow["phone_number_id"]
        to = crow["phone_e164"]

        try:
            loop = asyncio.get_event_loop()
            if kind == "text":
                result = loop.run_until_complete(
                    wa_client.send_text(phone_number_id=phone_number_id,
                                        to=to, body=payload.get("body", ""))
                )
            elif kind == "interactive" and payload.get("type") == "buttons":
                result = loop.run_until_complete(
                    wa_client.send_interactive_buttons(
                        phone_number_id=phone_number_id, to=to,
                        header_text=payload.get("header"),
                        body_text=payload.get("body", ""),
                        footer_text=payload.get("footer"),
                        buttons=payload.get("buttons", []),
                    )
                )
            elif kind == "template":
                result = loop.run_until_complete(
                    wa_client.send_template(
                        phone_number_id=phone_number_id, to=to,
                        template_name=payload["name"],
                        language_code=payload.get("language", "en"),
                        components=payload.get("components"),
                    )
                )
            else:
                logger.warning("outbox_unknown_kind", kind=kind, outbox_id=outbox_id)
                with engine.begin() as conn:
                    mark_failed(conn, outbox_id=outbox_id, error="UNKNOWN_KIND", permanent=True)
                continue

            with engine.begin() as conn:
                mark_sent(conn, outbox_id=outbox_id, wamid=result.wamid)

        except WaApiError as exc:
            with engine.begin() as conn:
                mark_failed(conn, outbox_id=outbox_id, error=exc.code,
                            permanent=not exc.retryable)
            logger.error("outbox_send_failed", outbox_id=outbox_id, code=exc.code)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _mark_inbox(engine, inbox_id: int, state: str) -> None:
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE wa.inbox SET state = :state WHERE id = :id"),
            {"state": state, "id": inbox_id},
        )


# ---------------------------------------------------------------------------
# Worker entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import procrastinate
    from wa_service.config import get_settings as _gs

    _s = _gs()
    url = _s.database_url
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    elif url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    connector = procrastinate.SyncPsycopgConnector()
    worker_app = procrastinate.App(connector=connector)
    worker_app.run_worker(queues=["default"])
