"""OpenTelemetry Logs export transport (optional).

Activated only if `pip install pleno-pii-scanner[otlp]` is present.
Without the deps, instantiation degrades to a no-op transport that
records the missing-dependency error rather than crashing the scanner.
"""

from __future__ import annotations

import json
import sys

from pleno_pii_scanner.notify.base import (
    NotificationBatch,
    NotificationResult,
    SEVERITY_OTEL_NUMBER,
    excerpt,
    severity_for,
)

# Probe for OpenTelemetry deps; record either the imported symbols or
# the failure reason. We prefer this to a try/except inside `__init__`
# because the no-op path needs to remain importable even from very
# stripped distroless images.
try:
    from opentelemetry._logs import (  # type: ignore[import-not-found]
        SeverityNumber,
        get_logger_provider,
        set_logger_provider,
    )
    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (  # type: ignore[import-not-found]
        OTLPLogExporter,
    )
    from opentelemetry.sdk._logs import (  # type: ignore[import-not-found]
        LoggerProvider,
    )
    from opentelemetry.sdk._logs._internal import (  # type: ignore[import-not-found]
        LogRecord,
    )
    from opentelemetry.sdk._logs.export import (  # type: ignore[import-not-found]
        BatchLogRecordProcessor,
    )

    _OTLP_IMPORT_ERROR: ImportError | None = None
except ImportError as exc:
    _OTLP_IMPORT_ERROR = exc
    SeverityNumber = None  # type: ignore[assignment]
    get_logger_provider = None  # type: ignore[assignment]
    set_logger_provider = None  # type: ignore[assignment]
    OTLPLogExporter = None  # type: ignore[assignment]
    LoggerProvider = None  # type: ignore[assignment]
    LogRecord = None  # type: ignore[assignment]
    BatchLogRecordProcessor = None  # type: ignore[assignment]
    sys.stderr.write(
        "[notify.otlp] opentelemetry deps missing; OTLPNotifier will no-op. "
        "Install pleno-pii-scanner[otlp] to enable.\n"
    )


class OTLPNotifier:
    name: str = "otlp"

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        logger_name: str = "pleno.pii.scanner",
        provider=None,
    ) -> None:
        self._available = _OTLP_IMPORT_ERROR is None
        self._provider = None
        self._logger = None
        if not self._available:
            return
        if provider is None:
            provider = LoggerProvider()
            exporter = (
                OTLPLogExporter(endpoint=endpoint) if endpoint else OTLPLogExporter()
            )
            provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
            set_logger_provider(provider)
        self._provider = provider
        self._logger = provider.get_logger(logger_name)

    async def send(self, batch: NotificationBatch) -> NotificationResult:
        if not self._available:
            return NotificationResult(
                transport=self.name,
                delivered=False,
                delivered_count=0,
                error="otlp not installed",
            )
        if not batch.findings:
            return NotificationResult(
                transport=self.name, delivered=True, delivered_count=0
            )
        for f in batch.findings:
            payload = {
                "scan_id": batch.scan_id,
                "entity": f.entity,
                "file": f.file,
                "line": f.line,
                "severity": severity_for(f),
                "verification": f.verification,
                "fingerprint": f.fingerprint(),
                "excerpt": excerpt(f),
                **dict(batch.metadata),
            }
            record = LogRecord(  # type: ignore[misc]
                severity_number=SeverityNumber(  # type: ignore[misc]
                    SEVERITY_OTEL_NUMBER.get(severity_for(f), 9)
                ),
                severity_text=severity_for(f),
                body=json.dumps(payload, ensure_ascii=False),
                attributes=payload,
            )
            self._logger.emit(record)  # type: ignore[union-attr]
        return NotificationResult(
            transport=self.name,
            delivered=True,
            delivered_count=len(batch.findings),
        )

    async def close(self) -> None:
        if self._provider is not None and hasattr(self._provider, "shutdown"):
            self._provider.shutdown()
