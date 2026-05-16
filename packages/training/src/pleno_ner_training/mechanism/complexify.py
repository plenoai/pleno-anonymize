"""Complexification: difficulty as an independent axis.

Per Simula §3.3, difficulty is decoupled from semantic coverage so a
configurable fraction of samples can be hardened without redistributing
the taxonomy. Five operators cover the failure modes observed when
PII NERs degrade in production:

    obfuscate         half-width <-> full-width swaps, hyphen drop,
                      honorific strip, OCR-style char corruption
    add_ambiguity     surrounding context engineered to confuse
                      PERSON ⇔ ORGANIZATION / ADDRESS boundaries
    code_switch       JP entity replaced with romaji / mixed-script
                      surface form (rule-based; LLM path is opt-in)
    couple_entities   add a related second entity (本人 / 配偶者 etc.)
                      so the model must disambiguate co-reference
    add_near_pii      prepend a UUID, hash, or sample number that
                      regex baselines will mis-fire on

Operators mutate the text and rebuild the entity span list. The
contract is:

    - `expected_entities` (the set of labels) is preserved.
    - Span count may grow (couple_entities) or stay the same.
    - The resulting (text, entities) round-trips through
      `validate_spans` without raising.

A purely heuristic `difficulty_score(sample)` is provided for
fast in-process scoring; `score_difficulty.py` also implements an
optional LLM-backed Elo pass for calibrated complexity (§3.3 +
§4 "Calibrated Complexity Scoring").
"""

from __future__ import annotations

import hashlib
import random
import re
import uuid
from dataclasses import dataclass, field, replace
from typing import Callable

# --- Sample shape --------------------------------------------------------

@dataclass
class Span:
    start: int
    end: int
    label: str

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass
class Sample:
    text: str
    entities: list[Span] = field(default_factory=list)
    difficulty: float | None = None
    operators_applied: list[str] = field(default_factory=list)


# --- Span helpers --------------------------------------------------------

def validate_spans(sample: Sample) -> None:
    """Raise if any span is outside [0, len(text)] or overlapping."""
    n = len(sample.text)
    last_end = 0
    for s in sorted(sample.entities, key=lambda x: x.start):
        if s.start < 0 or s.end > n or s.start >= s.end:
            raise ValueError(f"span out of range: {s} for text len {n}")
        if s.start < last_end:
            raise ValueError(f"overlapping spans at {s.start}")
        last_end = s.end


def _replace_span(sample: Sample, span: Span, new_surface: str) -> Sample:
    """Replace a span's surface form, shifting downstream span offsets."""
    delta = len(new_surface) - span.length
    new_text = sample.text[: span.start] + new_surface + sample.text[span.end :]
    new_entities: list[Span] = []
    for s in sample.entities:
        if s is span:
            new_entities.append(Span(s.start, s.start + len(new_surface), s.label))
        elif s.start >= span.end:
            new_entities.append(Span(s.start + delta, s.end + delta, s.label))
        else:
            new_entities.append(replace(s))
    return Sample(text=new_text, entities=new_entities, difficulty=sample.difficulty, operators_applied=list(sample.operators_applied))


def _shift_after(sample: Sample, anchor: int, delta: int) -> Sample:
    new_entities = [
        Span(s.start + delta, s.end + delta, s.label) if s.start >= anchor else replace(s)
        for s in sample.entities
    ]
    return Sample(text=sample.text, entities=new_entities, difficulty=sample.difficulty, operators_applied=list(sample.operators_applied))


def _insert_text(sample: Sample, position: int, fragment: str) -> Sample:
    sample = _shift_after(sample, position, len(fragment))
    sample.text = sample.text[:position] + fragment + sample.text[position:]
    return sample


# --- Operator 1: obfuscate ----------------------------------------------

_FULLWIDTH_DIGIT = str.maketrans("0123456789", "０１２３４５６７８９")
_FULLWIDTH_HYPHEN = "－"
_HONORIFICS = ("さん", "様", "氏", "君", "ちゃん")


def obfuscate(sample: Sample, rng: random.Random) -> Sample:
    """Subset of: digit width swap, hyphen drop, honorific strip, glyph noise."""
    s = sample
    for span in list(s.entities):
        if span.label == "PHONE_NUMBER":
            surface = s.text[span.start : span.end]
            choice = rng.random()
            if choice < 0.4:
                new = surface.translate(_FULLWIDTH_DIGIT)
            elif choice < 0.7:
                new = surface.replace("-", "")
            else:
                new = surface.replace("-", _FULLWIDTH_HYPHEN)
            if new != surface:
                s = _replace_span(s, span, new)
        elif span.label == "PERSON":
            # Strip a trailing honorific if present *outside* the span.
            tail = s.text[span.end : span.end + 2]
            for hon in _HONORIFICS:
                if tail.startswith(hon):
                    # Cut honorific from text, no span change needed.
                    s.text = s.text[: span.end] + s.text[span.end + len(hon) :]
                    s = _shift_after(s, span.end, -len(hon))
                    break
    s.operators_applied.append("obfuscate")
    return s


