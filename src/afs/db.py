"""Database access: connection, migrations, ingest-run bookkeeping, upserts.

All SQL lives here; callers pass and receive plain Python values.
"""

import os
from collections.abc import Iterable
from dataclasses import asdict
from datetime import date, datetime
from importlib.resources import files

import psycopg
from psycopg.types.json import Jsonb

from afs.transform import Establishment, Inspection


def connect(dsn: str | None = None) -> psycopg.Connection:
    """Connect using an explicit DSN or the DATABASE_URL environment variable.

    In AWS this will be replaced by an IAM auth token (Phase 2).

    autocommit=True means nothing is transactional unless wrapped in an
    explicit `with conn.transaction():` block, so every transaction boundary
    is visible in the code.
    """
    return psycopg.connect(dsn or os.environ["DATABASE_URL"], autocommit=True)


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


# --- ingest runs -----------------------------------------------------------------

def start_run(conn: psycopg.Connection, watermark: datetime, query: str, source_uri: str) -> int:
    """Record a run as 'running' immediately (committed), so even a crash leaves a trace."""
    row = conn.execute(
        "INSERT INTO ingest_runs (watermark, query, source_uri) VALUES (%s, %s, %s) RETURNING id",
        (watermark, query, source_uri),
    ).fetchone()
    return row[0]


def already_loaded(conn: psycopg.Connection, watermark: datetime, query: str) -> bool:
    """Has a previous run already succeeded for this exact source version and query?"""
    row = conn.execute(
        "SELECT 1 FROM ingest_runs WHERE status = 'succeeded' AND watermark = %s AND query = %s LIMIT 1",
        (watermark, query),
    ).fetchone()
    return row is not None


def finish_run(
    conn: psycopg.Connection,
    run_id: int,
    status: str,
    *,
    rows_upserted: int | None = None,
    rows_rejected: int | None = None,
    reject_samples: list[dict] | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE ingest_runs
        SET status = %s, finished_at = now(), rows_upserted = %s,
            rows_rejected = %s, reject_samples = %s, error = %s
        WHERE id = %s
        """,
        (status, rows_upserted, rows_rejected, Jsonb(reject_samples) if reject_samples else None, error, run_id),
    )


# --- upserts ---------------------------------------------------------------------

# seen_on is the snapshot's fetch date. first_seen only moves earlier and
# last_seen only moves later, so replaying an old snapshot can't shrink the
# window. Known coordinates are never replaced by NULL. Other fields take the
# most recently loaded value, so replay archived snapshots in date order.
_UPSERT_ESTABLISHMENT = """
    INSERT INTO establishments AS e (facility_id, name, address, zip5, lat, lon, first_seen, last_seen)
    VALUES (%(facility_id)s, %(name)s, %(address)s, %(zip5)s, %(lat)s, %(lon)s, %(seen_on)s, %(seen_on)s)
    ON CONFLICT (facility_id) DO UPDATE SET
        name       = EXCLUDED.name,
        address    = EXCLUDED.address,
        zip5       = EXCLUDED.zip5,
        lat        = coalesce(EXCLUDED.lat, e.lat),
        lon        = coalesce(EXCLUDED.lon, e.lon),
        first_seen = least(e.first_seen, EXCLUDED.first_seen),
        last_seen  = greatest(e.last_seen, EXCLUDED.last_seen)
"""

# The WHERE clause turns "update to identical values" into a no-op, so the
# affected-row count means "new or actually changed", and unchanged rows
# aren't rewritten (no dead tuples, no WAL churn).
_UPSERT_INSPECTION = """
    INSERT INTO inspections AS i (id, facility_id, inspected_on, score, process)
    VALUES (%(id)s, %(facility_id)s, %(inspected_on)s, %(score)s, %(process)s)
    ON CONFLICT (id) DO UPDATE SET
        facility_id  = EXCLUDED.facility_id,
        inspected_on = EXCLUDED.inspected_on,
        score        = EXCLUDED.score,
        process      = EXCLUDED.process
    WHERE (i.facility_id, i.inspected_on, i.score, i.process)
          IS DISTINCT FROM (EXCLUDED.facility_id, EXCLUDED.inspected_on, EXCLUDED.score, EXCLUDED.process)
"""


def upsert_establishments(conn: psycopg.Connection, establishments: Iterable[Establishment], seen_on: date) -> None:
    with conn.cursor() as cur:
        cur.executemany(_UPSERT_ESTABLISHMENT, [{**asdict(e), "seen_on": seen_on} for e in establishments])


def upsert_inspections(conn: psycopg.Connection, inspections: Iterable[Inspection]) -> int:
    """Upsert inspections; returns how many were inserted or actually changed."""
    with conn.cursor() as cur:
        cur.executemany(_UPSERT_INSPECTION, [asdict(i) for i in inspections])
        return cur.rowcount
