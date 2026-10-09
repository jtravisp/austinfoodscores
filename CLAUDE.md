# austinfoodscores

Portfolio project: a web map of Travis County food establishments showing each one's latest health inspection score and its trend over time. Built to practice AWS, Terraform, and Python for a cloud engineering assessment.

Product requirements (goals, endpoints, metric definitions, open questions): [`docs/PRD.md`](docs/PRD.md). Keep it in sync when requirements change.

## Working agreement

- The owner wants to understand every piece. Explain decisions as you go; build one phase at a time.
- At the end of each phase: list what to verify and which AWS/Terraform concepts it exercised.
- Commit messages: brief and to the point.
- Repo: github.com/jtravisp/austinfoodscores (public).

## Why ingest instead of querying live

The source keeps only a rolling ~3 years. Our database accumulates history beyond that window, which enables long-term trends. The city already publishes a live map of the same data (Socrata view `xqww-eh98`, "Food Establishment Inspection Score Map"); our differentiator is history, trends, and decliners.

## Source data

City of Austin Socrata dataset `ecmv-9xxi` ("Food Establishment Inspection Scores"), data.austintexas.gov.
- One row per inspection. ~20.5k rows, ~6.5k facilities (as of 2026-10). Fits in one 50k-row page, but always page.
- Publisher updates bi-weekly (Tuesdays). We ingest weekly (prod) — cheap and never misses a batch.
- SODA3 requires an app token (sent as the `X-App-Token` header), stored as an SSM Parameter Store SecureString per env and in a gitignored `.env` locally. The app *secret* is not used.

| API field | Type | Maps to | Notes |
|---|---|---|---|
| `inspectionid` | number | `inspections.id` | Verified unique across all rows |
| `facility_id` | number | `establishments.facility_id` | Source system FOLDERRSN |
| `restaurant_name` | text | `name` | Decode HTML entities (`&#39;`) |
| `address` | text | `address` | Collapse doubled whitespace |
| `zip_code` | text | `zip5` | Mix of 5-digit and ZIP+4 |
| `inspection_date` | floating timestamp | `inspected_on` | Date only |
| `score` | number | `score` | |
| `process_description` | text | `process` | e.g. "Routine Inspection" |
| `inspectionscorecategory` | text | `band` | e.g. "90+ (Green)", "70-89 (Yellow)" |
| `lat`, `lng` | number | `lat`, `lon` | |
| `georeferenct` | point | — | Ignored; duplicates lat/lng |

Coverage is Travis County, not just Austin. Name prefixes like `PF -`, `LW -`, `BC -`, `VV -` mark other jurisdictions. Includes schools, hospitals, churches, daycares. No category field.

## Data model (Postgres)

- `establishments(facility_id PK, name, address, zip5, lat, lon, first_seen, last_seen)`
- `inspections(id PK, facility_id FK, inspected_on, score, process, band)`
- `ingest_runs(id, started_at, finished_at, watermark, rows_upserted, status)`
- Derived per-establishment metrics: latest score, trend over last 3 inspections, score delta, count of inspections under 80, days since last inspection. Computed in a **SQL view** (window functions, `regr_slope`), not Python.
- All upserts idempotent (`INSERT ... ON CONFLICT`). Loading the same file twice must not change row counts.
- Schema changes: numbered SQL files in `src/afs/migrations/`, never edit an applied one; add a new file. No ORM.
- `inspections.band` is a generated column derived from score (`green` ≥90, `yellow` 70–89, `red` <70, NULL if no score). The source's band text is ignored (it has stray whitespace).
- `inspections.score` is nullable: missing scores **and scores of 0** (placeholders in the source) become NULL in `transform.to_score`. Metrics ignore NULLs. Follow-up inspections count toward metrics.
- Metric definitions (trend dead band ±3, decliner rule) are in `docs/PRD.md`. The view is `establishment_metrics`; `trend_label()` and `austin_today()` are SQL functions. Business dates use America/Chicago, never `CURRENT_DATE` (the DB runs in UTC).
- psycopg connections use `autocommit=True`; transactions are explicit `with conn.transaction():` blocks.
- Integration tests use a separate `afs_test` database (created by `tests/conftest.py`) and are skipped if Postgres isn't running.
- Establishments without coordinates are stored (lat/lon NULL), excluded from map GeoJSON. Upserts must not overwrite known coords with NULL.
- `first_seen`/`last_seen` are ingest-run dates, not inspection dates.

## Ingest strategy

The source rewrites every row on each publish (`:updated_at` is identical across rows), so there is no per-row watermark. The watermark is the dataset's `dataUpdatedAt` (from `/api/views/metadata/v1/ecmv-9xxi`).

