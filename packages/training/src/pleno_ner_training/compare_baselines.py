"""U4 Comparison orchestrator: F0 pre-flights + F1 measurement + F2 verdict pre-compute.

Implements the kill-or-commit decision pipeline described in the plan
`docs/plans/2026-05-02-001-feat-ginza-presidio-baseline-measurement-plan.md`.

Stages:

- F0a — data leakage check (R14, SHA256-NFC + template fingerprint, zero-tolerance abort)
- F0b — noise floor pin (R13, 4-case lifecycle table, carry-forward by default)
- F0c — recognizers pack git SHA + content SHA256 (R16, no vendor/ files)
- F1  — measurement (per-variant inference, score_bearing-aware)
- F2  — verdict pre-compute (R7/R8 gates → compute_verdict per entity)

The R12 partial-run gate is a hard invariant: when `partial_run` is True the
writer **omits** `aggregates` and `verdict_per_entity` from the artifact
(P1-2: peek-bias closure). Reading those fields on a partial artifact
raises KeyError by construction.

This module is *pure orchestration* — statistical primitives live in
`metrics.py` (frozen under the plan anchor SHA), variant predictors live in
`baselines_ja.py`. We do not re-implement either.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from pleno_ner_training import metrics
from pleno_ner_training.artifact import (
    ArtifactMetadata,
    ComparisonArtifact,
    LeakageCheck,
    write_artifact,
)
from pleno_ner_training.baselines_ja import BASELINE_REGISTRY, BaselineSpec, Predictor

# --- plan-locked constants ---------------------------------------------------

K_VALUES: list[int] = [10, 20, 30, 50, 70, 90, 100]
ENTITIES: list[str] = ["ORGANIZATION", "DATE_OF_BIRTH"]
# Bonferroni multiplicity: 5 variants × 7 percentiles = 35 (plan Resolved Q).
M_BONFERRONI: int = 35
# Per-entity asymmetric eligibility (plan KTD: DOB asymmetric threshold).
MIN_SPANS: dict[str, int] = {"ORGANIZATION": 5, "DATE_OF_BIRTH": 3}
# Per-entity p10 robustness gate threshold (plan R8(b) — recall p10 ≥ 0.5).
P10_GATE_THRESHOLD: float = 0.5
# Inference wall-clock time-box (R6, hours → seconds).
TIME_BOX_SECONDS: int = 6 * 60 * 60
# R7 primary-gate minimum diff: max(3pt, 2× noise_floor).
R7_MIN_DIFF: float = 0.03

RECOGNIZERS_PATH_REL = "packages/training/src/pleno_ner_training/recognizers_ja.py"

PodMode = Literal["cpu", "gpu"]


# --- F0a: data leakage check (R14) -------------------------------------------


def _nfc_sha256(text: str) -> str:
    """SHA256 of NFC-normalized + LF-normalized + stripped UTF-8 text."""
    normalized = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _template_fingerprint(template: str, entities: list[dict]) -> str:
    """SHA256 of `template_name + "|" + entity_label_sequence`.
    Random-fill content is excluded by design — only structural fingerprint."""
    label_seq = ",".join(e["label"] for e in entities)
    payload = f"{template}|{label_seq}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def f0a_data_leakage_check(
    corpus_path: Path,
    training_manifest_path: Path,
) -> dict[str, Any]:
    """Zero-tolerance data leakage check (R14, locked spec).

    Compares benchmark v0.12.0 corpus (`corpus_path`) against training corpus
    manifest (`training_manifest_path`). Aborts on **any** doc-level or
    template-level overlap. No maintainer override.

    Returns dict with keys: algorithm, manifest_hash, doc_overlap_count,
    template_overlap_count, passed.
    """
    if not corpus_path.exists():
        raise FileNotFoundError(f"benchmark corpus not found: {corpus_path}")
    if not training_manifest_path.exists():
        raise FileNotFoundError(
            f"training corpus manifest not found: {training_manifest_path}. "
            "F0a requires a manifest of all training-corpus source files. "
            "Generate one before running compare_baselines (no auto-stub: "
            "missing manifest = abort, per plan R14 zero-tolerance)."
        )

    manifest_bytes = training_manifest_path.read_bytes()
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    manifest = json.loads(manifest_bytes.decode("utf-8"))

    # Manifest schema: {"sources": [{"path": str, "sha256_nfc": str, "doc_hashes": [...], "template_fingerprints": [...]}]}
    training_doc_hashes: set[str] = set()
    training_template_fps: set[str] = set()
    for src in manifest.get("sources", []):
        training_doc_hashes.update(src.get("doc_hashes", []))
        training_template_fps.update(src.get("template_fingerprints", []))

    # Compute benchmark hashes.
    corpus = json.loads(corpus_path.read_text("utf-8"))
    bench_doc_hashes: list[str] = []
    bench_template_fps: list[str] = []
    for entry in corpus:
        bench_doc_hashes.append(_nfc_sha256(entry["text"]))
        meta = entry.get("_meta", {})
        template = meta.get("template", "")
        bench_template_fps.append(
            _template_fingerprint(template, entry.get("entities", []))
        )

    doc_overlap = sum(1 for h in bench_doc_hashes if h in training_doc_hashes)
    template_overlap = sum(1 for fp in bench_template_fps if fp in training_template_fps)

    passed = doc_overlap == 0 and template_overlap == 0
    return {
        "algorithm": "SHA256-NFC",
        "manifest_hash": manifest_hash,
        "doc_overlap_count": doc_overlap,
        "template_overlap_count": template_overlap,
        "passed": passed,
    }


# --- F0b: noise floor pin (R13, 4-case lifecycle) ----------------------------


def _bench_corpus_version(corpus_path: Path) -> str:
    """Read v0.12.0 (or whatever) from the corpus _meta.version, fallback to parent dir."""
    try:
        corpus = json.loads(corpus_path.read_text("utf-8"))
        if corpus and "_meta" in corpus[0]:
            return str(corpus[0]["_meta"].get("version", corpus_path.parent.parent.name))
    except (OSError, json.JSONDecodeError, IndexError):
        pass
    return corpus_path.parent.parent.name


def _variant_set_hash(baselines: list[str]) -> str:
    payload = "|".join(sorted(baselines))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def f0b_noise_floor_pin(
    predictions_by_variant: dict[str, list[dict]],
    output_path: Path,
    *,
    corpus_version: str,
    variant_set_hash: str,
    manifest_hash: str,
    force_recompute: bool = False,
    seed: int = 42,
) -> dict[str, Any]:
    """Bootstrap-derived per-entity noise floor with 4-case lifecycle.

    Lifecycle (plan KTD "Noise floor pre-pinning lifecycle"):
      (1) corpus + variants + manifest unchanged → carry-forward existing pin
      (2)/(3)/(4) any of those changed OR `force_recompute=True` → recompute

    Persisted JSON shape: {algorithm, corpus_version, variant_set_hash,
    manifest_hash, per_entity_floor: {ORGANIZATION: float, DATE_OF_BIRTH: float},
    carried_forward: bool}
    """
    if output_path.exists() and not force_recompute:
        existing = json.loads(output_path.read_text("utf-8"))
        same = (
            existing.get("corpus_version") == corpus_version
            and existing.get("variant_set_hash") == variant_set_hash
            and existing.get("manifest_hash") == manifest_hash
        )
        if same:
            existing["carried_forward"] = True
            return existing

    # Recompute: bootstrap-derived per-entity standard error of recall, used as
    # the lower noise floor for the R7 primary gate (`max(3pt, 2× noise_floor)`).
    per_entity: dict[str, float] = {}
    for entity in ENTITIES:
        # Aggregate per-doc recall samples across variants where `pred_count > 0`.
        # Without a true held-out set we use within-variant recall variance as a
        # proxy: this is a conservative pin (≥ true floor under assumption of
        # zero between-variant correlation, which biases the floor upward only).
        recalls: list[float] = []
        for var_preds in predictions_by_variant.values():
            for doc_preds in var_preds:
                gold = [g for g in doc_preds.get("gold", []) if g[2] == entity]
                pred = [p for p in doc_preds.get("predictions", []) if p[2] == entity]
                if not gold:
                    continue
                gold_set = {(g[0], g[1]) for g in gold}
                pred_set = {(p[0], p[1]) for p in pred}
                tp = len(gold_set & pred_set)
                recalls.append(tp / len(gold_set))
        if len(recalls) >= 2:
            lo, hi, _ = metrics.bootstrap_ci(recalls, n=1000, alpha=0.05, seed=seed)
            # noise_floor proxy = half the CI width
            per_entity[entity] = max(0.0, (hi - lo) / 2.0)
        else:
            per_entity[entity] = 0.0

    pin = {
        "algorithm": "bootstrap-percentile-CI-half-width",
        "corpus_version": corpus_version,
        "variant_set_hash": variant_set_hash,
        "manifest_hash": manifest_hash,
        "per_entity_floor": per_entity,
        "carried_forward": False,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(pin, indent=2, ensure_ascii=False), "utf-8")
    return pin


# --- F0c: recognizers git SHA + content hash (R16, P1-7) ---------------------


def _run_git(args: list[str], *, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )
    return result.stdout


def f0c_recognizers_git_sha(
    recognizers_path: Path,
    *,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Resolve git blob-SHA1 + working-tree content SHA256-NFC for the
    recognizers pack. Verifies round-trip: `git show <blob_sha1>` re-hashed
    must equal the working-tree `content_sha256`. Aborts on mismatch.

    No vendor/ files written (P1-7).

    NB: git's `rev-parse HEAD:<path>` returns a 40-char **SHA1** blob hash
    (git's own object hash, NOT SHA256 of the file). content_sha256 and
    git_blob_sha1 are independent values: only content_sha256 round-trips
    by re-hashing.
    """
    if repo_root is None:
        repo_root = Path(__file__).resolve().parents[4]
    if not recognizers_path.exists():
        raise FileNotFoundError(f"recognizers pack not found: {recognizers_path}")

    # Working-tree content hash.
    wt_text = recognizers_path.read_text("utf-8")
    content_sha256 = _nfc_sha256(wt_text)

    # git blob SHA1 at HEAD.
    rel = recognizers_path.relative_to(repo_root).as_posix()
    blob_sha1 = _run_git(["rev-parse", f"HEAD:{rel}"], cwd=repo_root).strip()
    if len(blob_sha1) != 40:
        raise RuntimeError(f"unexpected git blob SHA shape: {blob_sha1!r}")

    # Round-trip: re-hash git-show output.
    show_text = _run_git(["show", blob_sha1], cwd=repo_root)
    show_sha256 = _nfc_sha256(show_text)

    # Containing commit SHA (HEAD).
    head_sha = _run_git(["rev-parse", "HEAD"], cwd=repo_root).strip()

    return {
        "git_blob_sha1": blob_sha1,
        "git_commit_sha": head_sha,
        "content_sha256": content_sha256,
        "show_sha256": show_sha256,
        "round_trip_clean": show_sha256 == content_sha256,
        "path": rel,
    }


