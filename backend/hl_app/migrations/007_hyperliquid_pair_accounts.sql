CREATE TABLE hyperliquid_pair_accounts (
    id BIGSERIAL PRIMARY KEY,
    pair_id TEXT NOT NULL CHECK (pair_id ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND length(pair_id) <= 64),
    label TEXT NOT NULL CHECK (length(trim(label)) BETWEEN 1 AND 80),
    address CHAR(42) NOT NULL CHECK (address ~ '^0x[0-9a-f]{40}$' AND address <> '0x0000000000000000000000000000000000000000'),
    is_active BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (pair_id, address)
);

CREATE UNIQUE INDEX hyperliquid_pair_accounts_active_idx
    ON hyperliquid_pair_accounts (pair_id) WHERE is_active;
CREATE INDEX hyperliquid_pair_accounts_pair_idx
    ON hyperliquid_pair_accounts (pair_id, id);
