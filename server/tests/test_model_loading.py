"""Regression guard: server must load NER models via spaCy package-name lookup.

Production playground broke after PR #34 because `_init_presidio` resolved models
through a filesystem path (`packages/models/...`) that no longer existed once the
Dockerfile switched from `COPY packages/models/` to HF wheel install (PR #23,
4 weeks earlier). The bug stayed latent until cache eviction.
"""

import pytest


def test_init_presidio_loads_real_ja_model():
    """`_init_presidio()` must succeed when ja_ner_ja wheel is installed."""
    pytest.importorskip("ja_ner_ja")

    import server.src.app as app

    # Reset module-level singletons so this test sees a clean init.
    app._nlp_ja = None
    app._nlp_en = None
    app._analyzer = None
    app._anonymizer = None

    app._init_presidio()

    assert app._analyzer is not None
    assert app._nlp_ja is not None


def test_app_does_not_resolve_models_by_filesystem_path():
    """Guard against the regressed pattern coming back via copy-paste."""
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "src" / "app.py").read_text()
    assert "packages/models" not in src, (
        "_init_presidio must not look up models via a filesystem path; "
        "use spacy.load(<package_name>) instead."
    )
