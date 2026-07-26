"""Public engine surface — Protocol + factory."""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


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


def PlenoAnonymize(
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    languages: tuple[str, ...] = ("ja",),
    auto_download: bool = True,
    timeout: float = 30.0,
    engine: str = "builtin",
    opf_checkpoint: str | None = None,
    opf_device: str | None = None,
) -> Engine:
    """Create an engine.

    ``engine="builtin"`` (default): :class:`LocalEngine` running Presidio +
    spaCy in-process. The first invocation per language downloads the
    matching NER wheel from Hugging Face when ``auto_download`` is True.

    ``engine="openai-privacy-filter"``: :class:`OpfEngine` running the
    open-source `openai/privacy-filter` checkpoint via the `opf` package.
    Requires the ``opf`` package (install separately; see _opf.py for instructions).

    Pass ``base_url`` (e.g. ``"https://pleno-anonymize.fly.dev"``) to
    instead use a hosted server via HTTP. The remote engine takes
    precedence over ``engine``.
    """
    resolved = base_url or os.environ.get("PLENO_ANONYMIZE_BASE_URL")
    if resolved:
        from ._remote import RemoteEngine

        return RemoteEngine(
            base_url=resolved,
            api_key=api_key or os.environ.get("PLENO_ANONYMIZE_API_KEY"),
            timeout=timeout,
        )

    if engine == "openai-privacy-filter":
        from ._opf import OpfEngine

        return OpfEngine(checkpoint=opf_checkpoint, device=opf_device)

    if engine != "builtin":
        raise ValueError(
            f"unknown engine: {engine!r} "
            "(expected 'builtin' or 'openai-privacy-filter')"
        )

    from ._local import LocalEngine

    return LocalEngine(
        languages=languages,
        auto_download=auto_download,
    )
