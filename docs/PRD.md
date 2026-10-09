# Product Requirements: Austin Food Scores

Status: approved plan, pre-build (2026-10-08). Architecture and conventions live in [`CLAUDE.md`](../CLAUDE.md); this document covers *what* and *why*.

## Problem

Austin Public Health publishes food establishment inspection scores (Socrata dataset `ecmv-9xxi`), and the city hosts a map of the current data (`xqww-eh98`). Both only cover a rolling ~3 years, and neither answers "is this place getting better or worse?" Once an inspection ages out of the window, it's gone.

## Goals

1. Show every inspected establishment in Travis County on a map, colored by its latest score.
2. Show each establishment's inspection history and trend, including inspections older than the source's 3-year window.
3. Surface establishments whose scores are declining.
4. Serve as a portfolio piece demonstrating AWS, Terraform, Python, and CI/CD practice. Cost and operational simplicity matter as much as features.

## Non-goals

- Real-time data. The publisher updates bi-weekly; weekly ingest is enough.
- Violation details (not in the source dataset).
- Categorizing establishments (restaurant vs. school vs. hospital). The source has no category field.
- User accounts, reviews, or any write path from the frontend.
- Custom domain and HA/multi-AZ (possible later additions).

## Users

- **Diners**: "Is this place clean, and has it been consistently?"
- **Assessment reviewers / hiring managers**: will read the repo, the Terraform, and the CI pipeline, and click the live map.

## Functional requirements

### Map (frontend)
- Clustered markers for all establishments with coordinates, colored by latest score band (Green 90+, Yellow 70–89, Red <70).
- Filters: score band, zip code. Only establishments in the current map view (bbox) are loaded.
- Clicking a marker opens a detail panel: name, address, latest score, inspection history (date, score, process), and a small trend chart.
- A "Decliners" view listing establishments with the largest recent score drops.
- Attribution for OSM tiles and the City of Austin data source.

### API
| Endpoint | Returns |
|---|---|
| `GET /establishments?bbox=&band=&zip=` | GeoJSON FeatureCollection; one Point feature per establishment with coordinates. `bbox` = `west,south,east,north` (Leaflet order); `band` = comma list of `green,yellow,red`; `zip` = 5 digits. Properties: `facility_id, name, address, zip5, latest_score, latest_band, trend, score_delta, days_since_last`. |
| `GET /establishments/{facility_id}` | All metrics, plus `history` (`id, inspected_on, score, band, process`, newest first). 404 if unknown. |
| `GET /stats/decliners?limit=&zip=` | `{min_drop, window_days, decliners: [...]}`, worst drop first. `limit` 1–100 (default 25). |

Errors: 400 `{"error": "..."}` for invalid parameters. Responses are gzipped when the client accepts it and cacheable for 5 minutes.

### Ingest
- Weekly scheduled pull in prod; manual trigger in dev (90-day slice).
- Raw API responses are archived to S3 unchanged, so any load can be replayed.
- Loads are idempotent: reprocessing the same raw file changes nothing.
- Each run is recorded in `ingest_runs` (watermark, row count, status).
- History accumulates: rows that fall out of the source window are never deleted.

### Data cleaning
- Decode HTML entities in names (`&#39;` → `'`).
- Normalize ZIP+4 to a 5-digit zip.
- Collapse repeated whitespace in addresses.
- Keep jurisdiction-prefixed names (`PF -`, `LW -`, `BC -`, `VV -`) as-is.

## Decisions from data profiling (2026-10-08)

- The source republishes every row each time, so ingest pulls a full snapshot. The load skips a run when the snapshot's watermark (`dataUpdatedAt`) and query match a previous successful run.
- Inspections with no score (48 rows) **or a score of 0** (7 rows, placeholders such as 100 > 0 > 0) are kept as visits with a NULL score and excluded from metrics.
- Follow-up inspections (103 rows, 0.5%) count toward trends.
- Score band is derived from the score, not taken from the source text.
- Facilities without coordinates (202) are stored but not shown on the map.
- Metrics are computed in a SQL view.
- Bad rows are skipped and recorded; a load fails if more than 1% of rows are rejected.

## Metric definitions (finalized 2026-10-08 against real data)

Computed per establishment over its **scored** inspections, newest first (same-day ties broken by inspection id). "Today" is the date in Austin (America/Chicago).

| Metric | Definition |
|---|---|
| Latest score / band | Score and band of the most recent scored inspection |
| Score delta | Latest score − previous scored inspection (null with fewer than 2) |
| Trend | Least-squares slope over the last 3 scored inspections, in points per inspection: > +3 `improving`, < −3 `declining`, otherwise `stable`. Null ("not enough history") with fewer than 3. |
| Inspections under 80 | Count of all scored inspections below 80 |
| Days since last | Austin today − most recent inspection date (scored or not) |
| Decliner | Score delta ≤ −10 and latest scored inspection within the last 180 days, ranked by delta |

Snapshot as of 2026-10-08: trend is stable for 2,987 establishments, declining for 478, improving for 430, and null for 2,590 (fewer than 3 inspections). There are 114 decliners, almost all of which dropped out of the green band. The median time between inspections is about 200 days.

## Non-functional requirements

| Area | Requirement |
|---|---|
| Cost | Prod ≤ ~$25/mo, dev ≤ ~$10/mo (AWS Budgets alert at 80%/100%). No NAT gateway, no interface endpoints unless justified. |
| Security | RDS private, IAM database auth, app token in SSM SecureString, no long-lived AWS keys (SSO locally, OIDC in CI), S3 behind CloudFront OAC. |
| Isolation | Separate AWS account per environment. |
| Reliability | Prod RDS has deletion protection and automated backups. Ingest failures alarm to SNS email. |
| Freshness | New source data appears on the map within 7 days of publication. |
| Testability | Cleaning, transform, and metric logic are pure functions with pytest coverage. |
| Reproducibility | All AWS resources are Terraform-managed (except the Organization, accounts, and Identity Center). |

## Milestones

| Phase | Deliverable | Done when |
|---|---|---|
| 1 | Local ETL | Full dataset loads into docker Postgres; reload is a no-op; tests pass |
| 2 | Dev infra + ingest | Manual fetch in dev lands raw JSON in S3 and rows in RDS |
| 3 | API | All three endpoints return correct data from dev |
| 4 | Frontend | Map served from CloudFront, working against the dev API |
| 5 | Prod + CI | PR shows plan; merge deploys dev; approved run deploys prod; weekly ingest running |

## Open questions

- None currently. (Resolved: database setup uses an RDS-managed master secret that the invoker passes to an in-VPC migrate Lambda. See CLAUDE.md, "Database access in AWS".)
