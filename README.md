# Austin Food Scores

A map of Travis County food establishment health inspections, showing each establishment's latest score **and how it has trended over time**.

The City of Austin's [inspection dataset](https://data.austintexas.gov/Health-and-Community-Services/Food-Establishment-Inspection-Scores/ecmv-9xxi) and [map](https://data.austintexas.gov/Health-and-Community-Services/Food-Establishment-Inspection-Score-Map/xqww-eh98) only keep a rolling 3 years. This project ingests the data weekly and keeps the history, so trends and decliners outlive the source window.

> Status: in progress. Building in phases; see [docs/PRD.md](docs/PRD.md).

## Architecture

```
EventBridge (weekly) → fetch Lambda → S3 raw/ → load Lambda (VPC) → RDS Postgres
                                                                        ↑
CloudFront + S3 (Leaflet map) → API Gateway (HTTP) → query Lambda (VPC) ┘
```

- **AWS**: Lambda (Python 3.12, arm64), S3, RDS Postgres with IAM auth, API Gateway HTTP API, CloudFront with OAC, EventBridge, SSM Parameter Store, CloudWatch/SNS. No NAT gateway; the in-VPC Lambdas reach S3 through a gateway endpoint.
- **Terraform**: reusable modules plus separate `dev` and `prod` root modules, deployed to separate AWS accounts.
- **CI/CD**: GitHub Actions with AWS OIDC. Plan on PR, auto-deploy dev, manually approved prod.

## Docs

- [Product requirements](docs/PRD.md)
- [Project decisions and conventions](CLAUDE.md)

## Data

Source: City of Austin Open Data, *Food Establishment Inspection Scores* (public domain).