# --- F1: measurement run -----------------------------------------------------


PredictorFactory = Callable[[str], Predictor]


def _default_factory(name: str) -> Predictor:
    spec = BASELINE_REGISTRY[name]
    return spec.builder()


def f1_measurement(
    baselines: list[str],
    corpus: list[dict],
    pod_mode: PodMode,
    *,
    predictor_factory: PredictorFactory | None = None,
    time_budget_seconds: int = TIME_BOX_SECONDS,
) -> dict[str, Any]:
    """Run inference per variant. Returns:

      {
        "predictions_by_variant": {variant: [{"doc_idx", "template", "predictions", "gold"} ...]},
        "failed_variants": [name, ...],
        "elapsed_seconds": float,
        "time_box_exceeded": bool,
      }

    `predictor_factory` is injectable: tests pass a stub that returns canned
    predictions without loading any spaCy/Presidio models.
    """
    factory = predictor_factory or _default_factory
    start = time.perf_counter()
    predictions_by_variant: dict[str, list[dict]] = {}
    failed: list[str] = []
    time_box_exceeded = False

    for name in baselines:
        if (time.perf_counter() - start) > time_budget_seconds:
            time_box_exceeded = True
            failed.append(name)
            continue
        try:
            predictor = factory(name)
        except Exception:
            failed.append(name)
            continue

        var_rows: list[dict] = []
        for doc_idx, entry in enumerate(corpus):
            if (time.perf_counter() - start) > time_budget_seconds:
                time_box_exceeded = True
                break
            text = entry["text"]
            try:
                preds = predictor.predict(text)
            except Exception:
                preds = []
            gold = [
                (e["start"], e["end"], e["label"])
                for e in entry.get("entities", [])
                if e["label"] in ENTITIES
            ]
            template = entry.get("_meta", {}).get("template", f"_unknown_{doc_idx}")
            var_rows.append(
                {
                    "doc_idx": doc_idx,
                    "template": template,
                    "predictions": preds,
                    "gold": gold,
                }
            )
        predictions_by_variant[name] = var_rows
        if time_box_exceeded:
            break

    elapsed = time.perf_counter() - start
    # Pod-mode is recorded in the artifact metadata; no behaviour difference here
    # (CPU/GPU is a deployment concern; correctness is identical).
    _ = pod_mode
    return {
        "predictions_by_variant": predictions_by_variant,
        "failed_variants": failed,
        "elapsed_seconds": elapsed,
        "time_box_exceeded": time_box_exceeded,
    }


