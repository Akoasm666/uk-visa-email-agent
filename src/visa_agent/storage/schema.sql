-- S1.2 email persistence (PostgreSQL)

CREATE TABLE IF NOT EXISTS email_events (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    provider_message_id TEXT,
    rfc_message_id TEXT,
    dedupe_key TEXT NOT NULL,
    from_addr TEXT,
    to_addrs JSONB NOT NULL DEFAULT '[]'::jsonb,
    subject TEXT NOT NULL DEFAULT '',
    body_text TEXT NOT NULL DEFAULT '',
    raw_file_id TEXT,
    case_id TEXT,
    claimed_case_id TEXT,
    attribution_status TEXT NOT NULL DEFAULT 'unattributed'
        CHECK (attribution_status IN ('attributed', 'unattributed', 'duplicate')),
    processing_status TEXT NOT NULL DEFAULT 'processed'
        CHECK (processing_status IN ('unprocessed', 'processed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, dedupe_key)
);

-- Inbound starts unprocessed; outbound/legacy rows default to processed.
ALTER TABLE email_events ADD COLUMN IF NOT EXISTS processing_status TEXT;
UPDATE email_events
SET processing_status = CASE
    WHEN direction = 'inbound' THEN COALESCE(processing_status, 'unprocessed')
    ELSE COALESCE(processing_status, 'processed')
END
WHERE processing_status IS NULL;
ALTER TABLE email_events ALTER COLUMN processing_status SET DEFAULT 'processed';
UPDATE email_events SET processing_status = 'processed' WHERE processing_status IS NULL;
ALTER TABLE email_events ALTER COLUMN processing_status SET NOT NULL;
ALTER TABLE email_events DROP CONSTRAINT IF EXISTS email_events_processing_status_check;
ALTER TABLE email_events
    ADD CONSTRAINT email_events_processing_status_check
    CHECK (processing_status IN ('unprocessed', 'processed'));

ALTER TABLE email_events ADD COLUMN IF NOT EXISTS processing_conclusion TEXT;
ALTER TABLE email_events ADD COLUMN IF NOT EXISTS processed_by TEXT;
ALTER TABLE email_events ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_email_events_created
    ON email_events (tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS outbox_messages (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    send_operation_id TEXT NOT NULL,
    case_id TEXT,
    case_version INTEGER NOT NULL DEFAULT 1,
    kind TEXT NOT NULL,
    recipients JSONB NOT NULL,
    subject TEXT NOT NULL,
    body_text TEXT NOT NULL,
    package_id TEXT,
    package_kind TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    block_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    provider_response TEXT,
    email_event_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, send_operation_id)
);

CREATE INDEX IF NOT EXISTS idx_outbox_status
    ON outbox_messages (tenant_id, status, created_at DESC);

-- Compatible upgrades for databases created by earlier S1.2 schema
ALTER TABLE email_events ADD COLUMN IF NOT EXISTS claimed_case_id TEXT;
ALTER TABLE outbox_messages ADD COLUMN IF NOT EXISTS send_operation_id TEXT;
UPDATE outbox_messages
SET send_operation_id = id
WHERE send_operation_id IS NULL;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'outbox_messages_send_operation_uniq'
    ) THEN
        ALTER TABLE outbox_messages
            ADD CONSTRAINT outbox_messages_send_operation_uniq
            UNIQUE (tenant_id, send_operation_id);
    END IF;
END $$;

ALTER TABLE outbox_messages DROP CONSTRAINT IF EXISTS outbox_messages_status_check;
ALTER TABLE outbox_messages
    ADD CONSTRAINT outbox_messages_status_check
    CHECK (status IN (
        'pending', 'blocked', 'processing', 'provider_accepted',
        'failed', 'status_unknown', 'simulated', 'partial_accepted'
    ));

-- S1.3 case persistence + workbench support
CREATE TABLE IF NOT EXISTS cases (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    case_version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    route TEXT,
    resume_status TEXT,
    customer_email TEXT,
    title TEXT NOT NULL DEFAULT '',
    waiting_customer BOOLEAN NOT NULL DEFAULT FALSE,
    waiting_third_party BOOLEAN NOT NULL DEFAULT FALSE,
    human_takeover BOOLEAN NOT NULL DEFAULT FALSE,
    system_blocked BOOLEAN NOT NULL DEFAULT FALSE,
    paused BOOLEAN NOT NULL DEFAULT FALSE,
    cancelled BOOLEAN NOT NULL DEFAULT FALSE,
    current_delivery_id TEXT,
    closure_seq INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE cases ADD COLUMN IF NOT EXISTS closure_seq INTEGER NOT NULL DEFAULT 0;
ALTER TABLE cases ADD COLUMN IF NOT EXISTS final_review_seq INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_cases_tenant_updated
    ON cases (tenant_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS case_customer_confirmations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    case_id TEXT NOT NULL REFERENCES cases(id),
    case_version INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, case_id, case_version)
);

CREATE TABLE IF NOT EXISTS case_final_reviews (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    case_id TEXT NOT NULL REFERENCES cases(id),
    case_version INTEGER NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('approve', 'reject')),
    note TEXT NOT NULL DEFAULT '',
    decision_seq INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- S4: final-package approval binds package_id + recipients (nullable for legacy reviews)
ALTER TABLE case_final_reviews ADD COLUMN IF NOT EXISTS package_id TEXT;
ALTER TABLE case_final_reviews ADD COLUMN IF NOT EXISTS recipients JSONB;
ALTER TABLE case_final_reviews ADD COLUMN IF NOT EXISTS reviewer_id TEXT;

-- S2.3: keep every decision; gate uses lock-allocated decision_seq (not txn start time).
ALTER TABLE case_final_reviews
    DROP CONSTRAINT IF EXISTS case_final_reviews_tenant_id_case_id_case_version_key;

ALTER TABLE case_final_reviews
    ADD COLUMN IF NOT EXISTS decision_seq INTEGER;

-- Backfill only rows missing decision_seq (legacy DBs before S2.3 seq).
UPDATE case_final_reviews AS r
SET decision_seq = s.seq
FROM (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY tenant_id, case_id
               ORDER BY created_at ASC, id ASC
           ) AS seq
    FROM case_final_reviews
    WHERE decision_seq IS NULL
) AS s
WHERE r.id = s.id
  AND r.decision_seq IS NULL;

