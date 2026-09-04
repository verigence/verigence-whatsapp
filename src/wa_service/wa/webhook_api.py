"""FastAPI router for the WhatsApp webhook endpoints.

GET  /wa/webhook  — Meta hub verification handshake
POST /wa/webhook  — inbound message events

The POST handler is deliberately simple: it verifies the HMAC, writes
the raw payload to wa.inbox, dispatches an async Procrastinate task,
and returns {} with 200. All heavy processing happens in the worker.
"""
from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.concurrency import run_in_threadpool

from wa_service.config import Settings, get_settings
from wa_service.db import get_engine
from wa_service.wa.webhook import insert_inbox, verify_signature

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/wa", tags=["whatsapp"])


@router.get("/webhook")
def verify_webhook(
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Meta hub verification — respond with the challenge string."""
    if (
        hub_mode == "subscribe"
        and hub_verify_token == settings.wa_verify_token.get_secret_value()
        and hub_challenge
    ):
        logger.info("wa_webhook_verified")
        return Response(content=hub_challenge, media_type="text/plain")
    logger.warning("wa_webhook_verify_failed", mode=hub_mode)
    return Response(status_code=403)


@router.post("/webhook", status_code=200)
async def receive_webhook(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict:
    """Receive inbound WhatsApp events. Always returns 200 to Meta."""
    raw_body = await request.body()
    hub_sig = request.headers.get("X-Hub-Signature-256")

    sig_ok = verify_signature(
        raw_body=raw_body,
        hub_signature=hub_sig,
        app_secret=settings.wa_app_secret.get_secret_value(),
    )
    if not sig_ok:
        logger.warning(
            "wa_webhook_signature_invalid",
            remote=request.client.host if request.client else "unknown",
        )

    engine = get_engine()

    def _write_inbox() -> int | None:
        with engine.begin() as conn:
            return insert_inbox(connection=conn, raw_body=raw_body, signature_ok=sig_ok)

    inbox_id = await run_in_threadpool(_write_inbox)

    if inbox_id is not None:
        from wa_service.worker import dispatch_inbox_processing  # lazy import
        try:
            await dispatch_inbox_processing(inbox_id=inbox_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("wa_dispatch_failed", inbox_id=inbox_id, error=str(exc))

    return {}
