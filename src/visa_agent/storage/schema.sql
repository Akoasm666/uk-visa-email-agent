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
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, dedupe_key)
);

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
        'failed', 'status_unknown'
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
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, case_id, case_version)
);

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

-- S2.1 document pipeline: isolated originals + human annotations (no OCR confirm)
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
