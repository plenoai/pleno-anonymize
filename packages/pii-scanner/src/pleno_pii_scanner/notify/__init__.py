"""Notifier subsystem — multi-transport delivery of scan findings.

ADR-0007 §9. Public API:

    from pleno_pii_scanner.notify import (
        NotificationBatch, NotificationResult, Notifier,
        Router, RoutingRule, RetryPolicy, severity_for, excerpt,
    )
    from pleno_pii_scanner.notify.transports.slack import SlackWebhookNotifier
    from pleno_pii_scanner.notify.transports.smtp import SMTPNotifier
    from pleno_pii_scanner.notify.transports.webhook import WebhookNotifier
    from pleno_pii_scanner.notify.transports.splunk import SplunkHECNotifier
    from pleno_pii_scanner.notify.transports.jira import JiraNotifier
    from pleno_pii_scanner.notify.transports.otlp import OTLPNotifier
"""

from pleno_pii_scanner.notify.base import (
    NotificationBatch,
    NotificationResult,
    Notifier,
    RetryPolicy,
    SEVERITY_COLOR,
    SEVERITY_ORDER,
    SEVERITY_OTEL_NUMBER,
    excerpt,
    retry_call,
    severity_for,
)
from pleno_pii_scanner.notify.router import Router, RoutingRule

__all__ = [
    "NotificationBatch",
    "NotificationResult",
    "Notifier",
    "RetryPolicy",
    "Router",
    "RoutingRule",
    "SEVERITY_COLOR",
    "SEVERITY_ORDER",
    "SEVERITY_OTEL_NUMBER",
    "excerpt",
    "retry_call",
    "severity_for",
]
