"""The raw snapshot file format shared by fetch (writer) and load (reader).

One gzipped JSON envelope per fetch:
    {"dataset", "fetched_at", "watermark", "query", "row_count", "rows": [...]}
`rows` are the API rows exactly as received, so any load can be replayed.
"""

import gzip
import json
from datetime import datetime

FORMAT_VERSION = 1


def build(*, dataset: str, fetched_at: datetime, watermark: datetime, query: str, rows: list[dict]) -> dict:
    return {
        "format_version": FORMAT_VERSION,
        "dataset": dataset,
        "fetched_at": fetched_at.isoformat(),
        "watermark": watermark.isoformat(),
        "query": query,
        "row_count": len(rows),
        "rows": rows,
    }


def key_for(dataset: str, fetched_at: datetime) -> str:
    """raw/2026-10-08/ecmv-9xxi-171500Z.json.gz (fetched_at must be UTC)."""
    return f"raw/{fetched_at:%Y-%m-%d}/{dataset}-{fetched_at:%H%M%S}Z.json.gz"


def encode(envelope: dict) -> bytes:
    return gzip.compress(json.dumps(envelope, separators=(",", ":")).encode())


def decode(data: bytes) -> dict:
    envelope = json.loads(gzip.decompress(data))
    if envelope.get("format_version") != FORMAT_VERSION:
        raise ValueError(f"unsupported snapshot format: {envelope.get('format_version')}")
    if envelope["row_count"] != len(envelope["rows"]):
        raise ValueError("snapshot is truncated: row_count does not match rows")
    return envelope
