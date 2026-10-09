"""Local command-line entry point: `uv run afs <command>`."""

import argparse

from afs import db


def cmd_migrate(args: argparse.Namespace) -> None:
    with db.connect() as conn:
        applied = db.migrate(conn)
    print(f"applied: {', '.join(applied)}" if applied else "schema up to date")


def main() -> None:
    try:
        from dotenv import load_dotenv  # dev-only dependency; absent in Lambda
        load_dotenv()
    except ImportError:
        pass

    parser = argparse.ArgumentParser(prog="afs")
    sub = parser.add_subparsers(required=True)
    sub.add_parser("migrate", help="apply pending schema migrations").set_defaults(func=cmd_migrate)

    args = parser.parse_args()
    args.func(args)
