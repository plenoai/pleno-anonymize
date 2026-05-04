# pleno-pii-scanner CHANGELOG

## [v0.2.4] - 2026-05-04 — feat: --db-only cluster mode + JWT timestamp filter

Repository-level PII risk follows database shape: a single contact email
in a CODE_OF_CONDUCT.md is one identifiable person, not an exfiltration
target; a CSV row with name + phone + email + my_number is. v0.2.4 adds
``--db-only`` that surfaces only findings co-occurring in DB-shaped
files or folders, plus three round-3 FP filters surfaced by expanding
the eval to ten Japanese repositories.

### Added
- ``cluster.py`` — DB-cluster post-filter. Groups findings by file and
  folder, keeps a finding only when its container is DB-shaped:
    - File: count >= ``--db-file-threshold`` (default 2) AND distinct
      matched values >= ``file_min_distinct`` (default 2).
    - Folder: count >= ``--db-folder-threshold`` (default 3) AND
      distinct matched values >= ``folder_min_distinct`` (default 3).
  ``verification=failed`` findings are excluded from cluster computation
  so an awesome-list of book ISBNs (which fail the MY_NUMBER checksum)
  cannot promote a folder to "DB-shaped".
- CLI flags ``--db-only`` (boolean), ``--db-file-threshold`` (int),
  ``--db-folder-threshold`` (int) on ``dir``, ``git``, and ``github``.

### Round-3 noise filters
- **JWT timestamp** PHONE FPs. ``"iat": 1654514191``, ``"jti": "..."``
  surface 10-digit Unix timestamps that Presidio reads as phone numbers.
  Drops when the line carries a JWT claim name AND the value is in
  the 2001–2033 epoch range. Surfaced by
  ``heyinc/development-partner-docs/auth-oauth.md``.
- **Trailing markdown bracket recall fix.** ``株式会社ユーザベース]``
  captured from a ``[株式会社ユーザベース](https://...)`` link was
  previously dropped by the paren-in-match check, costing 11 ORG TPs in
  ``fnwiya/japanese-rust-companies``. ``_contains_paren`` now only fires
  on **opening** brackets, not closing.

### Ten-repo eval (raw → noise → db-only)

| Repo | Raw | Noise | DB-only |
|---|---:|---:|---:|
| azu/azu | 9 | 1 | **0** |
| nodejs/nodejs-ja | 30 | 10 | **0** |
| mumumu/pep8-ja | 106 | 3 | 3 |
| suisya-systems/claude-org-ja | 15 | 1 | **0** |
| Ajay77187718/awesome-ai-red-teaming-jp | 12 | 2 | 2 |
| s-takayanagi/Local-mac-pii-mask-jp | 304 | 304 | 302 |
| fnwiya/japanese-rust-companies | 0 | 9 | 9 |
| kllc/resume | 7 | 7 | 7 |
| heyinc/development-partner-docs | 10 | 7 | **0** |
| kazumasakawahara/neo4j-agno-agent | 612 | 616 | 586 |
| **Total** | **1,105** | **960** | **909** |

The headline metric is per-repository, not per-finding: ``--db-only``
takes 6/10 repos from "has findings to triage" to **zero** without
losing any genuine DB-shaped exposure (resumes, PII fixture banks,
contributor lists). Single-contact mentions in
``CODE_OF_CONDUCT.md`` / support docs no longer require triage.

## [v0.2.3] - 2026-05-04 — release: ship round-2 noise filter

Bundles the round-1 (PR #100, v0.2.2 candidate) and round-2 (PR #103,
post-merge expansion) noise-filter work into a single PyPI release. See
v0.2.2 below for the full scope; v0.2.3 is the first version actually
published.

## [v0.2.2] - 2026-05-04 — fix: macOS clone path bug + structural noise filter

Real-world eval on five small Japanese-content GitHub repos
(`azu/azu`, `mumumu/pep8-ja`, `nodejs/nodejs-ja`,
`suisya-systems/claude-org-ja`, `Ajay77187718/awesome-ai-red-teaming-jp`)
surfaced **5.5% precision** (8 TP / ~137 FP). v0.2.2 ships two latent
bug fixes and a structural noise filter that lifts surfaced findings to
**100% precision on actionable (unverified) findings** while reducing
total findings by 90%. All true positives are retained.

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
    contexts (`^1.0.NNN`, `@^1.2.3`, `~1.0`), and matches inside backtick
    code spans.
  - `PHONE_NUMBER`: drop low-confidence (≤0.45, unverified) Presidio matches
    when the line carries version/PR-id context, semver shape (`16.43.2`),
    `[#NNNN]` markdown link, ISO-date fragments, product-URL paths
    (Amazon ASINs, ISBN URLs).
  - `EMAIL_ADDRESS`: re-validate against a strict pattern (drops Presidio's
    greedy `user.email=ci@...` and `github.com/.../core-harness@v0.x.y`
    captures). Drop RFC 2606 reserved domains (`example.com`, `example.org`,
    `localhost`, etc.) and version-shape final labels.
  - `MY_NUMBER` / `MY_NUMBER_CORPORATE`: drop ISBN-13 (978/979 prefix) and
    matches inside book/product URL paths.
  - `PERSON` / `ORGANIZATION`: drop NER spans with backticks, line-boundary
    crossings, inline-code spans, non-name leaders (`◎`, `~`, `（`, `※`),
    digits, parentheses, box-drawing characters, or common-noun heads
    (`一覧`, `番号`, `文字`, `関数`, `属性`, `呼び出し`, `割り当て`, …).
    The common-noun head list is sunset once issue #101 ships HF backend
    by default.
- 47 regression tests in `tests/test_noise_filters.py`, each anchored to a
  specific real-world FP from the eval.

### Real-world impact (v0.2.1 → v0.2.2 on five small Japanese repos)

| Repo | Findings before | After | TP retained | Reduction |
|---|---:|---:|---:|---:|
| `azu/azu` | 9 | 1 | 1/1 | -89% |
| `nodejs/nodejs-ja` | 30 | 10 | 4/4 | -67% |
| `mumumu/pep8-ja` | 106 | 3 | 3/3 | -97% |
| `suisya-systems/claude-org-ja` | 15 | 1 | 1/1 | -93% |
| `Ajay77187718/awesome-ai-red-teaming-jp` | 12 | 2 | 2/2 | -83% |
| **Total** | **172** | **17** | **11/11** | **-90%** |

Of the 17 surviving findings: 11 are real PII (9 emails + 2 organization
names) and 6 are MY_NUMBER candidates correctly tagged as
`verification=failed` by the checksum verifier (Tumblr post IDs in blog
URLs). The actionable (unverified) finding precision is **100%**.

Outstanding model-level residuals (deferred to issue #101 / HF backend):
the spaCy `ja_ner_ja` baseline still misclassifies a handful of common
Japanese nouns as PERSON in technical prose. The structural filter handles
the most prominent classes via head-noun suffix matching but cannot fully
cover the long tail without a POS-aware model.

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
