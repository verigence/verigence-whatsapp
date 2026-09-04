"""Deal-type aware checklist engine.

Builds the requirement set for a deal and formats the gap message sent
back to the PC. No PII in any output — only document-type display names
and booking number.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Connection, text

logger = structlog.get_logger(__name__)


@dataclass
class ChecklistState:
    deal_id: UUID
    booking_number: str
    blocking_total: int
    blocking_satisfied: int
    blocking_missing: list[str]
    received: list[str]
    is_complete: bool


def build_checklist(conn: Connection, *, tenant_id: UUID, deal_id: UUID) -> ChecklistState:
    booking_number = conn.execute(
        text("SELECT booking_number FROM doc.deal WHERE id = :did AND tenant_id = :tid"),
        {"did": deal_id, "tid": tenant_id},
    ).scalar_one_or_none()
    if booking_number is None:
        raise ValueError(f"Deal {deal_id} not found")

    rows = conn.execute(
        text("""
            SELECT ci.type_key, ci.requirement, ci.satisfied, dt.display_name
              FROM doc.checklist_item ci
              JOIN doc.type dt ON dt.key = ci.type_key
             WHERE ci.deal_id = :did
             ORDER BY ci.requirement DESC, dt.sort
        """),
        {"did": deal_id},
    ).mappings().all()

    blocking_total = blocking_satisfied = 0
    blocking_missing: list[str] = []
    received: list[str] = []

    for row in rows:
        if row["satisfied"]:
            received.append(row["display_name"])
        if row["requirement"] == "blocking":
            blocking_total += 1
            if row["satisfied"]:
                blocking_satisfied += 1
            else:
                blocking_missing.append(row["display_name"])

    return ChecklistState(
        deal_id=deal_id,
        booking_number=booking_number,
        blocking_total=blocking_total,
        blocking_satisfied=blocking_satisfied,
        blocking_missing=blocking_missing,
        received=received,
        is_complete=blocking_total > 0 and blocking_satisfied == blocking_total,
    )


def initialise_checklist(conn: Connection, *, deal_id: UUID, deal_type: str) -> int:
    count = 0
    for type_key, requirement in _default_requirements(deal_type):
        result = conn.execute(
            text("""
                INSERT INTO doc.checklist_item (deal_id, type_key, requirement)
                VALUES (:did, :type_key, :req)
                ON CONFLICT (deal_id, type_key) DO NOTHING
            """),
            {"did": deal_id, "type_key": type_key, "req": requirement},
        )
        count += result.rowcount
    return count


def mark_satisfied(
    conn: Connection, *, deal_id: UUID, type_key: str, document_id: UUID
) -> None:
    conn.execute(
        text("""
            UPDATE doc.checklist_item
               SET satisfied = true, document_id = :doc_id, updated_at = now()
             WHERE deal_id = :did AND type_key = :type_key
        """),
        {"did": deal_id, "type_key": type_key, "doc_id": document_id},
    )


def format_gap_message(
    state: ChecklistState, *, locale: str, copy: dict[str, Any]
) -> tuple[str, str]:
    if state.is_complete:
        body = copy.get("gaps_complete", "All required documents received.").format(
            booking_number=state.booking_number
        )
        return copy.get("gaps_header", "Submission Summary"), body

    received_list = "\n".join(f"\u2022 {n}" for n in state.received) or "\u2014"
    missing_list = "\n".join(f"\u2022 {n}" for n in state.blocking_missing) or "\u2014"
    body = copy.get("gaps_missing", "").format(
        received_list=received_list, missing_list=missing_list
    )
    return copy.get("gaps_header", "Submission Summary"), body


def _default_requirements(deal_type: str) -> list[tuple[str, str]]:
    base: list[tuple[str, str]] = [
        ("BOOKING_FORM", "blocking"),
        ("PAN", "blocking"),
        ("AADHAAR", "blocking"),
        ("TAX_INVOICE_VEHICLE", "blocking"),
        ("DELIVERY_NOTE", "blocking"),
        ("FORM_21", "blocking"),
        ("FORM_22", "blocking"),
        ("INSURANCE_POLICY", "blocking"),
        ("RC_BOOK", "optional"),
    ]
    if deal_type == "retail_financed":
        base += [("LOAN_SANCTION_LETTER", "blocking"), ("BANK_STATEMENT_3M", "blocking")]
    elif deal_type == "retail_exchange":
        base += [("TRADE_IN_EVALUATION", "blocking"), ("RC_BOOK_TRADEIN", "blocking")]
    elif deal_type == "corporate":
        base += [("COMPANY_PAN", "blocking"), ("GST_REGISTRATION", "blocking")]
    return base
