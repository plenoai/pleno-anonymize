"""SMTP transport — TLS mandatory, MSA 587 (STARTTLS) or SMTPS 465.

aiosmtplib is required (declared as a core dep in pyproject.toml).
HTML body is built via stdlib `email.message.EmailMessage` so we keep
templating in-process; no Jinja in the notifier surface.
"""

from __future__ import annotations

import html as _html
from email.message import EmailMessage
from typing import Sequence

import aiosmtplib

from pleno_pii_scanner.models import Finding
from pleno_pii_scanner.notify.base import (
    NotificationBatch,
    NotificationResult,
    SEVERITY_COLOR,
    SEVERITY_ORDER,
    excerpt,
    severity_for,
)


class SMTPNotifier:
    name: str = "smtp"

    def __init__(
        self,
        *,
        host: str,
        port: int = 587,
        username: str | None = None,
        password: str | None = None,
        sender: str,
        recipients: Sequence[str],
        use_tls: bool | None = None,
        start_tls: bool | None = None,
        timeout: float = 30.0,
        sender_factory=None,
    ) -> None:
        if not recipients:
            raise ValueError("SMTPNotifier requires at least one recipient")
        if use_tls is None and start_tls is None:
            # Port 465 = implicit TLS, anything else = STARTTLS upgrade.
            use_tls = port == 465
            start_tls = port != 465
        elif use_tls is None:
            use_tls = False
        elif start_tls is None:
            start_tls = not use_tls
        if use_tls and start_tls:
            raise ValueError("use_tls and start_tls are mutually exclusive")
        if not (use_tls or start_tls):
            raise ValueError("SMTPNotifier requires TLS — set use_tls or start_tls")
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._sender = sender
        self._recipients = tuple(recipients)
        self._use_tls = use_tls
        self._start_tls = start_tls
        self._timeout = timeout
        # Hook seam for tests: substitute aiosmtplib.send.
        self._sender_factory = sender_factory or aiosmtplib.send

    async def send(self, batch: NotificationBatch) -> NotificationResult:
        if not batch.findings:
            return NotificationResult(
                transport=self.name, delivered=True, delivered_count=0
            )
        message = self._compose_message(batch)
        try:
            await self._sender_factory(
                message,
                hostname=self._host,
                port=self._port,
                username=self._username,
                password=self._password,
                use_tls=self._use_tls,
                start_tls=self._start_tls,
                timeout=self._timeout,
            )
        except Exception as exc:
            return NotificationResult(
                transport=self.name,
                delivered=False,
                delivered_count=0,
                error=f"smtp error: {exc!r}",
            )
        return NotificationResult(
            transport=self.name,
            delivered=True,
            delivered_count=len(batch.findings),
        )

    async def close(self) -> None:
        # aiosmtplib.send creates per-call connections; nothing to close.
        return None

    def _compose_message(self, batch: NotificationBatch) -> EmailMessage:
        msg = EmailMessage()
        critical = batch.severity_summary.get("critical", 0)
        subject_prefix = "[CRITICAL] " if critical else "[PII] "
        msg["Subject"] = (
            f"{subject_prefix}scan {batch.scan_id} — "
            f"{len(batch.findings)} finding(s)"
        )
        msg["From"] = self._sender
        msg["To"] = ", ".join(self._recipients)

        msg.set_content(self._plain_body(batch))
        msg.add_alternative(self._html_body(batch), subtype="html")
        return msg

    def _plain_body(self, batch: NotificationBatch) -> str:
        lines = [f"PII scan {batch.scan_id}", ""]
        for sev in SEVERITY_ORDER:
            count = batch.severity_summary.get(sev, 0)
            if count:
                lines.append(f"  {sev}: {count}")
        lines.append("")
        for f in batch.findings:
            lines.append(self._plain_line(f))
        return "\n".join(lines) + "\n"

    @staticmethod
    def _plain_line(f: Finding) -> str:
        return (
            f"[{severity_for(f)}] {f.entity}  {f.file}:{f.line} "
            f"verification={f.verification} excerpt={excerpt(f)}"
        )

    def _html_body(self, batch: NotificationBatch) -> str:
        rows = []
        for f in batch.findings:
            sev = severity_for(f)
            color = SEVERITY_COLOR.get(sev, "#888888")
            rows.append(
                "<tr>"
                f"<td style='color:{color};font-weight:bold'>{_html.escape(sev)}</td>"
                f"<td>{_html.escape(f.entity)}</td>"
                f"<td>{_html.escape(f.verification)}</td>"
                f"<td>{_html.escape(f.file)}:{f.line}</td>"
                f"<td><code>{_html.escape(excerpt(f))}</code></td>"
                "</tr>"
            )
        summary = "".join(
            f"<li><b>{sev}</b>: {count}</li>"
            for sev in SEVERITY_ORDER
            if (count := batch.severity_summary.get(sev, 0))
        )
        return (
            "<html><body>"
            f"<h2>PII scan {_html.escape(batch.scan_id)}</h2>"
            f"<ul>{summary}</ul>"
            "<table border='1' cellpadding='4' cellspacing='0'>"
            "<thead><tr><th>severity</th><th>entity</th><th>verification</th>"
            "<th>location</th><th>excerpt</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody>"
            "</table>"
            "</body></html>"
        )
