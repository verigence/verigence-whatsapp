"""Evidence store — POST to Audit Core's evidence endpoint.

The WhatsApp service is a SEPARATE service from audit-core. Evidence upload
is an HTTP call, not an in-process import. The Idempotency-Key ensures that
a retry after a crash between send and DB commit cannot double-store.

Cold-start path (design §6.4):
  journey_id is the intake_journey_id stored on wa.route — a standing
  'WhatsApp Intake' journey pre-created per tenant/outlet. Evidence is
  uploaded here first; once the booking form is confirmed by DI and the
  real deal journey is created, evidence is re-linked by audit-core.

URL: POST /v1/tenants/{tenant_id}/journeys/{journey_id}/evidence
Auth: Bearer {audit_core_internal_token}  (service-to-service)
Form fields:
  evidencePurpose   — required ("WHATSAPP_CAPTURE")
  requirementKey    — optional (None for WA uploads; DI infers from doc type)
  documentTypeKey   — optional (None for cold-start; DI will classify)
Headers:
  Idempotency-Key   — wa:{wamid}  (deduplicates Meta redelivery)
  X-Correlation-ID  — session correlation id
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import httpx
import structlog

logger = structlog.get_logger(__name__)


class StoreError(Exception):
    def __init__(self, *, code: str, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class StoreResult:
    evidence_id: UUID
    journey_id: UUID
    processing_status: str | None
    document_type_key: str | None


def store_via_audit_core(
    *,
    audit_core_base_url: str,
    audit_core_internal_token: str,
    tenant_id: UUID,
    journey_id: UUID,          # intake_journey_id from wa.route (cold-start) or real journey
    wamid: str,
    session_correlation_id: str,
    content: bytes,
    declared_name: str | None,
    mime: str,
    transport=None,
) -> StoreResult:
    """POST file to audit-core evidence endpoint. Returns StoreResult.

    The audit-core evidence endpoint handles the full DI pipeline:
    upload → DI classify → redact (D-08, inside DI) → extract.
    We only need to pass evidencePurpose; audit-core / DI determine
    requirementKey and documentTypeKey from classification.
    """
    headers = {
        "Authorization": f"Bearer {audit_core_internal_token}",
        "Idempotency-Key": f"wa:{wamid}",
        "X-Correlation-ID": session_correlation_id,
    }
    data: dict = {
        "evidencePurpose": "WHATSAPP_CAPTURE",
        # requirementKey and documentTypeKey intentionally omitted:
        # DI classifies the document and audit-core resolves the requirement.
    }

    files = {"file": (declared_name or "document", content, mime)}
    url = (
        f"{audit_core_base_url.rstrip('/')}"
        f"/v1/tenants/{tenant_id}/journeys/{journey_id}/evidence"
    )
    kwargs: dict = {"timeout": 30.0}
    if transport:
        kwargs["transport"] = transport

    try:
        with httpx.Client(**kwargs) as client:
            resp = client.post(url, headers=headers, data=data, files=files)
    except httpx.HTTPError as exc:
        raise StoreError(code="AUDIT_CORE_NETWORK_ERROR", retryable=True) from exc

    if resp.status_code == 201:
        try:
            body = resp.json()
            return StoreResult(
                evidence_id=UUID(body["evidenceId"]),
                journey_id=UUID(body["journeyId"]),
                processing_status=body.get("processingStatus"),
                document_type_key=body.get("documentTypeKey"),
            )
        except (ValueError, KeyError) as exc:
            raise StoreError(code="AUDIT_CORE_CONTRACT_ERROR", retryable=False) from exc

    if resp.status_code == 409:
        # Idempotent replay — same evidence already stored
        try:
            body = resp.json()
            return StoreResult(
                evidence_id=UUID(body["evidenceId"]),
                journey_id=UUID(body["journeyId"]),
                processing_status=body.get("processingStatus"),
                document_type_key=body.get("documentTypeKey"),
            )
        except (ValueError, KeyError):
            pass

    retryable = resp.status_code >= 500
    raise StoreError(code=f"AUDIT_CORE_HTTP_{resp.status_code}", retryable=retryable)
