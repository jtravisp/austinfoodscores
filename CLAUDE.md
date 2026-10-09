# austinfoodscores

Portfolio project: a web map of Travis County food establishments showing each one's latest health inspection score and its trend over time. Built to practice AWS, Terraform, and Python for a cloud engineering assessment.

Product requirements (goals, endpoints, metric definitions, open questions): [`docs/PRD.md`](docs/PRD.md). Keep it in sync when requirements change.

## Working agreement

- The owner wants to understand every piece. Explain decisions as you go; build one phase at a time.
- Involve the owner in real decisions (present options with a recommendation); conventional choices can be stated and made.
- At the end of each phase: list what to verify and which AWS/Terraform concepts it exercised.
- Terraform applies are pre-authorized: show the plan, apply routine changes, and stop to ask before anything expensive, destructive, or touching prod data.
- Commit messages: brief and to the point.
- **PR workflow (since Phase 5):** `main` is protected. Work on a branch, open a PR with `gh`, and let CI (tests + plans) go green. The owner merges, or asks Claude to. Merging to `main` deploys prod behind the `production` environment approval.
- Repo: github.com/jtravisp/austinfoodscores (public).

## Why ingest instead of querying live

The source keeps only a rolling ~3 years. Our database accumulates history beyond that window, which enables long-term trends. The city already publishes a live map of the same data (Socrata view `xqww-eh98`, "Food Establishment Inspection Score Map"); our differentiator is history, trends, and decliners.

## Source data

City of Austin Socrata dataset `ecmv-9xxi` ("Food Establishment Inspection Scores"), data.austintexas.gov.
- One row per inspection. ~20.5k rows, ~6.5k facilities (as of 2026-10). Fits in one 50k-row page, but always page.
- Publisher updates bi-weekly (Tuesdays). We ingest weekly (prod) — cheap and never misses a batch.
- SODA3 requires an app token (sent as the `X-App-Token` header). Locally it's in gitignored `.env`. In AWS it's SSM SecureString `/afs/socrata-app-token`, **created by the bootstrap stack** (placeholder value + `ignore_changes`, so it survives dev teardowns and is never in state); the real value was set once per account outside Terraform. Never read it with the `aws_ssm_parameter` *data source* (that copies the value into state); build the ARN from the name. The app *secret* is not used.

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
- API Gateway HTTP API → **query Lambda** (in VPC, IAM DB auth) → RDS. Endpoints: `/api/establishments` (GeoJSON; filter by bbox, band, zip), `/api/establishments/{facility_id}` (history), `/api/stats/decliners`. **No CORS:** browsers reach the API through the site's CloudFront distribution (same origin).
- **migrate Lambda** (in VPC): role bootstrap + migrations (see Database access).
- Frontend: plain HTML/JS, Leaflet + OSM tiles + leaflet.markercluster, on S3 + CloudFront (OAC). One distribution serves `/*` from S3 and `/api/*` from API Gateway (see Hosting and DNS).
- Prod: deletion protection, backups, CloudWatch alarms → SNS, VPC flow logs.

### Network (`infra/modules/network`)

- Private-only VPC: dev `10.20.0.0/16`, prod `10.21.0.0/16`. Two private /24 subnets in two AZs (RDS needs ≥2). **No IGW, no NAT, no public subnets.**
- One private route table: the local route plus the **S3 gateway endpoint** (free). There are no interface endpoints.
- Security groups:
  - `lambda` SG egress is an allow-list: 5432 to the `rds` SG, and 443 to the S3 prefix list.
  - `rds` SG ingress: 5432 from the `lambda` SG only.
  - The default SG is managed with no rules.
- VPC flow logs: a module flag `flow_logs_enabled`. **On in prod** (CloudWatch, 30 days), off in dev.

## Hosting and DNS

Modeled on ncoer.travispollard.com (`../armybandncoer/infra/dns` and `infra/prod/site.tf`).

