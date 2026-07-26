"""Consistency gate between the single-source-of-truth versions.json (#296)
and pleno_anonymize._models.MODEL_WHEELS.

_models.py never reads versions.json at runtime — the SDK must keep working
standalone even if packages/models/ is absent from an installed wheel (see
_models.py:23-25). So instead of generating MODEL_WHEELS from versions.json,
this test asserts they agree and fails CI the moment someone bumps one file
without the other.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pleno_anonymize._models import MODEL_WHEELS

# packages/sdk/tests/test_model_versions.py -> packages/models/versions.json
VERSIONS_JSON = Path(__file__).resolve().parents[2] / "models" / "versions.json"

LANGUAGE_TO_MODEL_NAME = {
    "ja": "pleno_anonymize_ja",
    "en": "pleno_anonymize_en",
}


def _load_versions() -> dict:
    with VERSIONS_JSON.open(encoding="utf-8") as f:
        return json.load(f)


def test_versions_json_exists() -> None:
    assert VERSIONS_JSON.is_file(), f"single source of truth missing: {VERSIONS_JSON}"


@pytest.mark.parametrize("language", sorted(LANGUAGE_TO_MODEL_NAME))
def test_model_wheels_matches_versions_json(language: str) -> None:
    versions = _load_versions()
    model_name = LANGUAGE_TO_MODEL_NAME[language]
    entry = versions[language]

    assert model_name in MODEL_WHEELS, (
        f"{model_name} declared in versions.json but missing from MODEL_WHEELS"
    )
    assert MODEL_WHEELS[model_name] == entry["wheel_url"], (
        f"MODEL_WHEELS[{model_name!r}] and versions.json[{language!r}].wheel_url "
        "have drifted — bump both together (see packages/training/Makefile "
        "release-model target)."
    )
    # The version pinned in versions.json must actually be embedded in the
    # wheel filename the URL resolves to — catches a version bump that forgot
    # to also update the URL (or vice versa).
    assert f"-{entry['version']}-py3-none-any.whl" in entry["wheel_url"], (
        f"versions.json[{language!r}]: version {entry['version']!r} is not "
        f"reflected in wheel_url {entry['wheel_url']!r}"
    )


def test_no_extra_models_in_wheels() -> None:
    """Every MODEL_WHEELS entry must trace back to versions.json — otherwise
    the SDK could ship a model with no recorded version provenance."""
    versions = _load_versions()
    known_models = {
        LANGUAGE_TO_MODEL_NAME[lang]
        for lang in versions
        if lang in LANGUAGE_TO_MODEL_NAME
    }
    assert set(MODEL_WHEELS) == known_models
