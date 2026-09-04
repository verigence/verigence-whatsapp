"""Deal builder — cold-start deal creation with exact + fuzzy deduplication.

Cold start is the PRIMARY path (detailed design §1.1): deals do not
pre-exist. The booking form creates the deal keyed on (tenant_id, booking_number).

Deduplication order:
  1. Exact match on (tenant_id, booking_number) — always wins
  2. Trigram fuzzy match on customer_name + model + booking_date window
     A fuzzy hit is a CANDIDATE, never a decision — raises a review task
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import structlog
from sqlalchemy import Connection, text

logger = structlog.get_logger(__name__)
_FUZZY_THRESHOLD = 0.6
_DATE_WINDOW_DAYS = 3


@dataclass(frozen=True)
class DealMatch:
    deal_id: UUID
    booking_number: str
    model: str | None
    state: str
    is_exact: bool
    similarity_score: float | None


@dataclass(frozen=True)
class DealCreated:
    deal_id: UUID
    booking_number: str
    model: str | None
    customer_name: str
    is_provisional: bool


def find_existing_deal(
    conn: Connection,
    *,
    tenant_id: UUID,
    booking_number: str,
    customer_name: str | None,
    model: str | None,
    booking_date,
) -> DealMatch | None:
    """Exact first, fuzzy second. Returns DealMatch or None."""
    # 1. Exact
    row = conn.execute(
        text("SELECT id, booking_number, model, state FROM doc.deal"
             " WHERE tenant_id = :tid AND booking_number = :bn"),
        {"tid": tenant_id, "bn": booking_number},
    ).mappings().one_or_none()
    if row is not None:
        logger.info("deal_dedup_exact_hit", deal_id=str(row["id"]), booking_number=booking_number)
        return DealMatch(
            deal_id=UUID(str(row["id"])), booking_number=row["booking_number"],
            model=row.get("model"), state=row["state"],
            is_exact=True, similarity_score=None,
        )

    # 2. Fuzzy — only when we have enough signal
    if not customer_name or model is None:
        return None
    params: dict = {
        "tid": tenant_id, "model": model,
        "cust": customer_name, "threshold": _FUZZY_THRESHOLD,
    }
    date_clause = ""
    if booking_date is not None:
        date_clause = "AND booking_date BETWEEN :bd - :window AND :bd + :window"
        params["bd"] = booking_date
        params["window"] = _DATE_WINDOW_DAYS

    row = conn.execute(
        text(
            f"SELECT id, booking_number, model, state, similarity(customer_name, :cust) AS sim"
            f" FROM doc.deal WHERE tenant_id = :tid AND model = :model"
            f" AND customer_name % :cust {date_clause}"
            f" ORDER BY sim DESC LIMIT 1"
        ),
        params,
    ).mappings().one_or_none()
    if row is None or float(row["sim"]) < _FUZZY_THRESHOLD:
        return None
    logger.info("deal_dedup_fuzzy_hit", deal_id=str(row["id"]), similarity=round(float(row["sim"]), 3))
    return DealMatch(
        deal_id=UUID(str(row["id"])), booking_number=row["booking_number"],
        model=row.get("model"), state=row["state"],
        is_exact=False, similarity_score=float(row["sim"]),
    )


def create_provisional_deal(
    conn: Connection,
    *,
    tenant_id: UUID,
    org_unit_id: UUID,
    booking_number: str,
    customer_name: str,
    customer_mobile: str | None,
    model: str | None,
    variant: str | None,
    booking_date,
    deal_type: str,
    is_financed: bool,
    has_exchange: bool,
    is_corporate: bool,
    created_by: UUID,
) -> DealCreated:
    row = conn.execute(
        text("""
            INSERT INTO doc.deal
                (tenant_id, org_unit_id, booking_number, customer_name, customer_mobile,
                 model, variant, booking_date, deal_type, is_financed, has_exchange,
                 is_corporate, state, created_from, created_by)
            VALUES
                (:tenant_id, :org_unit_id, :booking_number, :customer_name, :customer_mobile,
                 :model, :variant, :booking_date, :deal_type, :is_financed, :has_exchange,
                 :is_corporate, 'provisional', 'whatsapp', :created_by)
            ON CONFLICT (tenant_id, booking_number) DO NOTHING
            RETURNING id, booking_number, model, customer_name
        """),
        {
            "tenant_id": tenant_id, "org_unit_id": org_unit_id,
            "booking_number": booking_number, "customer_name": customer_name,
            "customer_mobile": customer_mobile, "model": model, "variant": variant,
            "booking_date": booking_date, "deal_type": deal_type,
            "is_financed": is_financed, "has_exchange": has_exchange,
            "is_corporate": is_corporate, "created_by": created_by,
        },
    ).mappings().one_or_none()

    if row is None:  # conflict — fetch existing
        row = conn.execute(
            text("SELECT id, booking_number, model, customer_name FROM doc.deal"
                 " WHERE tenant_id = :tid AND booking_number = :bn"),
            {"tid": tenant_id, "bn": booking_number},
        ).mappings().one()

    logger.info("deal_provisional_created", deal_id=str(row["id"]), booking_number=booking_number)
    return DealCreated(
        deal_id=UUID(str(row["id"])), booking_number=row["booking_number"],
        model=row.get("model"), customer_name=row["customer_name"],
        is_provisional=True,
    )


def confirm_deal(conn: Connection, *, tenant_id: UUID, deal_id: UUID, confirmed_by: UUID) -> None:
    conn.execute(
        text("""
            UPDATE doc.deal
               SET state = 'confirmed', confirmed_by = :confirmed_by,
                   confirmed_at = now(), updated_at = now()
             WHERE tenant_id = :tid AND id = :deal_id AND state = 'provisional'
        """),
        {"tid": tenant_id, "deal_id": deal_id, "confirmed_by": confirmed_by},
    )
    logger.info("deal_confirmed", deal_id=str(deal_id))


def raise_ambiguous_review(
    conn: Connection, *, tenant_id: UUID, match: DealMatch, detail: str
) -> None:
    conn.execute(
        text("""
            INSERT INTO doc.review_task (tenant_id, deal_id, reason, detail, state)
            VALUES (:tid, :deal_id, 'deal_ambiguous', :detail, 'open')
            ON CONFLICT DO NOTHING
        """),
        {"tid": tenant_id, "deal_id": match.deal_id, "detail": detail},
    )
    logger.warning("deal_ambiguous_flagged", deal_id=str(match.deal_id),
                   similarity=match.similarity_score)