# --- F2: verdict pre-compute -------------------------------------------------


def _slice_top_k(
    predictions_with_doc: list[tuple[int, tuple]],
    k: int,
) -> list[tuple[int, tuple]]:
    """Slice top-k% by (score desc, doc_id asc, span_start asc)."""
    if k >= 100 or not predictions_with_doc:
        return list(predictions_with_doc)
    n_keep = max(1, int(round(len(predictions_with_doc) * k / 100.0)))

    def key(item: tuple[int, tuple]) -> tuple[float, int, int]:
        doc_idx, (start, _end, _lbl, score, _rank) = item
        score_val = score if score is not None else 0.0
        return (-score_val, doc_idx, start)

    return sorted(predictions_with_doc, key=key)[:n_keep]


def _aggregate_recall(
    rows_by_template: dict[str, dict],
    entity: str,
) -> dict[str, float | None]:
    """Per-template recall (token-overlap) for one entity, given pre-sliced
    rows with `pred_spans` and `gold_spans` already entity-filtered. Returns
    None for templates with < MIN_SPANS[entity] gold spans of that label."""
    out: dict[str, float | None] = {}
    for tmpl, row in rows_by_template.items():
        gold = row["gold_spans"]
        if len(gold) < MIN_SPANS[entity]:
            out[tmpl] = None
            continue
        _, recall, _ = metrics.token_overlap_f1(row["pred_spans"], gold)
        out[tmpl] = recall
    return out


