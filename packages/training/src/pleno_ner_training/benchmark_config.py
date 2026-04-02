"""ベンチマーク設定の単一ソース."""

from dataclasses import dataclass, field
from pathlib import Path

PROMPTS_ROOT = Path(__file__).parent / "prompts"


@dataclass(frozen=True)
class BenchmarkConfig:
    """バージョン別ベンチマーク設定."""

    version: str
    prompts_subdir: str
    template_weights: dict[str, float] = field(default_factory=dict)
    templates: tuple[str, ...] | None = None


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
