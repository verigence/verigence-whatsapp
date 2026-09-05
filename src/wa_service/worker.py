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
    ├─ fetch_media  (streaming, SHA-256 verified)
    ├─ store_via_audit_core  (HTTP POST → audit-core evidence endpoint)
    │     audit-core → DI: classify, redact (D-08), extract
    └─ poll_di_document  (deferred, starts AWAITING_DI loop)
       ↓
  poll_di_document
    ├─ poll audit-core for DI processing status
    ├─ on CONFIRMED: write document_type_key + di_facts to wa.file
    ├─ call mark_satisfied() for the deal checklist
    └─ once ALL files polled → defer process_session
       ↓
  flush_ready_sessions (scheduler, every 60 s)
    └─ claim_sessions_for_flush → process_session task per session
       ↓
  process_session
    ├─ read booking fields from wa.file.di_facts (BOOKING_FORM)
    ├─ find/create deal (cold start dedup)
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
    confirm_deal,
    create_provisional_deal,
    find_existing_deal,
    raise_ambiguous_review,
)
from wa_service.deal.checklist import (
    build_checklist,
    format_gap_message,
    initialise_checklist,
    mark_satisfied,
)
from wa_service.media.di_poll import (
    BookingFields,
    DiPollError,
    fetch_booking_fields,
    poll_evidence_status,
)
from wa_service.media.fetch import MediaFetchError, fetch_media
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

# Max DI poll attempts per file before we park the session (AWAITING_DI SLA)
_MAX_DI_POLL_ATTEMPTS = 7
# Fibonacci backoff delays in seconds: 3, 5, 8, 13, 21, 34, 55
_DI_POLL_DELAYS = (3, 5, 8, 13, 21, 34, 55)


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

    # Fetch intake_journey_id from wa.route for this session's tenant
    rrow = conn.execute(
        text(
            "SELECT r.intake_journey_id FROM wa.route r"
            " JOIN wa.contact c ON c.tenant_id = r.tenant_id"
            " JOIN wa.session s ON s.contact_id = c.id"
            " WHERE s.id = :sid AND r.active = true"
            " LIMIT 1"
        ),
        {"sid": session_id},
    ).mappings().one_or_none()
    intake_journey_id = str(rrow["intake_journey_id"]) if rrow and rrow["intake_journey_id"] else None

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
                intake_journey_id=intake_journey_id,
            )
        )


# ---------------------------------------------------------------------------
# Task: fetch + store one file
# Redaction is NOT performed here. It runs inside DI between classify and
# extract (design decision D-08). Classification must happen first, and only
# DI knows the document type.
# ---------------------------------------------------------------------------
@proc_app.task(name="fetch_and_store_file", retry=3, pass_context=False)
def fetch_and_store_file(
    file_id: str,
    session_id: str,
    tenant_id: str,
    user_id: str,
    org_unit_id: str,
    locale: str = "en",
    intake_journey_id: str | None = None,
) -> None:
    from sqlalchemy import text

    settings = get_settings()
    engine = get_engine()
    _file_id = UUID(file_id)
    _tenant_id = UUID(tenant_id)
    _session_id = UUID(session_id)

    # Resolve intake_journey_id if not passed (fallback: look up from wa.route)
    if intake_journey_id is None:
        with engine.connect() as conn:
            rrow = conn.execute(
                text(
                    "SELECT r.intake_journey_id FROM wa.route r"
                    " JOIN wa.contact c ON c.tenant_id = r.tenant_id"
                    " JOIN wa.session s ON s.contact_id = c.id"
                    " WHERE s.id = :sid AND r.active = true"
                    " LIMIT 1"
                ),
                {"sid": _session_id},
            ).mappings().one_or_none()
            if rrow and rrow["intake_journey_id"]:
                intake_journey_id = str(rrow["intake_journey_id"])

    if not intake_journey_id:
        logger.error(
            "wa_no_intake_journey",
            session_id=session_id,
            file_id=file_id,
        )
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE wa.file SET state = 'failed', error_code = 'NO_INTAKE_JOURNEY'"
                     " WHERE id = :fid"),
                {"fid": _file_id},
            )
        return

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

    # Store via audit-core HTTP. DI receives the raw bytes and handles
    # classification, Aadhaar redaction (D-08), and extraction internally.
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
            journey_id=UUID(intake_journey_id),
            wamid=frow["wamid"],
            session_correlation_id=str(_session_id),
            content=result.content,
            declared_name=frow["declared_name"],
            mime=result.mime,
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

    # Persist the evidence_id; document_type_key populated by poll_di_document
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE wa.file SET state = 'stored', local_sha256 = :sha,"
                " byte_size = :size, stored_at = now(),"
                " storage_uri = :uri,"
                " di_document_id = :di_doc_id"
                " WHERE id = :fid"
            ),
            {
                "sha": result.sha256,
                "size": result.byte_size,
                "uri": str(store_result.evidence_id),
                "di_doc_id": store_result.evidence_id,
                "fid": _file_id,
            },
        )
    logger.info(
        "wa_file_stored",
        file_id=file_id,
        evidence_id=str(store_result.evidence_id),
        processing_status=store_result.processing_status,
    )

    # Kick off DI poll — runs with Fibonacci backoff until terminal or SLA breach
    asyncio.get_event_loop().run_until_complete(
        proc_app.tasks["poll_di_document"].defer_async(
            file_id=file_id,
            session_id=session_id,
            tenant_id=tenant_id,
            journey_id=intake_journey_id,
            evidence_id=str(store_result.evidence_id),
            attempt=0,
        )
    )