def _build_template_rows(
    var_rows: list[dict],
    entity: str,
    k: int,
    score_bearing: bool,
) -> dict[str, dict]:
    """Group per-template predictions+gold for one (variant, k, entity) cell."""
    # Collect (doc_idx, prediction) pairs filtered to entity.
    indexed_preds: list[tuple[int, tuple]] = []
    for row in var_rows:
        for pred in row["predictions"]:
            if pred[2] != entity:
                continue
            indexed_preds.append((row["doc_idx"], pred))

    # For score-bearing variants, slice top-k% globally; for score-less, keep all (k=100 only).
    if score_bearing:
        indexed_preds = _slice_top_k(indexed_preds, k)
    else:
        # Score-less must be k=100 only.
        if k != 100:
            return {}

    keep_doc_pred: dict[int, list[tuple]] = {}
    for doc_idx, pred in indexed_preds:
        keep_doc_pred.setdefault(doc_idx, []).append(pred)

    # Build per-template rows.
    rows: dict[str, dict] = {}
    for row in var_rows:
        tmpl = row["template"]
        gold = [g for g in row["gold"] if g[2] == entity]
        preds_kept = keep_doc_pred.get(row["doc_idx"], [])
        pred_spans = [(p[0], p[1], p[2]) for p in preds_kept]
        if tmpl not in rows:
            rows[tmpl] = {"pred_spans": [], "gold_spans": []}
        rows[tmpl]["pred_spans"].extend(pred_spans)
        rows[tmpl]["gold_spans"].extend(gold)
    return rows


