-- query: the SoQL that produced the snapshot. The skip check compares
--   (watermark, query), so a 90-day slice never masks a later full load.
-- rows_rejected / reject_samples: rows the transform refused, with a few
--   examples for debugging. A run fails if rejects exceed 1% of rows.
ALTER TABLE ingest_runs
    ADD COLUMN query          text,
    ADD COLUMN rows_rejected  integer,
    ADD COLUMN reject_samples jsonb;
