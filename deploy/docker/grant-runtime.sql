GRANT USAGE ON SCHEMA hl_app TO hl_app_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA hl_app TO hl_app_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA hl_app TO hl_app_runtime;

REVOKE INSERT, UPDATE, DELETE ON operation_control FROM hl_app_runtime;
REVOKE INSERT, UPDATE, DELETE ON auth_control FROM hl_app_runtime;
REVOKE INSERT, UPDATE, DELETE ON schema_migrations FROM hl_app_runtime;
REVOKE INSERT, UPDATE, DELETE ON migration_bundles FROM hl_app_runtime;
REVOKE INSERT, UPDATE, DELETE ON migration_id_map FROM hl_app_runtime;
REVOKE INSERT, UPDATE, DELETE ON baseline_ledger FROM hl_app_runtime;
REVOKE INSERT, UPDATE, DELETE ON export_ledger FROM hl_app_runtime;
REVOKE UPDATE, DELETE ON hyperliquid_api_wallet_events FROM hl_app_runtime;
REVOKE UPDATE, DELETE ON hyperliquid_pair_risk_events FROM hl_app_runtime;
REVOKE UPDATE, DELETE ON hyperliquid_rebalance_events FROM hl_app_runtime;
REVOKE UPDATE, DELETE ON hl_news_sync_event_receipt FROM hl_app_runtime;
REVOKE UPDATE, DELETE ON hl_news_sync_source_map FROM hl_app_runtime;
