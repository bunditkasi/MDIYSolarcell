-- =============================================================================
-- 01_extensions.sql
-- Runs first (filename order) inside docker-entrypoint-initdb.d.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- gen_random_uuid() is built into PostgreSQL 13+; no pgcrypto needed.

-- Case-insensitive, accent-tolerant store search (used by StoreFilter.search).
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;

DO $$
BEGIN
    RAISE NOTICE 'TimescaleDB extension ready: %',
        (SELECT extversion FROM pg_extension WHERE extname = 'timescaledb');
END
$$;
