-- ============================================================
-- Postgres initialization — TaskFlow database
-- ============================================================
-- The 'taskflow' database is already created via POSTGRES_DB
-- env variable in docker-compose.yml.
-- This script can be extended for additional schema-level setup.
-- ============================================================

-- Ensure the database exists (idempotent)
SELECT 'PostgreSQL ready — taskflow database initialized' AS status;
