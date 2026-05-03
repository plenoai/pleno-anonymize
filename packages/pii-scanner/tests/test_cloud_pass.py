"""Cloud-mode tests using a mocked httpx transport (no real network)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from pleno_pii_scanner.cloud_pass import CloudConfig, _chunk, scan_files_cloud


def test_chunk_short_text_yields_single():
    out = list(_chunk("hello"))
    assert out == [(0, "hello")]


def test_chunk_splits_on_newline_boundary():
    text = "aaaa\nbbbb\ncccc\n"
    out = list(_chunk(text, limit=8))
    # First chunk should end at a newline boundary, not mid-line.
    offsets = [o for o, _ in out]
    assert offsets[0] == 0
    rejoined = "".join(c for _, c in out)
    assert rejoined == text


def test_chunk_falls_back_to_hard_split_when_no_newline():
    out = list(_chunk("a" * 25, limit=10))
    # Three chunks: 0-10, 10-20, 20-25
    assert [o for o, _ in out] == [0, 10, 20]
    assert "".join(c for _, c in out) == "a" * 25


def _make_transport(responses_per_text):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        results = responses_per_text.get(body["text"], [])
        return httpx.Response(200, json=results)

    return httpx.MockTransport(handler)


def test_scan_files_cloud_translates_offsets(tmp_path: Path, monkeypatch):
    file = tmp_path / "x.txt"
    file.write_text("line1\n連絡先: 090-1234-5678\n")
    text = file.read_text()
    # Server says "PHONE_NUMBER" at offset 11..24 (位置に意味は無い、テキスト内のbyte位置).
    phone_start = text.index("090-1234-5678")
    server_response = [
        {
            "entity_type": "PHONE_NUMBER",
            "start": phone_start,
            "end": phone_start + len("090-1234-5678"),
            "score": 0.9,
            "text": "090-1234-5678",
        }
    ]
    transport = _make_transport({text: server_response})

    # Patch httpx.AsyncClient to use our transport.
    real_init = httpx.AsyncClient.__init__

    def fake_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", fake_init)

    cfg = CloudConfig(base_url="https://example.invalid", concurrency=1)
    findings = scan_files_cloud(
        [(Path("x.txt"), file)], {"x.txt": text}, cfg
    )
    assert len(findings) == 1
    f = findings[0]
    assert f.entity == "PHONE_NUMBER"
    assert f.line == 2  # 1-indexed; phone is on second line
    assert f.matched == "090-1234-5678"
    assert f.pattern_name == "cloud"


def test_scan_files_cloud_handles_http_error(tmp_path: Path, monkeypatch):
    file = tmp_path / "x.txt"
    file.write_text("hello world")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    transport = httpx.MockTransport(handler)
    real_init = httpx.AsyncClient.__init__

    def fake_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", fake_init)

    cfg = CloudConfig(base_url="https://example.invalid", concurrency=1)
    findings = scan_files_cloud(
        [(Path("x.txt"), file)], {"x.txt": "hello world"}, cfg
    )
    # 500 errors are swallowed and produce no findings (per-file resilience).
    assert findings == []


def test_scan_files_cloud_empty_files_short_circuits():
    cfg = CloudConfig(base_url="https://example.invalid")
    assert scan_files_cloud([], {}, cfg) == []
