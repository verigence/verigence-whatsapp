# verigence-whatsapp

WhatsApp evidence-capture service for the Verigence platform.

**Standalone FastAPI service** — separate from `verigence-audit-core` and `verigence-di`.  
Shares the same Postgres database via dedicated `wa.*` and `doc.*` schemas.  
Deployed as **two Railway services** from this repository: `api` (webhook) and `worker` (background tasks).

---

## Architecture

```mermaid
flowchart TD
    subgraph Meta ["Meta Cloud API"]
        M1([WhatsApp Business Number])
    end

    subgraph WA ["verigence-whatsapp (this repo)"]
        WA1["wa-api\nFastAPI · uvicorn\nPOST /wa/webhook"]
        WA2["wa-worker\nProcrastinate tasks\nfetch · store · flush · outbox"]
    end

    subgraph Core ["verigence-audit-core"]
        C1["Evidence Service\nPOST /internal/evidence"]
    end

    subgraph DI ["verigence-di"]
        D1["DI pipeline\nclassify · extract · validate"]
    end

    subgraph DB ["Shared Neon Postgres"]
        DB1[(wa.* schemas)]
        DB2[(doc.* schemas)]
        DB3[(iam.* schemas)]
    end

    M1 -->|webhook| WA1
    WA1 -->|Procrastinate| WA2
    WA2 -->|HTTP POST| C1
    C1 --> D1
    WA1 & WA2 --- DB1 & DB2
    C1 --- DB3
```

> **Authentication setup**: see [`docs/authentication.md`](docs/authentication.md) for the full
> WhatsApp account setup guide, integration steps, and a detailed walkthrough of how a
> Process Consultant is authenticated on every inbound message.

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
docs/
  authentication.md      # WhatsApp setup + user authentication guide
Dockerfile               # api service image
Dockerfile.worker        # worker service image
railway.toml             # api Railway config
railway.worker.toml      # worker Railway config
```

---

## Booking + delivery cycle

```mermaid
sequenceDiagram
    actor PC as Process Consultant
    participant WA as WhatsApp / Meta
    participant API as wa-api
    participant W as wa-worker
    participant Core as audit-core

    PC->>WA: Sends booking + delivery documents
    WA->>API: POST /wa/webhook (HMAC signed)
    API->>API: Verify HMAC signature
    API->>API: INSERT wa.inbox
    API-->>WA: 200 OK (immediate)
    API->>W: dispatch process_inbox_row

    W->>W: resolve_route() → tenant
    W->>W: resolve_contact() → user + org_unit
    W->>W: SET LOCAL app.tenant_id (RLS)
    W->>W: get_or_create_session() 90s debounce

    loop Each media file
        W->>WA: fetch_media() streaming download
        W->>W: SHA-256 verify
        W->>W: redact() if Aadhaar
        W->>Core: POST /internal/evidence
        Core-->>W: 201 evidenceId
    end

    Note over W: Debounce expires (flush_at < now)
    W->>W: process_session()
    W->>W: find/create deal (cold-start + fuzzy dedup)
    W->>PC: Interactive confirm button
    PC->>WA: Confirms deal
    WA->>API: POST /wa/webhook (button reply)
    W->>W: confirm_deal() + initialise_checklist()
    W->>PC: Gap message (received ✓ / still missing)
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

See [`docs/authentication.md`](docs/authentication.md) for the complete setup guide.

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
