ALTER TABLE watchlist_items ADD COLUMN owner_account_id BIGINT REFERENCES auth_accounts(id);

ALTER TABLE watchlist_items DROP CONSTRAINT watchlist_items_security_id_key;

INSERT INTO watchlist_items (
    security_id, owner_account_id, group_name, note, research_url,
    sort_order, enabled, created_at, updated_at
)
SELECT w.security_id, a.id, w.group_name, w.note, w.research_url,
       w.sort_order, w.enabled, w.created_at, w.updated_at
FROM watchlist_items w
CROSS JOIN auth_accounts a
WHERE w.owner_account_id IS NULL
  AND a.id <> (SELECT min(id) FROM auth_accounts);

UPDATE watchlist_items
SET owner_account_id = (SELECT min(id) FROM auth_accounts)
WHERE owner_account_id IS NULL;

CREATE UNIQUE INDEX watchlist_items_owner_security_idx
    ON watchlist_items (COALESCE(owner_account_id, 0), security_id);
DROP INDEX watchlist_items_enabled_idx;
CREATE INDEX watchlist_items_enabled_idx
    ON watchlist_items (owner_account_id, enabled, security_id);

CREATE TABLE quote_collection_state (
    security_id BIGINT PRIMARY KEY REFERENCES securities(id),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'collecting', 'success', 'failed')),
    last_attempt_at TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,
    latest_trade_date DATE,
    last_record_count INTEGER CHECK (last_record_count IS NULL OR last_record_count >= 0),
    last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 500),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO quote_collection_state (security_id, status)
SELECT DISTINCT security_id, 'pending' FROM watchlist_items WHERE enabled = true;

CREATE INDEX quote_collection_state_due_idx
    ON quote_collection_state (next_attempt_at, status);
