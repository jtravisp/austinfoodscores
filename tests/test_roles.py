"""Integration test for bootstrap_roles.sql against local Postgres.

Local Postgres has no rds_iam role, so the test creates a stand-in. Roles
are cluster-wide, so the afs_* roles persist in the docker cluster after
the test; that's harmless. SET ROLE stands in for logging in as each role.
"""

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from afs import db
from conftest import ADMIN_DSN

ROLES_DSN = make_conninfo(ADMIN_DSN, dbname="afs_roles_test")


@pytest.fixture(scope="module")
def roles_db():
    try:
        admin = psycopg.connect(ADMIN_DSN, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip("Postgres not running (docker compose up -d)")
    with admin:
        admin.execute("DROP DATABASE IF EXISTS afs_roles_test WITH (FORCE)")
        admin.execute("CREATE DATABASE afs_roles_test")
        admin.execute(
            "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rds_iam') "
            "THEN CREATE ROLE rds_iam NOLOGIN; END IF; END $$"
        )

    with db.connect(ROLES_DSN) as conn:
        db.bootstrap_roles(conn)            # as the "master" (local superuser)
        conn.execute("SET ROLE afs_migrator")
        db.migrate(conn)                    # objects end up owned by afs_migrator
    return ROLES_DSN


def as_role(dsn: str, role: str) -> psycopg.Connection:
    conn = db.connect(dsn)
    conn.execute(f"SET ROLE {role}")
    return conn


def test_migrator_owns_the_schema(roles_db):
    with db.connect(roles_db) as conn:
        owners = {r[0] for r in conn.execute(
            "SELECT tableowner FROM pg_tables WHERE schemaname = 'public'"
        )}
        db_owner = conn.execute(
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()"
        ).fetchone()[0]
    assert owners == {"afs_migrator"}
    assert db_owner == "afs_migrator"


def test_loader_can_write_but_not_delete(roles_db):
    with as_role(roles_db, "afs_loader") as conn:
        conn.execute(
            "INSERT INTO establishments (facility_id, name, first_seen, last_seen) "
            "VALUES (1, 'x', '2026-01-01', '2026-01-01')"
        )
        conn.execute("UPDATE establishments SET name = 'y' WHERE facility_id = 1")
        conn.execute("INSERT INTO ingest_runs DEFAULT VALUES")  # needs the identity sequence
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM establishments")


def test_reader_is_read_only(roles_db):
    with as_role(roles_db, "afs_reader") as conn:
        conn.execute("SELECT * FROM establishment_metrics LIMIT 1")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO ingest_runs DEFAULT VALUES")


def test_bootstrap_is_idempotent(roles_db):
    with db.connect(roles_db) as conn:
        db.bootstrap_roles(conn)
