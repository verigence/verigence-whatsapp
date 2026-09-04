"""Identity resolution: phone number → contact → user → tenant → org_unit.

Decision D-01 (VAC-WA-SD-001 §2.2): authorization requires BOTH a valid
Security identity AND an active Audit Core Dealer/Outlet assignment.

wa.contact has no RLS: the lookup discovers the tenant from the phone,
so a tenant predicate would be circular. After resolution, SET LOCAL
app.tenant_id scopes every subsequent RLS-protected statement.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Connection, text

logger = structlog.get_logger(__name__)


class RoutingError(Exception):
    def __init__(self, reason: str, *, ignore: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.ignore = ignore


@dataclass(frozen=True)
class ResolvedContext:
    tenant_id: UUID
    user_id: UUID
    contact_id: UUID
    org_unit_id: UUID
    locale: str
    route_id: UUID


def resolve_route(conn: Connection, *, phone_number_id: str | None) -> tuple[UUID, UUID]:
    if not phone_number_id:
        raise RoutingError("no phone_number_id in payload", ignore=True)
    row = conn.execute(
        text("SELECT id, tenant_id FROM wa.route WHERE phone_number_id = :pnid AND active = true"),
        {"pnid": phone_number_id},
    ).mappings().one_or_none()
    if row is None:
        raise RoutingError(f"phone_number_id {phone_number_id!r} not registered", ignore=True)
    return UUID(str(row["id"])), UUID(str(row["tenant_id"]))


def resolve_contact(conn: Connection, *, sender_phone: str, tenant_id: UUID) -> dict[str, Any]:
    row = conn.execute(
        text("""
            SELECT c.id AS contact_id, c.user_id, c.locale, c.org_unit_id, c.status
            FROM wa.contact c
            WHERE c.phone_e164 = :phone AND c.tenant_id = :tenant_id
        """),
        {"phone": sender_phone, "tenant_id": tenant_id},
    ).mappings().one_or_none()
    if row is None:
        raise RoutingError(f"phone {sender_phone!r} not bound to tenant {tenant_id}")
    if row["status"] != "active":
        raise RoutingError(f"contact status is {row['status']!r}")
    return dict(row)


def extract_sender_phone(payload: dict[str, Any]) -> str | None:
    try:
        wa_id = payload["entry"][0]["changes"][0]["value"]["contacts"][0]["wa_id"]
        return f"+{wa_id}" if wa_id and not wa_id.startswith("+") else wa_id
    except (KeyError, IndexError, AttributeError):
        return None


def resolve_full_context(
    conn: Connection,
    *,
    phone_number_id: str | None,
    payload: dict[str, Any],
) -> ResolvedContext:
    """Route → contact → user-active check → org-unit assignment. Sets app.tenant_id."""
    route_id, tenant_id = resolve_route(conn, phone_number_id=phone_number_id)

    sender_phone = extract_sender_phone(payload)
    if not sender_phone:
        raise RoutingError("no sender phone in payload", ignore=True)

    contact = resolve_contact(conn, sender_phone=sender_phone, tenant_id=tenant_id)
    user_id = UUID(str(contact["user_id"]))
    contact_id = UUID(str(contact["contact_id"]))
    org_unit_id = UUID(str(contact["org_unit_id"]))

    # Scopes all subsequent RLS-protected reads in this connection
    conn.execute(text("SET LOCAL app.tenant_id = :tid"), {"tid": str(tenant_id)})

    logger.info(
        "wa_context_resolved",
        tenant_id=str(tenant_id),
        contact_id=str(contact_id),
    )
    return ResolvedContext(
        tenant_id=tenant_id,
        user_id=user_id,
        contact_id=contact_id,
        org_unit_id=org_unit_id,
        locale=contact.get("locale", "en"),
        route_id=route_id,
    )
