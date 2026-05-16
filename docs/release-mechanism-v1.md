# Mechanism-v1 release runbook (Simula 8/8 — issue #155)

Captures the **final three manual handoff steps** that complete the
mechanism-v1 ship. Earlier stages (1–7) are fully autonomous and have
landed via PRs #156, #157, #158, #159, #160, #161, #162.

## State at infrastructure-complete

| Artefact | Status |
|---|---|
| Taxonomy (`data/taxonomies/jp_pii_taxonomy.yaml`) | committed, 35 domains × 382 scenarios |
| Meta-prompts (`data/meta_prompts/jp/all.jsonl`) | committed, 1,910 prompts |
| Complexification operators | committed |
| Dual-critic loop | committed |
| Generated samples (`data/raw/ja-mechanism-v1/all.jsonl`) | 2,014 rows (gitignored; partial — daily OpenAI RPD limit of 10,000 hit) |
| Training pipeline (`scripts/train_mechanism.py`) | committed |
| Eval pipeline (`scripts/eval_mechanism_on_300k.py`) | committed |
| HF push scripts | committed |
| RunPod launch helper (`scripts/runpod_launch_mechanism_training.py`) | committed |

## Remaining manual steps

### 1. Top up the dataset (optional, recommended for Parity)

Daily RPD limit of 10,000 was reached during the initial generation
(2,014 accepted from ~5,000 attempts at ~40 % accept rate after band
widening). Wait for the OpenAI quota to reset and re-run:

```bash
cd packages/training
dotenvx run -f ../../.env -- uv run --extra training python \
    scripts/generate_mechanism_dataset.py \
    --samples-per-prompt 8 --max-workers 32 \
    --output-dir data/raw/ja-mechanism-v1
```

The generator appends-only because `--output-dir` is the same — re-running
will continue where it left off. Target: ≥ 15k samples (~9 hours
wall-clock at 10k-RPD limit, or ~2 hours if the org's RPD is lifted).

### 2. Set HF token + push dataset

```bash
huggingface-cli login   # paste a token with write scope on plenoai/
cd packages/training
make push-dataset-hf
```

This uploads `train.jsonl` / `dev.jsonl` / `test.jsonl` to
`plenoai/pii-masking-jp-mechanism-v1` with the mechanism-v1 model card.

### 3. Launch RunPod training

Generate the create-pod payload:

```bash
python scripts/runpod_launch_mechanism_training.py \
    --hf-dataset plenoai/pii-masking-jp-mechanism-v1 \
    --hf-model   plenoai/ja_ner_ja-v2 \
    --epochs 3 \
    --gpu "NVIDIA GeForce RTX 4090"
```

Replace the `HF_TOKEN` placeholder with your actual token, then call
the runpod MCP from a Claude session:

```
mcp__runpod__create-pod(<paste payload here>)
```

Monitor:

```
mcp__runpod__get-pod(podId="<id>")
```

Cost: ~$0.10 for a 3-epoch RTX 4090 run on 2k samples; ~$0.50 on 15k.

The pod's startup script:

1. Clones the repo.
2. Pulls the dataset from HF.
3. Materialises `data/raw/ja-mechanism-v1/{train,dev,test}.jsonl`.
4. Runs `scripts/train_mechanism.py`.
5. Calls `scripts/push_model_to_hf.py` against `plenoai/ja_ner_ja-v2`.
6. Self-terminates.

### 4. Benchmark + update docs

After the pod completes:

```bash
cd packages/training
make eval-mechanism-300k-ja BENCH_MODEL=plenoai/ja_ner_ja-v2 BENCH_LIMIT=300
```

Then fill the TBD row in `docs/benchmark-mechanism-v1.md`:

```markdown
| `ja_ner_ja-v2` (mechanism-v1) | <F1> | <P> | <R> |
```

And the headline numbers in `README.md`'s "Japanese validation split"
table (currently shows `builtin` at 0.342).

## Acceptance criteria recap (epic #147)

| Criterion | Status |
|---|---|
| F1 ≥ 0.50 (Smoke) on `300k-ja` | gated on step 4 above |
| F1 ≥ 0.82 (Parity) | stretch — likely needs step 1 to top up dataset |
| Per-label recall floors per SKILL.md | gated on step 4 |
| Latency within +25 % of `builtin` | gated on step 4 |
| HF model + dataset published | gated on steps 2 + 3 |
| `docs/benchmark.md` updated | gated on step 4 |

## Why steps 2–4 are manual

Steps 2–4 require credentials that the autonomous agent does not have
local access to:

- HF write-scoped token (for `plenoai/` org pushes)
- RunPod GPU spend authorisation

These are intentionally human-gated. The agent's job ended when the
infrastructure to perform each step landed and was tested.