- Prod URL: **https://austinfood.travispollard.com**. Dev uses its CloudFront default domain (`*.cloudfront.net`); dev has no custom domain and no DNS dependency.
- **`infra/dns`** (hand-applied, never CI; state `dns/terraform.tfstate` in the prod bucket; **applied 2026-10-09**, zone `Z08182772IDLMR2LTIEC2`, delegation verified in public DNS). It creates the hosted zone `austinfood.travispollard.com` in **afs-prod** ($0.50/mo), and writes the NS delegation record into the parent `travispollard.com` zone in account **679878703800** through an aliased `dns_parent` provider (CLI profile `tp-site`, its own SSO session: `aws sso login --sso-session tp-site`). Both providers pin `allowed_account_ids`. The parent repo (`../travispollard.com`) doesn't manage this record and won't remove it.
- The prod frontend looks up the zone **by name** with a data source (no remote-state coupling). It creates the ACM cert (us-east-1, which CloudFront requires; DNS-validated in our own zone, `create_before_destroy`), the CloudFront aliases, and alias A/AAAA records.
- **One CloudFront distribution per env, two origins:**
  - default `/*` → private S3 site bucket via **OAC**. Use the REST endpoint, not S3 website hosting, so the bucket stays private. The bucket policy is scoped by `AWS:SourceArn` to this distribution.
  - `/api/*` → the API Gateway endpoint. The path is forwarded unchanged, which is why API routes carry the `/api` prefix. Cache policy keys on query strings and honors the origin's `Cache-Control` (max-age=300). The origin request policy must **not** forward the viewer `Host` header (API Gateway rejects it): use `Managed-AllViewerExceptHostHeader`.
  - `PriceClass_100`, TLS ≥ `TLSv1.2_2021`, `redirect-to-https`, `compress = true`. Custom response headers policy with CSP: `connect-src 'self'`, OSM tile host allowed in `img-src`, and scripts only from `'self'` (Leaflet vendored, not CDN-loaded).
- The execute-api URL still works directly (throttled at 20 rps). The browser never uses it.
- Phase 5 (CI): a separate least-privilege **web-deploy role** (sync one bucket, invalidate one distribution; `main` only), as in ncoer, rather than using the admin apply role.

## Environments and accounts

- One AWS account per environment, in the `AFS` OU of the owner's AWS Organization: `afs-dev`, `afs-prod`. Region `us-east-1`.
- Local access via IAM Identity Center: CLI profiles `afs-dev` / `afs-prod` (log in with `aws sso login --sso-session tpollard`; sessions expire, so re-run it when Terraform reports "No valid credential sources").
- Monthly cost budgets (Terraform-managed, imported): `afs-dev-monthly` $10, `afs-prod-monthly` $25, email alerts at 80%/100% actual.
- **local**: docker compose Postgres; run the Python ETL locally.
- **dev**: 90-day data slice, ingest on manual trigger. **Disposable: `terraform destroy` the whole dev env between sessions** (RDS ~$14/mo would exceed the $10 budget). No backups, no final snapshot, no deletion protection. Session start:
  1. `uv run python scripts/build_lambda.py`
  2. `$env:AWS_PROFILE="afs-dev"; terraform -chdir=infra/envs/dev apply`
  3. `uv run afs remote-migrate --env dev --bootstrap`
  4. `uv run afs remote-fetch --env dev`, which uploads to S3; that triggers the load. Check `/aws/lambda/afs-dev-load` logs.
  5. `uv run python scripts/deploy_site.py --env dev`, then open the `site_url` output.
  Or run it all in one click: **Actions → dev → Run workflow → apply**.
  Session end: **Actions → dev → Run workflow → destroy** (or locally `terraform -chdir=infra/envs/dev destroy`). The bootstrap stacks stay.
