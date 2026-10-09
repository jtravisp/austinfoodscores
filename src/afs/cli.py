"""Local command-line entry point: `uv run afs <command>`."""

import argparse
import os
from datetime import date, timedelta
from pathlib import Path

from afs import db, pipeline, snapshot


def cmd_migrate(args: argparse.Namespace) -> None:
    with db.connect() as conn:
        applied = db.migrate(conn)
    print(f"applied: {', '.join(applied)}" if applied else "schema up to date")


def cmd_fetch(args: argparse.Namespace) -> None:
    token = os.environ.get("SOCRATA_APP_TOKEN")
    if not token:
        raise SystemExit("SOCRATA_APP_TOKEN is not set (see .env.example)")
    since = date.today() - timedelta(days=args.since_days) if args.since_days else None

    key, data, envelope = pipeline.fetch_snapshot(token, since)

    path = Path(args.out) / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    print(f"wrote {path}: {envelope['row_count']} rows, {len(data) / 1e6:.1f} MB, watermark {envelope['watermark']}")


def cmd_load(args: argparse.Namespace) -> None:
    path = Path(args.path)
    envelope = snapshot.decode(path.read_bytes())
    with db.connect() as conn:
        result = pipeline.load_snapshot(conn, envelope, source_uri=path.as_posix(), force=args.force)
    print(
        f"run {result.run_id} {result.status}: "
        f"{result.rows_upserted} inspections new or changed, {result.rows_rejected} rejected"
    )


def cmd_remote_migrate(args: argparse.Namespace) -> None:
    """Invoke the migrate Lambda in AWS, optionally with the master password for role bootstrap."""
    import json

    import boto3

    session = boto3.Session(profile_name=args.profile or f"afs-{args.env}")
    payload = {}
    if args.bootstrap:
        # RDS keeps the master password in Secrets Manager; read it here (outside the
        # VPC) and hand it to the in-VPC function in the invocation payload.
        instance = session.client("rds").describe_db_instances(DBInstanceIdentifier=f"afs-{args.env}")["DBInstances"][0]
        secret = session.client("secretsmanager").get_secret_value(SecretId=instance["MasterUserSecret"]["SecretArn"])
        payload["master_password"] = json.loads(secret["SecretString"])["password"]

    response = session.client("lambda").invoke(
        FunctionName=f"afs-{args.env}-migrate",
        Payload=json.dumps(payload).encode(),
    )
    body = json.loads(response["Payload"].read())
    if "FunctionError" in response:
        raise SystemExit(f"migrate failed: {body.get('errorType')}: {body.get('errorMessage')}")
    print(body)


def main() -> None:
    try:
        from dotenv import load_dotenv  # dev-only dependency; absent in Lambda
        load_dotenv()
    except ImportError:
        pass

    parser = argparse.ArgumentParser(prog="afs")
    sub = parser.add_subparsers(required=True)
    sub.add_parser("migrate", help="apply pending schema migrations").set_defaults(func=cmd_migrate)

    fetch = sub.add_parser("fetch", help="pull a snapshot from Socrata into data/raw/")
    fetch.add_argument("--since-days", type=int, help="only inspections from the last N days (dev uses 90)")
    fetch.add_argument("--out", default="data", help="output root (default: data)")
    fetch.set_defaults(func=cmd_fetch)

    load = sub.add_parser("load", help="load a snapshot file into the database")
    load.add_argument("path", help="a data/raw/.../*.json.gz snapshot")
    load.add_argument("--force", action="store_true", help="load even if this watermark+query already succeeded")
    load.set_defaults(func=cmd_load)

    remote = sub.add_parser("remote-migrate", help="run migrations in AWS via the migrate Lambda")
    remote.add_argument("--env", required=True, choices=["dev", "prod"])
    remote.add_argument("--bootstrap", action="store_true", help="also create DB roles (first run per environment)")
    remote.add_argument("--profile", help="AWS profile (default: afs-<env>)")
    remote.set_defaults(func=cmd_remote_migrate)

    args = parser.parse_args()
    args.func(args)
