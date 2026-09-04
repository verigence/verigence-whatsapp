# verigence-whatsapp

WhatsApp evidence-capture service for the Verigence platform.

**Standalone FastAPI service** — separate from `verigence-audit-core` and `verigence-di`.  
Shares the same Postgres database via dedicated `wa.*` and `doc.*` schemas.  
Deployed as **two Railway services** from this repository: `api` (webhook) and `worker` (background tasks).

---

## Architecture

```
Meta Cloud API
      │
      ▼ POST /wa/webhook  (HMAC-SHA256 verified)
┌─────────────────────────┐
│  verigence-whatsapp     │  Railway service: wa-api
│  FastAPI + uvicorn      │
│  wa.inbox (no RLS)      │
└──────────┬──────────────┘
           │ Procrastinate task dispatch
           ▼
┌─────────────────────────┐
│  verigence-whatsapp     │  Railway service: wa-worker
│  worker.py              │
│  Procrastinate tasks    │
└──────┬──────────────────┘
       │ HTTP POST /internal/evidence
       ▼
┌─────────────────────────┐
│  verigence-audit-core   │  existing service (unchanged)
│  Evidence Service       │
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│  verigence-di           │  existing service (unchanged)
│  DI pipeline            │
└─────────────────────────┘

All three services share one Neon Postgres database.
wa.* and doc.* schemas are owned exclusively by verigence-whatsapp.
```

### Database schemas

| Schema | Owner | Tables |
|--------|-------|--------|
| `wa`   | verigence-whatsapp | route, binding_code, inbox, contact, session, file, outbox |
| `doc`  | verigence-whatsapp | deal, checklist_item, type, review_task |
| `iam`  | verigence-audit-core | tenant, app_user, org_unit (cross-schema refs, read-only) |

**RLS exceptions** (documented in `migrations/001_wa_schema.sql`):
- `wa.inbox` — webhook writes before identity is known
- `wa.contact` — resolution discovers the tenant from the phone number

---

## Repository structure

```
src/wa_service/
  main.py                # FastAPI app factory + lifespan
  config.py              # pydantic-settings (all env vars)
  db.py                  # sync SQLAlchemy engine
  procrastinate_app.py   # Procrastinate App instance
  worker.py              # all Procrastinate tasks
  wa/
    webhook.py           # HMAC verify + wa.inbox insert
    webhook_api.py       # GET/POST /wa/webhook endpoints
    router.py            # phone → contact → tenant → org_unit
    session.py           # state machine + debounce
    outbox.py            # 24h window + SKIP LOCKED drain
    client.py            # Meta Cloud API send adapter (async httpx)
    copy/                # en.yaml, hi.yaml, pa.yaml
  media/
    fetch.py             # streaming download, dual SHA-256
    redact.py            # Aadhaar masking (governance gate D-08)
    store.py             # HTTP POST to audit-core /internal/evidence
  deal/
    builder.py           # cold-start deal creation + fuzzy dedup
    checklist.py         # deal-type requirements + gap message
migrations/
  001_wa_schema.sql      # full DDL: wa.* + doc.* schemas + RLS
  versions/001_bootstrap.py
tests/
  fake_meta/__init__.py  # FakeMetaTransport + payload builders
  test_whatsapp_cycle.py # 12 test classes (all critical paths)
Dockerfile               # api service image
Dockerfile.worker        # worker service image
railway.toml             # api Railway config
railway.worker.toml      # worker Railway config
```

---

## Booking + delivery cycle