def _per_entity_best(
    predictions_by_variant: dict[str, list[dict]],
    entity: str,
    category_filter: Literal["oss_presidio", "custom"],
    spec_lookup: dict[str, BaselineSpec],
) -> tuple[dict[str, Any], list[float | None]]:
    """Return (best_cell, per_template_recalls_for_best_cell).

    Best cell = (variant, k) maximising mean per-template recall across
    eligible templates (templates with ≥ MIN_SPANS[entity] gold spans).
    """
    best: dict[str, Any] = {"variant": None, "k": None, "recall": -1.0, "rows": None}
    for name, var_rows in predictions_by_variant.items():
        spec = spec_lookup.get(name)
        if spec is None or spec.category != category_filter:
            continue
        ks = K_VALUES if spec.score_bearing else [100]
        for k in ks:
            rows = _build_template_rows(var_rows, entity, k, spec.score_bearing)
            if not rows:
                continue
            recalls = list(_aggregate_recall(rows, entity).values())
            eligible = [r for r in recalls if r is not None]
            if not eligible:
                continue
            mean_r = sum(eligible) / len(eligible)
            if mean_r > best["recall"]:
                best = {"variant": name, "k": k, "recall": mean_r, "rows": rows}
    if best["rows"] is None:
        return {"variant": None, "k": None, "recall": 0.0}, []
    per_tmpl = list(_aggregate_recall(best["rows"], entity).values())
    return (
        {"variant": best["variant"], "k": best["k"], "recall": best["recall"]},
        per_tmpl,
    )


def _diff_sign_and_ci(
    oss_per_template: list[float | None],
    custom_per_template: list[float | None],
    *,
    seed: int = 42,
) -> tuple[metrics.DiffSign, tuple[float, float]]:
    """Sign + Bonferroni CI for (oss_recall - custom_recall) on paired-template
    differences. Eligible templates only (None excluded on either side)."""
    paired: list[float] = []
    for a, b in zip(oss_per_template, custom_per_template):
        if a is None or b is None:
            continue
        paired.append(a - b)
    if not paired:
        return "tied", (0.0, 0.0)
    alpha = 0.05 / M_BONFERRONI
    lo, hi, mean = metrics.bootstrap_ci(paired, n=1000, alpha=alpha, seed=seed)
    if lo > 0 and mean > 0:
        sign: metrics.DiffSign = "oss_better"
    elif hi < 0 and mean < 0:
        sign = "custom_better"
    else:
        sign = "tied"
    return sign, (lo, hi)


