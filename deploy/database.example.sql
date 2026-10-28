CREATE ROLE hl_app_runtime LOGIN PASSWORD 'replace-runtime-secret';
CREATE ROLE hl_app_migration LOGIN PASSWORD 'replace-migration-secret';
CREATE DATABASE hl_app OWNER hl_app_migration;

REVOKE ALL ON DATABASE hl_app FROM PUBLIC;
GRANT CONNECT ON DATABASE hl_app TO hl_app_runtime;

\c hl_app

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO hl_app_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO hl_app_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO hl_app_runtime;

REVOKE INSERT, UPDATE, DELETE ON operation_control FROM hl_app_runtime;
REVOKE INSERT, UPDATE, DELETE ON auth_control FROM hl_app_runtime;
REVOKE INSERT, UPDATE, DELETE ON schema_migrations FROM hl_app_runtime;
REVOKE INSERT, UPDATE, DELETE ON migration_bundles FROM hl_app_runtime;
REVOKE INSERT, UPDATE, DELETE ON migration_id_map FROM hl_app_runtime;
REVOKE INSERT, UPDATE, DELETE ON baseline_ledger FROM hl_app_runtime;
REVOKE INSERT, UPDATE, DELETE ON export_ledger FROM hl_app_runtime;

ALTER DEFAULT PRIVILEGES FOR ROLE hl_app_migration IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO hl_app_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE hl_app_migration IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO hl_app_runtime;

REVOKE INSERT, UPDATE, DELETE ON operation_control FROM hl_app_runtime;
REVOKE INSERT, UPDATE, DELETE ON auth_control FROM hl_app_runtime;
REVOKE INSERT, UPDATE, DELETE ON schema_migrations FROM hl_app_runtime;
REVOKE INSERT, UPDATE, DELETE ON migration_bundles FROM hl_app_runtime;
REVOKE INSERT, UPDATE, DELETE ON migration_id_map FROM hl_app_runtime;
REVOKE INSERT, UPDATE, DELETE ON baseline_ledger FROM hl_app_runtime;
REVOKE INSERT, UPDATE, DELETE ON export_ledger FROM hl_app_runtime;
