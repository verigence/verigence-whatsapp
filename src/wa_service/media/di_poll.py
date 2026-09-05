"""DI document status polling via the audit-core evidence endpoint.

The WA service does NOT call DI directly (design decision D-01). It polls
audit-core's evidence read API to learn whether DI has finished processing
a document that was uploaded via store_via_audit_core().

Once DI confirms processingStatus=CONFIRMED (or VERIFIED), this module:
  - Returns the final document_type_key so checklist.mark_satisfied() can run
  - Extracts booking fields from the DI facts API for BOOKING_FORM documents
    so process_session can run find_existing_deal() with real data

Poll backoff: 3s, 5s, 8s, 13s, 21s, 34s (Fibonacci). Max 7 attempts (≈84s).
If still PENDING after max attempts, caller parks the session (AWAITING_DI SLA).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from uuid import UUID

import httpx
import structlog

logger = structlog.get_logger(__name__)

_TERMINAL_STATUSES = {"CONFIRMED", "VERIFIED", "FAILED", "REJECTED"}
_FIBONACCI_DELAYS = (3, 5, 8, 13, 21, 34)  # seconds
_BOOKING_FORM_KEY = "BOOKING_FORM"


class DiPollError(Exception):
    def __init__(self, *, code: str, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class DiPollResult:
    """Result of polling audit-core for DI processing status."""
    evidence_id: UUID
    processing_status: str          # PENDING | CONFIRMED | VERIFIED | FAILED | REJECTED
    document_type_key: str | None   # set once DI classifies
    is_terminal: bool


@dataclass
class BookingFields:
    """Fields extracted from a BOOKING_FORM document by DI."""
    booking_number: str | None = None
    customer_name: str | None = None
    model: str | None = None
    variant: str | None = None
    booking_date: str | None = None   # ISO date string, e.g. '2025-01-15'
    is_financed: bool = False
    has_exchange: bool = False
    is_corporate: bool = False
    raw_facts: dict = field(default_factory=dict)


def poll_evidence_status(
    *,
    audit_core_base_url: str,
    audit_core_internal_token: str,
    tenant_id: UUID,
    journey_id: UUID,
    evidence_id: UUID,
    transport=None,
) -> DiPollResult:
    """Single-shot poll of audit-core /evidence/{evidence_id} status.

    Returns immediately — does NOT block. The caller (Procrastinate task)
    re-defers itself with a delay when processing_status is still PENDING.
    """
    headers = {"Authorization": f"Bearer {audit_core_internal_token}"}
    url = (
        f"{audit_core_base_url.rstrip('/')}"
        f"/v1/tenants/{tenant_id}/journeys/{journey_id}/evidence/{evidence_id}"
    )
    kwargs: dict = {"timeout": 10.0}
    if transport:
        kwargs["transport"] = transport

    try:
        with httpx.Client(**kwargs) as client:
            resp = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise DiPollError(code="AUDIT_CORE_NETWORK_ERROR", retryable=True) from exc

    if resp.status_code == 200:
        try:
            body = resp.json()
            status = body.get("processingStatus") or "PENDING"
            doc_type = body.get("documentTypeKey")
            return DiPollResult(
                evidence_id=evidence_id,
                processing_status=status,
                document_type_key=doc_type,
                is_terminal=status in _TERMINAL_STATUSES,
            )
        except (ValueError, KeyError) as exc:
            raise DiPollError(code="AUDIT_CORE_CONTRACT_ERROR", retryable=False) from exc

    if resp.status_code == 404:
        raise DiPollError(code="EVIDENCE_NOT_FOUND", retryable=False)

    retryable = resp.status_code >= 500
    raise DiPollError(code=f"AUDIT_CORE_HTTP_{resp.status_code}", retryable=retryable)


def fetch_booking_fields(
    *,
    audit_core_base_url: str,
    audit_core_internal_token: str,
    tenant_id: UUID,
    journey_id: UUID,
    evidence_id: UUID,
    transport=None,
) -> BookingFields:
    """Fetch DI-extracted facts for a BOOKING_FORM evidence item.

    Called ONCE after poll_evidence_status() returns is_terminal=True and
    document_type_key=BOOKING_FORM. Maps DI field_key names to BookingFields.

    DI field_key values for the vehicle booking domain (from DI project master):
      BOOKING_NUMBER, CUSTOMER_NAME, MODEL, VARIANT, BOOKING_DATE,
      FINANCE_FLAG, EXCHANGE_FLAG, CORPORATE_FLAG
    """
    headers = {"Authorization": f"Bearer {audit_core_internal_token}"}
    # The facts endpoint is on the evidence (evidence_id maps to di_document internally)
    url = (
        f"{audit_core_base_url.rstrip('/')}"
        f"/v1/tenants/{tenant_id}/journeys/{journey_id}/evidence/{evidence_id}/facts"
    )
    kwargs: dict = {"timeout": 10.0}
    if transport:
        kwargs["transport"] = transport

    try:
        with httpx.Client(**kwargs) as client:
            resp = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise DiPollError(code="AUDIT_CORE_NETWORK_ERROR", retryable=True) from exc

    if resp.status_code == 404:
        # No facts yet — return empty; process_session will use fallback
        logger.warning("di_facts_not_found", evidence_id=str(evidence_id))
        return BookingFields()

    if resp.status_code != 200:
        retryable = resp.status_code >= 500
        raise DiPollError(
            code=f"AUDIT_CORE_FACTS_HTTP_{resp.status_code}", retryable=retryable
        )

    try:
        body = resp.json()
        fields_list = body.get("fields") or body if isinstance(body, list) else []
    except ValueError:
        return BookingFields()

    # Build a key→value dict from the DI facts payload
    raw: dict[str, str] = {}
    for item in fields_list:
        if not isinstance(item, dict):
            continue
        key = item.get("fieldKey") or item.get("field_key") or ""
        value = item.get("currentValue") or item.get("value")
        if key and value is not None:
            raw[key.upper()] = str(value)

    def _bool(k: str) -> bool:
        v = raw.get(k, "").lower()
        return v in ("true", "yes", "1", "y")

    result = BookingFields(
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
    logger.info(
        "di_booking_fields_fetched",
        evidence_id=str(evidence_id),
        booking_number=result.booking_number,
        customer_name=result.customer_name,
        model=result.model,
    )
    return result
