-- Runs once, the first time the Postgres container starts.
-- The API connects as incident_app, which is NOT a superuser: superusers
-- bypass row-level security, so the app must never use one.
CREATE ROLE incident_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD 'incident_app';
CREATE DATABASE incident_agent OWNER incident_app ENCODING 'UTF8' TEMPLATE template0;
CREATE DATABASE incident_test OWNER incident_app ENCODING 'UTF8' TEMPLATE template0;
