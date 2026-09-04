# verigence-whatsapp

WhatsApp evidence-capture service for the Verigence platform.

Standalone FastAPI service. Shares the same Postgres database as `verigence-audit-core` and `verigence-di` via dedicated `wa.*` and `doc.*` schemas. Deployed as two Railway services from this repository: **api** (webhook + HTTP) and **worker** (Procrastinate background tasks).

See `docs/architecture.md` for the full design rationale.
