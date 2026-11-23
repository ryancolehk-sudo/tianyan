#!/bin/sh
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=migrate_password="$DB_MIGRATE_PASSWORD" --set=app_password="$DB_APP_PASSWORD" <<'SQL'
CREATE ROLE jlpm_migrate LOGIN PASSWORD :'migrate_password';
CREATE ROLE jlpm_app LOGIN PASSWORD :'app_password';
GRANT CONNECT ON DATABASE jiulong TO jlpm_migrate, jlpm_app;
ALTER SCHEMA public OWNER TO jlpm_migrate;
GRANT USAGE ON SCHEMA public TO jlpm_app;
ALTER DEFAULT PRIVILEGES FOR ROLE jlpm_migrate IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO jlpm_app;
ALTER DEFAULT PRIVILEGES FOR ROLE jlpm_migrate IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO jlpm_app;
SQL
if [ -n "${DB_MAINTENANCE_PASSWORD:-}" ]; then
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=maintenance_password="$DB_MAINTENANCE_PASSWORD" <<'SQL'
CREATE ROLE jlpm_maintenance LOGIN PASSWORD :'maintenance_password';
GRANT CONNECT ON DATABASE jiulong TO jlpm_maintenance;
GRANT USAGE ON SCHEMA public TO jlpm_maintenance;
GRANT pg_read_all_data TO jlpm_maintenance;
SQL
fi
