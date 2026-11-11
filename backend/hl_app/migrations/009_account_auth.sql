CREATE TABLE auth_accounts (
    id BIGSERIAL PRIMARY KEY,
    email TEXT NOT NULL UNIQUE CHECK (email = lower(btrim(email)) AND length(email) BETWEEN 3 AND 254),
    password_hash TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE auth_sessions ADD COLUMN account_id BIGINT REFERENCES auth_accounts(id);
CREATE INDEX auth_sessions_account_idx ON auth_sessions (account_id, expires_at) WHERE revoked_at IS NULL;

ALTER TABLE hyperliquid_pair_accounts ADD COLUMN owner_account_id BIGINT REFERENCES auth_accounts(id);
ALTER TABLE hyperliquid_api_wallets ADD COLUMN owner_account_id BIGINT REFERENCES auth_accounts(id);
ALTER TABLE hyperliquid_execution_requests ADD COLUMN owner_account_id BIGINT REFERENCES auth_accounts(id);

CREATE INDEX hyperliquid_pair_accounts_owner_idx ON hyperliquid_pair_accounts (owner_account_id, pair_id);
CREATE INDEX hyperliquid_api_wallets_owner_idx ON hyperliquid_api_wallets (owner_account_id, network, pair_id);
CREATE INDEX hyperliquid_execution_requests_owner_idx ON hyperliquid_execution_requests (owner_account_id, request_id);

DROP INDEX hyperliquid_pair_accounts_active_idx;
CREATE UNIQUE INDEX hyperliquid_pair_accounts_active_idx
    ON hyperliquid_pair_accounts (pair_id, COALESCE(owner_account_id, 0)) WHERE is_active;
