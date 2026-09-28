-- Runs once, when the volume is first created (docker-entrypoint-initdb.d).
-- The main database (POSTGRES_DB=ods_delivery) already exists; add the test one
-- and the extensions in both, so the application role never needs superuser.
CREATE DATABASE ods_delivery_test OWNER ods_delivery;

\connect ods_delivery
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

\connect ods_delivery_test
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
