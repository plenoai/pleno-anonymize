"""Transport implementations for the Notifier subsystem.

Each transport lives in its own module and exposes a single class
implementing the `Notifier` protocol from `pleno_pii_scanner.notify.base`.
Transports are imported lazily by the consumer to keep optional
dependencies (otlp) opt-in.
"""