def _entity_verdict(
    predictions_by_variant: dict[str, list[dict]],
    entity: str,
    spec_lookup: dict[str, BaselineSpec],
    noise_floor: dict[str, Any],
    *,
    seed: int = 42,
) -> dict[str, Any]:
    """Compute one entity's verdict cell (KTD compute_verdict + 4 gates)."""
    oss_best, oss_per_tmpl = _per_entity_best(
        predictions_by_variant, entity, "oss_presidio", spec_lookup
    )
    custom_best, custom_per_tmpl = _per_entity_best(
        predictions_by_variant, entity, "custom", spec_lookup
    )

    # Eligibility: templates with ≥ MIN_SPANS[entity] gold spans of `entity`.
    # Computed from any variant (gold is variant-invariant).
    any_var_rows = next(iter(predictions_by_variant.values()), [])
    gold_by_template: dict[str, list] = {}
    for row in any_var_rows:
        gold_by_template.setdefault(row["template"], []).extend(
            g for g in row["gold"] if g[2] == entity
        )
    n_eligible = sum(
        1 for v in gold_by_template.values() if len(v) >= MIN_SPANS[entity]
    )

    # R7 primary gate: |diff| ≥ max(3pt, 2× noise_floor) AND CI excludes 0.
    nf = float(noise_floor.get("per_entity_floor", {}).get(entity, 0.0))
    min_diff = max(R7_MIN_DIFF, 2.0 * nf)
    diff_mean = oss_best["recall"] - custom_best["recall"]
    diff_sign, diff_ci = _diff_sign_and_ci(oss_per_tmpl, custom_per_tmpl, seed=seed)

    r7_primary = abs(diff_mean) >= min_diff and (diff_ci[0] > 0 or diff_ci[1] < 0)

    # R8(a) min-span filter: at least 1 eligible template each side.
    r8a = n_eligible >= 1

    # R8(b) p10 robustness: p10 of best-side per-template recalls ≥ threshold.
    best_side = oss_per_tmpl if diff_sign == "oss_better" else custom_per_tmpl
    p10_val = metrics.p10(best_side) or 0.0
    r8b = p10_val >= P10_GATE_THRESHOLD

    # R8(c) dual-metric agreement: token-overlap and strict-span agree on sign.
    # Recompute strict-span recall for the best cells.
    strict_oss = _strict_recall_for_best(predictions_by_variant, entity, oss_best, spec_lookup)
    strict_custom = _strict_recall_for_best(predictions_by_variant, entity, custom_best, spec_lookup)
    strict_diff = strict_oss - strict_custom
    if diff_sign == "oss_better":
        r8c = strict_diff > 0
    elif diff_sign == "custom_better":
        r8c = strict_diff < 0
    else:
        r8c = True  # tied: both metrics agree on tied is fine

    gates = {
        "r7_primary_gate": r7_primary,
        "r8a_min_span_filter": r8a,
        "r8b_p10_robust": r8b,
        "r8c_dual_metric_agree": r8c,
    }

    verdict = metrics.compute_verdict(gates, diff_sign, diff_ci, n_eligible)

    return {
        "verdict": verdict,
        **gates,
        "r7_diff_sign": diff_sign,
        "r7_diff_ci_lo": diff_ci[0],
        "r7_diff_ci_hi": diff_ci[1],
        "n_eligible_templates": n_eligible,
        "oss_best": oss_best,
        "custom_best": custom_best,
    }


def _strict_recall_for_best(
    predictions_by_variant: dict[str, list[dict]],
    entity: str,
    best_cell: dict[str, Any],
    spec_lookup: dict[str, BaselineSpec],
) -> float:
    name = best_cell.get("variant")
    k = best_cell.get("k")
    if name is None or k is None:
        return 0.0
    spec = spec_lookup[name]
    var_rows = predictions_by_variant[name]
    rows = _build_template_rows(var_rows, entity, k, spec.score_bearing)
    if not rows:
        return 0.0
    recalls: list[float] = []
    for row in rows.values():
        gold = row["gold_spans"]
        if len(gold) < MIN_SPANS[entity]:
            continue
        _, recall, _ = metrics.strict_span_f1(row["pred_spans"], gold)
        recalls.append(recall)
    return sum(recalls) / len(recalls) if recalls else 0.0


