-- ============================================================================
-- Phase 1 - read-only auditor role, scoped to the Phase 0 seed database.
--
-- The auditor must NEVER be able to write to a database it scans
-- (see ../CLAUDE.md). This role is the primary enforcement of that rule:
-- it can log in and run SELECT in the `dma_auditor` database and nothing else.
--
-- Run as a superuser (the installer's `postgres` account):
--     psql -U postgres -f migrations/001_create_readonly_role.sql
--
-- Idempotent: safe to re-run. Uses psql meta-commands (\connect), so it must
-- be run through psql, not a driver.
-- ============================================================================

\set ON_ERROR_STOP on

-- ----------------------------------------------------------------------------
-- 1. The role: LOGIN only. No superuser, no CREATEDB/ROLE, no replication.
-- ----------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dma_auditor_ro') THEN
        CREATE ROLE dma_auditor_ro LOGIN PASSWORD 'dma_auditor_ro_pw';
        RAISE NOTICE 'created role dma_auditor_ro';
    ELSE
        RAISE NOTICE 'role dma_auditor_ro already exists - reapplying grants';
    END IF;
END
$$;

ALTER ROLE dma_auditor_ro
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT;

-- Keep every session this role opens read-only, even ones it starts itself,
-- and stop a runaway audit query from pinning the target DB.
ALTER ROLE dma_auditor_ro SET default_transaction_read_only = on;
ALTER ROLE dma_auditor_ro SET statement_timeout = '30s';
ALTER ROLE dma_auditor_ro SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE dma_auditor_ro SET lock_timeout = '5s';

-- ----------------------------------------------------------------------------
-- 2. Scope it to just the dma_auditor database / public schema, SELECT only.
-- ----------------------------------------------------------------------------
\connect dma_auditor

-- Strip the implicit privileges PUBLIC gets (CONNECT + TEMP), then hand back
-- only CONNECT, only to this role. No TEMP => the role cannot create temp
-- tables either.
REVOKE ALL ON DATABASE dma_auditor FROM PUBLIC;
GRANT  CONNECT ON DATABASE dma_auditor TO dma_auditor_ro;

GRANT USAGE  ON SCHEMA public TO dma_auditor_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO dma_auditor_ro;

-- Tables the seeder creates on a later --reset are covered automatically.
ALTER DEFAULT PRIVILEGES FOR ROLE dma_seed IN SCHEMA public
    GRANT SELECT ON TABLES TO dma_auditor_ro;

-- Belt and braces: explicitly remove every write path.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON ALL TABLES IN SCHEMA public FROM dma_auditor_ro;
REVOKE CREATE ON SCHEMA public FROM dma_auditor_ro;
REVOKE ALL ON ALL SEQUENCES  IN SCHEMA public FROM dma_auditor_ro;
REVOKE ALL ON ALL FUNCTIONS  IN SCHEMA public FROM dma_auditor_ro;

-- ----------------------------------------------------------------------------
-- 3. Show the result.
-- ----------------------------------------------------------------------------
\echo 'Role attributes:'
SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolcanlogin, rolreplication, rolbypassrls
FROM pg_roles WHERE rolname = 'dma_auditor_ro';

\echo 'Table privileges held by dma_auditor_ro (expect SELECT only):'
SELECT table_name, privilege_type
FROM information_schema.role_table_grants
WHERE grantee = 'dma_auditor_ro'
ORDER BY table_name, privilege_type;
