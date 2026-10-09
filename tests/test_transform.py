from datetime import date

import pytest

from afs.transform import (
    RejectedRow,
    clean_text,
    to_score,
    to_zip5,
    transform_row,
    transform_rows,
)


def raw_row(**overrides) -> dict:
    """A real row from the API (2026-10-08), with optional overrides."""
    row = {
        "restaurant_name": "Hotel Viata",
        "zip_code": "78746-0032",
        "inspection_date": "2026-08-24T00:00:00.000",
        "score": "96.000000",
        "address": "320 S Capital of Texas Hwy Ste B West Lake Hills, TX 78746-0032",
        "facility_id": "12393953",
        "process_description": "Routine Inspection",
        "lat": "30.276630",
        "lng": "-97.802010",
        "inspectionid": "1529497",
        "inspectionscorecategory": "90+ (Green)",
    }
    row.update(overrides)
    return row


# --- clean_text ---------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Joe&#39;s Bakery", "Joe's Bakery"),
        ("ABC's & 123's", "ABC's & 123's"),  # bare & is not an entity; leave it
        ("8237  RESEARCH NB BLVD AUSTIN, TX 78758", "8237 RESEARCH NB BLVD AUSTIN, TX 78758"),
        ("  padded\tname \n", "padded name"),
        ("   ", None),
        (None, None),
    ],
)
def test_clean_text(raw, expected):
    assert clean_text(raw) == expected


# --- to_zip5 ------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("78746-0032", "78746"),
        ("78758", "78758"),
        ("787460032", "78746"),
        (" 78758 ", "78758"),
        (None, None),
        ("7875", None),
        ("TX", None),
    ],
)
def test_to_zip5(raw, expected):
    assert to_zip5(raw) == expected


# --- to_score -----------------------------------------------------------------

def test_to_score_parses_socrata_decimal():
    assert to_score("96.000000") == 96


def test_to_score_zero_is_a_real_score():
    assert to_score("0.000000") == 0


def test_to_score_missing_is_none():
    assert to_score(None) is None


@pytest.mark.parametrize("raw", ["96.5", "101", "-1"])
def test_to_score_rejects_invalid(raw):
    with pytest.raises(RejectedRow):
        to_score(raw)


# --- transform_row ------------------------------------------------------------

def test_transform_row_happy_path():
    est, insp = transform_row(raw_row())

    assert est.facility_id == 12393953
    assert est.name == "Hotel Viata"
    assert est.zip5 == "78746"
    assert est.lat == pytest.approx(30.27663)
    assert est.lon == pytest.approx(-97.80201)

    assert insp.id == 1529497
    assert insp.facility_id == 12393953
    assert insp.inspected_on == date(2026, 8, 24)
    assert insp.score == 96
    assert insp.process == "Routine Inspection"


def test_transform_row_null_score_is_kept():
    row = raw_row(score=None)
    del row["inspectionscorecategory"]
    _, insp = transform_row(row)
    assert insp.score is None


def test_transform_row_missing_coords():
    row = raw_row()
    del row["lat"], row["lng"]  # Socrata omits null fields entirely
    est, _ = transform_row(row)
    assert est.lat is None and est.lon is None


@pytest.mark.parametrize("missing", ["facility_id", "inspectionid", "inspection_date", "restaurant_name"])
def test_transform_row_rejects_missing_required(missing):
    row = raw_row()
    del row[missing]
    with pytest.raises(RejectedRow, match=missing):
        transform_row(row)


@pytest.mark.parametrize(
    "field, value",
    [("inspectionid", "abc"), ("score", "abc"), ("lat", "north"), ("inspection_date", "yesterday")],
)
def test_transform_row_rejects_garbage(field, value):
    with pytest.raises(RejectedRow):
        transform_row(raw_row(**{field: value}))


# --- transform_rows -----------------------------------------------------------

def test_transform_rows_collects_rejects_without_failing():
    bad = raw_row(inspectionid=None)
    result = transform_rows([raw_row(), bad])

    assert len(result.inspections) == 1
    assert len(result.rejects) == 1
    assert result.rejects[0][0] is bad


def test_transform_rows_establishment_comes_from_latest_inspection():
    old = raw_row(inspectionid="1", inspection_date="2024-01-01T00:00:00.000", restaurant_name="Old Name")
    new = raw_row(inspectionid="2", inspection_date="2026-01-01T00:00:00.000", restaurant_name="New Name")

    # Order in the input must not matter.
    for rows in ([old, new], [new, old]):
        result = transform_rows(rows)
        assert result.establishments[12393953].name == "New Name"
        assert len(result.inspections) == 2


def test_transform_rows_keeps_coords_from_older_row():
    old = raw_row(inspectionid="1", inspection_date="2024-01-01T00:00:00.000")
    new = raw_row(inspectionid="2", inspection_date="2026-01-01T00:00:00.000")
    del new["lat"], new["lng"]

    est = transform_rows([old, new]).establishments[12393953]
    assert est.lat == pytest.approx(30.27663)