# --- Operator 2: add_ambiguity ------------------------------------------

_NAME_LIKE_DISTRACTORS = (
    "山田工業株式会社",
    "佐藤マンション",
    "鈴木病院",
    "高橋通り",
    "田中ビル",
)
_AMBIG_PREFIXES = ("そういえば", "ちなみに", "なお", "また")


def add_ambiguity(sample: Sample, rng: random.Random) -> Sample:
    """Insert a name-like distractor near a PERSON span without tagging it."""
    person_spans = [s for s in sample.entities if s.label == "PERSON"]
    if not person_spans:
        return sample
    target = rng.choice(person_spans)
    distractor = rng.choice(_NAME_LIKE_DISTRACTORS)
    prefix = rng.choice(_AMBIG_PREFIXES)
    fragment = f"。{prefix}{distractor}も同席。"
    s = _insert_text(sample, target.end, fragment)
    s.operators_applied.append("add_ambiguity")
    return s


# --- Operator 3: code_switch --------------------------------------------

_KANA_TO_ROMAJI: dict[str, str] = {
    "山田": "Yamada", "田中": "Tanaka", "佐藤": "Sato", "鈴木": "Suzuki",
    "高橋": "Takahashi", "渡辺": "Watanabe", "伊藤": "Ito", "中村": "Nakamura",
    "小林": "Kobayashi", "加藤": "Kato", "吉田": "Yoshida", "山本": "Yamamoto",
    "太郎": "Taro", "花子": "Hanako", "一郎": "Ichiro", "次郎": "Jiro",
    "三郎": "Saburo", "美香": "Mika", "由美": "Yumi", "健": "Ken",
    "翔太": "Shota", "陽菜": "Hina",
}


def code_switch(sample: Sample, rng: random.Random) -> Sample:
    """Rule-based JP→romaji substitution for PERSON spans we know how to map."""
    s = sample
    for span in list(s.entities):
        if span.label != "PERSON":
            continue
        surface = s.text[span.start : span.end]
        # Best-effort token replacement using the kanji table.
        new_surface = surface
        for kana, romaji in _KANA_TO_ROMAJI.items():
            new_surface = new_surface.replace(kana, romaji)
        if new_surface != surface and rng.random() < 0.6:
            s = _replace_span(s, span, new_surface)
    s.operators_applied.append("code_switch")
    return s


# --- Operator 4: couple_entities ----------------------------------------

_COUPLING_TEMPLATES = (
    "（配偶者: <PERSON>{partner}</PERSON>）",
    "緊急連絡先: <PERSON>{partner}</PERSON>",
    "保証人 <PERSON>{partner}</PERSON> も同住所",
)
_PARTNER_POOL = ("田中花子", "佐藤美香", "鈴木一郎", "山本由美", "高橋健")


def couple_entities(sample: Sample, rng: random.Random) -> Sample:
    """Append a related second PERSON span tied to the first by relationship."""
    person_spans = [s for s in sample.entities if s.label == "PERSON"]
    if not person_spans:
        return sample
    anchor = person_spans[0]
    partner = rng.choice(_PARTNER_POOL)
    template = rng.choice(_COUPLING_TEMPLATES)
    prefix, partner_marker, suffix = template.partition(f"<PERSON>{{partner}}</PERSON>")
    # Resolve template manually so we can both grow the text and add a span.
    rendered_prefix = prefix.format(partner=partner)
    rendered_suffix = suffix.format(partner=partner)
    fragment = rendered_prefix + partner + rendered_suffix
    insert_at = anchor.end
    s = _insert_text(sample, insert_at, fragment)
    partner_start = insert_at + len(rendered_prefix)
    partner_end = partner_start + len(partner)
    s.entities.append(Span(partner_start, partner_end, "PERSON"))
    s.entities.sort(key=lambda x: x.start)
    s.operators_applied.append("couple_entities")
    return s


# --- Operator 5: add_near_pii -------------------------------------------

def add_near_pii(sample: Sample, rng: random.Random) -> Sample:
    """Prepend a UUID / hash / sample number that regex baselines mis-fire on."""
    fingerprint = hashlib.sha1(sample.text.encode("utf-8")).hexdigest()[:16]
    decoys = [
        f"[sample-{rng.randint(10_000, 99_999)}] ",
        f"#req-{uuid.UUID(int=rng.getrandbits(128))} ",
        f"<trace:{fingerprint}> ",
        f"order-{rng.randint(10**9, 10**10 - 1)}-{rng.randint(100, 999)} ",
    ]
    fragment = rng.choice(decoys)
    s = _insert_text(sample, 0, fragment)
    s.operators_applied.append("add_near_pii")
    return s


