"""Lambda (in the VPC): load snapshot files into RDS when they land in S3.

Triggered by S3 ObjectCreated events on raw/*.json.gz. Connects as
afs_loader via IAM auth (env: DB_HOST, DB_PORT, DB_NAME, DB_USER).
S3 delivers events at least once; load_snapshot is idempotent, so a
duplicate delivery just records a 'skipped' run.
"""

from urllib.parse import unquote_plus

import boto3

from afs import db, pipeline, snapshot

s3 = boto3.client("s3")


def s3_objects(event: dict) -> list[tuple[str, str]]:
    """(bucket, key) for each record. Keys arrive URL-encoded ('+' for spaces)."""
    return [
        (r["s3"]["bucket"]["name"], unquote_plus(r["s3"]["object"]["key"]))
        for r in event.get("Records", [])
    ]


def handler(event: dict, context) -> list[dict]:
    results = []
    with db.connect() as conn:
        for bucket, key in s3_objects(event):
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            envelope = snapshot.decode(body)
            result = pipeline.load_snapshot(conn, envelope, source_uri=f"s3://{bucket}/{key}")
            summary = {"key": key, **result.__dict__}
            print(summary)
            results.append(summary)
    return results
