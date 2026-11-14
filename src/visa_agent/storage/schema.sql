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
