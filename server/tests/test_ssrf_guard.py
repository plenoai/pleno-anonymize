"""SSRF guard for remote image fetching and exception-detail hygiene.

`_assert_fetchable_url` must reject any URL whose scheme is not http(s) or
whose host resolves to a non-public address (loopback, RFC1918, link-local
cloud-metadata, IPv4-mapped IPv6, etc.). These cover the `py/full-ssrf`
finding on `redact_image` without needing the network or NER model.
"""

import ipaddress
import socket

import pytest
from fastapi import HTTPException

import src.app as app_module


def _patch_resolve(monkeypatch, ip: str) -> None:
    def fake_getaddrinfo(host, port, *args, **kwargs):
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port or 0))]

    monkeypatch.setattr(app_module.socket, "getaddrinfo", fake_getaddrinfo)


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # loopback
        "10.0.0.5",  # RFC1918
        "192.168.1.1",  # RFC1918
        "169.254.169.254",  # cloud metadata link-local
        "::1",  # IPv6 loopback
        "::ffff:127.0.0.1",  # IPv4-mapped IPv6 loopback
        "0.0.0.0",  # unspecified
    ],
)
def test_rejects_private_and_internal_targets(monkeypatch, ip) -> None:
    _patch_resolve(monkeypatch, ip)
    with pytest.raises(HTTPException) as exc:
        app_module._assert_fetchable_url("https://evil.example.com/a.png")
    assert exc.value.status_code == 400


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x/", "ftp://x/a"])
def test_rejects_non_http_schemes(monkeypatch, url) -> None:
    _patch_resolve(monkeypatch, "1.1.1.1")
    with pytest.raises(HTTPException) as exc:
        app_module._assert_fetchable_url(url)
    assert exc.value.status_code == 400


def test_allows_public_address(monkeypatch) -> None:
    _patch_resolve(monkeypatch, "1.1.1.1")  # public, globally routable
    app_module._assert_fetchable_url("https://images.example.com/cat.png")


def test_rejects_unresolvable_host(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise socket.gaierror("nope")

    monkeypatch.setattr(app_module.socket, "getaddrinfo", boom)
    with pytest.raises(HTTPException) as exc:
        app_module._assert_fetchable_url("https://nope.invalid/a.png")
    assert exc.value.status_code == 400


def test_is_disallowed_ip_unwraps_mapped_metadata() -> None:
    mapped = ipaddress.ip_address("::ffff:169.254.169.254")
    assert app_module._is_disallowed_ip(mapped) is True


def test_readiness_failure_hides_exception_detail(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    def boom() -> None:
        raise RuntimeError("secret model path /srv/internal/weights.bin")

    monkeypatch.setattr(app_module, "_init_presidio", boom)
    client = TestClient(app_module.app)
    resp = client.get("/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body == {"status": "not_ready"}
    assert "secret model path" not in resp.text
