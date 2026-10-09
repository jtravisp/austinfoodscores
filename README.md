# Austin Food Scores

A map of Travis County food establishment health inspections, showing each establishment's latest score **and how it has trended over time**.

The City of Austin's [inspection dataset](https://data.austintexas.gov/Health-and-Community-Services/Food-Establishment-Inspection-Scores/ecmv-9xxi) and [map](https://data.austintexas.gov/Health-and-Community-Services/Food-Establishment-Inspection-Score-Map/xqww-eh98) only keep a rolling 3 years. This project ingests the data weekly and keeps the history, so trends and decliners outlive the source window.

Live: **https://austinfood.travispollard.com**

> Status: in progress. Building in phases; see [docs/PRD.md](docs/PRD.md).

## Architecture

```
EventBridge (weekly) → fetch Lambda → S3 raw/ → load Lambda (VPC) → RDS Postgres
                                                                        ↑
austinfood.travispollard.com → CloudFront ─┬─ /*     → S3 (Leaflet map, OAC)
                                           └─ /api/* → API Gateway → query Lambda (VPC) ┘
```

- **AWS**: Lambda (Python 3.12, arm64), S3, RDS Postgres with IAM auth, API Gateway HTTP API, CloudFront with OAC, Route 53 (zone delegated cross-account from travispollard.com), ACM, EventBridge, SSM Parameter Store, CloudWatch/SNS. No NAT gateway; the in-VPC Lambdas reach S3 through a gateway endpoint.
- **Terraform**: reusable modules plus separate `dev` and `prod` root modules, deployed to separate AWS accounts.
- **CI/CD**: GitHub Actions with AWS OIDC. Tests and dev/prod plans on every PR; merging to `main` plans prod, waits for approval, then applies exactly that plan. Dev is a disposable sandbox started and stopped from a manual workflow.

## Run locally

Requires Docker and [uv](https://docs.astral.sh/uv/).

```
cp .env.example .env            # then add your Socrata app token
docker compose up -d --wait     # Postgres 17 on localhost:5432
uv sync
uv run afs migrate              # apply schema migrations
uv run afs fetch                # full snapshot -> data/raw/YYYY-MM-DD/*.json.gz
uv run afs load data/raw/<date>/<file>.json.gz
uv run afs serve                # map + API at http://localhost:8001
uv run pytest                   # unit + integration tests
node --test frontend/*.test.mjs  # frontend unit tests (search)
```

Then explore: `docker compose exec db psql -U afs -d afs -c "SELECT * FROM establishment_metrics LIMIT 5"`.

## Docs

- [Product requirements](docs/PRD.md)
- [Project decisions and conventions](CLAUDE.md)

## Data

Source: City of Austin Open Data, *Food Establishment Inspection Scores* (public domain).
