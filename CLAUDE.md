# austinfoodscores

Portfolio project: a web map of Travis County food establishments showing each one's latest health inspection score and its trend over time. Built to practice AWS, Terraform, and Python for a cloud engineering assessment.

Product requirements (goals, endpoints, metric definitions, open questions): [`docs/PRD.md`](docs/PRD.md). Keep it in sync when requirements change.

## Working agreement

- The owner wants to understand every piece. Explain decisions as you go; build one phase at a time.
- Involve the owner in real decisions (present options with a recommendation); conventional choices can be stated and made.
- At the end of each phase: list what to verify and which AWS/Terraform concepts it exercised.
- Terraform applies are pre-authorized: show the plan, apply routine changes, and stop to ask before anything expensive, destructive, or touching prod data.
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
- `ingest_runs(id, started_at, finished_at, watermark, query, source_uri, rows_upserted, rows_rejected, reject_samples, status, error)`
- Derived per-establishment metrics: latest score, trend over last 3 inspections, score delta, count of inspections under 80, days since last inspection. Computed in a **SQL view** (window functions, `regr_slope`), not Python.
- All upserts idempotent (`INSERT ... ON CONFLICT`). Loading the same file twice must not change row counts.
- Schema changes: numbered SQL files in `src/afs/migrations/`, never edit an applied one; add a new file. No ORM.
- `inspections.band` is a generated column derived from score (`green` ≥90, `yellow` 70–89, `red` <70, NULL if no score). The source's band text is ignored (it has stray whitespace).
- `inspections.score` is nullable: missing scores **and scores of 0** (placeholders in the source) become NULL in `transform.to_score`. Metrics ignore NULLs. Follow-up inspections count toward metrics.
- Metric definitions (trend dead band ±3, decliner rule) are in `docs/PRD.md`. The view is `establishment_metrics`; `trend_label()` and `austin_today()` are SQL functions. Business dates use America/Chicago, never `CURRENT_DATE` (the DB runs in UTC).
- psycopg connections use `autocommit=True`; transactions are explicit `with conn.transaction():` blocks.
- Integration tests use separate `afs_test` / `afs_roles_test` databases (created by the tests) and are skipped if Postgres isn't running.
- Establishments without coordinates are stored (lat/lon NULL), excluded from map GeoJSON. Upserts must not overwrite known coords with NULL.
- `first_seen`/`last_seen` are ingest-run dates, not inspection dates.

## Database access in AWS

- RDS Postgres 17, `db.t4g.micro`, private, single-AZ, `rds.force_ssl=1`, IAM database auth on.
- Master user `afs_admin`; password is **RDS-managed in Secrets Manager** (`manage_master_user_password`), never in Terraform state. Used only for the one-time role bootstrap.
- Roles (`src/afs/bootstrap_roles.sql`, idempotent; all `GRANT rds_iam`, so IAM tokens only):
  - `afs_migrator` owns the database and all objects; runs migrations.
  - `afs_loader` has SELECT/INSERT/UPDATE and **no DELETE** (history is never removed). Used by the load Lambda.
  - `afs_reader` has SELECT only. Used by the query Lambda.
  - Default privileges grant access to future objects created by afs_migrator.
- The in-VPC Lambdas can't reach Secrets Manager (no interface endpoint, by choice: cost). So the **migrate Lambda** gets the master password in its *invocation payload*, sent by a trusted caller: `uv run afs remote-migrate --env dev --bootstrap` reads the secret with your SSO profile. Without `--bootstrap`, it runs pending migrations as afs_migrator via IAM auth. The handler must never log the event.
- `db.connect()`: uses `DATABASE_URL` if set (local); otherwise an IAM token for `DB_USER` on `DB_HOST` (Lambda). Token generation is local signing, with no network call. TLS is `verify-full` against the RDS CA bundle shipped at `src/afs/certs/rds-global-bundle.pem`.
- Each Lambda's role gets `rds-db:connect` only for its own DB user (`<dbuser_arn_prefix>/<user>`).

## Ingest strategy

The source rewrites every row on each publish (`:updated_at` is identical across rows), so there is no per-row watermark. The watermark is the dataset's `dataUpdatedAt` (from `/api/views/metadata/v1/ecmv-9xxi`).

