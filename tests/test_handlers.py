import pytest

boto3 = pytest.importorskip("boto3")  # dev dependency; the handlers import it at module load


@pytest.fixture(autouse=True)
def aws_region(monkeypatch):
    # Module-level boto3 clients need a region even though no call is made.
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


def test_s3_objects_decodes_keys():
    from afs.handlers.load import s3_objects

    event = {"Records": [
        {"s3": {"bucket": {"name": "b"}, "object": {"key": "raw/2026-10-09/ecmv-9xxi-011813Z.json.gz"}}},
        {"s3": {"bucket": {"name": "b"}, "object": {"key": "raw/odd+name%3D1.json.gz"}}},
    ]}
    assert s3_objects(event) == [
        ("b", "raw/2026-10-09/ecmv-9xxi-011813Z.json.gz"),
        ("b", "raw/odd name=1.json.gz"),
    ]


def test_s3_objects_empty_event():
    from afs.handlers.load import s3_objects

    assert s3_objects({}) == []


def test_query_response_gzipped_when_accepted():
    import base64
    import gzip

    from afs.handlers.query import encode_response

    text = '{"features":[' + ",".join(['{"x":1}'] * 500) + "]}"
    resp = encode_response(200, {"content-type": "application/json"}, text, {"accept-encoding": "gzip, deflate, br"})

    assert resp["isBase64Encoded"] is True
    assert resp["headers"]["content-encoding"] == "gzip"
    assert gzip.decompress(base64.b64decode(resp["body"])).decode() == text


def test_query_response_plain_when_small_or_not_accepted():
    from afs.handlers.query import encode_response

    big = "x" * 5000
    assert "isBase64Encoded" not in encode_response(200, {}, big, {})
    assert "isBase64Encoded" not in encode_response(200, {}, "{}", {"accept-encoding": "gzip"})
