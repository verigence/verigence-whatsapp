-- =====================================================================
-- MIGRATION 003 — support for the public /v1 HTTP API
--
--   1. wa.contact.org_unit_id
--      The identity router (wa/router.py) and docs/authentication.md both
--      read/insert this column, but migration 001 never created it.
--   2. wa.api_message
--      Audit + idempotency record for messages sent through POST
--      /v1/tenants/{tenant}/messages. Separate from wa.outbox, which is
--      keyed to a bound wa.contact and so cannot hold messages to arbitrary
--      recipients.
-- =====================================================================

ALTER TABLE wa.contact ADD COLUMN IF NOT EXISTS org_unit_id uuid;

CREATE TABLE IF NOT EXISTS wa.api_message (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid        NOT NULL,
    route_id        uuid        NOT NULL REFERENCES wa.route(id),
    to_e164         text        NOT NULL,
    kind            text        NOT NULL CHECK (kind IN ('text','template','buttons')),
    payload         jsonb       NOT NULL,
    state           text        NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending','sent','failed')),
    wamid           text,
    error_code      text,
    error_message   text,
    retryable       boolean,
    idempotency_key text,
    requested_by    text        NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    sent_at         timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS wa_api_message_idem
    ON wa.api_message (tenant_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS wa_api_message_tenant_created
    ON wa.api_message (tenant_id, created_at DESC);

ALTER TABLE wa.api_message ENABLE ROW LEVEL SECURITY;
ALTER TABLE wa.api_message FORCE  ROW LEVEL SECURITY;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'wa' AND tablename = 'api_message' AND policyname = 'wa_tenant_isolation'
    ) THEN
        CREATE POLICY wa_tenant_isolation ON wa.api_message
            USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
    END IF;
END $$;
