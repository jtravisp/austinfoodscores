"""API tests: pure parameter parsing, plus routes against a real (test) database."""

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from afs import api
from afs.pipeline import load_snapshot
from factories import envelope, row


# --- parameter parsing ----------------------------------------------------------------------

def test_parse_bbox_leaflet_order():
    assert api.parse_bbox("-97.8,30.2,-97.7,30.3") == (-97.8, 30.2, -97.7, 30.3)


@pytest.mark.parametrize("value", ["1,2,3", "a,b,c,d", "-97.7,30.2,-97.8,30.3", "-97.8,95,-97.7,96"])
def test_parse_bbox_rejects_bad_input(value):
    with pytest.raises(api.BadRequest):
        api.parse_bbox(value)


def test_parse_bands():
    assert api.parse_bands("Green, red") == ["green", "red"]
    with pytest.raises(api.BadRequest, match="purple"):
        api.parse_bands("green,purple")


@pytest.mark.parametrize("value", ["0", "101", "ten", "-5"])
def test_parse_limit_rejects(value):
    with pytest.raises(api.BadRequest):
        api.parse_limit(value)


def test_parse_zip_rejects_zip4():
    with pytest.raises(api.BadRequest):
        api.parse_zip("78704-1234")


def test_to_json_handles_dates_and_decimals():
    from decimal import Decimal
    from datetime import date

    assert api.to_json({"d": date(2026, 1, 2), "n": Decimal("-2.50")}) == '{"d":"2026-01-02","n":-2.5}'


# --- routes against the database ---------------------------------------------------------------

def austin_days_ago(n: int) -> str:
    return (datetime.now(ZoneInfo("America/Chicago")).date() - timedelta(days=n)).isoformat()


@pytest.fixture
def seeded(conn):
    """Three facilities: a central decliner, a green one in 78704, and one with no coordinates."""
    rows = [
        # 100: downtown, 95 -> 92 -> 78 (recent): yellow, declining, a decliner
        row(1, 100, day=austin_days_ago(400), score=95),
        row(2, 100, day=austin_days_ago(200), score=92),
        row(3, 100, day=austin_days_ago(30), score=78),
        # 200: south Austin, stable green
        row(4, 200, day=austin_days_ago(100), score=96, zip_code="78704", lat="30.24", lng="-97.77"),
        # 300: no coordinates
        {k: v for k, v in row(5, 300, day=austin_days_ago(50), score=85).items() if k not in ("lat", "lng")},
    ]
    load_snapshot(conn, envelope(rows, fetched=datetime.now(UTC)), "test")
    return conn


def call(conn, route_key, query=None, path=None):
    status, content_type, body = api.route(conn, route_key, query or {}, path or {})
    return status, json.loads(api.to_json(body))


def test_establishments_geojson(seeded):
    status, body = call(seeded, "GET /establishments")

    assert status == 200
    assert body["type"] == "FeatureCollection"
    assert {f["id"] for f in body["features"]} == {100, 200}   # 300 has no coordinates
    f100 = next(f for f in body["features"] if f["id"] == 100)
    assert f100["geometry"] == {"type": "Point", "coordinates": [-97.74, 30.27]}  # GeoJSON is [lon, lat]
    assert f100["properties"]["latest_band"] == "yellow"
    assert f100["properties"]["trend"] == "declining"
    assert "lat" not in f100["properties"]


@pytest.mark.parametrize(
    "query, expected",
    [
        ({"bbox": "-97.80,30.20,-97.75,30.25"}, {200}),
        ({"band": "yellow"}, {100}),
        ({"band": "green,yellow"}, {100, 200}),
        ({"zip": "78704"}, {200}),
        ({"zip": "78704", "band": "yellow"}, set()),
    ],
)
def test_establishments_filters(seeded, query, expected):
    _, body = call(seeded, "GET /establishments", query)
    assert {f["id"] for f in body["features"]} == expected


def test_establishments_bad_param_is_400(seeded):
    status, body = call(seeded, "GET /establishments", {"bbox": "nope"})
    assert status == 400 and "bbox" in body["error"]


def test_detail_includes_history_newest_first(seeded):
    status, body = call(seeded, "GET /establishments/{facility_id}", path={"facility_id": "100"})

    assert status == 200
    assert body["name"] == "Place 100"
    assert body["score_delta"] == -14
    assert [h["score"] for h in body["history"]] == [78, 92, 95]


def test_detail_without_coordinates_still_works(seeded):
    status, body = call(seeded, "GET /establishments/{facility_id}", path={"facility_id": "300"})
    assert status == 200 and body["lat"] is None


def test_detail_unknown_is_404(seeded):
    status, _ = call(seeded, "GET /establishments/{facility_id}", path={"facility_id": "999"})
    assert status == 404


def test_decliners(seeded):
    status, body = call(seeded, "GET /stats/decliners")

    assert status == 200
    assert [d["facility_id"] for d in body["decliners"]] == [100]
    assert body["decliners"][0]["score_delta"] == -14
    assert body["min_drop"] == 10 and body["window_days"] == 180


def test_unknown_route_is_404(seeded):
    status, _ = call(seeded, "DELETE /establishments")
    assert status == 404
