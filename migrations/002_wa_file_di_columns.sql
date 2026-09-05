-- =====================================================================
-- MIGRATION 002 — DI result columns on wa.file + intake_journey_id on wa.route
--
-- Adds the columns needed for the poll_di_document task:
--   wa.file.di_document_id   — audit-core evidenceId (UUID) returned by store
--   wa.file.di_facts         — raw JSONB of extracted fields (from DI poll)
--   wa.file.document_type_key — DI classification result
--
-- Adds the cold-start intake journey reference:
--   wa.route.intake_journey_id — pre-created WA Intake journey in audit-core
--     All WA uploads go to this journey. After deal confirmation, audit-core
--     re-links evidence to the real deal journey (design §6.4).
-- =====================================================================

-- wa.file additions
ALTER TABLE wa.file
    ADD COLUMN IF NOT EXISTS di_document_id    uuid,
    ADD COLUMN IF NOT EXISTS document_type_key text,
    ADD COLUMN IF NOT EXISTS di_facts          jsonb;

COMMENT ON COLUMN wa.file.di_document_id IS
    'The audit-core evidenceId returned after store_via_audit_core(); '
    'used to poll DI processing status.';

COMMENT ON COLUMN wa.file.document_type_key IS
    'DI classification result (e.g. BOOKING_FORM, PAN, AADHAAR). '
    'Populated once poll_di_document reaches a terminal status.';

COMMENT ON COLUMN wa.file.di_facts IS
    'Extracted field key/value pairs from DI (JSONB). '
    'Only populated for BOOKING_FORM documents; used by process_session '
    'to run find_existing_deal() with real booking data.';

-- wa.route: intake journey reference for cold-start uploads
ALTER TABLE wa.route
    ADD COLUMN IF NOT EXISTS intake_journey_id uuid;

COMMENT ON COLUMN wa.route.intake_journey_id IS
    'UUID of the pre-created WA Intake journey in audit-core for this '
    'tenant/outlet. Every cold-start evidence upload targets this journey. '
    'Must be set during tenant onboarding (see README deploy runbook).';

-- Index for quick lookup of un-polled stored files
CREATE INDEX IF NOT EXISTS wa_file_needs_di_poll
    ON wa.file (session_id, id)
    WHERE state = 'stored' AND document_type_key IS NULL;
