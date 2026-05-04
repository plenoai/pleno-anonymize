"""ベンチマーク設定の単一ソース."""

from dataclasses import dataclass, field
from pathlib import Path

PROMPTS_ROOT = Path(__file__).parent / "prompts"
CORPORA_ROOT = Path(__file__).parent / "benchmark_corpora"


@dataclass(frozen=True)
class BenchmarkConfig:
    """バージョン別ベンチマーク設定."""

    version: str
    prompts_subdir: str
    template_weights: dict[str, float] = field(default_factory=dict)
    templates: tuple[str, ...] | None = None
    generation_backend: str = "openai"
    corpus_subdir: str | None = None
    suite_kind: str = "benchmark"
    purpose: str = ""


BENCHMARK_CONFIGS: dict[str, BenchmarkConfig] = {
    "v0.2.0": BenchmarkConfig(
        version="v0.2.0",
        prompts_subdir="benchmark_v02",
        template_weights={
            "negative_only.j2": 6.0,
            "distractor_heavy.j2": 2.5,
            "narrative_embedded.j2": 2.0,
            "mixed_language.j2": 1.5,
        },
    ),
    "v0.3.0": BenchmarkConfig(
        version="v0.3.0",
        prompts_subdir="benchmark_v03",
        template_weights={
            "adversarial_negative.j2": 4.0,
            "type_confusion.j2": 2.0,
            "boundary_ambiguity.j2": 2.0,
            "corrupted_structured.j2": 1.5,
            "cross_sentence.j2": 1.5,
        },
    ),
    "v0.4.0": BenchmarkConfig(
        version="v0.4.0",
        prompts_subdir="benchmark_v04",
        template_weights={
            "adversarial_negative_v2.j2": 4.0,
            "semantic_trap.j2": 2.5,
            "minimal_context.j2": 2.0,
            "redacted_partial.j2": 1.5,
            "boundary_ambiguity.j2": 2.0,
            "type_confusion.j2": 2.0,
            "extreme_format.j2": 1.0,
            "dense_multi_entity.j2": 1.0,
            "corrupted_structured.j2": 1.0,
        },
    ),
    "v0.5.0": BenchmarkConfig(
        version="v0.5.0",
        prompts_subdir="benchmark_v05",
        template_weights={
            "placeholder_mirage.j2": 3.0,
            "fragment_chain.j2": 2.5,
            "schema_bleed.j2": 2.5,
            "minimal_context.j2": 2.0,
            "orthography_shift.j2": 2.0,
            "corrupted_structured.j2": 1.5,
            "semantic_trap.j2": 1.5,
            "geo_org_switchback.j2": 1.5,
        },
        templates=(
            "placeholder_mirage.j2",
            "fragment_chain.j2",
            "schema_bleed.j2",
            "minimal_context.j2",
            "orthography_shift.j2",
            "corrupted_structured.j2",
            "semantic_trap.j2",
            "geo_org_switchback.j2",
        ),
    ),
    "v0.10.0": BenchmarkConfig(
        version="v0.10.0",
        prompts_subdir="benchmark_v10",
        templates=(
            "negative_placeholders.txt",
            "negative_specs.txt",
            "negative_placeholders_part2.txt",
            "negative_specs_part2.txt",
            "collapsed_boundaries_part2.txt",
            "orthography_aliases_part2.txt",
            "dense_exports_part2.txt",
            "geo_org_dense.txt",
            "geo_org_dense_part2.txt",
            "mixed_script_dense.txt",
            "mixed_script_dense_part2.txt",
        ),
        generation_backend="corpus",
        corpus_subdir="benchmark_v10",
        suite_kind="quality_gate",
        purpose="出荷阻止用の curated DLP quality gate",
    ),
    "v0.11.0": BenchmarkConfig(
        version="v0.11.0",
        prompts_subdir="benchmark_v11",
        templates=(
            "ocr_forms_a.txt",
            "payment_exports_a.txt",
            "logistics_labels_b.txt",
            "mixed_dummy_real_a.txt",
            "negative_operational_a.txt",
            "negative_operational_b.txt",
            "negative_operational_c.txt",
            "negative_operational_d.txt",
            "public_information_a.txt",
            "public_information_b.txt",
            "meeting_minutes_a.txt",
            "meeting_minutes_b.txt",
            "product_manuals_a.txt",
            "product_manuals_b.txt",
            "news_features_a.txt",
            "news_features_b.txt",
            "community_bulletins_a.txt",
            "academic_articles_a.txt",
            "travel_guides_a.txt",
            "technical_blogs_a.txt",
            "leaked_attachments_a.txt",
            "partially_redacted_public_a.txt",
            "general_operations_a.txt",
            "general_operations_b.txt",
            "release_notes_a.txt",
            "product_pages_a.txt",
            "forum_threads_a.txt",
            "municipal_guides_a.txt",
            "academic_notices_b.txt",
            "travel_blogs_b.txt",
            "press_releases_a.txt",
            "onboarding_docs_a.txt",
        ),
        generation_backend="corpus",
        corpus_subdir="benchmark_v11",
        suite_kind="benchmark",
        purpose="広い slice を監視する curated DLP benchmark",
    ),
    "v0.12.0": BenchmarkConfig(
        version="v0.12.0",
        prompts_subdir="benchmark_v12",
        templates=(
            "ocr_forms_a.txt",
            "payment_exports_a.txt",
            "logistics_labels_b.txt",
            "mixed_dummy_real_a.txt",
            "negative_operational_a.txt",
            "negative_operational_b.txt",
            "negative_operational_c.txt",
            "negative_operational_d.txt",
            "public_information_a.txt",
            "public_information_b.txt",
            "meeting_minutes_a.txt",
            "meeting_minutes_b.txt",
            "product_manuals_a.txt",
            "product_manuals_b.txt",
            "news_features_a.txt",
            "news_features_b.txt",
            "community_bulletins_a.txt",
            "academic_articles_a.txt",
            "travel_guides_a.txt",
            "technical_blogs_a.txt",
            "leaked_attachments_a.txt",
            "partially_redacted_public_a.txt",
            "general_operations_a.txt",
            "general_operations_b.txt",
            "release_notes_a.txt",
            "product_pages_a.txt",
            "forum_threads_a.txt",
            "municipal_guides_a.txt",
            "academic_notices_b.txt",
            "travel_blogs_b.txt",
            "press_releases_a.txt",
            "onboarding_docs_a.txt",
            "facility_org_false_positive_a.txt",
            "bankish_code_negative_a.txt",
            "catalog_placeholder_negative_a.txt",
            "policy_pages_negative_a.txt",
            "faq_negative_b.txt",
            "event_brochures_negative_a.txt",
            "route_guides_negative_a.txt",
            "ui_mock_negative_a.txt",
            "docs_glossary_negative_a.txt",
        ),
        generation_backend="corpus",
        corpus_subdir="benchmark_v12",
        suite_kind="benchmark",
        purpose="OCR・key-value 崩れと一般文書の偽陽性圧力を強めた curated DLP benchmark",
    ),
    "v0.13.0-held-out": BenchmarkConfig(
        # Held-out test set (test partition) for both ja and en.
        # Wording is paraphrased rather than copied from v11/v12 corpus, so
        # this set stays disjoint from training-time data — the F0a R14
        # leakage check should pass cleanly.
        # No templates / corpus_subdir: this benchmark is consumed via a
        # pre-built raw.json under data/benchmark/v0.13.0-held-out/<lang>/.
        # generate_benchmark cannot rebuild it (no prompts / no curated
        # source files), only evaluate_benchmark + a manual --benchmark-data
        # pointer can use it.
        version="v0.13.0-held-out",
        prompts_subdir="",
        generation_backend="held-out",
        suite_kind="held_out",
        purpose=(
            "Held-out test partition kept disjoint from training data. "
            "Slice mix mirrors v11/v12 (positive: logistics_labels_b, "
            "partially_redacted_public_a, ocr_forms_a; negative: 10 v12 "
            "FP-pressure slices). 80 docs / 89 entities / 50 negatives."
        ),
    ),
}