# ---------------------------------------------------------------------------
# Task: poll DI for document processing status (AWAITING_DI state)
# Runs after fetch_and_store_file succeeds. Re-defers itself with Fibonacci
# backoff until DI reaches a terminal status or the SLA limit is reached.
# ---------------------------------------------------------------------------
@proc_app.task(name="poll_di_document", retry=0, pass_context=False)
def poll_di_document(
    file_id: str,
    session_id: str,
    tenant_id: str,
    journey_id: str,
    evidence_id: str,
    attempt: int = 0,
) -> None:
    from sqlalchemy import text

    settings = get_settings()
    engine = get_engine()
    _file_id = UUID(file_id)
    _tenant_id = UUID(tenant_id)
    _journey_id = UUID(journey_id)
    _evidence_id = UUID(evidence_id)
    _session_id = UUID(session_id)

    try:
        poll = poll_evidence_status(
            audit_core_base_url=settings.audit_core_base_url,
            audit_core_internal_token=settings.audit_core_internal_token.get_secret_value(),
            tenant_id=_tenant_id,
            journey_id=_journey_id,
            evidence_id=_evidence_id,
        )
    except DiPollError as exc:
        logger.warning(
            "di_poll_error", file_id=file_id, code=exc.code, attempt=attempt
        )
        if not exc.retryable or attempt >= _MAX_DI_POLL_ATTEMPTS - 1:
            _park_on_di_sla(engine, _session_id, f"DI poll error: {exc.code}")
            return
        _reschedule_poll(file_id, session_id, tenant_id, journey_id, evidence_id, attempt)
        return

    if not poll.is_terminal:
        if attempt >= _MAX_DI_POLL_ATTEMPTS - 1:
            logger.warning(
                "di_poll_sla_exceeded",
                file_id=file_id,
                evidence_id=evidence_id,
                attempts=attempt + 1,
            )
            _park_on_di_sla(engine, _session_id, "AWAITING_DI SLA exceeded")
            return
        _reschedule_poll(file_id, session_id, tenant_id, journey_id, evidence_id, attempt)
        return

    # --- Terminal status reached ---
    doc_type_key = poll.document_type_key
    di_facts: dict = {}

    # For BOOKING_FORM: fetch extracted facts for cold-start deal resolution
    if doc_type_key == "BOOKING_FORM" and poll.processing_status in ("CONFIRMED", "VERIFIED"):
        try:
            booking = fetch_booking_fields(
                audit_core_base_url=settings.audit_core_base_url,
                audit_core_internal_token=settings.audit_core_internal_token.get_secret_value(),
                tenant_id=_tenant_id,
                journey_id=_journey_id,
                evidence_id=_evidence_id,
            )
            di_facts = booking.raw_facts
        except DiPollError as exc:
            logger.warning("di_facts_fetch_error", file_id=file_id, code=exc.code)

    # Persist document_type_key and di_facts on wa.file
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE wa.file"
                " SET document_type_key = :dtk,"
                "     di_facts = CAST(:facts AS jsonb)"
                " WHERE id = :fid"
            ),
            {
                "dtk": doc_type_key,
                "facts": json.dumps(di_facts),
                "fid": _file_id,
            },
        )

    # Mark checklist item satisfied if deal is already linked to the session
    if doc_type_key and poll.processing_status in ("CONFIRMED", "VERIFIED"):
        with engine.begin() as conn:
            srow = conn.execute(
                text("SELECT deal_id, tenant_id FROM wa.session WHERE id = :sid"),
                {"sid": _session_id},
            ).mappings().one_or_none()

        if srow and srow["deal_id"]:
            with engine.begin() as conn:
                conn.execute(
                    text("SET LOCAL app.tenant_id = :tid"),
                    {"tid": str(srow["tenant_id"])},
                )
                mark_satisfied(
                    conn,
                    deal_id=UUID(str(srow["deal_id"])),
                    type_key=doc_type_key,
                    document_id=_evidence_id,
                )

    logger.info(
        "di_poll_terminal",
        file_id=file_id,
        evidence_id=evidence_id,
        document_type_key=doc_type_key,
        processing_status=poll.processing_status,
        attempts=attempt + 1,
    )

    # Check if ALL files in the session have been polled
    # If so, nudge the session flush so process_session runs without waiting
    # for the 60-second scheduler.
    with engine.begin() as conn:
        pending_count = conn.execute(
            text(
                "SELECT COUNT(*) FROM wa.file"
                " WHERE session_id = :sid"
                "   AND state = 'stored'"
                "   AND document_type_key IS NULL"
            ),
            {"sid": _session_id},
        ).scalar_one()

    if pending_count == 0:
        # All files polled — advance session flush timer to now so the
        # scheduler picks it up in the next 60-second tick
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE wa.session"
                    " SET flush_at = now()"
                    " WHERE id = :sid"
                    "   AND state IN ('collecting', 'confirming_deal', 'processing')"
                ),
                {"sid": _session_id},
            )
        logger.info("di_all_files_polled_flush_advanced", session_id=session_id)


