from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PiiPattern:
    name: str
    regex: str
    score: float


@dataclass(frozen=True, slots=True)
class PiiRecognizer:
    entity: str
    language: str
    patterns: tuple[PiiPattern, ...]
    context: tuple[str, ...] = ()
