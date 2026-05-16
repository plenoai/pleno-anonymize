"""Local Diversification: meta-prompts per taxonomy leaf.

Each taxonomy leaf is expanded into N distinct meta-prompts. A
meta-prompt is the instruction that will be fed to the generator LLM
later (#152). The variation lives in five orthogonal axes layered on
top of the leaf's intrinsic register / density / document type:

    perspective    self | third_party | neutral
    length_hint    short (<200) | medium (200-700) | long (700-1500)
    opening_cue    mid_thread | header | salutation | abrupt | form_label
    vocabulary     plain | jargon | dialect | mixed_script
    twist          straight | redaction_attempt | partial_ocr | code_switch

Five canonical lens combinations are hard-coded so each leaf yields at
least five meta-prompts deterministically. An optional LLM enrichment
pass can append further variants (additive only).

Output (default): packages/training/data/meta_prompts/jp/all.jsonl,
one JSON line per meta-prompt.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from pleno_ner_training.mechanism.taxonomy import Scenario, Taxonomy

PERSPECTIVES = ("self", "third_party", "neutral")
LENGTH_HINTS = ("short", "medium", "long")
OPENING_CUES = ("mid_thread", "header", "salutation", "abrupt", "form_label")
VOCABULARY = ("plain", "jargon", "dialect", "mixed_script")
TWISTS = ("straight", "redaction_attempt", "partial_ocr", "code_switch")


@dataclass(frozen=True)
class Lens:
    perspective: str
    length_hint: str
    opening_cue: str
    vocabulary: str
    twist: str


# Five canonical lenses span all axes without redundancy.
CANONICAL_LENSES: tuple[Lens, ...] = (
    Lens("self", "short", "mid_thread", "plain", "straight"),
    Lens("third_party", "medium", "header", "jargon", "straight"),
    Lens("neutral", "long", "form_label", "plain", "redaction_attempt"),
    Lens("self", "medium", "salutation", "dialect", "partial_ocr"),
    Lens("third_party", "short", "abrupt", "mixed_script", "code_switch"),
)


@dataclass(frozen=True)
class MetaPrompt:
    id: str
    scenario_id: str
    domain_id: str
    sub_domain_id: str
    register: str
    document_type: str
    entity_density: str
    expected_entities: tuple[str, ...]
    lens: Lens
    instruction: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["expected_entities"] = list(self.expected_entities)
        return d


_LENGTH_DESCRIPTION = {
    "short": "100〜200 文字。1〜3 文の短い断片。",
    "medium": "200〜700 文字。段落 1〜2 個。",
    "long": "700〜1500 文字。複数段落、見出し可。",
}

_OPENING_DESCRIPTION = {
    "mid_thread": "文脈が省略されたスレッド途中から開始。挨拶なし。",
    "header": "上部に件名・日付・宛先などのヘッダーを置く。",
    "salutation": "「お世話になっております」「拝啓」などの定型挨拶で開始。",
    "abrupt": "前置きを完全に省略し、用件のみ書く。",
    "form_label": "「氏名:」「住所:」のようなラベル付きで構造化。",
}

_VOCAB_DESCRIPTION = {
    "plain": "標準的な書き言葉。",
    "jargon": "業界専門用語・略語を多用する。",
    "dialect": "話し手の方言・口語が滲む。",
    "mixed_script": "全角半角、漢字・ひらがな・ローマ字を混在させる。",
}

_TWIST_DESCRIPTION = {
    "straight": "通常通り PII をそのまま書く。",
    "redaction_attempt": "一部 PII は本人が伏字 (○○、X、****) で隠そうとしているが他は露出。",
    "partial_ocr": "OCR の誤読・改行崩れがあり一部の文字が ! ・ ` などに化けている。",
    "code_switch": "日本語と英語/中国語/韓国語が混在し、PII がローマ字表記のこともある。",
}

_PERSPECTIVE_DESCRIPTION = {
    "self": "PII の本人が一人称で書いている。",
    "third_party": "本人ではない第三者 (担当者・家族・記者など) が三人称で書いている。",
    "neutral": "システムが定型出力したかのような無人称の記述。",
}


def render_instruction(scenario: Scenario, register: str, lens: Lens) -> str:
    entities = "、".join(scenario.expected_entities)
    parts = [
        f"あなたは日本語の合成 PII データ生成エージェントです。次の制約を **すべて** 満たす 1 件のサンプルを生成してください。",
        "",
        f"- シナリオ: {scenario.ja_name} ({scenario.id})",
        f"- 文書種別: {scenario.document_type}",
        f"- レジスタ (文体): {register}",
        f"- エンティティ密度: {scenario.entity_density}",
        f"- 出現させる PII エンティティ種別: {entities}",
        "",
        "ローカル多様化レンズ:",
        f"- 視点: {_PERSPECTIVE_DESCRIPTION[lens.perspective]}",
        f"- 分量: {_LENGTH_DESCRIPTION[lens.length_hint]}",
        f"- 冒頭: {_OPENING_DESCRIPTION[lens.opening_cue]}",
        f"- 語彙: {_VOCAB_DESCRIPTION[lens.vocabulary]}",
        f"- ひねり: {_TWIST_DESCRIPTION[lens.twist]}",
        "",
        "出力要件:",
        "- PII 部分はすべて XML タグで囲む。例: <PERSON>山田太郎</PERSON>。",
        "- 使用してよいタグは上記の expected_entities のみ。タグはネストしない。",
        "- 期待エンティティのうち、entity_density に応じて妥当な範囲で **複数回** 出現させる。",
        "- 一般名詞や固有名詞風の単語のうち PII でないものはタグで囲まない。",
        "- JSON や前置きは出力しない。本文のみ。",
    ]
    return "\n".join(parts)


def build_meta_prompts(taxonomy: Taxonomy, lenses: tuple[Lens, ...] = CANONICAL_LENSES) -> list[MetaPrompt]:
    out: list[MetaPrompt] = []
    # Index leaves by their domain / sub-domain for output metadata.
    for domain in taxonomy.domains:
        for sub_domain in domain.sub_domains:
            for scen in sub_domain.scenarios:
                # Cycle through the leaf's registers so all are exercised
                # across the N lenses; if there are fewer registers than
                # lenses, registers wrap.
                for i, lens in enumerate(lenses):
                    register = scen.registers[i % len(scen.registers)]
                    out.append(
                        MetaPrompt(
                            id=f"{scen.id}#{i:02d}",
                            scenario_id=scen.id,
                            domain_id=domain.id,
                            sub_domain_id=sub_domain.id,
                            register=register,
                            document_type=scen.document_type,
                            entity_density=scen.entity_density,
                            expected_entities=scen.expected_entities,
                            lens=lens,
                            instruction=render_instruction(scen, register, lens),
                        )
                    )
    return out


def save_jsonl(prompts: Iterable[MetaPrompt], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8") as f:
        for mp in prompts:
            f.write(json.dumps(mp.to_dict(), ensure_ascii=False) + "\n")
            n += 1
    return n


def _fingerprint(mp: MetaPrompt) -> tuple:
    """Identity tuple that captures everything that would change a sample.

    The instruction body shares boilerplate across prompts by design — a
    char-n-gram Jaccard on the whole instruction would conflate template
    skeleton with content. The fingerprint is the cartesian point in
    (scenario × register × lens) space, plus the expected-entity set
    that is allowed to vary if a future enrichment pass restricts it.
    """
    return (
        mp.scenario_id,
        mp.register,
        mp.lens.perspective,
        mp.lens.length_hint,
        mp.lens.opening_cue,
        mp.lens.vocabulary,
        mp.lens.twist,
        tuple(sorted(mp.expected_entities)),
    )


def estimate_dup_rate(prompts: list[MetaPrompt]) -> float:
    """Fraction of prompts whose fingerprint already appears earlier in the list."""
    if len(prompts) < 2:
        return 0.0
    seen: set[tuple] = set()
    dup = 0
    for mp in prompts:
        fp = _fingerprint(mp)
        if fp in seen:
            dup += 1
        else:
            seen.add(fp)
    return dup / len(prompts)
