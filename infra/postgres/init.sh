#!/bin/sh
# Runs once, the first time the Postgres container starts (docker-compose.yml).
# The app connects as incident_app, which is NOT a superuser: superusers bypass
# row-level security, so the app must never use one.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" -v pw="$APP_DB_PASSWORD" <<'SQL'
CREATE ROLE incident_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD :'pw';
CREATE DATABASE incident_agent OWNER incident_app ENCODING 'UTF8' TEMPLATE template0;
SQL
