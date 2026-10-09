-- One row per facility. Name/address/coords reflect the most recent snapshot.
-- first_seen/last_seen are ingest dates: when this facility first and last
-- appeared in the source dataset (not inspection dates).
CREATE TABLE establishments (
    facility_id bigint PRIMARY KEY,
    name        text NOT NULL,
    address     text,
    zip5        text CHECK (zip5 ~ '^[0-9]{5}$'),
    lat         double precision,
    lon         double precision,
    first_seen  date NOT NULL,
    last_seen   date NOT NULL
);

-- One row per inspection. id is the source's inspectionid (verified unique).
-- score is NULL for the handful of source rows with no score.
-- band is derived by the database from score, so it can never disagree with it.
CREATE TABLE inspections (
    id           bigint PRIMARY KEY,
    facility_id  bigint NOT NULL REFERENCES establishments (facility_id),
    inspected_on date NOT NULL,
    score        smallint CHECK (score BETWEEN 0 AND 100),
    process      text NOT NULL,
    band         text GENERATED ALWAYS AS (
                     CASE
                         WHEN score >= 90 THEN 'green'
                         WHEN score >= 70 THEN 'yellow'
                         WHEN score IS NOT NULL THEN 'red'
                     END
                 ) STORED
);

-- Serves "history for one facility, newest first" and the window functions
-- in the metrics view, which partition by facility and order by date.
CREATE INDEX inspections_facility_date_idx
    ON inspections (facility_id, inspected_on DESC, id DESC);

-- One row per ingest attempt. watermark is the source's rowsUpdatedAt; a run
-- is skipped when it matches the last succeeded run's watermark.
CREATE TABLE ingest_runs (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    watermark     timestamptz,
    source_uri    text,
    rows_upserted integer,
    status        text NOT NULL DEFAULT 'running'
                  CHECK (status IN ('running', 'succeeded', 'skipped', 'failed')),
    error         text
);
