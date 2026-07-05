"""Auto-download of spaCy NER model wheels.

Model wheels are too large to ship inside the package, so they are pulled on
demand from Hugging Face the first time a language is requested. After
install they live in the active interpreter's site-packages and are loadable
via ``spacy.load(<name>)``.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import shutil
import subprocess
import sys
from typing import Literal

logger = logging.getLogger("pleno_anonymize")

Language = Literal["ja", "en"]

# Pinned model URLs mirror the server's Dockerfile. Bumping a model means
# bumping this dict — never read pleno-anonymize-server's Dockerfile at
# runtime, the SDK must work standalone.
MODEL_WHEELS: dict[str, str] = {
    # 0.3.0 = iter10 データ (org_boundary/hard-negative 済み 28k docs) +
    # Faker ja_JP 合成 10k を統合再訓練。凍結ベンチ v0.4.0 / v0.13.0-held-out
    # の両方で出荷 0.2.0 を上回る (実測は experiments/log.jsonl 参照)。
    "pleno_anonymize_ja": "https://huggingface.co/0xhikae/pleno_anonymize_ja/resolve/main/pleno_anonymize_ja-0.3.0-py3-none-any.whl",
    # 0.3.0 = license-clean synthetic retrain (Faker component-granularity
    # spans + LLM-generated docs, ~25MB wheel). pii-masking-300k EN F1
    # 0.32→0.58 together with the Presidio taxonomy mapping fix; the
    # benchmark dataset itself is never trained on (AI4Privacy license).
    "pleno_anonymize_en": "https://huggingface.co/0xhikae/pleno_anonymize_en/resolve/main/pleno_anonymize_en-0.3.0-py3-none-any.whl",
}

LANGUAGE_TO_MODEL: dict[Language, str] = {
    "ja": "pleno_anonymize_ja",
    "en": "pleno_anonymize_en",
}


def model_for(language: Language) -> str:
    if language not in LANGUAGE_TO_MODEL:
        raise ValueError(f"unsupported language: {language!r}")
    return LANGUAGE_TO_MODEL[language]


def is_installed(model_name: str) -> bool:
    return importlib.util.find_spec(model_name) is not None


def _pip_works() -> bool:
    # `find_spec("pip")` lies inside uvx-managed envs: a stale `pip*.dist-info`
    # makes the spec discoverable even though the module fails to import
    # (`No module named pip`). Actually invoke pip to confirm.
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "--version"],
            check=True,
            capture_output=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _install_command(url: str, *, quiet: bool = False) -> list[str]:
    if _pip_works():
        cmd = [sys.executable, "-m", "pip", "install", "--no-cache-dir"]
        if quiet:
            cmd.append("--quiet")
        cmd.append(url)
        return cmd

    uv = shutil.which("uv")
    if uv:
        cmd = [uv, "pip", "install", "--python", sys.executable]
        if quiet:
            cmd.append("--quiet")
        cmd.append(url)
        return cmd

    raise RuntimeError(
        "cannot install model wheel: neither pip nor uv is available in "
        f"{sys.executable}"
    )


def install(model_name: str, *, quiet: bool = False) -> None:
    """Install a model wheel into the running interpreter."""
    if model_name not in MODEL_WHEELS:
        raise ValueError(f"unknown model: {model_name!r}")
    url = MODEL_WHEELS[model_name]
    cmd = _install_command(url, quiet=quiet)
    logger.info("installing %s from %s", model_name, url)
    subprocess.run(cmd, check=True)
    importlib.invalidate_caches()


def ensure(
    language: Language,
    *,
    auto_download: bool = True,
    quiet: bool = False,
) -> str | None:
    """Ensure the NER model for ``language`` is importable.

    Returns the model name on success, ``None`` if the model is missing and
    ``auto_download`` is False (callers fall back to tokenizer-only mode).
    """
    name = model_for(language)
    if is_installed(name):
        return name
    if not auto_download:
        return None
    install(name, quiet=quiet)
    return name
