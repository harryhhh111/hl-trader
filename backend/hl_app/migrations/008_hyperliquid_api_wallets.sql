CREATE TABLE hyperliquid_api_wallets (
    network TEXT NOT NULL CHECK (network IN ('mainnet', 'testnet')),
    pair_id TEXT NOT NULL CHECK (pair_id ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND length(pair_id) <= 64),
    account_address CHAR(42) NOT NULL CHECK (account_address ~ '^0x[0-9a-f]{40}$' AND account_address <> '0x0000000000000000000000000000000000000000'),
    wallet_address CHAR(42) NOT NULL CHECK (wallet_address ~ '^0x[0-9a-f]{40}$' AND wallet_address <> '0x0000000000000000000000000000000000000000' AND wallet_address <> account_address),
    wallet_version BIGINT NOT NULL CHECK (wallet_version >= 1),
    status TEXT NOT NULL CHECK (status IN ('active', 'deleted')),
    encrypted_private_key TEXT,
    encryption_version SMALLINT NOT NULL CHECK (encryption_version = 1),
    data_key_id TEXT NOT NULL CHECK (length(data_key_id) BETWEEN 1 AND 128),
    authorization_master CHAR(42) CHECK (authorization_master IS NULL OR authorization_master ~ '^0x[0-9a-f]{40}$'),
    authorization_checked_at TIMESTAMPTZ,
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (network, pair_id, account_address),
    CHECK ((status = 'active' AND encrypted_private_key LIKE 'ENC:v1:%') OR (status = 'deleted' AND encrypted_private_key IS NULL))
);

CREATE UNIQUE INDEX hyperliquid_api_wallets_active_address_idx
    ON hyperliquid_api_wallets (network, wallet_address) WHERE status = 'active';

CREATE TABLE hyperliquid_api_wallet_challenges (
    challenge_hash CHAR(64) PRIMARY KEY CHECK (challenge_hash ~ '^[0-9a-f]{64}$'),
    actor_binding TEXT NOT NULL,
    network TEXT NOT NULL CHECK (network IN ('mainnet', 'testnet')),
    pair_id TEXT NOT NULL,
    account_address CHAR(42) NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('save', 'rotate', 'delete')),
    wallet_version BIGINT NOT NULL CHECK (wallet_version >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ,
    CHECK (expires_at > created_at)
);

CREATE INDEX hyperliquid_api_wallet_challenges_scope_idx
    ON hyperliquid_api_wallet_challenges (network, pair_id, account_address, expires_at);

CREATE TABLE hyperliquid_api_wallet_retired_addresses (
    network TEXT NOT NULL CHECK (network IN ('mainnet', 'testnet')),
    wallet_address CHAR(42) NOT NULL CHECK (wallet_address ~ '^0x[0-9a-f]{40}$'),
    pair_id TEXT NOT NULL,
    account_address CHAR(42) NOT NULL,
    first_used_at TIMESTAMPTZ NOT NULL,
    retired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (network, wallet_address)
);

CREATE TABLE hyperliquid_execution_requests (
    request_id UUID PRIMARY KEY,
    actor TEXT NOT NULL,
    network TEXT NOT NULL CHECK (network IN ('mainnet', 'testnet')),
    pair_id TEXT NOT NULL,
    account_address CHAR(42) NOT NULL,
    config_revision CHAR(64) NOT NULL CHECK (config_revision ~ '^[0-9a-f]{64}$'),
    target_notional NUMERIC NOT NULL CHECK (target_notional >= 0),
    request_payload_hash CHAR(64) NOT NULL CHECK (request_payload_hash ~ '^[0-9a-f]{64}$'),
    preview_hash CHAR(64) NOT NULL CHECK (preview_hash ~ '^[0-9a-f]{64}$'),
    wallet_address CHAR(42) NOT NULL,
    wallet_version BIGINT NOT NULL CHECK (wallet_version >= 1),
    status TEXT NOT NULL CHECK (status IN ('previewed', 'running', 'succeeded', 'incomplete', 'failed', 'unknown')),
    action_id TEXT,
    result JSONB,
    error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);

CREATE INDEX hyperliquid_execution_requests_scope_idx
    ON hyperliquid_execution_requests (network, pair_id, account_address, created_at DESC);

CREATE UNIQUE INDEX hyperliquid_execution_requests_active_idx
    ON hyperliquid_execution_requests (network, pair_id, account_address)
    WHERE status IN ('running', 'unknown');

CREATE TABLE hyperliquid_api_wallet_events (
    id BIGSERIAL PRIMARY KEY,
    event_type TEXT NOT NULL CHECK (event_type IN ('save', 'rotate', 'delete', 'execute_requested', 'execute_finished')),
    network TEXT NOT NULL CHECK (network IN ('mainnet', 'testnet')),
    pair_id TEXT NOT NULL,
    account_address CHAR(42) NOT NULL,
    wallet_address CHAR(42) NOT NULL,
    wallet_version BIGINT NOT NULL,
    actor TEXT NOT NULL,
    request_id UUID,
    action_id TEXT,
    outcome TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX hyperliquid_api_wallet_events_scope_idx
    ON hyperliquid_api_wallet_events (network, pair_id, account_address, id);
