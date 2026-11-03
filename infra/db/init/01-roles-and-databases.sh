#!/bin/bash
# Runs once, as the bootstrap superuser, on an empty data volume (ADR-0004). Creates the four
# application roles and the per-database schema ownership/grants. Table grants live in migrations.
# Must keep LF line endings (the container runs it with bash).
set -euo pipefail

psql() { command psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" "$@"; }

psql --dbname postgres \
  -v migrator_pw="$AEGIS_MIGRATOR_PASSWORD" -v api_pw="$AEGIS_API_PASSWORD" \
  -v worker_pw="$AEGIS_WORKER_PASSWORD" -v system_pw="$AEGIS_SYSTEM_PASSWORD" <<'SQL'
CREATE ROLE aegis_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'migrator_pw';
CREATE ROLE aegis_api      LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'api_pw';
CREATE ROLE aegis_worker   LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'worker_pw';
CREATE ROLE aegis_system   LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS PASSWORD :'system_pw';
CREATE DATABASE aegisgrid_test OWNER aegis_migrator;
ALTER DATABASE aegisgrid OWNER TO aegis_migrator;
SQL

for db in aegisgrid aegisgrid_test; do
  psql --dbname "$db" -v db="$db" <<'SQL'
REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db" TO aegis_migrator, aegis_api, aegis_worker, aegis_system;

ALTER SCHEMA public OWNER TO aegis_migrator;
REVOKE CREATE, USAGE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO aegis_migrator, aegis_api, aegis_worker, aegis_system;

-- Checkpoint schema (ADR-0012): only the worker may touch it; the api and system roles get nothing.
CREATE SCHEMA runtime AUTHORIZATION aegis_migrator;
GRANT USAGE ON SCHEMA runtime TO aegis_worker;
SQL
done