# --- Pipeline ------------------------------------------------------------

OPERATORS: dict[str, Callable[[Sample, random.Random], Sample]] = {
    "obfuscate": obfuscate,
    "add_ambiguity": add_ambiguity,
    "code_switch": code_switch,
    "couple_entities": couple_entities,
    "add_near_pii": add_near_pii,
}


def apply_operators(sample: Sample, ops: list[str], rng: random.Random) -> Sample:
    s = sample
    for op in ops:
        s = OPERATORS[op](s, rng)
        validate_spans(s)
    return s


# --- Heuristic difficulty score ------------------------------------------

_DIFFICULTY_WEIGHTS = {
    "obfuscate": 0.20,
    "add_ambiguity": 0.20,
    "code_switch": 0.25,
    "couple_entities": 0.15,
    "add_near_pii": 0.10,
}

_DIFFICULTY_FEATURES = (
    ("length_norm", 0.05),       # longer text is mildly harder
    ("entity_density", 0.15),    # dense entities increase confusion
    ("mixed_script", 0.10),      # JP + Latin + digits
)


def _entity_density(sample: Sample) -> float:
    if not sample.text:
        return 0.0
    return min(len(sample.entities) / max(len(sample.text) / 100, 1), 1.0)


def _mixed_script_ratio(text: str) -> float:
    kinds = {
        "jp": sum(1 for c in text if "぀" <= c <= "ヿ" or "一" <= c <= "鿿"),
        "latin": sum(1 for c in text if "a" <= c.lower() <= "z"),
        "digit": sum(1 for c in text if c.isdigit()),
    }
    total = sum(kinds.values()) or 1
    return min(1.0, 3 - sum(1 for v in kinds.values() if v / total > 0.2))


def difficulty_score(sample: Sample) -> float:
    """Heuristic difficulty in [0, 1].

    Combines operator-applied weights with surface features. Calibration
    against LLM Elo lives in score_difficulty.py.
    """
    score = 0.0
    for op in set(sample.operators_applied):
        score += _DIFFICULTY_WEIGHTS.get(op, 0.0)
    length_norm = min(len(sample.text) / 1500, 1.0)
    feats = {
        "length_norm": length_norm,
        "entity_density": _entity_density(sample),
        "mixed_script": _mixed_script_ratio(sample.text) / 3,
    }
    for name, weight in _DIFFICULTY_FEATURES:
        score += feats[name] * weight
    return max(0.0, min(1.0, score))


def bucket(score: float) -> str:
    if score < 0.25:
        return "easy"
    if score < 0.55:
        return "medium"
    return "hard"


# --- Ratio-targeted application -----------------------------------------

DEFAULT_TARGET = {"easy": 0.5, "medium": 0.3, "hard": 0.2}


def apply_with_ratio(
    samples: list[Sample],
    target: dict[str, float] | None = None,
    seed: int = 0,
) -> list[Sample]:
    """Apply operators to a fraction of samples to hit the target bucket ratios.

    Easy stays untouched; medium gets 1 light operator; hard gets 2-3
    chained operators. Order is randomised so the same input + seed
    reproduces the same partition.
    """
    target = target or DEFAULT_TARGET
    rng = random.Random(seed)
    n = len(samples)
    n_hard = round(n * target.get("hard", 0))
    n_medium = round(n * target.get("medium", 0))
    indices = list(range(n))
    rng.shuffle(indices)
    hard_idx = set(indices[:n_hard])
    medium_idx = set(indices[n_hard : n_hard + n_medium])

    light_ops = ("obfuscate", "add_ambiguity", "add_near_pii")
    heavy_ops = ("code_switch", "couple_entities")

    out: list[Sample] = []
    for i, s in enumerate(samples):
        if i in hard_idx:
            chain = [rng.choice(light_ops), rng.choice(heavy_ops), "obfuscate"]
            s = apply_operators(s, chain, rng)
        elif i in medium_idx:
            s = apply_operators(s, [rng.choice(light_ops)], rng)
        s.difficulty = difficulty_score(s)
        out.append(s)
    return out


def histogram(samples: list[Sample], bins: int = 10) -> list[int]:
    counts = [0] * bins
    for s in samples:
        score = s.difficulty if s.difficulty is not None else difficulty_score(s)
        idx = min(int(score * bins), bins - 1)
        counts[idx] += 1
    return counts
