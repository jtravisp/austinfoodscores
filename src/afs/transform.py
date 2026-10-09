"""Pure functions that turn raw Socrata rows into clean records.

No I/O here: everything takes plain Python values and returns plain Python
values, so it can be unit tested without a database, network, or AWS.
"""

import html
import re
from dataclasses import dataclass, field, replace
from datetime import date


@dataclass(frozen=True)
class Establishment:
    facility_id: int
    name: str
    address: str | None
    zip5: str | None
    lat: float | None
    lon: float | None


@dataclass(frozen=True)
class Inspection:
    id: int
    facility_id: int
    inspected_on: date
    score: int | None
    process: str


@dataclass
class TransformResult:
    establishments: dict[int, Establishment] = field(default_factory=dict)
    inspections: list[Inspection] = field(default_factory=list)
    rejects: list[tuple[dict, str]] = field(default_factory=list)


class RejectedRow(ValueError):
    """A row that can't be turned into an inspection (missing or invalid key fields)."""


_WHITESPACE = re.compile(r"\s+")
_ZIP = re.compile(r"^(\d{5})(?:-?\d{4})?$")


def clean_text(value: str | None) -> str | None:
    """Decode HTML entities, collapse runs of whitespace, trim. Empty becomes None."""
    if value is None:
        return None
    cleaned = _WHITESPACE.sub(" ", html.unescape(value)).strip()
    return cleaned or None


def to_zip5(value: str | None) -> str | None:
    """'78751-1717' or '787511717' or '78751' -> '78751'. Anything else -> None."""
    if value is None:
        return None
    match = _ZIP.match(value.strip())
    return match.group(1) if match else None


def to_score(value: str | None) -> int | None:
    """'96.000000' -> 96. Missing -> None. Fractional or out of 0-100 -> RejectedRow."""
    if value is None or value.strip() == "":
        return None
    number = float(value)
    if not number.is_integer() or not 0 <= number <= 100:
        raise RejectedRow(f"invalid score {value!r}")
    return int(number)


def to_coord(value: str | None) -> float | None:
    if value is None or value.strip() == "":
        return None
    number = float(value)
    return None if number == 0 else number


def to_date(value: str) -> date:
    """Socrata floating timestamp '2026-08-24T00:00:00.000' -> date(2026, 8, 24)."""
    return date.fromisoformat(value[:10])


def _required(raw: dict, key: str) -> str:
    value = raw.get(key)
    if value is None or str(value).strip() == "":
        raise RejectedRow(f"missing {key}")
    return value


def transform_row(raw: dict) -> tuple[Establishment, Inspection]:
    """Transform one raw API row. Raises RejectedRow if it can't be used."""
    try:
        facility_id = int(_required(raw, "facility_id"))
        name = clean_text(_required(raw, "restaurant_name"))
        if name is None:
            raise RejectedRow("missing restaurant_name")

        establishment = Establishment(
            facility_id=facility_id,
            name=name,
            address=clean_text(raw.get("address")),
            zip5=to_zip5(raw.get("zip_code")),
            lat=to_coord(raw.get("lat")),
            lon=to_coord(raw.get("lng")),
        )
        inspection = Inspection(
            id=int(_required(raw, "inspectionid")),
            facility_id=facility_id,
            inspected_on=to_date(_required(raw, "inspection_date")),
            score=to_score(raw.get("score")),
            process=clean_text(raw.get("process_description")) or "Unknown",
        )
    except RejectedRow:
        raise
    except ValueError as exc:  # int(), float(), or date parse failures on garbage input
        raise RejectedRow(str(exc)) from exc
    return establishment, inspection


def transform_rows(rows: list[dict]) -> TransformResult:
    """Transform a full snapshot.

    Bad rows are collected in `rejects` instead of failing the whole batch.
    Each facility appears in many rows; its establishment record is taken from
    its most recent inspection, but coordinates fall back to any earlier row
    that had them.
    """
    result = TransformResult()
    latest_date: dict[int, date] = {}

    for raw in rows:
        try:
            est, insp = transform_row(raw)
        except RejectedRow as exc:
            result.rejects.append((raw, str(exc)))
            continue

        result.inspections.append(insp)

        current = result.establishments.get(est.facility_id)
        if current is None:
            result.establishments[est.facility_id] = est
            latest_date[est.facility_id] = insp.inspected_on
            continue

        newer, older = (est, current) if insp.inspected_on > latest_date[est.facility_id] else (current, est)
        if newer.lat is None and older.lat is not None:
            newer = replace(newer, lat=older.lat, lon=older.lon)
        result.establishments[est.facility_id] = newer
        latest_date[est.facility_id] = max(latest_date[est.facility_id], insp.inspected_on)

    return result
