-- One-time database setup, run as the RDS master user (the only time the
-- master password is used). Idempotent: safe to run again.
--
-- Roles (all log in with IAM auth tokens via the rds_iam role):
--   afs_migrator  owns the database and every object in it; runs migrations
--   afs_loader    read + insert/update (load Lambda). No DELETE: history is never removed.
--   afs_reader    read-only (query Lambda)

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'afs_migrator') THEN
        CREATE ROLE afs_migrator LOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'afs_loader') THEN
        CREATE ROLE afs_loader LOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'afs_reader') THEN
        CREATE ROLE afs_reader LOGIN;
    END IF;
END $$;

-- rds_iam: this role must authenticate with an IAM token, not a password.
GRANT rds_iam TO afs_migrator, afs_loader, afs_reader;

-- Membership lets the master hand ownership to afs_migrator and define
-- default privileges on its behalf.
GRANT afs_migrator TO CURRENT_USER;

-- In Postgres 15+ the public schema is owned by the database owner, so this
-- also gives afs_migrator CREATE on public.
DO $$
BEGIN
    EXECUTE format('ALTER DATABASE %I OWNER TO afs_migrator', current_database());
END $$;

GRANT USAGE ON SCHEMA public TO afs_loader, afs_reader;

-- Privileges on objects afs_migrator creates in the future (every migration)...
ALTER DEFAULT PRIVILEGES FOR ROLE afs_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE ON TABLES TO afs_loader;
ALTER DEFAULT PRIVILEGES FOR ROLE afs_migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO afs_loader;
ALTER DEFAULT PRIVILEGES FOR ROLE afs_migrator IN SCHEMA public
    GRANT SELECT ON TABLES TO afs_reader;

-- ...and on anything that already exists (when re-run after migrations).
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO afs_loader;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO afs_loader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO afs_reader;