- **prod** (live since 2026-10-09 at https://austinfood.travispollard.com): full dataset, weekly schedule (Wed 07:00 America/Chicago), deletion protection, 7-day backups, final snapshot, flow logs, `apply_immediately = false`, alarms → SNS email (subscription confirmed). Root `infra/envs/prod`; `alarm_email` comes from gitignored `alarm.local.auto.tfvars` locally and the `ALARM_EMAIL` Actions variable in CI.
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
- **CI/CD** (`.github/workflows/`; actions: checkout v7, setup-uv v10, setup-terraform v4 pinned to TF 1.15.3, configure-aws-credentials v6, upload/download-artifact v7/v8):
  - `test.yml` (reusable): pytest against a `postgres:17` service container.
  - `ci.yml` (pull requests): test, plus `plan (dev)` and `plan (prod)` (fmt -check, validate, plan → job summary) with the read-only plan roles. Fork PRs get no OIDC token, so plans are skipped for them.
  - `deploy.yml` (push to main, except Markdown/`docs/`-only changes): test → **plan** prod (plan role; `-detailed-exitcode`; summary to review; site bucket/distribution read from state) → **apply only if the plan has changes**, in the `production` environment (required reviewer: jtravisp). It applies the saved plan artifact (stale → fails) and runs `remote-migrate`. → **site** in the reviewer-free **`production-site`** environment (protected branches only) with the web-deploy role. It runs after a successful *or skipped* apply, never after a failed/rejected one. **Net effect: frontend-only merges go live with no approval; infra or Lambda-code changes wait for you.** Concurrency `deploy-prod`, never cancelled.
  - `dev.yml` (manual, from main): `apply` builds dev end to end (apply, `--bootstrap` migrate, fetch, site deploy); `destroy` tears it down. **Merges never touch dev.**
  - IAM: every CI trust lists the OIDC `sub` in **both** shapes GitHub mints: classic `repo:jtravisp/austinfoodscores:…` and immutable `repo:jtravisp@109884588/austinfoodscores@1411096114:…` (the first CI run was refused with only the classic shape). The plan role trusts `pull_request` **and** `ref:refs/heads/main` (read-only; the deploy plan runs on main). The apply role trusts `main` (dev) / `environment:production` (prod). The **web-deploy** role (`afs-<env>-github-web-deploy`: put/get/delete/list on the site bucket, CloudFront invalidations) trusts `web_deploy_subjects`: `main` in dev, and `environment:production-site` plus `environment:production` in prod.
  - CLI/scripts accept `--profile ""` to use ambient (OIDC) credentials; `deploy_site.py --bucket --distribution` skips the terraform-output lookup.

## Terraform modules

- `bootstrap`: see above.
- `network`: see above.
- `database`: RDS, subnet/parameter groups, migrate Lambda. Outputs `connection_env` (DB_HOST/PORT/NAME) and `dbuser_arn_prefix` for other modules' IAM policies.
- `lambda_function`: helper used for every Lambda. It creates the function plus its own IAM role and log group (14-day retention). Inputs: optional `vpc`, optional `policy_json`. All functions share `build/lambda.zip` and differ only by `handler`.
- `ingest`: raw bucket `afs-<env>-raw-<account_id>` (versioned, TLS-only; `force_destroy` in dev). It holds the fetch Lambda (outside the VPC; `ssm:GetParameter` on the token, `s3:PutObject` on `raw/*`), the load Lambda (in the VPC; `s3:GetObject` on `raw/*`, `rds-db:connect` as afs_loader), the S3 notification (ObjectCreated, `raw/` + `.json.gz`) with an `aws_lambda_permission`, and an optional EventBridge Scheduler (`schedule_enabled`; Wednesdays 07:00 America/Chicago, the day after the city's Tuesday publishes). The dev settings are `since_days = 90` and no schedule; trigger a fetch with `uv run afs remote-fetch --env dev`.
- Gotcha: static RDS parameters (e.g. `rds.force_ssl`) need `apply_method = "pending-reboot"` in config, or every plan shows a diff.
- `api`: HTTP API (payload format 2.0, `$default` stage, auto-deploy) with three `GET /api/...` routes going to **one** query Lambda (in the VPC, `afs_reader`, 512 MB, 10 s). No CORS config (same-origin via CloudFront). Stage throttling is 20 rps with bursts of 40. JSON access logs go to `/aws/apigateway/<name>` (14 days).
- `frontend`: site bucket, OAC, CloudFront with the S3 + `/api/*` origins, response headers policy (CSP from `infra/modules/frontend/csp.txt`). Optional `domain` input: when set (prod), it also creates the ACM cert, aliases, and Route 53 records in the delegated zone. There's no `custom_error_response`: it would apply to the API origin too and turn JSON 404s into HTML.
- Gotcha: the managed cache policy `UseOriginCacheControlHeaders-QueryStrings` (no `Managed-` prefix) keys on, and so forwards, **`Host`** (API Gateway rejects that) and on all cookies. We use our own `aws_cloudfront_cache_policy.api` instead: query strings only, gzip/brotli on, `default_ttl 0`, `max_ttl 3600`. Headers in a cache key are always sent to the origin, whatever the origin request policy says.
- `infra/dns`: see Hosting and DNS.
- `monitoring` (prod): SNS topic + email subscription. Alarms: Lambda `Errors` for fetch/load/query, Scheduler `TargetErrorCount`, API `5xx` ≥5 in 5 min, RDS CPU >80% for 15 min, `FreeStorageSpace` <2 GB. Missing data counts as not breaching, except storage.

## Frontend

- `frontend/`: plain HTML/CSS + one ES module (`app.js`). No build step and no npm. Leaflet 1.9.4 and leaflet.markercluster 1.5.3 are **vendored** under `frontend/vendor/<lib>-<version>/`, downloaded from npm tarballs and checked against the registry's sha512 integrity hash.
- The API base is the relative `/api` (same origin everywhere), so there's no per-env config.
- Loads all establishments once (one cacheable URL) and filters in the browser: band checkboxes (incl. "no score"), ZIP, and **search**.
- **Search** (`frontend/search.js`, pure ES module): exact (not fuzzy) matching where every word must appear in the name or address, after folding case, accents, and apostrophes; jurisdiction prefixes (`PF - `) are ignored for matching. Ranking: name starts with the query, then all words in the name, then split across name and address, then address only. It **respects the band/ZIP filters** and narrows the map; results show score + full street address (to tell same-name locations apart). Debounced 150 ms; Enter opens the top hit; Escape clears; Back returns from a hit to the list. The panel shows results whenever there's no `#/…` route. Known gap: the 202 establishments without coordinates aren't in the map data, so they aren't searchable.
- **Shared spots:** 53% of establishments share a point with another (strip malls, food halls; the airport has 42). markercluster's spiderfy (unlabeled fanned-out dots) is **off** (`zoomToBoundsOnClick: false`, `spiderfyOnMaxZoom: false`). Our `clusterclick` handler: if the cluster's members span ≤ `SPOT_METERS` (30 m), or the map is at max zoom → route `#/spot/<lat>,<lng>` lists them (score, name, address; a shared address goes in the heading). Otherwise it calls `zoomToBounds`. `focusMarker()` centers with `setView` instead of `zoomToShowLayer` (which spiderfies). Markers have hover/focus **tooltips** (name · score) built as DOM nodes, never HTML strings.
- Testing gotcha: the Chrome automation tab is often `document.hidden`. Hidden tabs don't run `requestAnimationFrame`, so Leaflet's **animated** zooms (`zoomToBounds`, `fitBounds`, `flyTo`) never finish there and screenshots time out; it looks like a click bug but isn't. Set `map._zoomAnimated = false` in the test tab (the `zoomAnimation` option is cached at init), or test in a visible window. Screenshot coordinates are scaled (1512 px frame vs the CSS viewport); convert before clicking.
- Frontend unit tests: `node --test frontend/*.test.mjs` (Node's built-in runner, no npm; pass files, not the directory: Node 22 treats a directory argument as a file), also run in CI's `test.yml`. `*.test.*` files are excluded from site deploys.
- Clusters are **donuts** showing the band mix (inline SVG built only from computed numbers; colors via CSS classes). "Worst band wins" made the city look mostly failing.
- Side panel (a bottom sheet under 720px) shows an establishment's detail (badge, delta, trend text, facts, hand-built SVG chart, history table) or the Decliners list. Hash routes are `#/establishment/<id>` and `#/decliners`, so views are linkable and work with the back button.
- **XSS rule:** API data is inserted only via `textContent`/`setAttribute` (the `el()` helper), never `innerHTML`. Exception: cluster icon HTML, which contains only numbers.
- **CSP** (`infra/modules/frontend/csp.txt`, the single source for CloudFront and `afs serve`): `script-src 'self'`, `style-src 'self'` (no inline `style` attributes; setting `element.style` from JS is fine), `img-src 'self' data: https://tile.openstreetmap.org`, `connect-src 'self'`, `frame-ancestors 'none'`. No `upgrade-insecure-requests`: CloudFront already redirects to HTTPS, and the directive breaks `http://localhost`.
- Dates from the API are `YYYY-MM-DD`; parse them as local dates (`parseDate`), never `new Date(iso)` (that's UTC midnight, the previous evening in Austin).
- Local: `uv run afs serve` → http://localhost:8001 serves `frontend/` + `/api` with the CSP header (path-traversal guarded; `.js` MIME pinned because the Windows registry can say `text/plain`).
- Deploy: `uv run python scripts/deploy_site.py --env dev` reads bucket and distribution from Terraform outputs, uploads with Cache-Control (`index.html` no-cache, `vendor/` 1 year immutable, the rest 5 min), deletes stale keys, and invalidates `/*`.
- Verified in dev (2026-10-09): API through CloudFront goes Miss 3.0 s → Hit 0.26 s; there are no console/CSP errors in Chrome; the bucket returns 403 directly.

## Read API

- Contract (details in `docs/PRD.md`): `GET /api/establishments?bbox=w,s,e,n&band=green,yellow&zip=78704` (GeoJSON, excludes no-coords), `GET /api/establishments/{facility_id}` (metrics + history, 404 if unknown), `GET /api/stats/decliners?limit=25&zip=` (≤100). Bad params return 400 `{"error": ...}`. Route keys include the `/api` prefix.
- `api.route(conn, route_key, query, path)` is the single implementation. The Lambda handler (`handlers/query.py`) and `uv run afs serve` (local, **port 8001**; 8080 is taken by qBittorrent on the dev machine) are thin adapters. In Phase 4, `afs serve` also serves `frontend/` at `/`, so local is same-origin like CloudFront.
- The query Lambda reuses its DB connection across warm invocations and reconnects once on `OperationalError`.
- **HTTP APIs don't compress**, so the query Lambda gzips bodies ≥1 KB when the client sends `Accept-Encoding: gzip` (base64 + `isBase64Encoded`). The full map is ~2 MB → ~250 KB. Responses get `Cache-Control: public, max-age=300` and `Vary: accept-encoding`.
- Measured (dev): cold start ≈ 0.6 s init + 1.7 s first request (IAM token + TLS + auth); warm ≈ 40 ms for the full GeoJSON, 2–20 ms for small queries.

## Python

- Python 3.12, uv for deps, pytest for unit tests.
- Transform/cleaning logic lives in pure functions (`src/afs/transform.py`), separate from Lambda handlers (`src/afs/handlers/`), so it is testable locally.
- Lambda runtime: python3.12 on **arm64**.
- On Windows, write files with `write_bytes`/`newline="
"`: `Path.write_text()` silently writes CRLF (that caused a cross-platform zip diff).
- Packaging: **one deterministic zip** (`scripts/build_lambda.py` → `build/lambda.zip`, ~7 MB). Deps come from `uv export` (the lock file), installed with `--python-platform aarch64-manylinux_2_28 --only-binary=:all:`. Note: manylinux2014 is too old for psycopg 3.3; Lambda's AL2023 has glibc 2.34. boto3 is not packaged (the runtime provides it). Rebuilding unchanged code on the same platform gives an identical hash, so Terraform doesn't redeploy. **Across platforms it doesn't** (zlib differences, plus uv writing some wheel metadata differently on Windows), so the **Linux build in CI is the canonical artifact**. A local Windows plan may show a Lambda-only `source_code_hash` diff; that's expected. Don't apply prod from a laptop (CI owns prod). The build also normalizes CRLF→LF in `afs/` text files and pins `create_system = 3`. Chosen over a container image: a ~5 MB dependency doesn't justify ECR, Docker builds in CI, and slower cold starts.
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
src/afs/api.py         read API: param parsing, queries, GeoJSON shaping, route(); transport-agnostic
src/afs/handlers/      Lambda entry points: migrate, fetch, load, query
src/afs/cli.py         `uv run afs migrate|fetch|load|serve|remote-migrate|remote-fetch`
scripts/build_lambda.py, scripts/deploy_site.py
tests/                 pytest (unit + integration against docker Postgres)
frontend/              index.html, styles.css, app.js, vendor/ (Leaflet, markercluster)
infra/bootstrap/{dev,prod}, infra/dns, infra/modules/*, infra/envs/{dev,prod}
.github/workflows/
```

## Build phases

1. ✅ Local ETL against docker Postgres (schema, transforms + tests, Socrata client, metrics, CLI).
2. ✅ Dev infra: bootstrap (both accounts), network, database, ingest. Verified end to end 2026-10-09.
3. ✅ API (dev, verified 2026-10-09).
4. ✅ Frontend (dev, verified 2026-10-09) and DNS delegation for austinfood.travispollard.com.
5. ✅ Prod (live 2026-10-09 at austinfood.travispollard.com) + CI/CD. The first pipeline deploy was approved and succeeded 2026-10-09, and dev was destroyed via `dev.yml` (61 resources).

## Related repos

- `../travispollard.com`: the personal site. Its header has an **Apps** disclosure menu (CFB Forecast, NCOER Writer, Austin Food Scores) and a `austin-food-scores` entry in `frontend/content/projects.ts` (PR jtravisp/travispollard.com#102), and `austin-food-scores` in `status_targets` in its root `status-checker.tf` (applied, so /status monitors this site). That repo now checks out `*.tf` with LF (#103), so a full plan from Windows is clean. That repo has its own conventions (see its CLAUDE.md): project `items` are in Travis's voice, and generated sitemaps must not be committed by hand.

## Windows notes

- Git Bash rewrites arguments starting with `/` into Windows paths (it broke `--log-group-name /aws/lambda/...`). Prefix the command with `MSYS_NO_PATHCONV=1`, or use PowerShell.
