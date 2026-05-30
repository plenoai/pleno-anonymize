"""Endpoint-level validation guards for /api/redact.

These exercise the request-validation and malformed-input paths only, so they
do not need the NER model loaded (the analyzer is never reached): a bad
fill_color is rejected by pydantic (422) and undecodable image bytes are
rejected before the redactor runs (400, not a 500 stack trace).
"""

from fastapi.testclient import TestClient

import src.app as app_module

client = TestClient(app_module.app)


def test_redact_rejects_out_of_range_fill_color() -> None:
    resp = client.post("/api/redact", json={"image": "AAAA", "fill_color": [256, 0, 0]})
    assert resp.status_code == 422


def test_redact_rejects_wrong_length_fill_color() -> None:
    resp = client.post("/api/redact", json={"image": "AAAA", "fill_color": [0, 0]})
    assert resp.status_code == 422


def test_redact_rejects_malformed_data_url() -> None:
    # `data:` prefix without a comma must be a 400, not an unhandled 500.
    resp = client.post("/api/redact", json={"image": "data:image/png;base64"})
    assert resp.status_code == 400


def test_redact_rejects_invalid_base64_image() -> None:
    resp = client.post("/api/redact", json={"image": "!!!not-base64!!!"})
    assert resp.status_code == 400
