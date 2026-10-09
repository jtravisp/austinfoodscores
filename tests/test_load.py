"""Integration tests: load snapshots into a real Postgres and check the results."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from afs.pipeline import LoadError, load_snapshot, too_many_rejects
from factories import WATERMARK, envelope, row


def count(conn, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def runs(conn) -> list[tuple]:
    return conn.execute("SELECT status, rows_upserted, rows_rejected FROM ingest_runs ORDER BY id").fetchall()


# --- loading and idempotency -----------------------------------------------------

def test_load_inserts_rows(conn):
    result = load_snapshot(conn, envelope([row(1), row(2, facility_id=200)]), "test")

    assert result.status == "succeeded"
    assert result.rows_upserted == 2
    assert count(conn, "establishments") == 2
    assert count(conn, "inspections") == 2


def test_same_snapshot_twice_is_skipped(conn):
    env = envelope([row(1), row(2)])
    load_snapshot(conn, env, "test")
    second = load_snapshot(conn, env, "test")

    assert second.status == "skipped"
    assert runs(conn) == [("succeeded", 2, 0), ("skipped", None, None)]


def test_forced_reload_changes_nothing(conn):
    env = envelope([row(1), row(2)])
    load_snapshot(conn, env, "test")
    second = load_snapshot(conn, env, "test", force=True)

    assert second.status == "succeeded"
    assert second.rows_upserted == 0  # nothing new or changed
    assert count(conn, "inspections") == 2


def test_different_query_same_watermark_is_not_skipped(conn):
    load_snapshot(conn, envelope([row(1)], query="SELECT * WHERE recent"), "test")
    result = load_snapshot(conn, envelope([row(1), row(2)], query="SELECT *"), "test")

    assert result.status == "succeeded"
    assert count(conn, "inspections") == 2


def test_corrected_score_is_updated(conn):
    load_snapshot(conn, envelope([row(1, score=None)]), "test")
    result = load_snapshot(conn, envelope([row(1, score=88)], watermark=WATERMARK + timedelta(days=14)), "test")

    assert result.rows_upserted == 1
    assert conn.execute("SELECT score, band FROM inspections WHERE id = 1").fetchone() == (88, "yellow")


def test_history_is_kept_when_rows_leave_the_source(conn):
    load_snapshot(conn, envelope([row(1, day="2023-01-01"), row(2)]), "test")
    load_snapshot(conn, envelope([row(2)], watermark=WATERMARK + timedelta(days=14)), "test")

    assert count(conn, "inspections") == 2


def test_establishment_seen_window_and_coords(conn):
    first = datetime(2026, 9, 1, tzinfo=UTC)
    later = datetime(2026, 10, 8, tzinfo=UTC)
    load_snapshot(conn, envelope([row(1)], fetched=first), "test")
    no_coords = row(2, day="2026-02-01")
    del no_coords["lat"], no_coords["lng"]
    load_snapshot(conn, envelope([no_coords], watermark=WATERMARK + timedelta(days=14), fetched=later), "test")

    lat, first_seen, last_seen = conn.execute(
        "SELECT lat, first_seen, last_seen FROM establishments WHERE facility_id = 100"
    ).fetchone()
    assert lat == pytest.approx(30.27)  # not wiped by the coord-less row
    assert (first_seen, last_seen) == (date(2026, 9, 1), date(2026, 10, 8))


# --- rejects and failures ----------------------------------------------------------

def test_few_rejects_are_recorded_and_skipped(conn):
    rows = [row(i) for i in range(1, 201)] + [row(999, inspection_date="garbage")]  # 1 of 201 < 1%
    result = load_snapshot(conn, envelope(rows), "test")

    assert result.status == "succeeded"
    assert result.rows_rejected == 1
    sample = conn.execute("SELECT reject_samples FROM ingest_runs").fetchone()[0]
    assert sample[0]["row"]["inspectionid"] == "999"


def test_too_many_rejects_fails_and_loads_nothing(conn):
    rows = [row(1), row(2, inspection_date="garbage")]  # 50%
    with pytest.raises(LoadError, match="1 of 2 rows rejected"):
        load_snapshot(conn, envelope(rows), "test")

    assert count(conn, "inspections") == 0
    status, error = conn.execute("SELECT status, error FROM ingest_runs").fetchone()
    assert status == "failed" and "LoadError" in error


def test_failed_run_does_not_block_retry(conn):
    with pytest.raises(LoadError):
        load_snapshot(conn, envelope([]), "test")
    assert load_snapshot(conn, envelope([row(1)]), "test").status == "succeeded"


@pytest.mark.parametrize(
    "rejected, total, expected",
    [(0, 100, False), (1, 100, False), (2, 100, True), (0, 0, True)],
)
def test_too_many_rejects(rejected, total, expected):
    assert too_many_rejects(rejected, total) is expected


# --- metrics view -------------------------------------------------------------------

def metrics(conn, facility_id: int = 100) -> dict:
    cur = conn.execute("SELECT * FROM establishment_metrics WHERE facility_id = %s", (facility_id,))
    return dict(zip([c.name for c in cur.description], cur.fetchone()))


def test_metrics_declining(conn):
    load_snapshot(conn, envelope([
        row(1, day="2024-01-01", score=72),   # outside the last 3
        row(2, day="2025-01-01", score=98),
        row(3, day="2025-07-01", score=90),
        row(4, day="2026-01-01", score=78),
    ]), "test")
    m = metrics(conn)

    assert m["latest_score"] == 78
    assert m["latest_band"] == "yellow"
    assert m["previous_score"] == 90
    assert m["score_delta"] == -12
    assert m["trend"] == "declining"
    assert float(m["trend_slope"]) == -10.0   # 98 -> 90 -> 78
    assert m["inspections_under_80"] == 2
    assert m["scored_inspections"] == 4
    austin_today = datetime.now(ZoneInfo("America/Chicago")).date()
    assert m["days_since_last"] == (austin_today - date(2026, 1, 1)).days


def test_metrics_stable_and_same_day_tiebreak(conn):
    load_snapshot(conn, envelope([
        row(1, day="2025-01-01", score=95),
        row(2, day="2026-01-01", score=94),
        row(3, day="2026-01-01", score=96),   # same day, higher id = later
    ]), "test")
    m = metrics(conn)

    assert m["latest_score"] == 96
    assert m["previous_score"] == 94
    assert m["trend"] == "stable"


def test_metrics_gentle_slide_is_stable(conn):
    load_snapshot(conn, envelope([
        row(1, day="2025-01-01", score=96),
        row(2, day="2025-07-01", score=94),
        row(3, day="2026-01-01", score=92),   # slope -2: inside the +/-3 dead band
    ]), "test")
    assert metrics(conn)["trend"] == "stable"


def test_metrics_zero_score_is_ignored(conn):
    load_snapshot(conn, envelope([
        row(1, day="2025-01-01", score=98),
        row(2, day="2026-01-01", score=0),
    ]), "test")
    m = metrics(conn)

    assert m["latest_score"] == 98
    assert m["score_delta"] is None


def test_metrics_ignore_null_scores_and_need_three_points(conn):
    load_snapshot(conn, envelope([
        row(1, day="2025-01-01", score=90),
        row(2, day="2026-01-01", score=None),
        row(3, day="2026-02-01", score=80),
    ]), "test")
    m = metrics(conn)

    assert m["latest_score"] == 80              # the unscored visit is skipped
    assert m["score_delta"] == -10
    assert m["trend"] is None                    # only 2 scored inspections
    assert m["last_inspected_on"] == date(2026, 2, 1)
