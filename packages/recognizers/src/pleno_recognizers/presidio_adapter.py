"""Adapter to expose `PiiRecognizer` definitions as Presidio `PatternRecognizer`s.

Importing this module pulls in `presidio-analyzer`. Scanner code MUST NOT
import this module — it should consume the raw `PiiRecognizer` data from
`pleno_recognizers.ja` directly.
"""

from presidio_analyzer import Pattern, PatternRecognizer

from pleno_recognizers.types import PiiRecognizer


def to_presidio(recognizer: PiiRecognizer) -> PatternRecognizer:
    return PatternRecognizer(
        supported_entity=recognizer.entity,
        supported_language=recognizer.language,
        patterns=[Pattern(p.name, p.regex, p.score) for p in recognizer.patterns],
        context=list(recognizer.context),
    )


def all_ja_presidio() -> list[PatternRecognizer]:
    from pleno_recognizers.ja import ALL_JA_RECOGNIZERS

    return [to_presidio(r) for r in ALL_JA_RECOGNIZERS]
