"""FastAPI application factory for the WhatsApp service.

Two Railway services are deployed from this repo:
  1. **api** (this file)  — uvicorn wa_service.main:app
  2. **worker**           — python -m wa_service.worker
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from wa_service.config import get_settings
from wa_service.db import check_db_rtt, get_engine
from wa_service.wa.client import WaClient, set_wa_client, clear_wa_client
from wa_service.wa.webhook_api import router as webhook_router

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def _lifespan(application: FastAPI):
    """Start-up: validate config, warm DB, create WaClient.
    Shutdown: drain async HTTP client cleanly before SIGTERM.
    """
    settings = get_settings()
    # Warm DB connection pool and warn on high RTT
    get_engine()
    check_db_rtt()
    # Create and register the Meta Cloud API send client
    wa_client = WaClient(settings)
    set_wa_client(wa_client)
    logger.info(
        "wa_service_startup",
        audit_core_base_url=settings.audit_core_base_url,
        redaction_enabled=settings.wa_redaction_enabled,
    )
    yield
    # Graceful shutdown
    await wa_client.aclose()
    clear_wa_client()
    logger.info("wa_service_shutdown")


def create_app() -> FastAPI:
    application = FastAPI(
        title="Verigence WhatsApp Service",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
    )
    application.include_router(webhook_router)

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = create_app()
