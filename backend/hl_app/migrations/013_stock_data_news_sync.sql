ALTER TABLE watchlist_announcement_source
    ADD COLUMN ingest_mode TEXT NOT NULL DEFAULT 'direct' CHECK (ingest_mode IN ('direct', 'stock_data'));
CREATE INDEX watchlist_announcement_source_ingest_idx
    ON watchlist_announcement_source (ingest_mode, enabled, source_status);

CREATE TABLE hl_news_sync_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    source_instance TEXT,
    next_cursor TEXT,
    snapshot_at TIMESTAMPTZ,
    last_sync_seq BIGINT CHECK (last_sync_seq IS NULL OR last_sync_seq >= 0),
    bootstrap_status TEXT NOT NULL DEFAULT 'not_started' CHECK (bootstrap_status IN ('not_started', 'in_progress', 'completed')),
    bootstrap_completed_at TIMESTAMPTZ,
    bootstrap_business_hash TEXT,
    last_attempt_at TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,
    last_error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO hl_news_sync_state (singleton) VALUES (true);

CREATE TABLE hl_news_sync_event_receipt (
    source_instance TEXT NOT NULL,
    sync_seq BIGINT NOT NULL CHECK (sync_seq > 0),
    event_kind TEXT NOT NULL,
    payload_sha256 CHAR(64) NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source_instance, sync_seq)
);

CREATE TABLE hl_news_sync_source_map (
    source_instance TEXT NOT NULL,
    upstream_source_id BIGINT NOT NULL CHECK (upstream_source_id >= 0),
    local_source_id BIGINT NOT NULL REFERENCES watchlist_announcement_source(source_id) ON DELETE RESTRICT,
    natural_key JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source_instance, upstream_source_id),
    UNIQUE (local_source_id)
);

CREATE TABLE hl_news_sync_source_state (
    local_source_id BIGINT PRIMARY KEY REFERENCES watchlist_announcement_source(source_id) ON DELETE CASCADE,
    open_gaps INTEGER NOT NULL CHECK (open_gaps >= 0),
    earliest_gap_start TIMESTAMPTZ,
    sync_seq BIGINT NOT NULL CHECK (sync_seq > 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
