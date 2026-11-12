CREATE TABLE hyperliquid_rebalance_rules (
    id BIGSERIAL PRIMARY KEY,
    owner_account_id BIGINT REFERENCES auth_accounts(id),
    network TEXT NOT NULL CHECK (network IN ('mainnet', 'testnet')),
    pair_id TEXT NOT NULL CHECK (pair_id ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND length(pair_id) <= 64),
    account_address CHAR(42) NOT NULL CHECK (account_address ~ '^0x[0-9a-f]{40}$' AND account_address <> '0x0000000000000000000000000000000000000000'),
    pair_config_revision CHAR(64) NOT NULL CHECK (pair_config_revision ~ '^[0-9a-f]{64}$'),
    enabled BOOLEAN NOT NULL DEFAULT false,
    target_notional NUMERIC NOT NULL CHECK (target_notional > 0),
    threshold_pct NUMERIC NOT NULL CHECK (threshold_pct > 0 AND threshold_pct <= 100),
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision >= 1),
    status TEXT NOT NULL DEFAULT 'disabled' CHECK (status IN ('disabled', 'monitoring', 'candidate', 'executing', 'cooling', 'incomplete', 'unknown', 'unavailable')),
    last_attempt_at TIMESTAMPTZ,
    last_checked_at TIMESTAMPTZ,
    consecutive_count INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_count >= 0),
    current_notionals JSONB,
    drift_pct JSONB,
    open_orders INTEGER CHECK (open_orders IS NULL OR open_orders >= 0),
    position_state TEXT CHECK (position_state IS NULL OR position_state IN ('flat', 'single_leg', 'opposite', 'same_direction', 'unknown')),
    observed_leg_indices JSONB NOT NULL DEFAULT '[]',
    candidate_leg_indices JSONB NOT NULL DEFAULT '[]',
    candidate_preview JSONB,
    candidate_at TIMESTAMPTZ,
    cooldown_until TIMESTAMPTZ,
    last_action_id TEXT,
    last_request_id UUID,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX hyperliquid_rebalance_rules_scope_idx
    ON hyperliquid_rebalance_rules (pair_id, account_address, COALESCE(owner_account_id, 0));
CREATE INDEX hyperliquid_rebalance_rules_enabled_idx
    ON hyperliquid_rebalance_rules (enabled, id) WHERE enabled;
CREATE INDEX hyperliquid_rebalance_rules_owner_idx
    ON hyperliquid_rebalance_rules (owner_account_id, pair_id, account_address);

CREATE TABLE hyperliquid_rebalance_events (
    id BIGSERIAL PRIMARY KEY,
    rule_id BIGINT NOT NULL REFERENCES hyperliquid_rebalance_rules(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL CHECK (event_type IN ('rule_updated', 'candidate', 'candidate_cleared', 'execution_updated')),
    leg_indices JSONB NOT NULL DEFAULT '[]',
    snapshot JSONB,
    action_id TEXT,
    request_id UUID,
    detail TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX hyperliquid_rebalance_events_rule_idx
    ON hyperliquid_rebalance_events (rule_id, id DESC);
