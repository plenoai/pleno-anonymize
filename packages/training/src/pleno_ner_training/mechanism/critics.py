"""Dual-critic verification loop (Simula §3.4).

Two independent critics gate every generated sample:

    Critic A — label correctness
        Checks each (text, span, label) triple. Returns either
        `PASS` or a corrected span list. Two consecutive `FAIL`s
        rejects the sample.

    Critic B — realism + coverage
        Judges whether the sample is plausible for its taxonomy
        leaf and whether the entity density matches. Failures
        reject the sample and re-roll the meta-prompt upstream.

Both critics use a different model SKU from the generator to
mitigate sycophancy (per Simula's note on dual-population
critique). A local pure-rule baseline (`LocalCritic`) is also
provided so the pipeline stays runnable without network access
and so tests can exercise the loop deterministically.

The module purposefully does **not** call the LLM eagerly — the
generator (#152) decides how to route samples through critics
and is responsible for batching.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Protocol

from pleno_ner_training.mechanism.complexify import Sample, Span, validate_spans


# --- Verdict types -------------------------------------------------------

@dataclass(frozen=True)
class LabelVerdict:
    passed: bool
    fixed_spans: list[Span] | None = None
    reason: str = ""


@dataclass(frozen=True)
class RealismVerdict:
    passed: bool
    reason: str = ""


@dataclass
class CriticStats:
    seen: int = 0
    label_pass: int = 0
    label_fixed: int = 0
    label_rejected: int = 0
    realism_pass: int = 0
    realism_rejected: int = 0
    reject_reasons: dict[str, int] = field(default_factory=dict)

    def accept_rate(self) -> float:
        return (self.label_pass + self.label_fixed) / max(self.seen, 1) * (
            self.realism_pass / max(self.label_pass + self.label_fixed, 1)
        ) if self.seen else 0.0


# --- Critic protocol -----------------------------------------------------

class LabelCritic(Protocol):
    def critique_label(self, sample: Sample) -> LabelVerdict: ...


class RealismCritic(Protocol):
    def critique_realism(self, sample: Sample, taxonomy_leaf: dict) -> RealismVerdict: ...


# --- Local (rule-based) critics -----------------------------------------

# Patterns that approximate what each label *should* look like at the
# surface level. The rule-based critic is intentionally conservative —
# false-fails are far costlier than false-passes here, since this is
# the per-sample fallback when no LLM critic is wired in. Final
# correctness still depends on the LLM critic in production runs.
_LABEL_SHAPE = {
    "EMAIL_ADDRESS": re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+"),
    "PHONE_NUMBER": re.compile(r"[\d０-９][\d０-９\-−－ ]{6,}"),
    "MY_NUMBER": re.compile(r"[\d０-９][\d０-９ ]{10,}"),
    "CREDIT_CARD": re.compile(r"[\d０-９][\d０-９\- ]{12,}"),
    "POSTAL_CODE": re.compile(r"〒?\s*[\d０-９]{3}[\-−－]?[\d０-９]{4}"),
    "URL": re.compile(r"https?://"),
    "IP_ADDRESS": re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}"),
}


@dataclass
class LocalLabelCritic:
    """Rule-based label-correctness critic.

    Verdict logic:
        - For labels in _LABEL_SHAPE, the span surface must match.
        - For free-text labels (PERSON/ADDRESS/ORGANIZATION/...),
          require the surface to be non-empty and not pure whitespace.
        - Sample-level span invariants must hold.
    """

    def critique_label(self, sample: Sample) -> LabelVerdict:
        try:
            validate_spans(sample)
        except ValueError as e:
            return LabelVerdict(False, None, f"invalid spans: {e}")
        for span in sample.entities:
            surface = sample.text[span.start : span.end]
            if not surface.strip():
                return LabelVerdict(False, None, f"empty span at {span.start}:{span.end}")
            pattern = _LABEL_SHAPE.get(span.label)
            if pattern is not None and not pattern.search(surface):
                return LabelVerdict(False, None, f"{span.label} shape mismatch: {surface!r}")
        return LabelVerdict(True)


@dataclass
class LocalRealismCritic:
    """Rule-based realism critic.

    Verdict logic:
        - text must be at least 30 chars (too short = unrealistic) and
          at most 4000 (too long = unlikely scenario).
        - entity_density matches the leaf's declared density target
          within tolerance.
        - expected_entities — at least one expected entity must appear.
    """

    # Bands overlap because in short Japanese text 1 PII entity per 30 chars
    # is normal for "medium" — a strict non-overlapping scheme rejects most
    # realistic short-form samples. Calibrated against the seed taxonomy's
    # entity_density labels.
    # Empirical calibration after the smoke run: the LLM tends to under-
    # pack PII in JP "dense" docs (real-world density rarely exceeds 0.04
    # in long-form text). Bands widened to keep realistic samples while
    # still catching pathological cases.
    DENSITY_TARGETS = {
        "sparse": (0.0, 0.030),
        "medium": (0.005, 0.150),
        "dense": (0.015, 0.400),
    }
    MIN_LEN = 30
    MAX_LEN = 4000

    def critique_realism(self, sample: Sample, taxonomy_leaf: dict) -> RealismVerdict:
        n = len(sample.text)
        if n < self.MIN_LEN:
            return RealismVerdict(False, "too short")
        if n > self.MAX_LEN:
            return RealismVerdict(False, "too long")
        density = len(sample.entities) / max(n, 1)
        target = taxonomy_leaf.get("entity_density", "medium")
        lo, hi = self.DENSITY_TARGETS.get(target, (0.0, 1.0))
        if not (lo <= density <= hi):
            return RealismVerdict(False, f"density {density:.4f} outside {target} band [{lo}, {hi}]")
        expected = set(taxonomy_leaf.get("expected_entities", []))
        observed = {s.label for s in sample.entities}
        if expected and not (expected & observed):
            return RealismVerdict(False, "no expected entity present")
        return RealismVerdict(True)


# --- LLM-backed critics (opt-in) ----------------------------------------

@dataclass
class OpenAILabelCritic:
    """LLM-backed label-correctness critic.

    Uses a *different* model from the generator to mitigate sycophancy
    (per Simula §3.4 ``dual-population critique``). Default is
    `gpt-4o-mini`; the generator in #152 will be `gpt-4o` or a
    reasoning model.

    Returns a verdict. On `FAIL`, attempts to extract a `fixed_spans`
    array from the response so the caller can run the auto-correct
    branch.
    """

    model: str = "gpt-4o-mini"

    def critique_label(self, sample: Sample) -> LabelVerdict:
        try:
            from openai import OpenAI
        except ImportError:
            return LabelVerdict(False, None, "openai missing")
        if "OPENAI_API_KEY" not in os.environ:
            return LabelVerdict(False, None, "no api key")
        client = OpenAI()
        prompt = (
            "次の日本語サンプルについて、各スパンが正しい PII ラベル付けか判定してください。\n"
            "正しければ {\"verdict\": \"PASS\"} のみ。\n"
            "誤りがあれば修正済みスパンを {\"verdict\": \"FIX\", \"fixed_spans\":"
            " [{\"start\":..,\"end\":..,\"label\":..},...]} で返してください。\n"
            "本物の PII が抜けていれば加えても構いません。JSON 以外は出力禁止。\n\n"
            f"text: {sample.text}\n"
            f"spans: {[(s.start, s.end, s.label) for s in sample.entities]}"
        )
        resp = client.chat.completions.create(
            model=self.model,
            temperature=0.0,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = (resp.choices[0].message.content or "").strip().strip("`").strip()
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return LabelVerdict(False, None, f"bad JSON from critic: {raw[:80]}")
        if obj.get("verdict") == "PASS":
            return LabelVerdict(True)
        if obj.get("verdict") == "FIX":
            fixed = [Span(s["start"], s["end"], s["label"]) for s in obj.get("fixed_spans", [])]
            return LabelVerdict(False, fixed, "critic proposed fix")
        return LabelVerdict(False, None, "unrecognised verdict")


@dataclass
class OpenAIRealismCritic:
    model: str = "gpt-4o-mini"

    def critique_realism(self, sample: Sample, taxonomy_leaf: dict) -> RealismVerdict:
        try:
            from openai import OpenAI
        except ImportError:
            return RealismVerdict(False, "openai missing")
        if "OPENAI_API_KEY" not in os.environ:
            return RealismVerdict(False, "no api key")
        client = OpenAI()
        prompt = (
            f"シナリオ: {taxonomy_leaf.get('ja_name')} (id={taxonomy_leaf.get('id')})\n"
            f"想定文書種別: {taxonomy_leaf.get('document_type')}\n"
            f"想定エンティティ密度: {taxonomy_leaf.get('entity_density')}\n"
            f"想定 PII 種別: {taxonomy_leaf.get('expected_entities')}\n\n"
            f"以下のテキストはこのシナリオとして自然か、エンティティ密度が想定と整合的か判定してください。\n"
            f"自然なら {{\"verdict\": \"PASS\"}}、不自然なら {{\"verdict\": \"FAIL\", \"reason\": ...}}\n"
            "JSON 以外は出力禁止。\n\n"
            f"text: {sample.text}"
        )
        resp = client.chat.completions.create(
            model=self.model,
            temperature=0.0,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = (resp.choices[0].message.content or "").strip().strip("`").strip()
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return RealismVerdict(False, f"bad JSON: {raw[:80]}")
        if obj.get("verdict") == "PASS":
            return RealismVerdict(True)
        return RealismVerdict(False, obj.get("reason", "rejected"))


# --- Verification loop ---------------------------------------------------

@dataclass
class CriticPipeline:
    label_critic: LabelCritic
    realism_critic: RealismCritic
    max_label_retries: int = 1
    stats: CriticStats = field(default_factory=CriticStats)

    def verify(self, sample: Sample, taxonomy_leaf: dict) -> tuple[Sample, str]:
        """Return (sample, verdict) where verdict is one of {pass, fixed, rejected}."""
        self.stats.seen += 1

        for attempt in range(self.max_label_retries + 1):
            verdict = self.label_critic.critique_label(sample)
            if verdict.passed:
                if attempt == 0:
                    self.stats.label_pass += 1
                else:
                    self.stats.label_fixed += 1
                break
            if verdict.fixed_spans is not None and attempt < self.max_label_retries:
                sample = Sample(
                    text=sample.text,
                    entities=verdict.fixed_spans,
                    difficulty=sample.difficulty,
                    operators_applied=list(sample.operators_applied),
                )
                continue
            self.stats.label_rejected += 1
            self.stats.reject_reasons[f"label:{verdict.reason or 'unknown'}"] = (
                self.stats.reject_reasons.get(f"label:{verdict.reason or 'unknown'}", 0) + 1
            )
            return sample, "rejected"

        realism = self.realism_critic.critique_realism(sample, taxonomy_leaf)
        if realism.passed:
            self.stats.realism_pass += 1
            return sample, "pass" if attempt == 0 else "fixed"
        self.stats.realism_rejected += 1
        self.stats.reject_reasons[f"realism:{realism.reason or 'unknown'}"] = (
            self.stats.reject_reasons.get(f"realism:{realism.reason or 'unknown'}", 0) + 1
        )
        return sample, "rejected"