UPDATE cases AS c
SET final_review_seq = GREATEST(
    c.final_review_seq,
    COALESCE((
        SELECT MAX(fr.decision_seq)
        FROM case_final_reviews fr
        WHERE fr.tenant_id = c.tenant_id AND fr.case_id = c.id
    ), 0)
);

ALTER TABLE case_final_reviews
    ALTER COLUMN decision_seq SET DEFAULT 0;

-- Remaining NULLs (no rows matched) → 0 before NOT NULL.
UPDATE case_final_reviews SET decision_seq = 0 WHERE decision_seq IS NULL;

ALTER TABLE case_final_reviews
    ALTER COLUMN decision_seq SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_case_final_reviews_case_seq
    ON case_final_reviews (tenant_id, case_id, decision_seq DESC);

CREATE TABLE IF NOT EXISTS case_reopen_grants (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    case_id TEXT NOT NULL REFERENCES cases(id),
    authorized BOOLEAN NOT NULL DEFAULT TRUE,
    note TEXT NOT NULL DEFAULT '',
    bound_from_status TEXT,
    bound_case_version INTEGER,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE case_reopen_grants ADD COLUMN IF NOT EXISTS bound_from_status TEXT;
ALTER TABLE case_reopen_grants ADD COLUMN IF NOT EXISTS bound_case_version INTEGER;
ALTER TABLE case_reopen_grants ADD COLUMN IF NOT EXISTS bound_closure_seq INTEGER;
ALTER TABLE case_reopen_grants ADD COLUMN IF NOT EXISTS consumed_at TIMESTAMPTZ;

CREATE UNIQUE INDEX IF NOT EXISTS case_reopen_grants_one_active_per_closure
    ON case_reopen_grants (tenant_id, case_id, bound_closure_seq)
    WHERE consumed_at IS NULL AND authorized = TRUE;

CREATE TABLE IF NOT EXISTS case_deliveries (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    case_id TEXT NOT NULL REFERENCES cases(id),
    case_version INTEGER NOT NULL,
    sent_at TIMESTAMPTZ,
    receipt_at TIMESTAMPTZ,
    simulated_send BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE case_deliveries ADD COLUMN IF NOT EXISTS sent_at TIMESTAMPTZ;
ALTER TABLE case_deliveries ADD COLUMN IF NOT EXISTS simulated_send BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE case_deliveries ADD COLUMN IF NOT EXISTS package_id TEXT;
ALTER TABLE case_deliveries ADD COLUMN IF NOT EXISTS outbox_id TEXT;

CREATE INDEX IF NOT EXISTS idx_case_deliveries_case
    ON case_deliveries (tenant_id, case_id, created_at DESC);

CREATE TABLE IF NOT EXISTS email_attribution_audit (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    email_event_id TEXT NOT NULL,
    from_case_id TEXT,
    to_case_id TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- S1.4 model draft audit (synthetic context only; no side effects)
CREATE TABLE IF NOT EXISTS model_calls (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    case_id TEXT,
    case_version INTEGER,
    model TEXT NOT NULL,
    prompt_digest TEXT NOT NULL,
    request_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    response_text TEXT NOT NULL DEFAULT '',
    draft_text TEXT NOT NULL DEFAULT '',
    candidate_facts JSONB NOT NULL DEFAULT '[]'::jsonb,
    suggested_actions JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_model_calls_case
    ON model_calls (tenant_id, case_id, created_at DESC);

-- S2.1 document pipeline: isolated originals + human annotations
-- OCR (when run) stores derived text only — never mutates original bytes / auto-confirm.
CREATE TABLE IF NOT EXISTS case_evidence (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    case_id TEXT NOT NULL REFERENCES cases(id),
    case_version INTEGER NOT NULL,
    file_id TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    original_filename TEXT NOT NULL DEFAULT '',
    content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    byte_size INTEGER NOT NULL DEFAULT 0,
    classification TEXT NOT NULL DEFAULT 'unclassified',
    quarantine_status TEXT NOT NULL DEFAULT 'isolated'
        CHECK (quarantine_status IN ('isolated', 'rejected', 'accepted_for_review')),
    rejection_reason TEXT NOT NULL DEFAULT '',
    source_email_event_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_case_evidence_case
    ON case_evidence (tenant_id, case_id, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS case_evidence_sha_per_case
    ON case_evidence (tenant_id, case_id, content_sha256);

CREATE TABLE IF NOT EXISTS evidence_annotations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL REFERENCES case_evidence(id),
    case_id TEXT NOT NULL REFERENCES cases(id),
    case_version INTEGER NOT NULL,
    field_key TEXT NOT NULL,
    field_value TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_evidence_annotations_evidence
    ON evidence_annotations (tenant_id, evidence_id, created_at DESC);

-- Derived OCR artifacts (immutable originals stay byte-intact)
CREATE TABLE IF NOT EXISTS evidence_ocr_results (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL REFERENCES case_evidence(id),
    case_id TEXT NOT NULL REFERENCES cases(id),
    case_version INTEGER NOT NULL,
    provider TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('pending', 'completed', 'failed', 'unsupported')),
    extracted_text TEXT NOT NULL DEFAULT '',
    source_content_sha256 TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_evidence_ocr_evidence
    ON evidence_ocr_results (tenant_id, evidence_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_evidence_ocr_case
    ON evidence_ocr_results (tenant_id, case_id, created_at DESC);

-- S2.2 rules container (W05 minimal): versioned packs, human publish only
CREATE TABLE IF NOT EXISTS rule_packs (
    pack_id TEXT NOT NULL,
    pack_version INTEGER NOT NULL CHECK (pack_version >= 1),
    route TEXT NOT NULL,
    title TEXT NOT NULL,
    source_url TEXT NOT NULL DEFAULT '',
    source_retrieved_on DATE,
    published BOOLEAN NOT NULL DEFAULT FALSE,
    published_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (pack_id, pack_version)
);

CREATE TABLE IF NOT EXISTS rule_requirements (
    id TEXT PRIMARY KEY,
    pack_id TEXT NOT NULL,
    pack_version INTEGER NOT NULL,
    requirement_key TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL DEFAULT 'required'
        CHECK (severity IN ('required', 'advisory', 'transfer_human')),
    UNIQUE (pack_id, pack_version, requirement_key),
    FOREIGN KEY (pack_id, pack_version)
        REFERENCES rule_packs (pack_id, pack_version)
);

CREATE TABLE IF NOT EXISTS case_requirement_states (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    case_id TEXT NOT NULL REFERENCES cases(id),
    case_version INTEGER NOT NULL,
    pack_id TEXT NOT NULL,
    pack_version INTEGER NOT NULL,
    requirement_key TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, case_id, pack_id, pack_version, requirement_key)
);

CREATE INDEX IF NOT EXISTS idx_case_requirement_states_case
    ON case_requirement_states (tenant_id, case_id, updated_at DESC);

ALTER TABLE cases ADD COLUMN IF NOT EXISTS active_pack_id TEXT;
ALTER TABLE cases ADD COLUMN IF NOT EXISTS active_pack_version INTEGER;

-- S4 W09: immutable delivery packages (manifest + hashes from accepted evidence)
CREATE TABLE IF NOT EXISTS delivery_packages (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    case_id TEXT NOT NULL REFERENCES cases(id),
    case_version INTEGER NOT NULL,
    package_kind TEXT NOT NULL,
    manifest_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    manifest_digest TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_delivery_packages_case
    ON delivery_packages (tenant_id, case_id, created_at DESC);

-- Glossary OpenQuestion: remediation /补件 lifecycle (auto open/close)
CREATE TABLE IF NOT EXISTS open_questions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    case_id TEXT NOT NULL REFERENCES cases(id),
    case_version INTEGER NOT NULL DEFAULT 1,
    requirement_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'waiting_customer'
        CHECK (status IN (
            'open', 'waiting_customer', 'closed', 'escalated_human'
        )),
    severity TEXT NOT NULL DEFAULT 'simple_auto'
        CHECK (severity IN ('simple_auto', 'complex_human')),
    reason_code TEXT NOT NULL DEFAULT '',
    reason_detail TEXT NOT NULL DEFAULT '',
    evidence_id TEXT,
    closed_by_evidence_id TEXT,
    source_email_event_id TEXT,
    outbound_email_event_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_open_questions_case
    ON open_questions (tenant_id, case_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_open_questions_active_req
    ON open_questions (tenant_id, case_id, requirement_key)
    WHERE status IN ('open', 'waiting_customer', 'escalated_human');

-- Dynamic outbound allowlist (unioned with EMAIL_OUTBOUND_ALLOWLIST in .env).
-- Case create auto-inserts customer_email so opening/reply mail can send.
CREATE TABLE IF NOT EXISTS outbound_allowlist_entries (
    tenant_id TEXT NOT NULL,
    email TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'case_create',
    case_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, email)
);

CREATE INDEX IF NOT EXISTS idx_outbound_allowlist_tenant
    ON outbound_allowlist_entries (tenant_id, created_at DESC);
