"""Public engine surface — Protocol + factory."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable, Protocol, runtime_checkable


@dataclass(slots=True, frozen=True)
class Finding:
    entity_type: str
    start: int
    end: int
    score: float
    text: str

    def to_dict(self) -> dict[str, object]:
        return {
            "entity_type": self.entity_type,
            "start": self.start,
            "end": self.end,
            "score": self.score,
            "text": self.text,
        }


@dataclass(slots=True, frozen=True)
class RedactResult:
    text: str

    def to_dict(self) -> dict[str, object]:
        return {"text": self.text}


@runtime_checkable
class Engine(Protocol):
    """Common surface implemented by both local and remote engines."""

    def analyze(
        self,
        text: str,
        *,
        language: str = "ja",
        entities: Iterable[str] | None = None,
    ) -> list[Finding]: ...

    def redact(
        self,
        text: str,
        *,
        language: str = "ja",
        entities: Iterable[str] | None = None,
        operators: dict[str, dict[str, object]] | None = None,
    ) -> RedactResult: ...


def PlenoAnonymize(  # noqa: N802 - factory disguised as a class for ergonomics
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    languages: tuple[str, ...] = ("ja",),
    auto_download: bool = True,
    timeout: float = 30.0,
) -> Engine:
    """Create an engine.

    Default (``base_url=None``): :class:`LocalEngine` running Presidio +
    spaCy in-process. The first invocation per language downloads the
    matching NER wheel from Hugging Face when ``auto_download`` is True.

    Pass ``base_url`` (e.g. ``"https://pleno-anonymize.fly.dev"``) to
    instead use a hosted server via HTTP. The remote engine has no local
    model footprint.
    """
    resolved = base_url or os.environ.get("PLENO_ANONYMIZE_BASE_URL")
    if resolved:
        from ._remote import RemoteEngine

        return RemoteEngine(
            base_url=resolved,
            api_key=api_key or os.environ.get("PLENO_ANONYMIZE_API_KEY"),
            timeout=timeout,
        )

    from ._local import LocalEngine

    return LocalEngine(
        languages=languages,
        auto_download=auto_download,
    )
