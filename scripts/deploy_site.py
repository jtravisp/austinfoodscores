"""Upload frontend/ to an environment's site bucket and invalidate CloudFront.

    uv run python scripts/deploy_site.py --env dev

Bucket and distribution come from that env's Terraform outputs, so the script
needs no configuration of its own. Locally it uses the afs-<env> SSO profile;
in CI pass --profile "" to use the job's OIDC credentials.

Cache-Control per file:
  index.html      no-cache: browsers revalidate every visit, so a deploy shows up at once
  vendor/<lib-x.y.z>/...  1 year, immutable: the version is in the path, so the URL changes when the file does
  everything else 5 minutes (app.js, styles.css have no content hash in their names)
"""

import argparse
import json
import mimetypes
import subprocess
import time
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "frontend"

mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")


def cache_control(key: str) -> str:
    if key == "index.html":
        return "no-cache"
    if key.startswith("vendor/"):
        return "public, max-age=31536000, immutable"
    return "public, max-age=300"


def terraform_outputs(env: str, profile: str | None) -> dict:
    import os

    env_vars = {**os.environ, **({"AWS_PROFILE": profile} if profile else {})}
    out = subprocess.run(
        ["terraform", f"-chdir={ROOT / 'infra' / 'envs' / env}", "output", "-json"],
        check=True, capture_output=True, text=True, env=env_vars,
    ).stdout
    return {k: v["value"] for k, v in json.loads(out).items()}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True, choices=["dev", "prod"])
    parser.add_argument("--profile", help='AWS profile (default afs-<env>; "" = ambient credentials, e.g. CI)')
    parser.add_argument("--bucket", help="skip terraform output lookup (CI passes these)")
    parser.add_argument("--distribution")
    args = parser.parse_args()
    profile = f"afs-{args.env}" if args.profile is None else (args.profile or None)

    if args.bucket and args.distribution:
        outputs = {"site_bucket": args.bucket, "distribution_id": args.distribution, "site_url": "(see terraform output)"}
    else:
        outputs = terraform_outputs(args.env, profile)
    bucket, distribution = outputs["site_bucket"], outputs["distribution_id"]
    session = boto3.Session(profile_name=profile)
    s3 = session.client("s3")

    local = {p.relative_to(SITE).as_posix(): p for p in sorted(SITE.rglob("*")) if p.is_file()}
    for key, path in local.items():
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=path.read_bytes(),
            ContentType=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            CacheControl=cache_control(key),
        )
    print(f"uploaded {len(local)} files to s3://{bucket}")

    # Remove files that no longer exist locally (versioning keeps the old copies).
    remote = [o["Key"] for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket) for o in page.get("Contents", [])]
    stale = [k for k in remote if k not in local]
    if stale:
        s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in stale]})
        print(f"deleted {len(stale)} stale files")

    # One wildcard path counts as one invalidation (1,000/month are free).
    inv = session.client("cloudfront").create_invalidation(
        DistributionId=distribution,
        InvalidationBatch={"Paths": {"Quantity": 1, "Items": ["/*"]}, "CallerReference": str(time.time())},
    )
    print(f"invalidation {inv['Invalidation']['Id']} started; site: {outputs['site_url']}")


if __name__ == "__main__":
    main()
