-- Widen the trend dead band from +/-1 to +/-3 points per inspection.
-- With +/-1, ~30% of establishments were "declining" (e.g. 96 > 94 > 92);
-- with +/-3, ~12% are, and the label reflects a real slide.
-- The label now lives in a function so a future tweak doesn't need a full
-- view rewrite.
CREATE FUNCTION trend_label(slope double precision, points bigint) RETURNS text
    LANGUAGE sql IMMUTABLE
    RETURN CASE
        WHEN points < 3   THEN NULL
        WHEN slope > 3    THEN 'improving'
        WHEN slope < -3   THEN 'declining'
        ELSE 'stable'
    END;

CREATE OR REPLACE VIEW establishment_metrics AS
WITH scored AS (
    SELECT
        facility_id,
        inspected_on,
        score,
        band,
        row_number() OVER (PARTITION BY facility_id ORDER BY inspected_on DESC, id DESC) AS rn
    FROM inspections
    WHERE score IS NOT NULL
),
per_facility AS (
    SELECT
        facility_id,
        max(score)        FILTER (WHERE rn = 1) AS latest_score,
        max(band)         FILTER (WHERE rn = 1) AS latest_band,
        max(inspected_on) FILTER (WHERE rn = 1) AS latest_scored_on,
        max(score)        FILTER (WHERE rn = 2) AS previous_score,
        regr_slope(score, -rn) FILTER (WHERE rn <= 3) AS trend_slope,
        count(*)          FILTER (WHERE rn <= 3) AS trend_points,
        count(*)          FILTER (WHERE score < 80) AS inspections_under_80,
        count(*)                                    AS scored_inspections
    FROM scored
    GROUP BY facility_id
),
last_visit AS (
    SELECT facility_id, max(inspected_on) AS last_inspected_on
    FROM inspections
    GROUP BY facility_id
)
SELECT
    e.facility_id,
    e.name,
    e.address,
    e.zip5,
    e.lat,
    e.lon,
    m.latest_score,
    m.latest_band,
    m.latest_scored_on,
    m.previous_score,
    m.latest_score - m.previous_score AS score_delta,
    trend_label(m.trend_slope, m.trend_points) AS trend,
    round(m.trend_slope::numeric, 2) AS trend_slope,
    coalesce(m.inspections_under_80, 0) AS inspections_under_80,
    coalesce(m.scored_inspections, 0) AS scored_inspections,
    v.last_inspected_on,
    austin_today() - v.last_inspected_on AS days_since_last
FROM establishments e
LEFT JOIN per_facility m USING (facility_id)
LEFT JOIN last_visit v USING (facility_id);