- **Fetch** (outside the VPC; can't reach RDS) always pulls the full snapshot (~20k rows, ~1.2 MB gzipped) and writes one file. It reads the watermark *before* the rows.
- **Load** decides: if a previous run with the same (watermark, query) succeeded, it records a `skipped` run and upserts nothing. `ingest_runs` is the single audit trail.
- Rejected rows are skipped and recorded, but the run **fails** (loads nothing) if rejects exceed 1% of rows. That catches upstream format changes.
- Full snapshots also pick up corrections to old rows. Dev uses `--since-days 90`.

Snapshot file (`src/afs/snapshot.py`): one gzipped JSON envelope per fetch at `raw/YYYY-MM-DD/ecmv-9xxi-HHMMSSZ.json.gz` (UTC), holding `{format_version, dataset, fetched_at, watermark, query, row_count, rows}`; `rows` are untouched API rows. Locally it's written under `data/` (gitignored); in AWS, the S3 bucket.

SODA3: `POST /api/v3/views/ecmv-9xxi/query.json` with `{"query", "page": {"pageNumber" (1-based), "pageSize" ≤ 50000}}`, ordered by `inspectionid` for stable paging. A bad token returns 403.

## Architecture (no NAT gateway; minimize cost)

- EventBridge schedule (weekly, prod only) → **fetch Lambda** (outside VPC) pulls Socrata, writes raw JSON to `s3://.../raw/YYYY-MM-DD/...`.
- S3 event → **load Lambda** (in VPC, private subnets) cleans, transforms, upserts to RDS. Reaches S3 via gateway endpoint; IAM database auth to RDS.
- API Gateway HTTP API → **query Lambda** (in VPC, IAM DB auth) → RDS. Endpoints: `/establishments` (GeoJSON; filter by bbox, band, zip), `/establishments/{facility_id}` (history), `/stats/decliners`. CORS on the API.
- **migrate Lambda** (in VPC): role bootstrap + migrations (see Database access).
- Frontend: plain HTML/JS, Leaflet + OSM tiles + leaflet.markercluster, on S3 + CloudFront (OAC).
- Prod: deletion protection, backups, CloudWatch alarms → SNS, VPC flow logs.

### Network (`infra/modules/network`)

- Private-only VPC: dev `10.20.0.0/16`, prod `10.21.0.0/16`. Two private /24 subnets in two AZs (RDS needs ≥2). **No IGW, no NAT, no public subnets.**
- One private route table: the local route plus the **S3 gateway endpoint** (free). There are no interface endpoints.
- Security groups:
  - `lambda` SG egress is an allow-list: 5432 to the `rds` SG, and 443 to the S3 prefix list.
  - `rds` SG ingress: 5432 from the `lambda` SG only.
  - The default SG is managed with no rules.
- VPC flow logs: a module flag `flow_logs_enabled`. **On in prod** (CloudWatch, 30 days), off in dev.

## Environments and accounts

- One AWS account per environment, in the `AFS` OU of the owner's AWS Organization: `afs-dev`, `afs-prod`. Region `us-east-1`.
- Local access via IAM Identity Center: CLI profiles `afs-dev` / `afs-prod` (log in with `aws sso login --sso-session tpollard`; sessions expire, so re-run it when Terraform reports "No valid credential sources").
- Monthly cost budgets (Terraform-managed, imported): `afs-dev-monthly` $10, `afs-prod-monthly` $25, email alerts at 80%/100% actual.
- **local**: docker compose Postgres; run the Python ETL locally.
- **dev**: 90-day data slice, ingest on manual trigger. **Disposable: `terraform destroy` the whole dev env between sessions** (RDS ~$14/mo would exceed the $10 budget). No backups, no final snapshot, no deletion protection. Session start:
  1. `uv run python scripts/build_lambda.py`
  2. `$env:AWS_PROFILE="afs-dev"; terraform -chdir=infra/envs/dev apply`
  3. `uv run afs remote-migrate --env dev --bootstrap`
  4. Trigger an ingest (fetch → load).
  Session end: `terraform -chdir=infra/envs/dev destroy`. The bootstrap stacks stay.
- **prod**: full backfill, weekly schedule, deletion protection, 7-day backups, flow logs.
- Bootstrap (applied by hand, never by CI; done for both accounts 2026-10-08): `infra/modules/bootstrap` + thin roots `infra/bootstrap/{dev,prod}`. Each creates:
  - state bucket `afs-tfstate-<account_id>` (versioned, TLS-only, `prevent_destroy`); bootstrap's own state is at key `bootstrap/terraform.tfstate`
  - the GitHub OIDC provider and two CI roles. `afs-<env>-github-plan` has ReadOnlyAccess plus write access to the `*.tflock` lock file, and trusts only `repo:jtravisp/austinfoodscores:pull_request`. `afs-<env>-github-apply` has AdministratorAccess, and trusts `ref:refs/heads/main` (dev) or `environment:production` (prod).
  - the imported budget (an `import` block in the root)
  - New account recipe: add a `local_override.tf` with `backend "local" {}`, apply, delete the override, then `terraform init -migrate-state`.
  - Budget email lives in gitignored `budget.local.auto.tfvars`.
- Env roots `infra/envs/{dev,prod}`: state key `env/terraform.tfstate` in that account's bucket, `use_lockfile = true`. No workspaces.
- Providers pin `allowed_account_ids` so a misconfigured profile can't apply to the wrong account. Bootstrap roots hardcode `profile`; env roots must not (CI uses OIDC credentials), so locally set `AWS_PROFILE`.
- Account IDs (dev 060516714585, prod 169406897968) appear in the repo. They are identifiers, not secrets.
- If a stale `.tflock` blocks Terraform: read the lock object to confirm the owner, then `terraform force-unlock <ID>`.
- CI: GitHub Actions with AWS OIDC. Plan on PR, auto-apply dev on merge to main, manual approval (GitHub `production` environment) before prod.

## Terraform modules

- `bootstrap`: see above.
- `network`: see above.
- `database`: RDS, subnet/parameter groups, migrate Lambda. Outputs `connection_env` (DB_HOST/PORT/NAME) and `dbuser_arn_prefix` for other modules' IAM policies.
- `lambda_function`: helper used for every Lambda. It creates the function plus its own IAM role and log group (14-day retention). Inputs: optional `vpc`, optional `policy_json`. All functions share `build/lambda.zip` and differ only by `handler`.
- `ingest`, `api`, `frontend`: to come.

## Python

- Python 3.12, uv for deps, pytest for unit tests.
- Transform/cleaning logic lives in pure functions (`src/afs/transform.py`), separate from Lambda handlers (`src/afs/handlers/`), so it is testable locally.
- Lambda runtime: python3.12 on **arm64**.
- Packaging: **one deterministic zip** (`scripts/build_lambda.py` → `build/lambda.zip`, ~7 MB). Deps come from `uv export` (the lock file), installed with `--python-platform aarch64-manylinux_2_28 --only-binary=:all:`. Note: manylinux2014 is too old for psycopg 3.3; Lambda's AL2023 has glibc 2.34. boto3 is not packaged (the runtime provides it). Rebuilding unchanged code gives an identical hash, so Terraform doesn't redeploy. Chosen over a container image: a ~5 MB dependency doesn't justify ECR, Docker builds in CI, and slower cold starts.
- Dev-only deps: pytest, python-dotenv, tzdata (Windows zoneinfo), boto3.

## Repo layout

```
docker-compose.yml, pyproject.toml, uv.lock
src/afs/transform.py   pure cleaning: raw row -> Establishment/Inspection
src/afs/socrata.py     stdlib-only API client (injectable transport for tests)
src/afs/snapshot.py    raw file format shared by fetch and load
src/afs/pipeline.py    orchestration shared by CLI and Lambda; storage left to caller
src/afs/db.py          connections (local DSN or IAM), migrations, role bootstrap, upserts
src/afs/bootstrap_roles.sql, certs/rds-global-bundle.pem
src/afs/migrations/NNN_*.sql   numbered, append-only; applied by db.migrate()
src/afs/handlers/      Lambda entry points (migrate; fetch/load/query to come)
src/afs/cli.py         `uv run afs migrate|fetch|load|remote-migrate`
scripts/build_lambda.py
tests/                 pytest (unit + integration against docker Postgres)
frontend/              index.html, app.js
infra/bootstrap/{dev,prod}, infra/modules/*, infra/envs/{dev,prod}
.github/workflows/
```

## Build phases

1. ✅ Local ETL against docker Postgres (schema, transforms + tests, Socrata client, metrics, CLI).
2. Dev infra: ✅ bootstrap (both accounts), ✅ network, database (in progress), ingest.
3. API.
4. Frontend.
5. Prod + CI.
