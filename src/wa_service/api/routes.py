"""/v1 HTTP API — the interface other Verigence modules use to drive WhatsApp.

Auth: ``Authorization: Bearer <token>`` (see api/auth.py). Every resource is
under ``/v1/tenants/{tenant_id}`` and every database statement runs with
``app.tenant_id`` set, so Postgres row-level security backs up the explicit
tenant filters used on the two tables that have no RLS (wa.route, wa.contact).
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.openapi.utils import get_openapi
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from wa_service.api.auth import Caller, require_caller
from wa_service.api.schemas import (
    ContactIn,
    ContactOut,
    ContactPatch,
    ContactStatus,
    MessageOut,
    RouteIn,
    RouteOut,
    RoutePatch,
    SendMessageRequest,
    SessionOut,
)
from wa_service.db import get_engine
from wa_service.wa.client import WaApiError, WaClient, get_wa_client

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/v1", tags=["whatsapp-api"], dependencies=[Depends(require_caller)])


def get_client() -> WaClient:
    """Dependency so tests can swap the Meta client."""
    return get_wa_client()


def _scope(conn: Connection, tenant_id: UUID) -> None:
    conn.execute(
        text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
    )


def _not_found(what: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"{what}_NOT_FOUND")


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------
@router.get("/openapi.json", include_in_schema=False)
def openapi_contract() -> dict[str, Any]:
    return get_openapi(
        title="Verigence WhatsApp API",
        version="1.0.0",
        description="Send WhatsApp messages and manage routes / contacts for a tenant.",
        routes=router.routes,
    )


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------
_MSG_COLS = (
    "id, to_e164 AS \"to\", kind, state, wamid, error_code, error_message, "
    "retryable, requested_by, created_at, sent_at"
)


def _msg_out(row: Any) -> MessageOut:
    return MessageOut(**{k: v for k, v in dict(row).items() if k in MessageOut.model_fields})


@router.post(
    "/tenants/{tenant_id}/messages",
    response_model=MessageOut,
    status_code=201,
    summary="Send a WhatsApp message",
    responses={
        200: {"description": "Idempotent replay of an already-sent message"},
        409: {"description": "NO_ACTIVE_ROUTE, or the same Idempotency-Key is still in flight"},
        422: {"description": "Idempotency-Key reused with a different request"},
        502: {"description": "Meta rejected the send; detail carries code / retryable"},
    },
)
async def send_message(
    tenant_id: UUID,
    body: SendMessageRequest,
    response: Response,
    caller: Caller = Depends(require_caller),
    client: WaClient = Depends(get_client),
    idempotency_key: str | None = Header(default=None, max_length=200),
) -> MessageOut:
    try:
        kind = body.kind()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    request_payload = body.model_dump(mode="json", exclude_none=True)
    engine = get_engine()

    def _reserve() -> tuple[dict[str, Any] | None, Any, str | None]:
        """Returns (existing_row, new_row_id, phone_number_id)."""
        with engine.begin() as conn:
            _scope(conn, tenant_id)
            route = conn.execute(
                text("""SELECT id, phone_number_id FROM wa.route
                        WHERE tenant_id = :t AND active ORDER BY created_at LIMIT 1"""),
                {"t": tenant_id},
            ).mappings().one_or_none()
            if route is None:
                raise HTTPException(status_code=409, detail="NO_ACTIVE_ROUTE")
            if idempotency_key:
                existing = conn.execute(
                    text("""SELECT id, state, payload, to_e164 AS "to", kind, wamid, error_code,
                                   error_message, retryable, requested_by, created_at, sent_at
                            FROM wa.api_message
                            WHERE tenant_id = :t AND idempotency_key = :k FOR UPDATE"""),
                    {"t": tenant_id, "k": idempotency_key},
                ).mappings().one_or_none()
                if existing is not None:
                    return dict(existing), None, route["phone_number_id"]
            try:
                new_id = conn.execute(
                    text("""INSERT INTO wa.api_message
                              (tenant_id, route_id, to_e164, kind, payload,
                               idempotency_key, requested_by)
                            VALUES (:t, :r, :to, :kind, CAST(:p AS jsonb), :k, :by)
                            RETURNING id"""),
                    {"t": tenant_id, "r": route["id"], "to": body.to, "kind": kind,
                     "p": json.dumps(request_payload), "k": idempotency_key, "by": caller.name},
                ).scalar_one()
            except IntegrityError:
                raise HTTPException(status_code=409, detail="IDEMPOTENCY_KEY_IN_FLIGHT") from None
            return None, new_id, route["phone_number_id"]

    existing, msg_id, phone_number_id = await run_in_threadpool(_reserve)

    if existing is not None:
        stored = existing["payload"] if isinstance(existing["payload"], dict) else json.loads(existing["payload"])
        if stored != request_payload:
            raise HTTPException(status_code=422, detail="IDEMPOTENCY_KEY_REUSED")
        if existing["state"] == "sent":
            response.status_code = 200
            return _msg_out(existing)
        if existing["state"] == "pending":
            raise HTTPException(status_code=409, detail="IDEMPOTENCY_KEY_IN_FLIGHT")
        msg_id = existing["id"]  # previously failed: retry on the same record

    assert phone_number_id is not None
    try:
        if kind == "text":
            result = await client.send_text(
                phone_number_id=phone_number_id, to=body.to, body=body.text or ""
            )
        elif kind == "template":
            t = body.template
            assert t is not None
            result = await client.send_template(
                phone_number_id=phone_number_id, to=body.to, template_name=t.name,
                language_code=t.language, components=t.components,
            )
        else:
            b = body.buttons
            assert b is not None
            result = await client.send_interactive_buttons(
                phone_number_id=phone_number_id, to=body.to, header_text=b.header,
                body_text=b.body, footer_text=b.footer,
                buttons=[x.model_dump() for x in b.buttons],
            )
    except WaApiError as exc:
        err_code, err_msg, err_retry = exc.code, exc.message[:500], exc.retryable

        def _fail() -> None:
            with engine.begin() as conn:
                _scope(conn, tenant_id)
                conn.execute(
                    text("""UPDATE wa.api_message SET state='failed', error_code=:c,
                              error_message=:m, retryable=:r WHERE id=:i"""),
                    {"c": err_code, "m": err_msg, "r": err_retry, "i": msg_id},
                )
        await run_in_threadpool(_fail)
        raise HTTPException(
            status_code=502,
            detail={"code": exc.code, "message": exc.message, "retryable": exc.retryable,
                    "message_id": str(msg_id)},
        ) from exc

    def _ok() -> Any:
        with engine.begin() as conn:
            _scope(conn, tenant_id)
            return conn.execute(
                text(f"""UPDATE wa.api_message SET state='sent', wamid=:w, sent_at=now(),
                           error_code=NULL, error_message=NULL, retryable=NULL
                         WHERE id=:i RETURNING {_MSG_COLS}"""),
                {"w": result.wamid, "i": msg_id},
            ).mappings().one()

    row = await run_in_threadpool(_ok)
    logger.info("wa_api_message_sent", message_id=str(msg_id), caller=caller.name, kind=kind)
    return _msg_out(row)


@router.get("/tenants/{tenant_id}/messages/{message_id}", response_model=MessageOut)
def get_message(tenant_id: UUID, message_id: UUID) -> MessageOut:
    with get_engine().begin() as conn:
        _scope(conn, tenant_id)
        row = conn.execute(
            text(f"SELECT {_MSG_COLS} FROM wa.api_message WHERE id=:i AND tenant_id=:t"),
            {"i": message_id, "t": tenant_id},
        ).mappings().one_or_none()
    if row is None:
        raise _not_found("MESSAGE")
    return _msg_out(row)


# ---------------------------------------------------------------------------
# Contacts (phone number <-> Verigence user binding)
# ---------------------------------------------------------------------------
_CONTACT_COLS = (
    "id, phone_e164, user_id, tenant_id, org_unit_id, status, locale, last_seen_at, created_at"
)


@router.put(
    "/tenants/{tenant_id}/contacts",
    response_model=ContactOut,
    summary="Bind (or re-bind) a phone number to a user",
    responses={409: {"description": "PHONE_BOUND_TO_OTHER_TENANT"}},
)
def put_contact(tenant_id: UUID, body: ContactIn) -> ContactOut:
    with get_engine().begin() as conn:
        _scope(conn, tenant_id)
        row = conn.execute(
            text(f"""INSERT INTO wa.contact
                       (phone_e164, user_id, tenant_id, org_unit_id, status, locale, verified_at)
                     VALUES (:p, :u, :t, :o, :s, :l,
                             CASE WHEN :s = 'active' THEN now() END)
                     ON CONFLICT (phone_e164) DO UPDATE
                        SET user_id = EXCLUDED.user_id, org_unit_id = EXCLUDED.org_unit_id,
                            status = EXCLUDED.status, locale = EXCLUDED.locale
                        WHERE wa.contact.tenant_id = EXCLUDED.tenant_id
                     RETURNING {_CONTACT_COLS}"""),
            {"p": body.phone_e164, "u": body.user_id, "t": tenant_id, "o": body.org_unit_id,
             "s": body.status, "l": body.locale},
        ).mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=409, detail="PHONE_BOUND_TO_OTHER_TENANT")
    return ContactOut(**dict(row))


@router.get("/tenants/{tenant_id}/contacts", response_model=list[ContactOut])
def list_contacts(
    tenant_id: UUID,
    status: ContactStatus | None = None,
    user_id: UUID | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[ContactOut]:
    with get_engine().begin() as conn:
        _scope(conn, tenant_id)
        rows = conn.execute(
            text(f"""SELECT {_CONTACT_COLS} FROM wa.contact
                     WHERE tenant_id = :t
                       AND (CAST(:s AS text) IS NULL OR status = :s)
                       AND (CAST(:u AS uuid) IS NULL OR user_id = :u)
                     ORDER BY created_at, id LIMIT :l OFFSET :o"""),
            {"t": tenant_id, "s": status, "u": user_id, "l": limit, "o": offset},
        ).mappings().all()
    return [ContactOut(**dict(r)) for r in rows]


@router.get("/tenants/{tenant_id}/contacts/{contact_id}", response_model=ContactOut)
def get_contact(tenant_id: UUID, contact_id: UUID) -> ContactOut:
    with get_engine().begin() as conn:
        _scope(conn, tenant_id)
        row = conn.execute(
            text(f"SELECT {_CONTACT_COLS} FROM wa.contact WHERE id=:i AND tenant_id=:t"),
            {"i": contact_id, "t": tenant_id},
        ).mappings().one_or_none()
    if row is None:
        raise _not_found("CONTACT")
    return ContactOut(**dict(row))


@router.patch("/tenants/{tenant_id}/contacts/{contact_id}", response_model=ContactOut)
def patch_contact(tenant_id: UUID, contact_id: UUID, body: ContactPatch) -> ContactOut:
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=422, detail="NO_FIELDS_TO_UPDATE")
    sets = ", ".join(f"{k} = :{k}" for k in changes)  # keys come from the pydantic model
    with get_engine().begin() as conn:
        _scope(conn, tenant_id)
        row = conn.execute(
            text(f"UPDATE wa.contact SET {sets} WHERE id=:i AND tenant_id=:t RETURNING {_CONTACT_COLS}"),
            {**changes, "i": contact_id, "t": tenant_id},
        ).mappings().one_or_none()
    if row is None:
        raise _not_found("CONTACT")
    return ContactOut(**dict(row))


@router.delete("/tenants/{tenant_id}/contacts/{contact_id}", status_code=204,
               summary="Revoke a contact (status -> revoked; the row is kept for audit)")
def revoke_contact(tenant_id: UUID, contact_id: UUID) -> Response:
    with get_engine().begin() as conn:
        _scope(conn, tenant_id)
        n = conn.execute(
            text("UPDATE wa.contact SET status='revoked' WHERE id=:i AND tenant_id=:t"),
            {"i": contact_id, "t": tenant_id},
        ).rowcount
    if not n:
        raise _not_found("CONTACT")
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Routes (WhatsApp Business numbers)
# ---------------------------------------------------------------------------
_ROUTE_COLS = "id, tenant_id, phone_number_id, display_number, active, intake_journey_id, created_at"


@router.post("/tenants/{tenant_id}/routes", response_model=RouteOut, status_code=201,
             responses={409: {"description": "PHONE_NUMBER_ID_ALREADY_REGISTERED"}})
def create_route(tenant_id: UUID, body: RouteIn) -> RouteOut:
    try:
        with get_engine().begin() as conn:
            _scope(conn, tenant_id)
            row = conn.execute(
                text(f"""INSERT INTO wa.route (tenant_id, phone_number_id, display_number, intake_journey_id)
                         VALUES (:t, :p, :d, :j) RETURNING {_ROUTE_COLS}"""),
                {"t": tenant_id, "p": body.phone_number_id, "d": body.display_number,
                 "j": body.intake_journey_id},
            ).mappings().one()
    except IntegrityError:
        raise HTTPException(status_code=409, detail="PHONE_NUMBER_ID_ALREADY_REGISTERED") from None
    return RouteOut(**dict(row))


@router.get("/tenants/{tenant_id}/routes", response_model=list[RouteOut])
def list_routes(tenant_id: UUID) -> list[RouteOut]:
    with get_engine().begin() as conn:
        _scope(conn, tenant_id)
        rows = conn.execute(
            text(f"SELECT {_ROUTE_COLS} FROM wa.route WHERE tenant_id=:t ORDER BY created_at, id"),
            {"t": tenant_id},
        ).mappings().all()
    return [RouteOut(**dict(r)) for r in rows]


@router.patch("/tenants/{tenant_id}/routes/{route_id}", response_model=RouteOut)
def patch_route(tenant_id: UUID, route_id: UUID, body: RoutePatch) -> RouteOut:
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=422, detail="NO_FIELDS_TO_UPDATE")
    sets = ", ".join(f"{k} = :{k}" for k in changes)
    with get_engine().begin() as conn:
        _scope(conn, tenant_id)
        row = conn.execute(
            text(f"UPDATE wa.route SET {sets} WHERE id=:i AND tenant_id=:t RETURNING {_ROUTE_COLS}"),
            {**changes, "i": route_id, "t": tenant_id},
        ).mappings().one_or_none()
    if row is None:
        raise _not_found("ROUTE")
    return RouteOut(**dict(row))


# ---------------------------------------------------------------------------
# Sessions (read-only view of the evidence-capture workflow state)
# ---------------------------------------------------------------------------
_SESSION_COLS = "id, contact_id, deal_id, state, file_count, note, created_at, submitted_at, completed_at"


@router.get("/tenants/{tenant_id}/sessions", response_model=list[SessionOut])
def list_sessions(
    tenant_id: UUID,
    request: Request,
    state: str | None = None,
    contact_id: UUID | None = None,
    deal_id: UUID | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[SessionOut]:
    with get_engine().begin() as conn:
        _scope(conn, tenant_id)
        rows = conn.execute(
            text(f"""SELECT {_SESSION_COLS} FROM wa.session
                     WHERE tenant_id = :t
                       AND (CAST(:s AS text) IS NULL OR state = :s)
                       AND (CAST(:c AS uuid) IS NULL OR contact_id = :c)
                       AND (CAST(:d AS uuid) IS NULL OR deal_id = :d)
                     ORDER BY created_at DESC, id LIMIT :l OFFSET :o"""),
            {"t": tenant_id, "s": state, "c": contact_id, "d": deal_id, "l": limit, "o": offset},
        ).mappings().all()
    return [SessionOut(**dict(r)) for r in rows]


@router.get("/tenants/{tenant_id}/sessions/{session_id}", response_model=SessionOut)
def get_session(tenant_id: UUID, session_id: UUID) -> SessionOut:
    with get_engine().begin() as conn:
        _scope(conn, tenant_id)
        row = conn.execute(
            text(f"SELECT {_SESSION_COLS} FROM wa.session WHERE id=:i AND tenant_id=:t"),
            {"i": session_id, "t": tenant_id},
        ).mappings().one_or_none()
    if row is None:
        raise _not_found("SESSION")
    return SessionOut(**dict(row))
