# pleno-pii-scanner CHANGELOG

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