def _reschedule_poll(
    file_id: str,
    session_id: str,
    tenant_id: str,
    journey_id: str,
    evidence_id: str,
    attempt: int,
) -> None:
    """Re-defer poll_di_document with Fibonacci backoff."""
    next_attempt = attempt + 1
    delay = _DI_POLL_DELAYS[min(attempt, len(_DI_POLL_DELAYS) - 1)]
    asyncio.get_event_loop().run_until_complete(
        proc_app.tasks["poll_di_document"].defer_async(
            file_id=file_id,
            session_id=session_id,
            tenant_id=tenant_id,
            journey_id=journey_id,
            evidence_id=evidence_id,
            attempt=next_attempt,
            schedule_in={"seconds": delay},
        )
    )
    logger.info(
        "di_poll_rescheduled",
        file_id=file_id,
        attempt=next_attempt,
        delay_seconds=delay,
    )


def _park_on_di_sla(engine, session_id: UUID, note: str) -> None:
    """Park the session when DI SLA is exceeded."""
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE wa.session SET state = 'parked', note = :note WHERE id = :sid"),
            {"note": note, "sid": session_id},
        )
    logger.warning("session_parked_di_sla", session_id=str(session_id), note=note)


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
            # Not all files ready — re-park, retry later
            park_session(conn, session_id=_session_id, note="files_not_ready")
            return

        # Check that all files have been through DI poll (document_type_key populated)
        all_polled = all(f.get("document_type_key") is not None for f in files)
        if not all_polled:
            # DI still processing — re-park; poll_di_document will advance flush_at
            park_session(conn, session_id=_session_id, note="awaiting_di")
            return

        # Extract booking fields from the BOOKING_FORM file's di_facts
        booking_fields = _extract_booking_fields(files)

        # Cold-start deal discovery using real DI-extracted booking data
        match = find_existing_deal(
            conn,
            tenant_id=_tenant_id,
            booking_number=booking_fields.booking_number or f"WA-{str(_session_id)[:8].upper()}",
            customer_name=booking_fields.customer_name,
            model=booking_fields.model,
            booking_date=booking_fields.booking_date,
        )

        if match is None:
            # Create provisional deal — PC must confirm
            booking_number = (
                booking_fields.booking_number
                or f"WA-{str(_session_id)[:8].upper()}"
            )
            # Infer deal type from flags extracted from the booking form
            deal_type = _infer_deal_type(booking_fields)

            deal = create_provisional_deal(
                conn,
                tenant_id=_tenant_id,
                org_unit_id=_org_unit_id,
                booking_number=booking_number,
                customer_name=booking_fields.customer_name or "(pending)",
                customer_mobile=None,
                model=booking_fields.model,
                variant=booking_fields.variant,
                booking_date=booking_fields.booking_date,
                deal_type=deal_type,
                is_financed=booking_fields.is_financed,
                has_exchange=booking_fields.has_exchange,
                is_corporate=booking_fields.is_corporate,
                created_by=_contact_id,
            )
            initialise_checklist(conn, deal_id=deal.deal_id, deal_type=deal_type)
            transition_to_confirming(conn, session_id=_session_id, deal_id=deal.deal_id)

            # Satisfy checklist items for all already-confirmed documents
            _satisfy_confirmed_files(conn, deal.deal_id, files)

            # Fetch contact locale for reply
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
            confirm_deal(
                conn, tenant_id=_tenant_id,
                deal_id=match.deal_id, confirmed_by=_contact_id
            )
            # Satisfy checklist for files already confirmed
            _satisfy_confirmed_files(conn, match.deal_id, files)
            _send_gap_message(
                conn, _session_id, _tenant_id, _contact_id,
                match.deal_id, settings
            )


