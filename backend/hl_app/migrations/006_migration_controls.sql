ALTER TABLE operation_control ADD COLUMN cutover_baseline_id TEXT;
ALTER TABLE operation_control ADD COLUMN drain_deadline TIMESTAMPTZ;

CREATE TABLE migration_bundles (
    bundle_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    direction TEXT NOT NULL,
    manifest_sha256 CHAR(64) NOT NULL,
    business_sha256 CHAR(64) NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('success')),
    scope JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE migration_id_map (
    id BIGSERIAL PRIMARY KEY,
    bundle_id TEXT NOT NULL,
    entity TEXT NOT NULL,
    source_id TEXT NOT NULL,
    natural_key JSONB NOT NULL,
    target_id TEXT NOT NULL,
    UNIQUE (bundle_id, entity, source_id)
);

CREATE TABLE baseline_ledger (
    baseline_id TEXT PRIMARY KEY,
    manifest_sha256 CHAR(64) NOT NULL,
    business_sha256 CHAR(64) NOT NULL,
    target_instance TEXT NOT NULL,
    full_bundle_id TEXT NOT NULL REFERENCES migration_bundles(bundle_id),
    full_manifest_sha256 CHAR(64) NOT NULL,
    scope JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    cutover_at TIMESTAMPTZ
);

CREATE TABLE export_ledger (
    export_id TEXT PRIMARY KEY,
    purpose TEXT NOT NULL CHECK (purpose IN ('baseline', 'rollback_snapshot', 'rollback_delta')),
    baseline_id TEXT REFERENCES baseline_ledger(baseline_id),
    content_sha256 CHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