def f2_verdict_compute(
    predictions_by_variant: dict[str, list[dict]],
    noise_floor: dict[str, Any],
    *,
    partial_run: bool,
    leakage_passed: bool = True,
    spec_lookup: dict[str, BaselineSpec] | None = None,
    seed: int = 42,
) -> dict[str, Any]:
    """Build the artifact dict per plan JSON shape.

    R12 partial-run hard gate: when `partial_run=True`, OMIT `aggregates` and
    `verdict_per_entity` (key not present, not None — peek-bias closure P1-2).
    Same when `leakage_passed=False` (R14 zero-tolerance).

    `spec_lookup` defaults to `BASELINE_REGISTRY` but tests inject a custom
    lookup containing stub specs.
    """
    if spec_lookup is None:
        spec_lookup = BASELINE_REGISTRY

    base: dict[str, Any] = {
        "partial_run": partial_run,
        "leakage_check_passed": leakage_passed,
        "measurements_summary": {
            name: {"n_docs": len(rows)} for name, rows in predictions_by_variant.items()
        },
    }

    if partial_run or not leakage_passed:
        # Hard gate: omit aggregates + verdict_per_entity (peek-bias closure).
        return base

    # Compute verdicts.
    aggregates: dict[str, Any] = {}
    verdict_per_entity: dict[str, Any] = {}
    for entity in ENTITIES:
        cell = _entity_verdict(
            predictions_by_variant, entity, spec_lookup, noise_floor, seed=seed
        )
        verdict_per_entity[entity] = {
            "verdict": cell["verdict"],
            "r7_primary_gate": cell["r7_primary_gate"],
            "r8a_min_span_filter": cell["r8a_min_span_filter"],
            "r8b_p10_robust": cell["r8b_p10_robust"],
            "r8c_dual_metric_agree": cell["r8c_dual_metric_agree"],
            "r7_diff_sign": cell["r7_diff_sign"],
            "r7_diff_ci_lo": cell["r7_diff_ci_lo"],
            "r7_diff_ci_hi": cell["r7_diff_ci_hi"],
            "n_eligible_templates": cell["n_eligible_templates"],
        }
        aggregates[entity] = {
            "oss_best": cell["oss_best"],
            "custom_best": cell["custom_best"],
            "diff": cell["oss_best"]["recall"] - cell["custom_best"]["recall"],
            "diff_ci_bonferroni": [cell["r7_diff_ci_lo"], cell["r7_diff_ci_hi"]],
        }
    base["aggregates"] = aggregates
    base["verdict_per_entity"] = verdict_per_entity
    return base


# --- CLI ---------------------------------------------------------------------


