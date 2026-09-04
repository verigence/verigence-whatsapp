"""Evidence store — POST to Audit Core's /internal/evidence endpoint.

The WhatsApp service is SEPARATE from audit-core. Evidence upload is an
HTTP call, not an in-process import. The Idempotency-Key ensures that
a retry after a crash between send and DB commit cannot double-store.
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


def store_via_audit_core(
    *,
    audit_core_base_url: str,
    audit_core_internal_token: str,
    tenant_id: UUID,
    journey_id: UUID | None,
    wamid: str,
    session_correlation_id: str,
    user_id: UUID,
    org_unit_id: UUID,
    content: bytes,
    declared_name: str | None,
    mime: str,
    sha256: str,
    fidelity: str,
    transport=None,
) -> StoreResult:
    """POST to audit-core /internal/evidence. Returns StoreResult(evidence_id)."""
    headers = {
        "Authorization": f"Bearer {audit_core_internal_token}",
        "Idempotency-Key": f"wa:{wamid}",
        "X-Correlation-ID": session_correlation_id,
    }
    data: dict = {
        "evidencePurpose": "WHATSAPP_CAPTURE",
        "fidelity": fidelity,
        "sha256": sha256,
        "actor": f'{{"userId":"{user_id}","role":"PC"}}',
    }
    if journey_id is not None:
        data["journeyId"] = str(journey_id)

    files = {"file": (declared_name or "document", content, mime)}
    url = f"{audit_core_base_url.rstrip('/')}/internal/evidence"
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
            return StoreResult(evidence_id=UUID(resp.json()["evidenceId"]))
        except (ValueError, KeyError) as exc:
            raise StoreError(code="AUDIT_CORE_CONTRACT_ERROR", retryable=False) from exc

    if resp.status_code == 409:
        try:
            return StoreResult(evidence_id=UUID(resp.json()["evidenceId"]))
        except (ValueError, KeyError):
            pass

    retryable = resp.status_code >= 500
    raise StoreError(code=f"AUDIT_CORE_HTTP_{resp.status_code}", retryable=retryable)
