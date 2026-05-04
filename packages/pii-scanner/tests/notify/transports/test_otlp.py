"""Tests for OTLP transport.

The optional `[otlp]` extra is the canonical scenario for opentelemetry
to be importable. We always exercise the no-op path (deps missing) and
exercise the live path only when the deps happen to be installed.
"""

from __future__ import annotations

import pytest

from pleno_pii_scanner.notify.transports import otlp as otlp_module
from .._helpers import make_batch, make_finding


# ---------------- no-op path (always runs) ----------------


async def test_otlp_no_op_when_deps_missing(monkeypatch):
    monkeypatch.setattr(otlp_module, "_OTLP_IMPORT_ERROR", ImportError("simulated"))
    n = otlp_module.OTLPNotifier()
    assert n._available is False
    result = await n.send(make_batch(make_finding()))
    await n.close()
    assert result.delivered is False
    assert result.error == "otlp not installed"


async def test_otlp_no_op_close_safe(monkeypatch):
    monkeypatch.setattr(otlp_module, "_OTLP_IMPORT_ERROR", ImportError("simulated"))
    n = otlp_module.OTLPNotifier()
    await n.close()


# ---------------- live path (only when extras installed) ----------------

_otlp_present = otlp_module._OTLP_IMPORT_ERROR is None
otlp_only = pytest.mark.skipif(
    not _otlp_present,
    reason="opentelemetry not installed (pip install pleno-pii-scanner[otlp])",
)


class FakeLogger:
    def __init__(self) -> None:
        self.records: list = []

    def emit(self, record):
        self.records.append(record)


class FakeProvider:
    def __init__(self) -> None:
        self.logger = FakeLogger()
        self.shutdown_called = False

    def get_logger(self, name: str):
        return self.logger

    def shutdown(self):
        self.shutdown_called = True


@otlp_only
async def test_otlp_send_emits_one_record_per_finding():
    provider = FakeProvider()
    n = otlp_module.OTLPNotifier(provider=provider)
    f = make_finding(verification="passed")
    result = await n.send(make_batch(f, source_kind="dir"))
    await n.close()
    assert result.delivered is True
    assert result.delivered_count == 1
    assert len(provider.logger.records) == 1
    assert provider.shutdown_called is True


@otlp_only
async def test_otlp_empty_batch_short_circuits():
    provider = FakeProvider()
    n = otlp_module.OTLPNotifier(provider=provider)
    result = await n.send(make_batch())
    await n.close()
    assert result.delivered is True
    assert result.delivered_count == 0
