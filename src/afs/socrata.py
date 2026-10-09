"""Minimal client for the City of Austin Socrata (SODA3) API.

Uses only the standard library so the Lambda zip stays small. The HTTP call
is injectable (`transport`) so paging logic can be unit tested offline.
"""

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import date, datetime

DOMAIN = "data.austintexas.gov"
DATASET = "ecmv-9xxi"
MAX_PAGE_SIZE = 50_000

# transport(url, token, body) -> parsed JSON. body=None means GET.
Transport = Callable[[str, str, dict | None], object]


class SocrataError(RuntimeError):
    pass


def http_transport(url: str, token: str, body: dict | None, *, attempts: int = 3) -> object:
    """Call the API, retrying throttling (429) and server errors (5xx) with backoff."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method="POST" if body is not None else "GET",
        headers={"X-App-Token": token, "Accept": "application/json", "Content-Type": "application/json"},
    )
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or exc.code >= 500
            if not retryable or attempt == attempts:
                detail = exc.read().decode(errors="replace")[:300]
                raise SocrataError(f"{exc.code} from {url}: {detail}") from exc
        except urllib.error.URLError as exc:
            if attempt == attempts:
                raise SocrataError(f"cannot reach {url}: {exc.reason}") from exc
        time.sleep(2**attempt)
    raise AssertionError("unreachable")


def get_watermark(token: str, transport: Transport = http_transport) -> datetime:
    """When the dataset's rows last changed (its `dataUpdatedAt`)."""
    meta = transport(f"https://{DOMAIN}/api/views/metadata/v1/{DATASET}", token, None)
    return datetime.fromisoformat(meta["dataUpdatedAt"])


def build_query(since: date | None = None) -> str:
    """SoQL for the snapshot. ORDER BY a unique column so paging is stable."""
    where = f" WHERE inspection_date >= '{since.isoformat()}'" if since else ""
    return f"SELECT *{where} ORDER BY inspectionid"


def fetch_rows(
    token: str,
    query: str,
    page_size: int = MAX_PAGE_SIZE,
    transport: Transport = http_transport,
) -> list[dict]:
    """Run a query, following pages until a short page signals the end."""
    url = f"https://{DOMAIN}/api/v3/views/{DATASET}/query.json"
    rows: list[dict] = []
    page_number = 1
    while True:
        body = {
            "query": query,
            "page": {"pageNumber": page_number, "pageSize": page_size},
            "includeSynthetic": False,
        }
        page = transport(url, token, body)
        if not isinstance(page, list):
            raise SocrataError(f"unexpected response on page {page_number}: {str(page)[:300]}")
        rows.extend(page)
        if len(page) < page_size:
            return rows
        page_number += 1
