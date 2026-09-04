-- =====================================================================
-- MIGRATION 001 — WhatsApp service schemas
--
-- Creates wa.* and doc.* schemas with all tables required for the
-- WhatsApp booking + delivery evidence-capture cycle.
--
-- Shares the same Postgres database as verigence-audit-core and
-- verigence-di. Cross-schema references to iam.tenant, iam.app_user,
-- and iam.org_unit are intentional and documented.
--
-- RLS EXCEPTIONS (deliberate, documented):
--   wa.inbox   — webhook writes BEFORE identity is resolved; no tenant known
--   wa.contact — resolution DISCOVERS the tenant from the phone number;
--                a tenant predicate would be circular
-- All other tables carry FORCE ROW LEVEL SECURITY on tenant_id.
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS wa;
CREATE SCHEMA IF NOT EXISTS doc;

-- ---------------------------------------------------------------------------
-- wa.route  — one row per registered WhatsApp Business number / tenant
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wa.route (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid        NOT NULL,
    phone_number_id text        UNIQUE NOT NULL,
    display_number  text        NOT NULL,
    active          boolean     NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS wa_route_active
    ON wa.route (phone_number_id) WHERE active;

-- ---------------------------------------------------------------------------
-- wa.binding_code  — OTP codes for linking a phone to a USER identity
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wa.binding_code (
    code_hash  text        PRIMARY KEY,
    user_id    uuid        NOT NULL,
    tenant_id  uuid        NOT NULL,
    phone_e164 text        NOT NULL,
    expires_at timestamptz NOT NULL,
    used_at    timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- wa.inbox  — raw inbound webhook payloads (NO RLS — see header)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wa.inbox (
    id              bigserial   PRIMARY KEY,
    wamid           text        UNIQUE,
    phone_number_id text,
    received_at     timestamptz NOT NULL DEFAULT now(),
    payload         jsonb       NOT NULL,
    signature_ok    boolean     NOT NULL,
    state           text        NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending','processing','done','failed','ignored')),
    attempts        integer     NOT NULL DEFAULT 0,
    last_error      text,
    locked_until    timestamptz
);
CREATE INDEX IF NOT EXISTS wa_inbox_claim
    ON wa.inbox (state, id) WHERE state = 'pending';

-- ---------------------------------------------------------------------------
-- wa.contact  — bound PC identities (NO RLS — see header)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wa.contact (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    phone_e164   text        UNIQUE NOT NULL,
    wa_id        text,
    user_id      uuid        NOT NULL,
    tenant_id    uuid        NOT NULL,
    status       text        NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','active','suspended','revoked')),
    locale       text        NOT NULL DEFAULT 'en'
                 CHECK (locale IN ('en','hi','pa')),
    verified_at  timestamptz,
    last_seen_at timestamptz,
    last_deal_id uuid,
    last_deal_at timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS wa_contact_tenant
    ON wa.contact (tenant_id);

-- ---------------------------------------------------------------------------
-- doc.deal  — one row per vehicle deal, keyed on (tenant_id, booking_number)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS doc.deal (
    id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        uuid        NOT NULL,
    org_unit_id      uuid,
    booking_number   text        NOT NULL,
    customer_name    text,
    customer_mobile  text,
    model            text,
    variant          text,
    vin              text,
    booking_date     date,
    delivery_date    date,
    deal_type        text        NOT NULL DEFAULT 'retail'
                     CHECK (deal_type IN ('retail','retail_financed','retail_exchange','corporate')),
    is_financed      boolean     NOT NULL DEFAULT false,
    has_exchange     boolean     NOT NULL DEFAULT false,
    is_corporate     boolean     NOT NULL DEFAULT false,
    state            text        NOT NULL DEFAULT 'provisional'
                     CHECK (state IN ('provisional','confirmed','delivered','closed','cancelled')),
    created_from     text        NOT NULL DEFAULT 'whatsapp',
    created_by       uuid,
    confirmed_by     uuid,
    confirmed_at     timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT doc_deal_tenant_booking_unique UNIQUE (tenant_id, booking_number)
);
CREATE INDEX IF NOT EXISTS doc_deal_tenant ON doc.deal (tenant_id);
CREATE INDEX IF NOT EXISTS doc_deal_vin     ON doc.deal (vin) WHERE vin IS NOT NULL;

-- Enable pg_trgm for fuzzy customer-name deduplication
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------------
-- doc.review_task  — flagged ambiguous deals needing manual resolution
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS doc.review_task (
    id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  uuid        NOT NULL,
    deal_id    uuid        REFERENCES doc.deal(id) ON DELETE CASCADE,
    reason     text        NOT NULL,
    detail     text,
    state      text        NOT NULL DEFAULT 'open'
               CHECK (state IN ('open','resolved','dismissed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT doc_review_task_deal_reason UNIQUE (deal_id, reason)
);

-- ---------------------------------------------------------------------------
-- doc.type  — document type registry (display names for checklist messages)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS doc.type (
    key          text PRIMARY KEY,
    display_name text NOT NULL,
    sort         integer NOT NULL DEFAULT 0
);

INSERT INTO doc.type (key, display_name, sort) VALUES
    ('BOOKING_FORM',          'Booking Form',              1),
    ('PAN',                   'PAN Card',                  2),
    ('AADHAAR',               'Aadhaar Card',              3),
    ('TAX_INVOICE_VEHICLE',   'Tax Invoice (Vehicle)',      4),
    ('DELIVERY_NOTE',         'Delivery Note',             5),
    ('FORM_21',               'Form 21',                   6),
    ('FORM_22',               'Form 22',                   7),
    ('INSURANCE_POLICY',      'Insurance Policy',          8),
    ('RC_BOOK',               'RC Book',                   9),
    ('LOAN_SANCTION_LETTER',  'Loan Sanction Letter',     10),
    ('BANK_STATEMENT_3M',     '3-Month Bank Statement',   11),
    ('TRADE_IN_EVALUATION',   'Trade-in Evaluation',      12),
    ('RC_BOOK_TRADEIN',       'RC Book (Trade-in)',        13),
    ('COMPANY_PAN',           'Company PAN',              14),
    ('GST_REGISTRATION',      'GST Registration',         15)
ON CONFLICT (key) DO NOTHING;

-- ---------------------------------------------------------------------------
-- doc.checklist_item  — per-deal document requirements
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS doc.checklist_item (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    deal_id     uuid        NOT NULL REFERENCES doc.deal(id) ON DELETE CASCADE,
    type_key    text        NOT NULL REFERENCES doc.type(key),
    requirement text        NOT NULL DEFAULT 'blocking'
                CHECK (requirement IN ('blocking','optional')),
    satisfied   boolean     NOT NULL DEFAULT false,
    document_id uuid,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT doc_checklist_deal_type UNIQUE (deal_id, type_key)
);

-- ---------------------------------------------------------------------------
-- wa.session  — bundle context; one open session per contact
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wa.session (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    uuid        NOT NULL,
    contact_id   uuid        NOT NULL REFERENCES wa.contact(id) ON DELETE CASCADE,
    deal_id      uuid        REFERENCES doc.deal(id) ON DELETE SET NULL,
    org_unit_id  uuid,
    state        text        NOT NULL DEFAULT 'collecting'
                 CHECK (state IN ('collecting','confirming_deal','processing',
                                  'gaps_pending','complete','parked','escalated','cancelled')),
    note         text,
    flush_at     timestamptz,
    expires_at   timestamptz,
    file_count   integer     NOT NULL DEFAULT 0,
    bytes_total  bigint      NOT NULL DEFAULT 0,
    created_at   timestamptz NOT NULL DEFAULT now(),
    submitted_at timestamptz,
    completed_at timestamptz
);
CREATE INDEX IF NOT EXISTS wa_session_flush
    ON wa.session (flush_at)
    WHERE state IN ('collecting','confirming_deal','gaps_pending');
CREATE UNIQUE INDEX IF NOT EXISTS wa_session_one_open_per_contact
    ON wa.session (contact_id)
    WHERE state IN ('collecting','confirming_deal','processing');

-- ---------------------------------------------------------------------------
-- wa.file  — one row per inbound media message
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wa.file (
    id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        uuid        NOT NULL,
    session_id       uuid        NOT NULL REFERENCES wa.session(id) ON DELETE CASCADE,
    wamid            text        UNIQUE NOT NULL,
    media_id         text        NOT NULL,
    received_seq     integer     NOT NULL,
    wa_timestamp     timestamptz NOT NULL,
    kind             text        NOT NULL CHECK (kind IN ('document','image')),
    fidelity         text        NOT NULL CHECK (fidelity IN ('original','recompressed')),
    declared_mime    text,
    declared_name    text,
    caption          text,
    meta_sha256      text,
    local_sha256     text,
    byte_size        bigint,
    page_count       integer,
    state            text        NOT NULL DEFAULT 'pending'
                     CHECK (state IN ('pending','downloading','redacting','storing',
                                      'stored','failed','skipped','quarantined')),
    attempts         integer     NOT NULL DEFAULT 0,
    last_error       text,
    error_code       text,
    storage_uri      text,
    media_expires_at timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    stored_at        timestamptz
);
CREATE INDEX IF NOT EXISTS wa_file_session ON wa.file (session_id);
CREATE INDEX IF NOT EXISTS wa_file_expiry  ON wa.file (media_expires_at) WHERE state <> 'stored';

-- ---------------------------------------------------------------------------
-- wa.outbox  — queued outbound messages with 24-hour window + backoff
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wa.outbox (
    id         bigserial   PRIMARY KEY,
    tenant_id  uuid        NOT NULL,
    contact_id uuid        NOT NULL REFERENCES wa.contact(id) ON DELETE CASCADE,
    session_id uuid        REFERENCES wa.session(id) ON DELETE SET NULL,
    kind       text        NOT NULL
               CHECK (kind IN ('text','interactive','list','audio','template')),
    payload    jsonb       NOT NULL,
    state      text        NOT NULL DEFAULT 'pending'
               CHECK (state IN ('pending','sent','failed','skipped_window')),
    attempts   integer     NOT NULL DEFAULT 0,
    send_after timestamptz NOT NULL DEFAULT now(),
    sent_at    timestamptz,
    wamid      text,
    last_error text
);
CREATE INDEX IF NOT EXISTS wa_outbox_claim
    ON wa.outbox (state, send_after) WHERE state = 'pending';

-- ---------------------------------------------------------------------------
-- RLS — applied to tenant-scoped tables only
-- wa.inbox and wa.contact are EXCLUDED (see header)
-- ---------------------------------------------------------------------------
ALTER TABLE wa.session   ENABLE ROW LEVEL SECURITY;
ALTER TABLE wa.session   FORCE  ROW LEVEL SECURITY;
ALTER TABLE wa.file      ENABLE ROW LEVEL SECURITY;
ALTER TABLE wa.file      FORCE  ROW LEVEL SECURITY;
ALTER TABLE wa.outbox    ENABLE ROW LEVEL SECURITY;
ALTER TABLE wa.outbox    FORCE  ROW LEVEL SECURITY;
ALTER TABLE doc.deal     ENABLE ROW LEVEL SECURITY;
ALTER TABLE doc.deal     FORCE  ROW LEVEL SECURITY;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'wa' AND tablename = 'session' AND policyname = 'wa_tenant_isolation'
    ) THEN
        CREATE POLICY wa_tenant_isolation ON wa.session
            USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'wa' AND tablename = 'file' AND policyname = 'wa_tenant_isolation'
    ) THEN
        CREATE POLICY wa_tenant_isolation ON wa.file
            USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'wa' AND tablename = 'outbox' AND policyname = 'wa_tenant_isolation'
    ) THEN
        CREATE POLICY wa_tenant_isolation ON wa.outbox
            USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'doc' AND tablename = 'deal' AND policyname = 'doc_tenant_isolation'
    ) THEN
        CREATE POLICY doc_tenant_isolation ON doc.deal
            USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
    END IF;
END $$;

-- Tenant-scoped view for wa.contact (safe for API layer)
CREATE OR REPLACE VIEW wa.contact_scoped AS
    SELECT * FROM wa.contact
    WHERE tenant_id = current_setting('app.tenant_id', true)::uuid;
