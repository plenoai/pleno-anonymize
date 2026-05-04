# pleno-pii-scanner CHANGELOG

## [v0.2.2] - 2026-05-04 — fix: macOS clone path bug + structural noise filter

Real-world eval on `azu/azu`, `mumumu/pep8-ja`, `nodejs/nodejs-ja` (706 KB
total, 59 files) surfaced **5.5% precision** (8 TP / 137 FP / 145 findings).
v0.2.2 ships two bug fixes and a structural noise filter that lifts overall
findings to **80% precision on unverified findings (8 TP / 2 FP / 16 total,
6 of which are correctly tagged `verification=failed`)** — a 89% reduction
in surfaced findings with **100% true-positive retention**.

### Fixed
- `cmd_github` now resolves the temp clone path before walking. On macOS,
  `tempfile.mkdtemp()` returns `/var/folders/...` while `os.walk` resolves
  to `/private/var/folders/...`, breaking `relative_to(root)` and silently
  dropping every cloned file (`scanned 0 files in 4 ms`). Mirrors what
  `cmd_dir` already did.
- `cmd_github` and `cmd_protect` no longer call `Finding.__dict__`.
  `Finding` is `@dataclass(frozen=True, slots=True)`, so `__dict__` raises
  `AttributeError`. Use `dataclasses.replace` instead.

### Added
- `noise_filters.py` — structural FP suppression layer between `verify` and
  `filter_findings`. Filters key off **content-type signals**, not
  entity-value blacklists, to avoid corpus overfitting:
  - `IP_ADDRESS`: drop reserved/loopback/private/multicast IPs (`127.0.0.1`,
    `192.168.x`), IPv6 `::`/`::1` literals (Sphinx `.. code-block::` noise),
    matches inside `version`/`upgrade`/`bump`/`V8`/`tgz#`/semver-range
    contexts, and matches inside backtick code spans.
  - `PHONE_NUMBER`: drop low-confidence (≤0.45, unverified) Presidio matches
    when the line carries version/PR-id context, semver shape (`16.43.2`),
    `[#NNNN]` markdown link, or a date fragment (`2015-02-16`).
  - `PERSON`: drop spaCy NER spans containing backticks, crossing line
    boundaries, or sitting inside an inline-code span.
- 17 regression tests in `tests/test_noise_filters.py`, each anchored to a
  specific real-world FP from the v0.2.1 eval.

### Real-world impact (v0.2.1 → v0.2.2 on the same three repos)

| Repo | Findings before | After | TP retained | Reduction |
|---|---:|---:|---:|---:|
| `azu/azu` | 9 | 1 | 1/1 | -89% |
| `nodejs/nodejs-ja` | 30 | 10 | 4/4 | -67% |
| `mumumu/pep8-ja` | 106 | 5 | 3/3 | -95% |
| **Total** | **145** | **16** | **8/8** | **-89%** |

Residual FPs (2× `PERSON='大文字'` in pep8-ja) are model-level — `ja_ner_ja`
misclassifies common Japanese nouns. Tracked separately for the HF backend
(`PLENO_PII_SCANNER_BACKEND=hf`) which has F1 0.701 vs the 0.452 spaCy
baseline.

## [v0.2.1] - 2026-05-04 — fix: load published ONNX artifact via optimum

The v0.2.0 [hf] extra pulled in `torch` + `transformers` and tried to
`AutoModelForTokenClassification.from_pretrained(...)` against
`0xhikae/ja-ner-onnx@v0.13.0`. That repo only ships the INT8-quantized ONNX
file (`model_quantized.onnx`) — there is no `model.safetensors`, so v0.2.0
crashed at first inference with "does not appear to have a file named
pytorch_model.bin or model.safetensors".

v0.2.1 switches to `optimum.onnxruntime.ORTModelForTokenClassification`,
which loads the quantized ONNX file directly. As side effects: the [hf]
extra drops `torch` (saves ~600 MB) and replaces logits softmax with a small
numpy kernel.

### Changed
- `[hf]` extra: `torch` → `optimum[onnxruntime]>=1.21`.
- `hf_ner_pass._load_pipeline` uses `ORTModelForTokenClassification`,
  preferring `model_quantized.onnx` when available.
- `scan_text_hf` runs softmax + argmax via numpy.

## [v0.2.0] - 2026-05-04 — opt-in HF NER backend (model/v0.13.0 consumer)

### Added
- `hf_ner_pass.py` — HuggingFace token-classification scan path with per-label
  confidence floor. Loads `0xhikae/ja-ner-onnx@v0.13.0` from HF Hub by default;
  cached after first run.
- `[hf]` optional dependency group (torch + transformers + huggingface_hub).
- Backend selection via env `PLENO_PII_SCANNER_BACKEND=hf`.
- Per-label thresholds via env `PLENO_PII_SCANNER_THRESHOLDS=ORGANIZATION=0.99,...`
  (default matches v0.13.0 model card).
- Custom model source via env `PLENO_PII_SCANNER_HF_MODEL` /
  `PLENO_PII_SCANNER_HF_REVISION`.

### Default behavior unchanged
- Without `PLENO_PII_SCANNER_BACKEND=hf` the scanner still uses Presidio +
  spaCy `ja_ner_ja@0.2.0`. v0.2.0 is fully backward-compatible.

### Why opt-in
- HF backend adds ~600 MB of torch + transformers; default `uvx
  pleno-pii-scanner` keeps the lightweight install.
- The HF model requires a network fetch on first run (HF Hub download). The
  spaCy path also fetches `ja_ner_ja` lazily but is much smaller (~20 MB).

### Migration
```bash
# Default (spaCy):
uvx pleno-pii-scanner ./repo

# Higher-precision (HF, ORG≥0.99):
PLENO_PII_SCANNER_BACKEND=hf uvx --with 'pleno-pii-scanner[hf]' pleno-pii-scanner ./repo
```

### Refs
- Consumes `model/v0.13.0` (`packages/training` Phase 2 result).
- Eval: `packages/training/models/hf-ja-v02-tiny-aug-ext-org-threshold-eval-v012.md`.
- Issues: #48, #98, #79.

## [v0.1.2] - prior

(Previous changelog entries were not maintained in a CHANGELOG file; see git
history for `pleno-pii-scanner@v0.1.x` commits.)
