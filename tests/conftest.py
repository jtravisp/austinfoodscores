"""Fixtures for integration tests that need the docker compose Postgres.

They use a separate `afs_test` database (created on demand), so running the
tests never touches data you've loaded into `afs`. If Postgres isn't running,
these tests are skipped rather than failed.
"""

import os

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from afs import db

ADMIN_DSN = os.environ.get("DATABASE_URL", "postgresql://afs:afs@localhost:5432/afs")
TEST_DSN = make_conninfo(ADMIN_DSN, dbname="afs_test")


@pytest.fixture(scope="session")
def test_database() -> str:
    try:
        admin = psycopg.connect(ADMIN_DSN, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip("Postgres not running (docker compose up -d)")
    with admin:
        name = conninfo_to_dict(TEST_DSN)["dbname"]
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {name}")
    with db.connect(TEST_DSN) as conn:
        db.migrate(conn)
    return TEST_DSN


@pytest.fixture
def conn(test_database):
    """A connection to a freshly emptied test database."""
    with db.connect(test_database) as conn:
        conn.execute("TRUNCATE ingest_runs, inspections, establishments RESTART IDENTITY")
        yield conn
