CREATE TABLE hyperliquid_pair_risk_rules (
    id BIGSERIAL PRIMARY KEY,
    owner_account_id BIGINT REFERENCES auth_accounts(id),
    network TEXT NOT NULL CHECK (network IN ('mainnet', 'testnet')),
    pair_id TEXT NOT NULL CHECK (pair_id ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND length(pair_id) <= 64),
    account_address CHAR(42) NOT NULL CHECK (account_address ~ '^0x[0-9a-f]{40}$' AND account_address <> '0x0000000000000000000000000000000000000000'),
    pair_config_revision CHAR(64) NOT NULL CHECK (pair_config_revision ~ '^[0-9a-f]{64}$'),
    enabled BOOLEAN NOT NULL DEFAULT false,
    take_profit_usd NUMERIC CHECK (take_profit_usd > 0),
    stop_loss_usd NUMERIC CHECK (stop_loss_usd > 0),
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision >= 1),
    status TEXT NOT NULL DEFAULT 'disabled' CHECK (status IN ('disabled', 'monitoring', 'triggered', 'executing', 'completed', 'incomplete', 'unknown', 'unavailable')),
    last_attempt_at TIMESTAMPTZ,
    last_checked_at TIMESTAMPTZ,
    pair_unrealized_pnl NUMERIC,
    position_state TEXT CHECK (position_state IS NULL OR position_state IN ('flat', 'single_leg', 'opposite', 'same_direction', 'unknown')),
    trigger_kind TEXT CHECK (trigger_kind IS NULL OR trigger_kind IN ('take_profit', 'stop_loss')),
    triggered_at TIMESTAMPTZ,
    trigger_pnl NUMERIC,
    last_action_id TEXT,
    last_request_id UUID,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (NOT enabled OR take_profit_usd IS NOT NULL OR stop_loss_usd IS NOT NULL)
);

CREATE UNIQUE INDEX hyperliquid_pair_risk_rules_scope_idx
    ON hyperliquid_pair_risk_rules (pair_id, account_address, COALESCE(owner_account_id, 0));
CREATE INDEX hyperliquid_pair_risk_rules_enabled_idx
    ON hyperliquid_pair_risk_rules (enabled, id) WHERE enabled;
CREATE INDEX hyperliquid_pair_risk_rules_owner_idx
    ON hyperliquid_pair_risk_rules (owner_account_id, pair_id, account_address);

CREATE TABLE hyperliquid_pair_risk_events (
    id BIGSERIAL PRIMARY KEY,
    rule_id BIGINT NOT NULL REFERENCES hyperliquid_pair_risk_rules(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL CHECK (event_type IN ('rule_updated', 'triggered', 'trigger_cleared', 'completed', 'execution_updated')),
    trigger_kind TEXT CHECK (trigger_kind IS NULL OR trigger_kind IN ('take_profit', 'stop_loss')),
    pair_unrealized_pnl NUMERIC,
    action_id TEXT,
    request_id UUID,
    detail TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX hyperliquid_pair_risk_events_rule_idx
    ON hyperliquid_pair_risk_events (rule_id, id DESC);
