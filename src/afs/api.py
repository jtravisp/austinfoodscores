"""Read API: parameter validation, queries, and response shaping.

Transport-agnostic: `route()` takes a route key plus query/path parameters and
returns (status, content_type, body). The Lambda handler and the local
`afs serve` server are thin adapters around it.
"""

import json
import re
from datetime import date
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row

# Decliner rule (docs/PRD.md): dropped >= 10 points, latest scored inspection within 180 days.
DECLINER_MIN_DROP = 10
DECLINER_WINDOW_DAYS = 180
DECLINERS_DEFAULT_LIMIT = 25
DECLINERS_MAX_LIMIT = 100

BANDS = ("green", "yellow", "red")
_ZIP = re.compile(r"^\d{5}$")


class BadRequest(ValueError):
    pass


# --- parameter parsing (pure) ---------------------------------------------------------------

def parse_bbox(value: str | None) -> tuple[float, float, float, float] | None:
    """'west,south,east,north' (Leaflet's toBBoxString order) -> floats."""
    if not value:
        return None
    try:
        west, south, east, north = (float(part) for part in value.split(","))
    except ValueError:
        raise BadRequest("bbox must be four numbers: west,south,east,north") from None
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise BadRequest("bbox out of range or inverted (expected west<east, south<north)")
    return west, south, east, north


def parse_bands(value: str | None) -> list[str] | None:
    if not value:
        return None
    bands = [b.strip().lower() for b in value.split(",") if b.strip()]
    unknown = sorted(set(bands) - set(BANDS))
    if unknown:
        raise BadRequest(f"unknown band(s) {', '.join(unknown)}; expected {', '.join(BANDS)}")
    return bands


def parse_zip(value: str | None) -> str | None:
    if not value:
        return None
    if not _ZIP.match(value):
        raise BadRequest("zip must be 5 digits")
    return value


def parse_limit(value: str | None) -> int:
    if not value:
        return DECLINERS_DEFAULT_LIMIT
    if not value.isdigit() or not 1 <= int(value) <= DECLINERS_MAX_LIMIT:
        raise BadRequest(f"limit must be 1-{DECLINERS_MAX_LIMIT}")
    return int(value)


def parse_facility_id(value: str | None) -> int:
    if not value or not value.isdigit():
        raise BadRequest("facility_id must be a positive integer")
    return int(value)


# --- queries ----------------------------------------------------------------------------------

_MAP_COLUMNS = "facility_id, name, address, zip5, lat, lon, latest_score, latest_band, trend, score_delta, days_since_last"


def establishments_geojson(
    conn: psycopg.Connection,
    bbox: tuple[float, float, float, float] | None = None,
    bands: list[str] | None = None,
    zip5: str | None = None,
) -> dict:
    """Map markers. Establishments without coordinates can't be drawn, so they're excluded."""
    # Clauses are fixed strings; every value goes through a %(name)s parameter.
    where = ["lat IS NOT NULL"]
    params: dict = {}
    if bbox:
        where.append("lon BETWEEN %(west)s AND %(east)s AND lat BETWEEN %(south)s AND %(north)s")
        params.update(zip(("west", "south", "east", "north"), bbox))
    if bands:
        where.append("latest_band = ANY(%(bands)s)")
        params["bands"] = bands
    if zip5:
        where.append("zip5 = %(zip5)s")
        params["zip5"] = zip5

    sql = f"SELECT {_MAP_COLUMNS} FROM establishment_metrics WHERE {' AND '.join(where)} ORDER BY facility_id"
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(sql, params).fetchall()

    features = []
    for r in rows:
        lon, lat = r.pop("lon"), r.pop("lat")
        features.append({
            "type": "Feature",
            "id": r["facility_id"],
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": r,
        })
    return {"type": "FeatureCollection", "features": features}


def establishment_detail(conn: psycopg.Connection, facility_id: int) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cur:
        est = cur.execute(
            "SELECT * FROM establishment_metrics WHERE facility_id = %s", (facility_id,)
        ).fetchone()
        if est is None:
            return None
        est["history"] = cur.execute(
            """
            SELECT id, inspected_on, score, band, process
            FROM inspections
            WHERE facility_id = %s
            ORDER BY inspected_on DESC, id DESC
            """,
            (facility_id,),
        ).fetchall()
    return est


def decliners(conn: psycopg.Connection, limit: int = DECLINERS_DEFAULT_LIMIT, zip5: str | None = None) -> dict:
    zip_clause = "AND zip5 = %(zip5)s" if zip5 else ""
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            f"""
            SELECT facility_id, name, address, zip5, lat, lon,
                   latest_score, latest_band, previous_score, score_delta, latest_scored_on
            FROM establishment_metrics
            WHERE score_delta <= -%(min_drop)s
              AND latest_scored_on >= austin_today() - %(window)s
              {zip_clause}
            ORDER BY score_delta, latest_scored_on DESC, facility_id
            LIMIT %(limit)s
            """,
            {"min_drop": DECLINER_MIN_DROP, "window": DECLINER_WINDOW_DAYS, "limit": limit, "zip5": zip5},
        ).fetchall()
    return {"min_drop": DECLINER_MIN_DROP, "window_days": DECLINER_WINDOW_DAYS, "decliners": rows}


# --- routing ----------------------------------------------------------------------------------

def route(conn: psycopg.Connection, route_key: str, query: dict, path: dict) -> tuple[int, str, dict]:
    """Dispatch one request. Returns (status, content_type, body)."""
    try:
        if route_key == "GET /api/establishments":
            body = establishments_geojson(
                conn,
                bbox=parse_bbox(query.get("bbox")),
                bands=parse_bands(query.get("band")),
                zip5=parse_zip(query.get("zip")),
            )
            return 200, "application/geo+json", body
        if route_key == "GET /api/establishments/{facility_id}":
            detail = establishment_detail(conn, parse_facility_id(path.get("facility_id")))
            if detail is None:
                return 404, "application/json", {"error": "establishment not found"}
            return 200, "application/json", detail
        if route_key == "GET /api/stats/decliners":
            body = decliners(conn, limit=parse_limit(query.get("limit")), zip5=parse_zip(query.get("zip")))
            return 200, "application/json", body
    except BadRequest as exc:
        return 400, "application/json", {"error": str(exc)}
    return 404, "application/json", {"error": f"no route for {route_key}"}


def to_json(body: dict) -> str:
    """JSON with dates as ISO strings and NUMERIC values as floats."""
    def default(value):
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, Decimal):
            return float(value)
        raise TypeError(f"not JSON serializable: {type(value).__name__}")

    return json.dumps(body, default=default, separators=(",", ":"))