- **Fetch** (outside the VPC; can't reach RDS) always pulls the full snapshot (~20k rows, ~1.2 MB gzipped) and writes one file. It reads the watermark *before* the rows.
- **Load** decides: if the snapshot's watermark equals the last `succeeded` run's watermark, it records a `skipped` run and upserts nothing. `ingest_runs` is the single audit trail.
- Rejected rows are skipped and recorded, but the run **fails** (loads nothing) if rejects exceed 1% of rows. That catches upstream format changes.
- Full snapshots also pick up corrections to old rows. Dev uses `--since-days 90`.

Snapshot file (`src/afs/snapshot.py`): one gzipped JSON envelope per fetch at `raw/YYYY-MM-DD/ecmv-9xxi-HHMMSSZ.json.gz` (UTC), holding `{format_version, dataset, fetched_at, watermark, query, row_count, rows}`; `rows` are untouched API rows. Locally it's written under `data/` (gitignored); in AWS, the S3 bucket.

SODA3: `POST /api/v3/views/ecmv-9xxi/query.json` with `{"query", "page": {"pageNumber" (1-based), "pageSize" ≤ 50000}}`, ordered by `inspectionid` for stable paging. A bad token returns 403.

## Architecture (no NAT gateway; minimize cost)

- EventBridge schedule (weekly, prod only) → **fetch Lambda** (outside VPC) pulls Socrata, writes raw JSON to `s3://.../raw/YYYY-MM-DD/...`.
- S3 event → **load Lambda** (in VPC, private subnets) cleans, transforms, upserts to RDS. Reaches S3 via gateway endpoint; IAM database auth to RDS.
- API Gateway HTTP API → **query Lambda** (in VPC, IAM DB auth) → RDS. Endpoints: `/establishments` (GeoJSON; filter by bbox, band, zip), `/establishments/{facility_id}` (history), `/stats/decliners`. CORS on the API.
- Frontend: plain HTML/JS, Leaflet + OSM tiles + leaflet.markercluster, on S3 + CloudFront (OAC).
- RDS Postgres `db.t4g.micro`, private, single-AZ. Prod: deletion protection, backups, CloudWatch alarms → SNS.
- Open decision (Phase 2): how to run DDL / `GRANT rds_iam` with master creds from inside a VPC with no NAT and no Secrets Manager endpoint.

## Environments and accounts

- One AWS account per environment, in the `AFS` OU of the owner's AWS Organization: `afs-dev`, `afs-prod`. Region `us-east-1`.
- Local access via IAM Identity Center: CLI profiles `afs-dev` / `afs-prod` (log in with `aws sso login --sso-session tpollard`).
- Monthly cost budgets exist (created via CLI 2026-10-08): `afs-dev-monthly` $10, `afs-prod-monthly` $25, email alerts at 80%/100% actual. `infra/bootstrap` should adopt them with Terraform `import` blocks, not create duplicates.
- **local**: docker compose Postgres; run the Python ETL locally.
- **dev**: 90-day data slice, ingest on manual trigger, small, teardown-friendly.
- **prod**: full backfill, weekly schedule, deletion protection.
- Terraform: `infra/bootstrap/` (applied once per account: state bucket, GitHub OIDC provider, deploy role, imported budget); `infra/modules/{network,database,ingest,api,frontend}`; `infra/envs/{dev,prod}` as separate root modules. Each env's state lives in a bucket in its own account, `use_lockfile = true`. No workspaces.
- CI: GitHub Actions with AWS OIDC. Plan on PR, auto-apply dev on merge to main, manual approval (GitHub `production` environment) before prod.

## Python

- Python 3.12, uv for deps, pytest for unit tests.
- Transform/cleaning logic lives in pure functions (`src/afs/transform.py`, `metrics.py`), separate from Lambda handlers (`src/afs/handlers/`), so it is testable locally.
- Lambda runtime: python3.12 on **arm64**.
- psycopg packaging: **zip with manylinux wheels** (`psycopg[binary]` bundles libpq), built with `uv pip install --target build/ --python-platform aarch64-manylinux2014 --only-binary=:all:`. Chosen over a container image: ~5 MB dependency doesn't justify ECR, Docker builds in CI, and slower cold starts.

## Repo layout

```
docker-compose.yml, pyproject.toml, uv.lock
src/afs/transform.py   pure cleaning: raw row -> Establishment/Inspection
src/afs/socrata.py     stdlib-only API client (injectable transport for tests)
src/afs/snapshot.py    raw file format shared by fetch and load
src/afs/pipeline.py    orchestration shared by CLI and Lambda; storage left to caller
src/afs/db.py, cli.py, handlers/{fetch,load,query}.py
src/afs/migrations/NNN_*.sql   numbered, append-only; applied by db.migrate()
                               (metrics live here as a SQL view)
src/afs/cli.py  local CLI: `uv run afs migrate|fetch|load`
tests/          pytest
frontend/       index.html, app.js
infra/bootstrap, infra/modules/*, infra/envs/{dev,prod}
.github/workflows/
```

## Build phases

1. Local ETL against docker Postgres (schema, transforms + tests, Socrata client, metrics, CLI).
2. Dev infra: bootstrap, network, database, ingest.
3. API.
4. Frontend.
5. Prod + CI.
