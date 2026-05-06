"""Uniform async retry decorator with jittered exponential backoff.

Connectors call HTTP APIs that fail transiently in idiosyncratic ways:
GitHub returns `502 Bad Gateway` mid-shallow-clone, Slack returns
`ratelimited`, AWS returns `503 SlowDown`. Each SDK has its own retry
machinery; the scheduler wraps connector calls in a single decorator so
behavior, telemetry, and configurable ceilings are consistent across
sources.

ADR-0007 §4.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from functools import wraps
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")


class RetryError(Exception):
    """All attempts exhausted. Carries the last exception as `__cause__`."""


@dataclass(frozen=True, slots=True)
class RetryConfig:
    """Tunables for `retry_async`.

    `max_attempts=1` means try-once-then-give-up (no retries, useful for
    idempotency-sensitive operations the connector wants to handle
    itself). `initial_backoff` and `max_backoff` are seconds; backoff
    grows multiplicatively (`base ** attempt`) capped at `max_backoff`,
    then a uniform jitter in `[0.5, 1.5)` is applied to avoid retry
    thundering herd from many sibling connectors hitting the same
    upstream simultaneously.
    """

    max_attempts: int = 5
    initial_backoff: float = 1.0
    max_backoff: float = 30.0
    base: float = 2.0
    jitter: tuple[float, float] = (0.5, 1.5)
    retry_on: tuple[type[BaseException], ...] = (Exception,)
    give_up_on: tuple[type[BaseException], ...] = ()

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.initial_backoff <= 0 or self.max_backoff <= 0:
            raise ValueError("backoff values must be > 0")
        if self.base <= 1.0:
            raise ValueError("base must be > 1.0")
        lo, hi = self.jitter
        if not 0 < lo <= hi:
            raise ValueError("jitter must satisfy 0 < lo <= hi")


def retry_async(
    config: RetryConfig | None = None,
    *,
    on_retry: Callable[[int, BaseException, float], Awaitable[None]] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rand: Callable[[float, float], float] = random.uniform,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Decorator that retries the wrapped coroutine per `config`.

    `on_retry` is awaited with `(attempt_index, exception, sleep_seconds)`
    after every failed attempt that will be retried — used for telemetry
    and AIMD signals to the rate limiter. Tests inject `sleep` and
    `rand` so behavior is deterministic without real wall-clock waits.
    """
    cfg = config or RetryConfig()

    def decorator(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            last_exc: BaseException | None = None
            for attempt in range(cfg.max_attempts):
                try:
                    return await fn(*args, **kwargs)
                except BaseException as exc:
                    if isinstance(exc, cfg.give_up_on):
                        raise
                    if not isinstance(exc, cfg.retry_on):
                        raise
                    last_exc = exc
                    if attempt + 1 >= cfg.max_attempts:
                        break
                    delay = _compute_delay(cfg, attempt, rand)
                    if on_retry is not None:
                        await on_retry(attempt, exc, delay)
                    await sleep(delay)
            assert last_exc is not None
            raise RetryError(
                f"{fn.__name__} exhausted {cfg.max_attempts} attempts; "
                f"last error: {type(last_exc).__name__}: {last_exc}"
            ) from last_exc

        return wrapper

    return decorator


def _compute_delay(
    cfg: RetryConfig,
    attempt: int,
    rand: Callable[[float, float], float],
) -> float:
    raw = cfg.initial_backoff * (cfg.base**attempt)
    capped = min(raw, cfg.max_backoff)
    lo, hi = cfg.jitter
    return capped * rand(lo, hi)


def collect_retryable(*types: type[BaseException]) -> tuple[type[BaseException], ...]:
    """Helper to combine retryable exception sets across modules.

    Connectors compose `(httpx.HTTPStatusError, RateLimited, OSError)` etc.
    by name; this little helper gives them a typed home.
    """
    seen: list[type[BaseException]] = []
    for t in types:
        if t not in seen:
            seen.append(t)
    return tuple(seen)


def merge_configs(base: RetryConfig, **overrides: object) -> RetryConfig:
    """Return a new RetryConfig with selected fields overridden.

    Used by per-connector `RetryConfig` overrides ("Slack uses 8 attempts
    because Tier 3 throttling is sticky") so we don't have to construct
    full configs from scratch every time.
    """
    fields = {
        "max_attempts": base.max_attempts,
        "initial_backoff": base.initial_backoff,
        "max_backoff": base.max_backoff,
        "base": base.base,
        "jitter": base.jitter,
        "retry_on": base.retry_on,
        "give_up_on": base.give_up_on,
    }
    fields.update(overrides)
    # mypy: ** kwargs unpacking into TypedDict-less factory.
    return RetryConfig(**fields)  # type: ignore[arg-type]


def retryable_sequence(
    seq: Sequence[type[BaseException]],
) -> tuple[type[BaseException], ...]:
    """Materialize an iterable of exception types as a frozen tuple."""
    return tuple(seq)
