"""Lambda (in the VPC): the read API behind API Gateway (HTTP API, payload format 2.0).

Connects as afs_reader. The connection is opened once per execution
environment and reused by later invocations ("warm" starts), reconnecting
if it has dropped. No CORS headers: browsers reach the API through the
site's CloudFront distribution under /api/*, i.e. the same origin.
"""

import base64
import gzip

import psycopg

from afs import api, db

CACHE_CONTROL = "public, max-age=300"
GZIP_MIN_BYTES = 1024

_conn: psycopg.Connection | None = None


def _connection() -> psycopg.Connection:
    global _conn
    if _conn is None or _conn.closed:
        _conn = db.connect()
    return _conn


def handler(event: dict, context) -> dict:
    global _conn
    args = (event["routeKey"], event.get("queryStringParameters") or {}, event.get("pathParameters") or {})
    try:
        status, content_type, body = api.route(_connection(), *args)
    except psycopg.OperationalError:
        # The DB closed an idle connection (e.g. after a restart): retry once on a fresh one.
        _conn = None
        status, content_type, body = api.route(_connection(), *args)

    headers = {"content-type": content_type, "vary": "accept-encoding"}
    if status == 200:
        headers["cache-control"] = CACHE_CONTROL
    return encode_response(status, headers, api.to_json(body), event.get("headers") or {})


def encode_response(status: int, headers: dict, text: str, request_headers: dict) -> dict:
    """HTTP APIs don't compress responses, so gzip here when the client accepts it.

    Binary bodies go back to API Gateway base64-encoded with isBase64Encoded=True;
    it decodes them before sending bytes to the client. (Header names arrive lowercased.)
    """
    data = text.encode()
    if len(data) >= GZIP_MIN_BYTES and "gzip" in request_headers.get("accept-encoding", ""):
        return {
            "statusCode": status,
            "headers": {**headers, "content-encoding": "gzip"},
            "body": base64.b64encode(gzip.compress(data)).decode(),
            "isBase64Encoded": True,
        }
    return {"statusCode": status, "headers": headers, "body": text}