BENCHMARK_VERSIONS = list(BENCHMARK_CONFIGS)
LATEST_BENCHMARK_VERSION = BENCHMARK_VERSIONS[-1]


def resolve_benchmark_template_paths(
    config: BenchmarkConfig,
    language: str,
) -> list[Path]:
    """使用するテンプレート集合を解決する.

    旧版はディレクトリ走査を維持して互換性を保ち、新版は templates を明示指定する。
    """

    prompts_dir = PROMPTS_ROOT / config.prompts_subdir / language
    if config.templates is None:
        return sorted(prompts_dir.glob("*.j2"))

    missing = [name for name in config.templates if not (prompts_dir / name).exists()]
    if missing:
        missing_names = ", ".join(missing)
        raise FileNotFoundError(f"Missing benchmark templates in {prompts_dir}: {missing_names}")

    return [prompts_dir / name for name in config.templates]


def resolve_benchmark_corpus_paths(
    config: BenchmarkConfig,
    language: str,
) -> list[Path]:
    """固定 corpus のソースファイルを解決する."""
    if config.corpus_subdir is None:
        raise ValueError(f"Benchmark {config.version} does not define corpus_subdir")

    corpus_dir = CORPORA_ROOT / config.corpus_subdir / language
    if config.templates is None:
        return sorted(corpus_dir.glob("*.txt"))

    missing = [name for name in config.templates if not (corpus_dir / name).exists()]
    if missing:
        missing_names = ", ".join(missing)
        raise FileNotFoundError(f"Missing benchmark corpus files in {corpus_dir}: {missing_names}")

    return [corpus_dir / name for name in config.templates]
