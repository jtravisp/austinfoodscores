from datetime import UTC, date, datetime

import pytest

from afs import snapshot
from afs.socrata import SocrataError, build_query, fetch_rows, get_watermark


class FakeTransport:
    """Serves a fixed list of rows in pages, recording each request body."""

    def __init__(self, total_rows: int):
        self.rows = [{"inspectionid": str(i)} for i in range(total_rows)]
        self.bodies: list[dict] = []

    def __call__(self, url, token, body):
        self.bodies.append(body)
        page, size = body["page"]["pageNumber"], body["page"]["pageSize"]
        start = (page - 1) * size
        return self.rows[start : start + size]


@pytest.mark.parametrize(
    "total, page_size, expected_requests",
    [
        (7, 3, 3),   # 3 + 3 + 1
        (6, 3, 3),   # 3 + 3 + 0: an exact multiple needs one empty page to know it's done
        (2, 3, 1),
        (0, 3, 1),
    ],
)
def test_fetch_rows_pages_until_short_page(total, page_size, expected_requests):
    fake = FakeTransport(total)
    rows = fetch_rows("token", "SELECT *", page_size=page_size, transport=fake)

    assert [r["inspectionid"] for r in rows] == [str(i) for i in range(total)]
    assert [b["page"]["pageNumber"] for b in fake.bodies] == list(range(1, expected_requests + 1))


def test_fetch_rows_rejects_error_payload():
    def transport(url, token, body):
        return {"error": True, "message": "bad query"}

    with pytest.raises(SocrataError, match="bad query"):
        fetch_rows("token", "SELECT nonsense", transport=transport)


def test_get_watermark_parses_socrata_timestamp():
    def transport(url, token, body):
        assert body is None and url.endswith("/metadata/v1/ecmv-9xxi")
        return {"dataUpdatedAt": "2026-10-01T17:02:23+0000"}

    assert get_watermark("token", transport) == datetime(2026, 10, 1, 17, 2, 23, tzinfo=UTC)


def test_build_query():
    assert build_query() == "SELECT * ORDER BY inspectionid"
    assert build_query(date(2026, 7, 10)) == "SELECT * WHERE inspection_date >= '2026-07-10' ORDER BY inspectionid"


# --- snapshot -----------------------------------------------------------------

FETCHED = datetime(2026, 10, 8, 17, 15, 0, tzinfo=UTC)
WATERMARK = datetime(2026, 10, 1, 17, 2, 23, tzinfo=UTC)


def test_snapshot_round_trip():
    rows = [{"inspectionid": "1"}, {"inspectionid": "2"}]
    env = snapshot.build(dataset="ecmv-9xxi", fetched_at=FETCHED, watermark=WATERMARK, query="q", rows=rows)

    decoded = snapshot.decode(snapshot.encode(env))

    assert decoded == env
    assert decoded["row_count"] == 2
    assert datetime.fromisoformat(decoded["watermark"]) == WATERMARK


def test_snapshot_key():
    assert snapshot.key_for("ecmv-9xxi", FETCHED) == "raw/2026-10-08/ecmv-9xxi-171500Z.json.gz"


def test_snapshot_detects_truncation():
    env = snapshot.build(dataset="d", fetched_at=FETCHED, watermark=WATERMARK, query="q", rows=[{}, {}])
    env["rows"].pop()
    with pytest.raises(ValueError, match="truncated"):
        snapshot.decode(snapshot.encode(env))
