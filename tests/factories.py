"""Builders for raw API rows and snapshot envelopes, shared by integration tests."""

from datetime import UTC, datetime

from afs import snapshot

WATERMARK = datetime(2026, 10, 1, 17, 2, 23, tzinfo=UTC)


def row(inspection_id: int, facility_id: int = 100, day: str = "2026-01-01", score: int | None = 95, **extra) -> dict:
    """A raw API row as Socrata returns it (strings; null fields omitted)."""
    r = {
        "inspectionid": str(inspection_id),
        "facility_id": str(facility_id),
        "restaurant_name": f"Place {facility_id}",
        "address": "1 Main St Austin, TX 78701",
        "zip_code": "78701-1234",
        "inspection_date": f"{day}T00:00:00.000",
        "process_description": "Routine Inspection",
        "lat": "30.27",
        "lng": "-97.74",
    }
    if score is not None:
        r["score"] = f"{score}.000000"
    r.update(extra)
    return r


def envelope(rows, watermark=WATERMARK, query="SELECT *", fetched=datetime(2026, 10, 8, tzinfo=UTC)) -> dict:
    return snapshot.build(dataset="ecmv-9xxi", fetched_at=fetched, watermark=watermark, query=query, rows=rows)