def _extract_booking_fields(files: list) -> "BookingFields":
    """Find the BOOKING_FORM file and return its extracted fields."""
    for f in files:
        if f.get("document_type_key") == "BOOKING_FORM":
            raw = f.get("di_facts") or {}
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except ValueError:
                    raw = {}

            def _bool(k: str) -> bool:
                v = str(raw.get(k, "")).lower()
                return v in ("true", "yes", "1", "y")

            return BookingFields(
                booking_number=raw.get("BOOKING_NUMBER"),
                customer_name=raw.get("CUSTOMER_NAME"),
                model=raw.get("MODEL"),
                variant=raw.get("VARIANT"),
                booking_date=raw.get("BOOKING_DATE"),
                is_financed=_bool("FINANCE_FLAG"),
                has_exchange=_bool("EXCHANGE_FLAG"),
                is_corporate=_bool("CORPORATE_FLAG"),
                raw_facts=raw,
            )
    return BookingFields()


def _infer_deal_type(bf: "BookingFields") -> str:
    """Derive deal_type from booking form flags."""
    if bf.is_corporate:
        return "corporate"
    if bf.is_financed:
        return "retail_financed"
    if bf.has_exchange:
        return "retail_exchange"
    return "retail"


def _satisfy_confirmed_files(
    conn, deal_id: UUID, files: list
) -> None:
    """Mark checklist items satisfied for all CONFIRMED/VERIFIED files."""
    for f in files:
        dtk = f.get("document_type_key")
        di_doc_id = f.get("di_document_id")
        if dtk and di_doc_id:
            try:
                mark_satisfied(
                    conn,
                    deal_id=deal_id,
                    type_key=dtk,
                    document_id=UUID(str(di_doc_id)),
                )
            except Exception:  # noqa: BLE001
                pass  # checklist item may not exist for this doc type


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
