"""OpenAI Privacy Filter (OPF) engine — wraps the open-source `opf` package.

Backed by the `openai/privacy-filter` HuggingFace checkpoint (Apache 2.0,
1.5B params, 50M active). The `[openai]` extra was removed (PyPI rejects
direct-URL extras). Install the dependency separately:

    pip install pleno-anonymize 'opf @ git+https://github.com/openai/privacy-filter@main'

Model weights auto-download to ``~/.opf/privacy_filter`` (or ``$OPF_CHECKPOINT``)
on first call. CPU is supported but GPU is recommended for any non-trivial
volume.

OPF emits 8 native label classes; we normalize them to pleno's entity_type
taxonomy so the same anonymizer / scanner / proxy pipeline works regardless
of the underlying backend.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from ._engine import Finding, RedactResult

logger = logging.getLogger("pleno_anonymize.opf")


# OPF native label -> pleno entity_type. `secret` has no pleno equivalent so
# we surface a new SECRET class — the scanner / anonymizer treat unknown types
# generically, only the name matters.
OPF_LABEL_TO_PLENO: dict[str, str] = {
    "account_number": "BANK_ACCOUNT",
    "private_address": "ADDRESS",
    "private_email": "EMAIL_ADDRESS",
    "private_person": "PERSON",
    "private_phone": "PHONE_NUMBER",
    "private_url": "URL",
    "private_date": "DATE_OF_BIRTH",
    "secret": "SECRET",
}


def _default_device() -> str:
    """Pick `cuda` if available, else `cpu`. Keeps the CLI usable on laptops."""
    try:
        import torch  # type: ignore[import-not-found]

        if torch.cuda.is_available():
            return "cuda"
    except Exception:  # noqa: BLE001  # pragma: no cover
        pass
    return "cpu"


class OpfEngine:
    """OpenAI Privacy Filter engine.

    The underlying `opf.OPF` instance is created lazily on first analyze /
    redact call and then cached.
    """

    def __init__(
        self,
        *,
        checkpoint: str | None = None,
        device: str | None = None,
    ) -> None:
        self._checkpoint = checkpoint
        self._device = device
        self._opf = None

    # public API ---------------------------------------------------------------

    def analyze(
        self,
        text: str,
        *,
        language: str = "ja",
        entities: Iterable[str] | None = None,
    ) -> list[Finding]:
        if not text:
            return []
        result = self._get_opf().redact(text)
        filt = set(entities) if entities else None
        out: list[Finding] = []
        for span in result.detected_spans:
            entity_type = OPF_LABEL_TO_PLENO.get(span.label, span.label.upper())
            if filt and entity_type not in filt:
                continue
            out.append(
                Finding(
                    entity_type=entity_type,
                    start=int(span.start),
                    end=int(span.end),
                    score=1.0,
                    text=text[int(span.start) : int(span.end)],
                )
            )
        return out

    def redact(
        self,
        text: str,
        *,
        language: str = "ja",
        entities: Iterable[str] | None = None,
        operators: dict[str, dict[str, object]] | None = None,
    ) -> RedactResult:
        if not text:
            return RedactResult(text=text)
        # When no entity filter and no custom operators are requested, hand the
        # job to OPF directly — its placeholder substitution is offset-safe.
        if not entities and not operators:
            result = self._get_opf().redact(text)
            return RedactResult(text=result.redacted_text)
        findings = self.analyze(text, language=language, entities=entities)
        out = text
        for f in sorted(findings, key=lambda x: x.start, reverse=True):
            replacement = f"<{f.entity_type}>"
            if operators and f.entity_type in operators:
                cfg = operators[f.entity_type]
                op_type = cfg.get("type", "replace")
                # OpfEngine only implements "replace". Silently ignoring an
                # unsupported operator (e.g. "mask"/"hash") would emit the
                # default placeholder while the caller believes their operator
                # was applied — fail loudly instead.
                if op_type != "replace":
                    raise ValueError(
                        f"OpfEngine supports only the 'replace' operator, "
                        f"got {op_type!r} for {f.entity_type}"
                    )
                replacement = str(cfg.get("new_value", replacement))
            out = out[: f.start] + replacement + out[f.end :]
        return RedactResult(text=out)

    # internal -----------------------------------------------------------------

    def _get_opf(self):
        if self._opf is not None:
            return self._opf
        try:
            from opf import OPF  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "OpenAI Privacy Filter requires the `opf` package. "
                "Install it with: pip install 'opf @ git+https://github.com/openai/privacy-filter@main'"
            ) from exc
        device = self._device or _default_device()
        kwargs: dict[str, object] = {"device": device, "output_mode": "typed"}
        if self._checkpoint is not None:
            kwargs["model"] = self._checkpoint
        logger.info("loading OPF checkpoint (device=%s)", device)
        self._opf = OPF(**kwargs)
        return self._opf
