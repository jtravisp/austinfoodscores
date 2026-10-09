"""Database connection and schema migrations."""

import os
from importlib.resources import files

import psycopg


def connect(dsn: str | None = None) -> psycopg.Connection:
    """Connect using an explicit DSN or the DATABASE_URL environment variable.

    In AWS this will be replaced by an IAM auth token (Phase 2).
    """
    return psycopg.connect(dsn or os.environ["DATABASE_URL"])


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply any migrations in afs/migrations/ not yet recorded. Returns versions applied.

    Each migration runs in its own transaction together with its bookkeeping
    row, so a failed migration leaves no partial schema behind.
    """
    with conn.transaction():
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    text PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}

    scripts = sorted(
        (f for f in files("afs.migrations").iterdir() if f.name.endswith(".sql")),
        key=lambda f: f.name,
    )
    newly_applied = []
    for script in scripts:
        version = script.name.removesuffix(".sql")
        if version in applied:
            continue
        with conn.transaction():
            conn.execute(script.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
        newly_applied.append(version)
    return newly_applied
