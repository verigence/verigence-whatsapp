"""Procrastinate application instance shared by main.py and worker.py."""
from __future__ import annotations

import procrastinate

from wa_service.config import get_settings


def _make_app() -> procrastinate.App:
    settings = get_settings()
    url = settings.database_url
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    elif url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    connector = procrastinate.SyncPsycopgConnector()
    return procrastinate.App(connector=connector)


app = _make_app()
