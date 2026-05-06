"""Tests for the SMTP notifier — uses an injected sender stub."""

from __future__ import annotations

import pytest

from pleno_pii_scanner.notify.transports.smtp import SMTPNotifier
from .._helpers import make_batch, make_finding


class FakeSender:
    def __init__(self, *, raise_exc: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.raise_exc = raise_exc

    async def __call__(self, message, **kwargs):
        self.calls.append({"message": message, "kwargs": kwargs})
        if self.raise_exc is not None:
            raise self.raise_exc
        return {"ok": True}


def _make(**overrides):
    sender = FakeSender(raise_exc=overrides.pop("raise_exc", None))
    notifier = SMTPNotifier(
        host="smtp.example.com",
        port=overrides.pop("port", 587),
        sender="bot@example.com",
        recipients=overrides.pop("recipients", ["sec@example.com"]),
        username=overrides.pop("username", "u"),
        password=overrides.pop("password", "p"),
        sender_factory=sender,
        **overrides,
    )
    return notifier, sender


def test_smtp_requires_recipients():
    with pytest.raises(ValueError):
        SMTPNotifier(host="x", sender="a@b", recipients=[], sender_factory=FakeSender())


def test_smtp_requires_tls():
    with pytest.raises(ValueError):
        SMTPNotifier(
            host="x",
            sender="a@b",
            recipients=["c@d"],
            use_tls=False,
            start_tls=False,
            sender_factory=FakeSender(),
        )


def test_smtp_use_tls_and_start_tls_mutually_exclusive():
    with pytest.raises(ValueError):
        SMTPNotifier(
            host="x",
            sender="a@b",
            recipients=["c@d"],
            use_tls=True,
            start_tls=True,
            sender_factory=FakeSender(),
        )


def test_smtp_port_465_defaults_to_implicit_tls():
    n, _ = _make(port=465)
    assert n._use_tls is True
    assert n._start_tls is False


def test_smtp_port_587_defaults_to_starttls():
    n, _ = _make(port=587)
    assert n._use_tls is False
    assert n._start_tls is True


def test_smtp_explicit_use_tls_only():
    sender = FakeSender()
    n = SMTPNotifier(
        host="x",
        sender="a@b",
        recipients=["c@d"],
        use_tls=True,
        sender_factory=sender,
    )
    assert n._start_tls is False


def test_smtp_explicit_start_tls_only():
    sender = FakeSender()
    n = SMTPNotifier(
        host="x",
        sender="a@b",
        recipients=["c@d"],
        start_tls=True,
        sender_factory=sender,
    )
    assert n._use_tls is False


async def test_smtp_send_empty_batch_short_circuits():
    n, sender = _make()
    result = await n.send(make_batch())
    assert result.delivered is True
    assert sender.calls == []


async def test_smtp_send_composes_multipart_message_with_html_and_no_raw():
    n, sender = _make()
    raw = "AKIAIOSFODNN7EXAMPLE"
    f = make_finding(matched=raw, verification="passed")
    result = await n.send(make_batch(f, source_kind="dir"))
    await n.close()
    assert result.delivered is True
    assert result.delivered_count == 1
    msg = sender.calls[0]["message"]
    assert "[CRITICAL]" in msg["Subject"]
    raw_bytes = msg.as_bytes()
    assert raw.encode() not in raw_bytes
    parts = list(msg.iter_parts())
    assert any(p.get_content_type() == "text/html" for p in parts)
    assert any(p.get_content_type() == "text/plain" for p in parts)


async def test_smtp_subject_pii_prefix_when_no_critical():
    n, sender = _make()
    f = make_finding(score=0.3)
    await n.send(make_batch(f))
    msg = sender.calls[0]["message"]
    assert "[PII]" in msg["Subject"]


async def test_smtp_send_failure_returns_error_result():
    n, _ = _make(raise_exc=RuntimeError("smtp down"))
    result = await n.send(make_batch(make_finding()))
    assert result.delivered is False
    assert "smtp down" in (result.error or "")


async def test_smtp_close_is_noop():
    n, _ = _make()
    await n.close()


async def test_smtp_passes_credentials_and_timeout():
    n, sender = _make(timeout=12.5)
    await n.send(make_batch(make_finding()))
    kw = sender.calls[0]["kwargs"]
    assert kw["hostname"] == "smtp.example.com"
    assert kw["username"] == "u"
    assert kw["password"] == "p"
    assert kw["timeout"] == 12.5
