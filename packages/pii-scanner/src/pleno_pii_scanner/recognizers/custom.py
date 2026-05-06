"""Load BYOD recognizers from a TOML config file.

The file format is documented in `docs/connectors/custom-recognizers.md`
(forthcoming). Minimal example:

    [[recognizer]]
    entity = "INTERNAL_API_TOKEN"
    language = "any"
    context = ["api_key", "internal_token"]

    [[recognizer.patterns]]
    name = "internal_v1"
    regex = "INT-[A-Z0-9]{32}"
    score = 0.9

    [recognizer.verifier]
    type = "regex_check"
    extra_pattern = "^INT-[A-Z]{4}-"

The loader is strict — unknown top-level keys, missing required fields,
or malformed regex raise rather than silently degrading. A scanner run
with bad custom config must fail loudly so the operator catches the
typo before the scan completes returning false negatives.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from pleno_recognizers.types import PiiPattern, PiiRecognizer

from pleno_pii_scanner.recognizers.verifiers import (
    Verifier,
    VerifierResolutionError,
    resolve_verifier,
)


class CustomRecognizerError(Exception):
    """Base error for the BYOD loader."""


class CustomRecognizerLoadError(CustomRecognizerError):
    """File could not be opened or parsed as TOML."""


class CustomRecognizerSchemaError(CustomRecognizerError):
    """TOML parsed but the structure does not match the recognizer schema."""


_REQUIRED_RECOGNIZER_KEYS = frozenset({"entity", "patterns"})
_ALLOWED_RECOGNIZER_KEYS = frozenset(
    {"entity", "language", "context", "patterns", "verifier"}
)
_REQUIRED_PATTERN_KEYS = frozenset({"name", "regex", "score"})
_ALLOWED_PATTERN_KEYS = frozenset({"name", "regex", "score"})


def load_custom_recognizers(
    path: str | Path,
) -> tuple[tuple[PiiRecognizer, ...], dict[str, Verifier]]:
    """Load recognizers from `path` and return (recognizers, verifiers_by_entity).

    The two return values are separated because `PiiRecognizer` from the
    upstream `pleno_recognizers.types` does not have a verifier field —
    we keep the dataclass shape unchanged so existing scanner code paths
    (regex_pass, presidio_adapter) accept BYOD recognizers without
    modification, and we attach verifiers as a sidecar map keyed on
    entity for the verify pass to consult.
    """
    p = Path(path).expanduser()
    if not p.is_file():
        raise CustomRecognizerLoadError(f"custom recognizer file not found: {p}")
    try:
        with p.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise CustomRecognizerLoadError(f"could not parse {p} as TOML: {exc}") from exc
    except OSError as exc:
        raise CustomRecognizerLoadError(f"could not read {p}: {exc}") from exc

    recognizers_raw = raw.get("recognizer")
    if recognizers_raw is None:
        raise CustomRecognizerSchemaError(
            f"{p}: missing required `[[recognizer]]` array"
        )
    if not isinstance(recognizers_raw, list):
        raise CustomRecognizerSchemaError(
            f"{p}: `recognizer` must be a TOML array of tables, "
            f"got {type(recognizers_raw).__name__}"
        )

    extra_keys = set(raw) - {"recognizer"}
    if extra_keys:
        raise CustomRecognizerSchemaError(
            f"{p}: unknown top-level keys: {sorted(extra_keys)}"
        )

    recognizers: list[PiiRecognizer] = []
    verifiers: dict[str, Verifier] = {}
    seen_entities: set[str] = set()

    for idx, item in enumerate(recognizers_raw):
        if not isinstance(item, dict):
            raise CustomRecognizerSchemaError(
                f"{p}: recognizer[{idx}] must be a table, got {type(item).__name__}"
            )
        recognizer, verifier = _parse_recognizer(p, idx, item)
        if recognizer.entity in seen_entities:
            # Same entity defined twice in one file is almost always a
            # copy-paste bug. Loud failure beats silent override.
            raise CustomRecognizerSchemaError(
                f"{p}: recognizer[{idx}] declares duplicate entity "
                f"{recognizer.entity!r}"
            )
        seen_entities.add(recognizer.entity)
        recognizers.append(recognizer)
        if verifier is not None:
            verifiers[recognizer.entity] = verifier

    return tuple(recognizers), verifiers


def _parse_recognizer(
    p: Path,
    idx: int,
    item: Mapping[str, Any],
) -> tuple[PiiRecognizer, Verifier | None]:
    keys = set(item)
    missing = _REQUIRED_RECOGNIZER_KEYS - keys
    if missing:
        raise CustomRecognizerSchemaError(
            f"{p}: recognizer[{idx}] missing required keys: {sorted(missing)}"
        )
    extra = keys - _ALLOWED_RECOGNIZER_KEYS
    if extra:
        raise CustomRecognizerSchemaError(
            f"{p}: recognizer[{idx}] has unknown keys: {sorted(extra)}"
        )

    entity = item["entity"]
    if not isinstance(entity, str) or not entity:
        raise CustomRecognizerSchemaError(
            f"{p}: recognizer[{idx}].entity must be a non-empty string"
        )

    language = item.get("language", "any")
    if not isinstance(language, str) or not language:
        raise CustomRecognizerSchemaError(
            f"{p}: recognizer[{idx}].language must be a non-empty string"
        )

    context_raw = item.get("context", [])
    if not isinstance(context_raw, list) or not all(
        isinstance(c, str) for c in context_raw
    ):
        raise CustomRecognizerSchemaError(
            f"{p}: recognizer[{idx}].context must be a list of strings"
        )

    patterns = _parse_patterns(p, idx, item["patterns"])

    verifier_raw = item.get("verifier")
    verifier: Verifier | None = None
    if verifier_raw is not None:
        verifier = _parse_verifier(p, idx, verifier_raw)

    return (
        PiiRecognizer(
            entity=entity,
            language=language,
            patterns=patterns,
            context=tuple(context_raw),
        ),
        verifier,
    )


def _parse_patterns(p: Path, idx: int, raw: Iterable[Any]) -> tuple[PiiPattern, ...]:
    if not isinstance(raw, list) or not raw:
        raise CustomRecognizerSchemaError(
            f"{p}: recognizer[{idx}].patterns must be a non-empty array"
        )
    patterns: list[PiiPattern] = []
    seen_names: set[str] = set()
    for pidx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise CustomRecognizerSchemaError(
                f"{p}: recognizer[{idx}].patterns[{pidx}] must be a table"
            )
        keys = set(item)
        missing = _REQUIRED_PATTERN_KEYS - keys
        if missing:
            raise CustomRecognizerSchemaError(
                f"{p}: recognizer[{idx}].patterns[{pidx}] missing keys: "
                f"{sorted(missing)}"
            )
        extra = keys - _ALLOWED_PATTERN_KEYS
        if extra:
            raise CustomRecognizerSchemaError(
                f"{p}: recognizer[{idx}].patterns[{pidx}] unknown keys: {sorted(extra)}"
            )
        name = item["name"]
        regex_str = item["regex"]
        score = item["score"]
        if not isinstance(name, str) or not name:
            raise CustomRecognizerSchemaError(
                f"{p}: recognizer[{idx}].patterns[{pidx}].name must be a "
                f"non-empty string"
            )
        if name in seen_names:
            raise CustomRecognizerSchemaError(
                f"{p}: recognizer[{idx}].patterns[{pidx}] duplicate name {name!r}"
            )
        seen_names.add(name)
        if not isinstance(regex_str, str) or not regex_str:
            raise CustomRecognizerSchemaError(
                f"{p}: recognizer[{idx}].patterns[{pidx}].regex must be a "
                f"non-empty string"
            )
        try:
            re.compile(regex_str)
        except re.error as exc:
            raise CustomRecognizerSchemaError(
                f"{p}: recognizer[{idx}].patterns[{pidx}].regex is invalid: {exc}"
            ) from exc
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            raise CustomRecognizerSchemaError(
                f"{p}: recognizer[{idx}].patterns[{pidx}].score must be a number"
            )
        score_f = float(score)
        if not 0.0 <= score_f <= 1.0:
            raise CustomRecognizerSchemaError(
                f"{p}: recognizer[{idx}].patterns[{pidx}].score must be in [0,1], "
                f"got {score_f}"
            )
        patterns.append(PiiPattern(name=name, regex=regex_str, score=score_f))
    return tuple(patterns)


def _parse_verifier(p: Path, idx: int, raw: Any) -> Verifier:
    if not isinstance(raw, dict):
        raise CustomRecognizerSchemaError(
            f"{p}: recognizer[{idx}].verifier must be a table"
        )
    type_name = raw.get("type")
    if not isinstance(type_name, str) or not type_name:
        raise CustomRecognizerSchemaError(
            f"{p}: recognizer[{idx}].verifier.type must be a non-empty string"
        )
    params = {k: v for k, v in raw.items() if k != "type"}
    try:
        return resolve_verifier(type_name, params)
    except VerifierResolutionError as exc:
        raise CustomRecognizerSchemaError(
            f"{p}: recognizer[{idx}].verifier: {exc}"
        ) from exc
