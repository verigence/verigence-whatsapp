"""Synchronous SQLAlchemy engine + session factory.

The WhatsApp service uses a **synchronous** engine. The webhook handler
(FastAPI async endpoint) runs DB writes in a threadpool executor via
FastAPI's `run_in_threadpool`, exactly like audit-core does. The
Procrastinate worker is its own process and uses the engine directly.
"""
from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from wa_service.config import get_settings


@lru_cache(maxsize=1)
def get_engine():
    settings = get_settings()
    # psycopg3 driver (psycopg) works with the postgresql+psycopg:// prefix
    url = settings.database_url
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    elif url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    return create_engine(
        url,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
    )


# Convenience factory — use as `with Session() as conn: ...`
Session = sessionmaker(bind=None)  # bind set lazily in get_engine()


def get_connection():
    """Context-manager: yields a SQLAlchemy Connection inside a transaction."""
    engine = get_engine()
    with engine.begin() as conn:
        yield conn


def check_db_rtt() -> None:
    """Warn if DB round-trip exceeds 5 ms (cross-region Neon)."""
    import time
    import structlog
    log = structlog.get_logger(__name__)
    engine = get_engine()
    with engine.connect() as conn:
        t0 = time.perf_counter()
        conn.execute(text("SELECT 1"))
        rtt_ms = (time.perf_counter() - t0) * 1000
    if rtt_ms > 5:
        log.warning("db_rtt_high", rtt_ms=round(rtt_ms, 1))
    else:
        log.info("db_rtt_ok", rtt_ms=round(rtt_ms, 1))