```
1.  PC sends documents to WhatsApp number
        │
2.  Meta → POST /wa/webhook
    └── HMAC verified, 200 returned immediately, wa.inbox row written
        │
3.  worker: process_inbox_row
    ├── resolve_full_context (phone → tenant → contact → org_unit)
    ├── get_or_create_session (90s debounce timer on flush_at)
    └── per media message: register_file → dispatch fetch_and_store_file
        │
4.  worker: fetch_and_store_file (one Procrastinate task per file)
    ├── fetch_media (streaming, SHA-256, ≤4 attempts before Meta limit)
    ├── redact (if WA_REDACTION_ENABLED=true and Aadhaar document)
    └── store_via_audit_core (HTTP → /internal/evidence → DI pipeline)
        │
5.  Scheduler (every 60s): flush_ready_sessions
    └── claim sessions where flush_at < now() → dispatch process_session
        │
6.  worker: process_session
    ├── find_existing_deal (exact booking_number → fuzzy customer_name)
    ├── create_provisional_deal if no match (cold start)
    ├── enqueue interactive confirm button → PC on WhatsApp
    └── on PC confirmation: confirm_deal → initialise_checklist → gap message
        │
7.  Scheduler (every 60s): send_outbox
    └── claim_pending (SKIP LOCKED) → WaClient.send_* → mark_sent/failed
```

---

## Railway deployment

### Two services from this single repository

| Service | Config | Dockerfile |
|---------|--------|-----------|
| `wa-api` | `railway.toml` | `Dockerfile` |
| `wa-worker` | `railway.worker.toml` | `Dockerfile.worker` |

**Steps in Railway:**
1. Create new Railway project, connect repo `verigence/verigence-whatsapp`
2. **Service 1 (wa-api)**: uses `railway.toml` — runs `preDeployCommand` (alembic migrations) then uvicorn
3. **Service 2 (wa-worker)**: add second service, override config to `railway.worker.toml` — runs `python -m wa_service.worker`
4. Set shared environment variables (see below) on both services

### Required environment variables

```bash
# Shared Neon Postgres (same database as audit-core and DI)
DATABASE_URL=postgresql://user:pass@host/dbname?sslmode=require

# WhatsApp Cloud API — from Meta Developer Portal
WA_APP_SECRET=<app_secret_from_meta>
WA_ACCESS_TOKEN=<permanent_system_user_token>
WA_VERIFY_TOKEN=<your_chosen_verify_token>

# Audit Core internal evidence upload
AUDIT_CORE_BASE_URL=https://your-audit-core.railway.app
AUDIT_CORE_INTERNAL_TOKEN=<service_to_service_bearer_token>
```

### Optional environment variables

```bash
WA_REDACTION_ENABLED=false          # KEEP FALSE — see governance gate below
WA_DEBOUNCE_SECONDS=90              # session debounce window
WA_MEDIA_CONCURRENT_PER_CONTACT=3  # Meta hard-blocks the number at 5 failures/hour
WA_DEFAULT_LOCALE=en                # en | hi | pa
WA_DI_SLA_MINUTES=15                # park session if DI result not received
```

### Meta webhook setup

1. Meta Developer Portal → App → WhatsApp → Configuration:
   - **Webhook URL**: `https://<wa-api-railway-url>/wa/webhook`
   - **Verify Token**: value of `WA_VERIFY_TOKEN`
   - **Subscribed fields**: `messages`

2. Register the WhatsApp Business number in the database:
   ```sql
   INSERT INTO wa.route (tenant_id, phone_number_id, display_number)
   VALUES ('<tenant_uuid>', '<meta_phone_number_id>', '+91XXXXXXXXXX');
   ```

3. Bind each Process Consultant's phone number:
   ```sql
   INSERT INTO wa.contact
       (phone_e164, user_id, tenant_id, org_unit_id, status, locale)
   VALUES ('+91XXXXXXXXXX', '<user_uuid>', '<tenant_uuid>', '<org_unit_uuid>', 'active', 'en');
   ```

---

## Governance gate D-08

> **`WA_REDACTION_ENABLED` must remain `false` until the Aadhaar redactor
> is verified end-to-end on synthetic data.**

Once an unmasked Aadhaar image has reached any storage system or model,
no subsequent fix undoes it. Real customer documents must not enter the
system before this gate is cleared.

---

## Running tests locally

```bash
pip install -e ".[test]"
pytest tests/ -v
```

---

*Made with IBM Bob*
