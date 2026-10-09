"""Orchestration shared by the local CLI and the Lambda handlers.

These functions wire the pure pieces (socrata, snapshot, transform, db)
together but leave storage to the caller: the CLI reads/writes data/, Lambda
reads/writes S3.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime

import psycopg

from afs import db, snapshot, socrata
from afs.transform import transform_rows

MAX_REJECT_RATIO = 0.01


class LoadError(RuntimeError):
    pass


@dataclass(frozen=True)
class LoadResult:
    run_id: int
    status: str  # 'succeeded' or 'skipped'
    rows_upserted: int = 0
    rows_rejected: int = 0


def fetch_snapshot(token: str, since: date | None = None, now: datetime | None = None) -> tuple[str, bytes, dict]:
    """Pull the dataset and return (storage key, gzipped bytes, envelope).

    The watermark is read *before* the rows so that, if the city publishes
    mid-fetch, we label the snapshot with the older watermark and the next
    run picks up the change instead of skipping it.
    """
    fetched_at = now or datetime.now(UTC)
    watermark = socrata.get_watermark(token)
    query = socrata.build_query(since)
    rows = socrata.fetch_rows(token, query)
    envelope = snapshot.build(
        dataset=socrata.DATASET, fetched_at=fetched_at, watermark=watermark, query=query, rows=rows
    )
    return snapshot.key_for(socrata.DATASET, fetched_at), snapshot.encode(envelope), envelope


def too_many_rejects(rejected: int, total: int, limit: float = MAX_REJECT_RATIO) -> bool:
    """An empty snapshot counts as a failure too: the source never legitimately has 0 rows."""
    return total == 0 or rejected / total > limit


def load_snapshot(conn: psycopg.Connection, envelope: dict, source_uri: str, *, force: bool = False) -> LoadResult:
    """Load one decoded snapshot. Safe to call repeatedly with the same snapshot.

    Every call leaves exactly one ingest_runs row: 'skipped' if this
    (watermark, query) already succeeded, 'succeeded', or 'failed' (then re-raises).
    """
    watermark = datetime.fromisoformat(envelope["watermark"])
    query = envelope["query"]
    seen_on = datetime.fromisoformat(envelope["fetched_at"]).date()

    run_id = db.start_run(conn, watermark, query, source_uri)
    rejected = None
    try:
        if not force and db.already_loaded(conn, watermark, query):
            db.finish_run(conn, run_id, "skipped")
            return LoadResult(run_id, "skipped")

        result = transform_rows(envelope["rows"])
        rejected = len(result.rejects)
        samples = [{"reason": reason, "row": row} for row, reason in result.rejects[:5]]
        if too_many_rejects(rejected, envelope["row_count"]):
            raise LoadError(f"{rejected} of {envelope['row_count']} rows rejected (limit {MAX_REJECT_RATIO:.0%})")

        # Establishments first (inspections reference them), all in one
        # transaction with the run's success record: all or nothing.
        with conn.transaction():
            db.upsert_establishments(conn, result.establishments.values(), seen_on)
            upserted = db.upsert_inspections(conn, result.inspections)
            db.finish_run(
                conn, run_id, "succeeded",
                rows_upserted=upserted, rows_rejected=rejected, reject_samples=samples,
            )
        return LoadResult(run_id, "succeeded", upserted, rejected)
    except Exception as exc:
        db.finish_run(conn, run_id, "failed", rows_rejected=rejected, error=f"{type(exc).__name__}: {exc}")
        raise
