"""Lambda (outside the VPC): pull a Socrata snapshot and store it in S3.

Env: RAW_BUCKET, TOKEN_PARAM, optional SINCE_DAYS (dev: 90).
The S3 upload triggers the load Lambda.
"""

import os
from datetime import date, timedelta

import boto3

from afs import pipeline

s3 = boto3.client("s3")
ssm = boto3.client("ssm")


def handler(event: dict, context) -> dict:
    token = ssm.get_parameter(Name=os.environ["TOKEN_PARAM"], WithDecryption=True)["Parameter"]["Value"]
    since_days = os.environ.get("SINCE_DAYS")
    since = date.today() - timedelta(days=int(since_days)) if since_days else None

    key, data, envelope = pipeline.fetch_snapshot(token, since)
    s3.put_object(Bucket=os.environ["RAW_BUCKET"], Key=key, Body=data, ContentType="application/gzip")

    result = {"key": key, "row_count": envelope["row_count"], "watermark": envelope["watermark"]}
    print(result)
    return result