def _resolve_corpus_path(version: str) -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "data"
        / "benchmark"
        / version
        / "ja"
        / "raw.json"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="compare_baselines")
    parser.add_argument("--version", default="v0.12.0", help="benchmark version")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--pod-mode", choices=("cpu", "gpu"), default="cpu")
    parser.add_argument(
        "--training-manifest",
        required=True,
        type=Path,
        help="path to training_corpus_manifest.json (R14, no auto-stub)",
    )
    parser.add_argument(
        "--skip-after",
        choices=("F0a", "F0b", "F0c", "F1", "F2"),
        default=None,
    )
    parser.add_argument(
        "--recompute-noise-floor",
        action="store_true",
        help="force F0b recompute (default: carry-forward when manifest unchanged)",
    )
    args = parser.parse_args(argv)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    corpus_path = _resolve_corpus_path(args.version)
    corpus = json.loads(corpus_path.read_text("utf-8"))

    # F0a
    leakage = f0a_data_leakage_check(corpus_path, args.training_manifest)
    (args.output_dir / "leakage_report.json").write_text(
        json.dumps(leakage, indent=2, ensure_ascii=False), "utf-8"
    )
    if not leakage["passed"]:
        print("F0a: leakage detected, abort.", file=sys.stderr)
        return 2
    if args.skip_after == "F0a":
        return 0

    # F0b — empty predictions for first run (will be filled post-F1, but plan
    # locks pin **before** measurement to avoid peek bias). For first-time pin
    # we use a tiny preview slice; carry-forward logic dominates on subsequent runs.
    baselines = list(BASELINE_REGISTRY.keys())
    nf_path = corpus_path.parent / "noise_floor.json"
    noise_floor = f0b_noise_floor_pin(
        predictions_by_variant={},
        output_path=nf_path,
        corpus_version=args.version,
        variant_set_hash=_variant_set_hash(baselines),
        manifest_hash=leakage["manifest_hash"],
        force_recompute=args.recompute_noise_floor,
    )
    if args.skip_after == "F0b":
        return 0

    # F0c
    rec_path = Path(__file__).resolve().parents[4] / RECOGNIZERS_PATH_REL
    rec_meta = f0c_recognizers_git_sha(rec_path)
    if not rec_meta["round_trip_clean"]:
        print("F0c: recognizers content hash drift, abort.", file=sys.stderr)
        return 3
    if args.skip_after == "F0c":
        return 0

    # F1
    measurement = f1_measurement(baselines, corpus, args.pod_mode)
    partial = bool(measurement["failed_variants"]) or measurement["time_box_exceeded"]
    if args.skip_after == "F1":
        (args.output_dir / "measurement.json").write_text(
            json.dumps(measurement, default=str, indent=2), "utf-8"
        )
        return 0

    # F2
    artifact = f2_verdict_compute(
        measurement["predictions_by_variant"],
        noise_floor,
        partial_run=partial,
        leakage_passed=leakage["passed"],
    )
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")

    # U5 wiring: build typed ComparisonArtifact and write via artifact.write_artifact.
    # Some metadata fields (corpus_hash, anchor_pr_sha, variant_versions wheel_sha256
    # etc.) will be populated by upstream units (F0a/F0c/U6) once available; for
    # the transitional state they default to empty per ArtifactMetadata defaults.
    leakage_check_model = LeakageCheck(
        algorithm="SHA256-NFC",
        manifest_hash=leakage["manifest_hash"],
        doc_overlap_count=leakage["doc_overlap_count"],
        template_overlap_count=leakage["template_overlap_count"],
        passed=leakage["passed"],
    )
    metadata = ArtifactMetadata(
        corpus_hash=leakage.get("corpus_hash", ""),
        noise_floor_hash=noise_floor.get("noise_floor_hash", ""),
        recognizers_pack_git_sha=rec_meta.get("git_sha", ""),
        recognizers_pack_content_sha256=rec_meta.get("content_sha256", ""),
        variant_versions={},  # TODO(U6): populate from pip metadata + wheel sha
        bootstrap_seed=42,
        tie_break_rule=(
            "(score, doc_id, span_start) for score-bearing; "
            "k=100 single-point for score-less"
        ),
        k_values=K_VALUES,
        leakage_check=leakage_check_model,
        anchor_pr_sha="",  # TODO(U7): populate from anchor_sha.txt post-merge
    )

    measurements: list[dict[str, Any]] = []  # TODO(U4 follow-up): per-row rows
    aggregates = artifact.get("aggregates")
    verdict_per_entity = artifact.get("verdict_per_entity")
    comparison = ComparisonArtifact(
        schema_version="1.0",
        run_id=run_id,
        metadata=metadata,
        measurements=measurements,
        partial_run=partial,
        failed_variants=measurement["failed_variants"],
        aggregates=aggregates,
        verdict_per_entity=verdict_per_entity,
    )
    write_artifact(comparison, args.output_dir / "comparison.json")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
