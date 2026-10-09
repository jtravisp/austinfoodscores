"""Local command-line entry point: `uv run afs <command>`."""

import argparse
import os
from datetime import date, timedelta
from pathlib import Path

from afs import db, pipeline


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

    args = parser.parse_args()
    args.func(args)
