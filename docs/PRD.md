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
| `GET /establishments?bbox=&band=&zip=` | GeoJSON FeatureCollection with latest-score metrics per feature |
| `GET /establishments/{facility_id}` | Establishment details and full inspection history |
| `GET /stats/decliners` | Ranked list of declining establishments |

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

## Metric definitions (proposed; to be finalized in Phase 1)

Computed per establishment over its inspections ordered by date:

| Metric | Definition |
|---|---|
| Latest score | Score of the most recent inspection |
| Score delta | Latest score − previous score (null if only one inspection) |
| Trend (last 3) | `improving` / `declining` / `stable` based on the sign of the least-squares slope over the last 3 scores, with a ±1 point/inspection dead band |
| Inspections under 80 | Count of all inspections with score < 80 |
| Days since last | Today − latest inspection date |
| Decliner | Score delta ≤ −10 with latest inspection in the last 180 days, ranked by delta |

Open question: should follow-up and re-inspections count toward trends, or only "Routine Inspection"? To be decided after looking at the `process_description` distribution in Phase 1.

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

- How to run DDL and `GRANT rds_iam` with master credentials inside a VPC with no NAT (Phase 2).
- Whether to include follow-up inspections in trend metrics (Phase 1).
