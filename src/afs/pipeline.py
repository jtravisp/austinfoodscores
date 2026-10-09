"""Orchestration shared by the local CLI and the Lambda handlers.

These functions wire the pure pieces (socrata, snapshot, transform) together
but leave storage to the caller: the CLI writes to data/, Lambda writes to S3.
"""

from datetime import UTC, date, datetime

from afs import snapshot, socrata


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
