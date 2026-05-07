.PHONY: generate convert train train-cnn evaluate benchmark-external benchmark-huggingface package clean \
       generate-en augment-en convert-en train-en evaluate-en benchmark-external-en package-en all-en \
       benchmark-v02-generate benchmark-v02-generate-en benchmark-v02-evaluate benchmark-v02-evaluate-en \
       benchmark-v03-generate benchmark-v03-generate-en benchmark-v03-evaluate benchmark-v03-evaluate-en \
       benchmark-v04-generate benchmark-v04-generate-en benchmark-v04-evaluate benchmark-v04-evaluate-en \
       benchmark-v05-generate benchmark-v05-generate-en benchmark-v05-evaluate benchmark-v05-evaluate-en \
       benchmark-v10-generate benchmark-v10-evaluate benchmark-v10-generate-en benchmark-v10-evaluate-en \
       benchmark-v11-generate benchmark-v11-evaluate benchmark-v11-generate-en benchmark-v11-evaluate-en \
       benchmark-v12-generate benchmark-v12-evaluate benchmark-v12-generate-en benchmark-v12-evaluate-en \
       benchmark-v13-held-out-evaluate benchmark-v13-held-out-evaluate-en \
       verify-leakage-v12 pin-noise-floor-v12 compare-baselines-v12 runpod-gpu-compare-v12 \
       convert-hf-v02 train-hf-v02 export-onnx-v02 push-hf-v02 all-hf-ja-v02 \
       convert-hf-v02-tiny train-hf-v02-tiny export-onnx-v02-tiny push-hf-v02-tiny all-hf-ja-v02-tiny \
       mine-external-hardneg-ja mine-external-hardneg-ja-dry-run \
       convert-hf-v02-tiny-hardneg-ext train-hf-v02-tiny-hardneg-ext \
       mine-external-hardneg-en mine-external-hardneg-en-dry-run \
       convert-hf-en-v01-tiny convert-hf-en-v01-tiny-hardneg-ext convert-hf-en-v01-tiny-aug-ext \
       train-hf-en-v01-tiny train-hf-en-v01-tiny-hardneg-ext train-hf-en-v01-tiny-aug-ext \
       export-onnx-en-v01-tiny push-hf-en-v01-tiny all-hf-en-v01-tiny

MODEL ?= gpt-5.4-nano
DOCS_PER_TEMPLATE ?= 20
BATCHES_PER_TEMPLATE ?= 50
MAX_WORKERS ?= 5
GPU_ID ?= 0
DOTENVX = dotenvx run -f ../../.env --

# Path (or spaCy package name) used by the en benchmark evaluate targets.
# Default mirrors the ja side which uses $(OUTPUT_DIR)/model-best — i.e. the
# locally trained output. Use the existing en transformer pipeline target so
# it lines up with `train-en` (`configs/train_transformer_en.cfg`).
# Override with EN_MODEL=en_ner_en (pip-installed wheel) or any other path
# when evaluating a published / packaged model instead.
EN_MODEL ?= $(OUTPUT_DIR)/en-transformer/model-best

DATA_RAW = data/raw
DATA_PROCESSED = data/processed
OUTPUT_DIR = output
MODELS_DIR = ../models

# === 日本語 (既存) ===

generate:
	$(DOTENVX) uv run --extra training python -m pleno_ner_training.generate_data \
		--language ja \
		--output-dir $(DATA_RAW)/ja \
		--docs-per-template $(DOCS_PER_TEMPLATE) \
		--batches-per-template $(BATCHES_PER_TEMPLATE) \
		--model $(MODEL) \
		--max-workers $(MAX_WORKERS)

convert:
	uv run python -m pleno_ner_training.convert_to_docbin \
		--language ja \
		--input $(DATA_RAW)/ja/generated.json \
		--output-dir $(DATA_PROCESSED)/ja

train:
	uv run --extra training python -m spacy train configs/train_transformer.cfg \
		--output $(OUTPUT_DIR) \
		--paths.train $(DATA_PROCESSED)/ja/train.spacy \
		--paths.dev $(DATA_PROCESSED)/ja/dev.spacy \
		--gpu-id $(GPU_ID)

train-cnn:
	uv run python -m spacy train configs/train_cnn.cfg \
		--output $(OUTPUT_DIR) \
		--paths.train $(DATA_PROCESSED)/ja/train.spacy \
		--paths.dev $(DATA_PROCESSED)/ja/dev.spacy

evaluate:
	uv run python -m pleno_ner_training.evaluate \
		--model $(OUTPUT_DIR)/model-best \
		--language ja \
		--test-data $(DATA_PROCESSED)/ja/test.spacy \
		--output-json $(OUTPUT_DIR)/scores.json

benchmark-external:
	uv run python -m pleno_ner_training.benchmark_external \
		--language ja \
		--test-data $(DATA_PROCESSED)/ja/test.spacy \
		--output $(OUTPUT_DIR)/ja/external_scores.json

benchmark-huggingface:
	uv run python -m pleno_ner_training.benchmark_huggingface \
		--language ja \
		--test-data $(DATA_PROCESSED)/ja/test.spacy \
		--merge-into $(OUTPUT_DIR)/ja/external_scores.json

package:
	mkdir -p $(MODELS_DIR)
	uv run python -m spacy package $(OUTPUT_DIR)/model-best $(MODELS_DIR) \
		--name ner_ja \
		--version 0.1.0 \
		--build wheel

# === 日本語 v0.2.0 ===

generate-v02:
	$(DOTENVX) uv run --extra training python -m pleno_ner_training.generate_data \
		--language ja \
		--output-dir $(DATA_RAW)/ja-v02 \
		--docs-per-template $(DOCS_PER_TEMPLATE) \
		--batches-per-template $(BATCHES_PER_TEMPLATE) \
		--model $(MODEL) \
		--max-workers $(MAX_WORKERS)

augment-ja:
	uv run python -m pleno_ner_training.augment_ja_data \
		--input $(DATA_RAW)/ja-v02/generated.json \
		--output $(DATA_RAW)/ja-v02/augmented.json \
		--augment-count 5000

convert-v02:
	uv run python -m pleno_ner_training.convert_to_docbin \
		--language ja \
		--input $(DATA_RAW)/ja-v02/augmented.json \
		--output-dir $(DATA_PROCESSED)/ja-v02

train-v02:
	uv run python -m spacy train configs/train_cnn.cfg \
		--output $(OUTPUT_DIR)/ja-v02 \
		--paths.train $(DATA_PROCESSED)/ja-v02/train.spacy \
		--paths.dev $(DATA_PROCESSED)/ja-v02/dev.spacy

evaluate-v02:
	uv run python -m pleno_ner_training.evaluate \
		--model $(OUTPUT_DIR)/ja-v02/model-best \
		--language ja \
		--test-data $(DATA_PROCESSED)/ja-v02/test.spacy \
		--output-json $(OUTPUT_DIR)/ja-v02/scores.json

benchmark-v02-evaluate-ja-v02:
	uv run python -m pleno_ner_training.evaluate_benchmark \
		--model $(OUTPUT_DIR)/ja-v02/model-best \
		--language ja

package-v02:
	mkdir -p $(MODELS_DIR)
	uv run python -m spacy package $(OUTPUT_DIR)/ja-v02/model-best $(MODELS_DIR) \
		--name ner_ja \
		--version 0.2.0 \
		--build wheel

all-ja-v02: generate-v02 augment-ja convert-v02 train-v02 evaluate-v02 benchmark-v02-evaluate-ja-v02 package-v02

# === 英語 ===

generate-en:
	$(DOTENVX) uv run --extra training python -m pleno_ner_training.generate_data \
		--language en \
		--output-dir $(DATA_RAW)/en \
		--docs-per-template $(DOCS_PER_TEMPLATE) \
		--batches-per-template $(BATCHES_PER_TEMPLATE) \
		--model $(MODEL) \
		--max-workers $(MAX_WORKERS)

augment-en:
	uv run python -m pleno_ner_training.augment_en_data \
		--input $(DATA_RAW)/en/generated.json \
		--output $(DATA_RAW)/en/augmented.json \
		--augment-count 5000

convert-en:
	uv run python -m pleno_ner_training.convert_to_docbin \
		--language en \
		--input $(DATA_RAW)/en/generated.json \
		--augment $(DATA_RAW)/en/augmented.json \
		--output-dir $(DATA_PROCESSED)/en

train-en:
	uv run --extra training python -m spacy train configs/train_transformer_en.cfg \
		--output $(OUTPUT_DIR)/en \
		--paths.train $(DATA_PROCESSED)/en/train.spacy \
		--paths.dev $(DATA_PROCESSED)/en/dev.spacy \
		--gpu-id $(GPU_ID)

train-en-cnn:
	uv run python -m spacy train configs/train_cnn_en.cfg \
		--output $(OUTPUT_DIR)/en \
		--paths.train $(DATA_PROCESSED)/en/train.spacy \
		--paths.dev $(DATA_PROCESSED)/en/dev.spacy

evaluate-en:
	uv run python -m pleno_ner_training.evaluate \
		--model $(OUTPUT_DIR)/en/model-best \
		--language en \
		--test-data $(DATA_PROCESSED)/en/test.spacy \
		--output-json $(OUTPUT_DIR)/en/scores.json

benchmark-external-en:
	uv run python -m pleno_ner_training.benchmark_external \
		--language en \
		--test-data $(DATA_PROCESSED)/en/test.spacy \
		--output $(OUTPUT_DIR)/en/external_scores.json

package-en:
	mkdir -p $(MODELS_DIR)
	uv run python -m spacy package $(OUTPUT_DIR)/en/model-best $(MODELS_DIR) \
		--name ner_en \
		--version 0.1.1 \
		--build wheel

all-en: generate-en augment-en convert-en train-en evaluate-en package-en

# === Benchmark v0.2.0 ===

BENCHMARK_BATCHES ?= 10

benchmark-v02-generate:
	$(DOTENVX) uv run --extra training python -m pleno_ner_training.generate_benchmark \
		--language ja \
		--docs-per-template $(DOCS_PER_TEMPLATE) \
		--batches-per-template $(BENCHMARK_BATCHES) \
		--model $(MODEL) \
		--max-workers $(MAX_WORKERS)

benchmark-v02-generate-en:
	$(DOTENVX) uv run --extra training python -m pleno_ner_training.generate_benchmark \
		--language en \
		--docs-per-template $(DOCS_PER_TEMPLATE) \
		--batches-per-template $(BENCHMARK_BATCHES) \
		--model $(MODEL) \
		--max-workers $(MAX_WORKERS)

benchmark-v02-evaluate:
	uv run python -m pleno_ner_training.evaluate_benchmark \
		--model $(OUTPUT_DIR)/model-best \
		--language ja --version v0.2.0

benchmark-v02-evaluate-en:
	uv run python -m pleno_ner_training.evaluate_benchmark \
		--model $(EN_MODEL) \
		--language en --version v0.2.0

benchmark-v03-generate:
	$(DOTENVX) uv run --extra training python -m pleno_ner_training.generate_benchmark \
		--version v0.3.0 \
		--language ja \
		--docs-per-template $(DOCS_PER_TEMPLATE) \
		--batches-per-template $(BENCHMARK_BATCHES) \
		--model $(MODEL) \
		--max-workers $(MAX_WORKERS)

benchmark-v03-generate-en:
	$(DOTENVX) uv run --extra training python -m pleno_ner_training.generate_benchmark \
		--version v0.3.0 \
		--language en \
		--docs-per-template $(DOCS_PER_TEMPLATE) \
		--batches-per-template $(BENCHMARK_BATCHES) \
		--model $(MODEL) \
		--max-workers $(MAX_WORKERS)

benchmark-v03-evaluate:
	uv run python -m pleno_ner_training.evaluate_benchmark \
		--model $(OUTPUT_DIR)/model-best \
		--language ja --version v0.3.0

benchmark-v03-evaluate-en:
	uv run python -m pleno_ner_training.evaluate_benchmark \
		--model $(EN_MODEL) \
		--language en --version v0.3.0

benchmark-v04-generate:
	$(DOTENVX) uv run --extra training python -m pleno_ner_training.generate_benchmark \
		--version v0.4.0 \
		--language ja \
		--docs-per-template $(DOCS_PER_TEMPLATE) \
		--batches-per-template $(BENCHMARK_BATCHES) \
		--model $(MODEL) \
		--max-workers $(MAX_WORKERS)

benchmark-v04-generate-en:
	$(DOTENVX) uv run --extra training python -m pleno_ner_training.generate_benchmark \
		--version v0.4.0 \
		--language en \
		--docs-per-template $(DOCS_PER_TEMPLATE) \
		--batches-per-template $(BENCHMARK_BATCHES) \
		--model $(MODEL) \
		--max-workers $(MAX_WORKERS)

benchmark-v04-evaluate:
	uv run python -m pleno_ner_training.evaluate_benchmark \
		--model $(OUTPUT_DIR)/model-best \
		--language ja --version v0.4.0

benchmark-v04-evaluate-en:
	uv run python -m pleno_ner_training.evaluate_benchmark \
		--model $(EN_MODEL) \
		--language en --version v0.4.0

benchmark-v05-generate:
	$(DOTENVX) uv run --extra training python -m pleno_ner_training.generate_benchmark \
		--version v0.5.0 \
		--language ja \
		--docs-per-template $(DOCS_PER_TEMPLATE) \
		--batches-per-template $(BENCHMARK_BATCHES) \
		--model $(MODEL) \
		--max-workers $(MAX_WORKERS)

benchmark-v05-generate-en:
	$(DOTENVX) uv run --extra training python -m pleno_ner_training.generate_benchmark \
		--version v0.5.0 \
		--language en \
		--docs-per-template $(DOCS_PER_TEMPLATE) \
		--batches-per-template $(BENCHMARK_BATCHES) \
		--model $(MODEL) \
		--max-workers $(MAX_WORKERS)

benchmark-v05-evaluate:
	uv run python -m pleno_ner_training.evaluate_benchmark \
		--model $(OUTPUT_DIR)/model-best \
		--language ja --version v0.5.0

benchmark-v05-evaluate-en:
	uv run python -m pleno_ner_training.evaluate_benchmark \
		--model $(EN_MODEL) \
		--language en --version v0.5.0

benchmark-v10-generate:
	uv run python -m pleno_ner_training.generate_benchmark \
		--version v0.10.0 \
		--language ja \
		--docs-per-template $(DOCS_PER_TEMPLATE) \
		--batches-per-template $(BENCHMARK_BATCHES) \
		--max-workers $(MAX_WORKERS)

benchmark-v10-generate-en:
	uv run python -m pleno_ner_training.generate_benchmark \
		--version v0.10.0 \
		--language en \
		--docs-per-template $(DOCS_PER_TEMPLATE) \
		--batches-per-template $(BENCHMARK_BATCHES) \
		--max-workers $(MAX_WORKERS)

benchmark-v10-evaluate:
	uv run python -m pleno_ner_training.evaluate_benchmark \
		--model $(OUTPUT_DIR)/model-best \
		--language ja --version v0.10.0

benchmark-v10-evaluate-en:
	uv run python -m pleno_ner_training.evaluate_benchmark \
		--model $(EN_MODEL) \
		--language en --version v0.10.0

benchmark-v11-generate:
	uv run python -m pleno_ner_training.generate_benchmark \
		--version v0.11.0 \
		--language ja \
		--docs-per-template $(DOCS_PER_TEMPLATE) \
		--batches-per-template $(BENCHMARK_BATCHES) \
		--max-workers $(MAX_WORKERS)

benchmark-v11-generate-en:
	uv run python -m pleno_ner_training.generate_benchmark \
		--version v0.11.0 \
		--language en \
		--docs-per-template $(DOCS_PER_TEMPLATE) \
		--batches-per-template $(BENCHMARK_BATCHES) \
		--max-workers $(MAX_WORKERS)

benchmark-v11-evaluate:
	uv run python -m pleno_ner_training.evaluate_benchmark \
		--model $(OUTPUT_DIR)/model-best \
		--language ja --version v0.11.0

benchmark-v11-evaluate-en:
	uv run python -m pleno_ner_training.evaluate_benchmark \
		--model $(EN_MODEL) \
		--language en --version v0.11.0

benchmark-v12-generate:
	uv run python -m pleno_ner_training.generate_benchmark \
		--version v0.12.0 \
		--language ja \
		--docs-per-template $(DOCS_PER_TEMPLATE) \
		--batches-per-template $(BENCHMARK_BATCHES) \
		--max-workers $(MAX_WORKERS)

benchmark-v12-generate-en:
	uv run python -m pleno_ner_training.generate_benchmark \
		--version v0.12.0 \
		--language en \
		--docs-per-template $(DOCS_PER_TEMPLATE) \
		--batches-per-template $(BENCHMARK_BATCHES) \
		--max-workers $(MAX_WORKERS)

benchmark-v12-evaluate:
	uv run python -m pleno_ner_training.evaluate_benchmark \
		--model $(OUTPUT_DIR)/model-best \
		--language ja --version v0.12.0

benchmark-v12-evaluate-en:
	uv run python -m pleno_ner_training.evaluate_benchmark \
		--model $(EN_MODEL) \
		--language en --version v0.12.0

# v0.13.0-held-out: pre-built raw.json under data/benchmark/v0.13.0-held-out/<lang>/
# (no generate target — held-out is the *test* partition kept disjoint from
# training data; rebuilding it from prompts would defeat that). Convert the
# raw.json once to test.spacy via convert_to_docs, then evaluate.
benchmark-v13-held-out-evaluate:
	uv run python -m pleno_ner_training.evaluate_benchmark \
		--model $(OUTPUT_DIR)/model-best \
		--language ja --version v0.13.0-held-out

benchmark-v13-held-out-evaluate-en:
	uv run python -m pleno_ner_training.evaluate_benchmark \
		--model $(EN_MODEL) \
		--language en --version v0.13.0-held-out

# === HuggingFace Pipeline ===

convert-hf-v02:
	uv run python -m pleno_ner_training.convert_to_hf_dataset \
		--input data/raw/ja-v02/generated.json \
		--output data/hf/ja-v02 \
		--tokenizer ku-nlp/deberta-v2-base-japanese

train-hf-v02:
	uv run python -m pleno_ner_training.train_hf \
		--dataset data/hf/ja-v02 \
		--model ku-nlp/deberta-v2-base-japanese \
		--output output/hf-ja-v02 \
		--fp16

export-onnx-v02:
	uv run python -m pleno_ner_training.export_onnx \
		--model output/hf-ja-v02 \
		--output output/hf-ja-v02-onnx

push-hf-v02:
	uv run python -m pleno_ner_training.export_onnx \
		--model output/hf-ja-v02 \
		--output output/hf-ja-v02-onnx \
		--push-to 0xhikae/ja-ner-onnx

all-hf-ja-v02: convert-hf-v02 train-hf-v02 export-onnx-v02 push-hf-v02

# === HuggingFace Pipeline (tiny, browser-optimized) ===

convert-hf-v02-tiny:
	uv run python -m pleno_ner_training.convert_to_hf_dataset \
		--input data/raw/ja-v02/generated.json \
		--output data/hf/ja-v02-tiny \
		--tokenizer ku-nlp/deberta-v2-tiny-japanese

# v02-tiny + hard negatives (entities=[] docs as all-O training).
# Same target schema as convert-hf-v02-tiny, different output dir.
convert-hf-v02-tiny-hardneg:
	uv run python -m pleno_ner_training.convert_to_hf_dataset \
		--input data/raw/ja-v02/generated.json \
		--output data/hf/ja-v02-tiny-hardneg \
		--tokenizer ku-nlp/deberta-v2-tiny-japanese \
		--include-negatives

# Mine external corpus (Wikipedia ja, CC-BY-SA 4.0) for ORG-FP-likely
# hardneg docs (#64). Output JSON schema is identical to generated.json so
# it can be merged before the convert step.
mine-external-hardneg-ja:
	uv run python -m pleno_ner_training.mine_external_hardneg \
		--output data/raw/ja-v02/external_hardneg.json \
		--max-docs 4000

# Same as `mine-external-hardneg-ja` but skips the network and emits a
# tiny hand-crafted sample. Used by CI / local smoke tests.
mine-external-hardneg-ja-dry-run:
	uv run python -m pleno_ner_training.mine_external_hardneg \
		--output data/raw/ja-v02/external_hardneg.json \
		--max-docs 5 --dry-run

# v02-tiny + hard negatives + external Wikipedia mine (#64). Pre-merges
# generated.json with external_hardneg.json into a single tmp file so the
# downstream converter sees one input. Inline python avoids adding a
# stand-alone merge module for a 6-line operation.
convert-hf-v02-tiny-hardneg-ext: mine-external-hardneg-ja
	uv run python -c "import json,sys; \
a=json.load(open('data/raw/ja-v02/generated.json',encoding='utf-8')); \
b=json.load(open('data/raw/ja-v02/external_hardneg.json',encoding='utf-8')); \
json.dump(a+b,open('data/raw/ja-v02/generated_with_external.json','w',encoding='utf-8'),ensure_ascii=False)"
	uv run python -m pleno_ner_training.convert_to_hf_dataset \
		--input data/raw/ja-v02/generated_with_external.json \
		--output data/hf/ja-v02-tiny-hardneg-ext \
		--tokenizer ku-nlp/deberta-v2-tiny-japanese \
		--include-negatives

# v02-tiny + augmented (ORG diverse #80) + external Wikipedia hardneg (#64).
# Uses augmented.json (which folds in org_diverse seeds via augment-ja) instead
# of raw generated.json, so the trained model sees the full ORG vocabulary mix
# (公益/学校/略称/カタカナ) plus FP-pressure hardneg from Wikipedia. Required
# for #48 Phase 2 to drive adversarial precision past the 70% AC.
convert-hf-v02-tiny-aug-ext: augment-ja mine-external-hardneg-ja
	uv run python -c "import json,sys; \
a=json.load(open('data/raw/ja-v02/augmented.json',encoding='utf-8')); \
b=json.load(open('data/raw/ja-v02/external_hardneg.json',encoding='utf-8')); \
json.dump(a+b,open('data/raw/ja-v02/augmented_with_external.json','w',encoding='utf-8'),ensure_ascii=False)"
	uv run python -m pleno_ner_training.convert_to_hf_dataset \
		--input data/raw/ja-v02/augmented_with_external.json \
		--output data/hf/ja-v02-tiny-aug-ext \
		--tokenizer ku-nlp/deberta-v2-tiny-japanese \
		--include-negatives

# Train counterpart (RunPod). Mirrors `train-hf-v02-tiny-hardneg` shape so
# evaluate_benchmark auto-derives the entry key `hf_v02_tiny_hardneg_ext`.
train-hf-v02-tiny-hardneg-ext:
	uv run python -m pleno_ner_training.train_hf \
		--dataset data/hf/ja-v02-tiny-hardneg-ext \
		--model ku-nlp/deberta-v2-tiny-japanese \
		--output output/hf-ja-v02-tiny-hardneg-ext \
		--fp16 --num-epochs 20 --batch-size 16 \
		--learning-rate 3e-5 --warmup-ratio 0.15

# Train counterpart for the augmented + hardneg-ext dataset (#48 Phase 2).
# Same hyperparameters as hardneg-ext so deltas isolate the dataset variable.
train-hf-v02-tiny-aug-ext:
	uv run python -m pleno_ner_training.train_hf \
		--dataset data/hf/ja-v02-tiny-aug-ext \
		--model ku-nlp/deberta-v2-tiny-japanese \
		--output output/hf-ja-v02-tiny-aug-ext \
		--fp16 --num-epochs 20 --batch-size 16 \
		--learning-rate 3e-5 --warmup-ratio 0.15

train-hf-v02-tiny:
	uv run python -m pleno_ner_training.train_hf \
		--dataset data/hf/ja-v02-tiny \
		--model ku-nlp/deberta-v2-tiny-japanese \
		--output output/hf-ja-v02-tiny \
		--fp16 --num-epochs 20 --batch-size 16 \
		--learning-rate 3e-5 --warmup-ratio 0.15

train-hf-v02-tiny-hardneg:
	uv run python -m pleno_ner_training.train_hf \
		--dataset data/hf/ja-v02-tiny-hardneg \
		--model ku-nlp/deberta-v2-tiny-japanese \
		--output output/hf-ja-v02-tiny-hardneg \
		--fp16 --num-epochs 20 --batch-size 16 \
		--learning-rate 3e-5 --warmup-ratio 0.15

export-onnx-v02-tiny:
	uv run python -m pleno_ner_training.export_onnx \
		--model output/hf-ja-v02-tiny \
		--output output/hf-ja-v02-tiny-onnx

push-hf-v02-tiny:
	uv run python -m pleno_ner_training.export_onnx \
		--model output/hf-ja-v02-tiny \
		--output output/hf-ja-v02-tiny-onnx \
		--push-to 0xhikae/ja-ner-onnx

all-hf-ja-v02-tiny: convert-hf-v02-tiny train-hf-v02-tiny export-onnx-v02-tiny push-hf-v02-tiny

# === Baseline Comparison (S6 / U6) ===

verify-leakage-v12:
	uv run --extra bench python -m pleno_ner_training.compare_baselines \
		--version v0.12.0 \
		--training-manifest data/benchmark/v0.12.0/ja/training_corpus_manifest.json \
		--skip-after F0a \
		--output-dir experiments/artifacts/leakage-check-$(shell date +%Y%m%d_%H%M%S)

pin-noise-floor-v12:
	uv run --extra bench python -m pleno_ner_training.compare_baselines \
		--version v0.12.0 \
		--training-manifest data/benchmark/v0.12.0/ja/training_corpus_manifest.json \
		--skip-after F0b \
		--output-dir experiments/artifacts/noise-floor-$(shell date +%Y%m%d_%H%M%S)

compare-baselines-v12:
	uv run --extra bench python -m pleno_ner_training.compare_baselines \
		--version v0.12.0 \
		--training-manifest data/benchmark/v0.12.0/ja/training_corpus_manifest.json \
		--pod-mode cpu \
		--output-dir experiments/artifacts/$(shell date +%Y%m%d_%H%M%S)

runpod-gpu-compare-v12:
	@echo "GPU run: see packages/training/docs/runpod-gpu-inference.md"
	@echo "Use chrome MCP per CLAUDE.md to orchestrate the pod kick."

# === Confidence threshold sweep (#68) ===
# Two-step pipeline: predict once (heavy, requires HF model) → sweep many
# thresholds offline (cheap, pure Python). Splitting lets you re-sweep with a
# different grid without re-running the model.

predict-scores-v12:
	uv run --extra hf python -m pleno_ner_training.predict_hf_with_scores \
		--model output/hf-ja-v02-tiny-hardneg \
		--input data/benchmark/v0.12.0/ja/raw.json \
		--output data/benchmark/v0.12.0/ja/raw_with_scores.json

sweep-threshold-v12: predict-scores-v12
	uv run python -m pleno_ner_training.sweep_threshold \
		--predictions data/benchmark/v0.12.0/ja/raw_with_scores.json \
		--output-md data/benchmark/v0.12.0/ja/threshold_sweep.md \
		--output-csv data/benchmark/v0.12.0/ja/threshold_sweep.csv \
		--thresholds 0.3 0.5 0.7 0.85 0.95 \
		--recall-floor 0.90

# #98: Predict + ORG-only confidence floor sweep on the latest hf_v02_tiny_aug_ext
# checkpoint. ORG precision (8.8%) is the dominant FP source — sweeping the ORG
# threshold while holding others at 0.0 isolates the precision lever from the
# well-tuned recall on PERSON/ADDRESS/DOB/BANK.
predict-scores-v12-aug-ext:
	uv run --extra hf python -m pleno_ner_training.predict_hf_with_scores \
		--model output/hf-ja-v02-tiny-aug-ext \
		--input data/benchmark/v0.12.0/ja/raw.json \
		--output data/benchmark/v0.12.0/ja/raw_with_scores_aug_ext.json

sweep-threshold-org-v12: predict-scores-v12-aug-ext
	uv run --extra hf python -m pleno_ner_training.sweep_org_threshold \
		--scored-predictions data/benchmark/v0.12.0/ja/raw_with_scores_aug_ext.json \
		--sweep-label ORGANIZATION \
		--thresholds 0.0 0.5 0.7 0.8 0.85 0.9 0.95 0.99 \
		--output-md data/benchmark/v0.12.0/ja/threshold_sweep_org.md \
		--output-json data/benchmark/v0.12.0/ja/threshold_sweep_org.json

# === HuggingFace Pipeline (en, tiny, browser-optimized) ===
# Mirrors the ja v02-tiny pipeline (#48) for English. The en side has only
# data/raw/en/generated.json (4818 docs) today vs ja's ~64.5k docs across
# generated + augmented + address_extra + ja-v02-extra; this scaffolding lets
# subsequent runs grow the en training set incrementally without bespoke
# Makefile edits each time.
#
# Tokenizer / model: microsoft/deberta-v3-xsmall (~22M params), the en
# analogue of ku-nlp/deberta-v2-tiny-japanese. Fast tokenizer, ONNX-friendly,
# small enough for browser inference via optimum onnxruntime-web.

# Mine PII-free hard-negative docs from English Wikipedia (CC-BY-SA 4.0) for
# ORG / person-name / address FP pressure. Same generated.json schema as the
# ja flow so it can be merged before convert.
mine-external-hardneg-en:
	uv run python -m pleno_ner_training.mine_external_hardneg \
		--dataset wikimedia/wikipedia \
		--config 20231101.en \
		--output data/raw/en/external_hardneg.json \
		--max-docs 4000

# Network-free smoke test for CI / local dev.
mine-external-hardneg-en-dry-run:
	uv run python -m pleno_ner_training.mine_external_hardneg \
		--output data/raw/en/external_hardneg.json \
		--max-docs 5 --dry-run

# en-v01-tiny: convert raw/en/generated.json → HF dataset (BIO labels, fast
# tokenizer alignment). PII_HINT_RE in convert_to_hf_dataset.py is a strict
# superset of ja patterns; en text simply does not match the ja-only regexes
# so they degrade to no-ops, keeping en zero-entity docs as-is (true negatives,
# not silently dropped).
convert-hf-en-v01-tiny:
	uv run python -m pleno_ner_training.convert_to_hf_dataset \
		--input data/raw/en/generated.json \
		--output data/hf/en-v01-tiny \
		--tokenizer microsoft/deberta-v3-xsmall

# en-v01-tiny + Wikipedia hardneg merged (ORG-FP pressure analogue of #64).
# Pre-merges generated.json + external_hardneg.json into a single tmp file
# so the converter sees one input. Inline python avoids adding a stand-alone
# merge module for a 6-line operation (mirrors convert-hf-v02-tiny-hardneg-ext).
convert-hf-en-v01-tiny-hardneg-ext: mine-external-hardneg-en
	uv run python -c "import json; \
a=json.load(open('data/raw/en/generated.json',encoding='utf-8')); \
b=json.load(open('data/raw/en/external_hardneg.json',encoding='utf-8')); \
json.dump(a+b,open('data/raw/en/generated_with_external.json','w',encoding='utf-8'),ensure_ascii=False)"
	uv run python -m pleno_ner_training.convert_to_hf_dataset \
		--input data/raw/en/generated_with_external.json \
		--output data/hf/en-v01-tiny-hardneg-ext \
		--tokenizer microsoft/deberta-v3-xsmall \
		--include-negatives

# en-v01-tiny + augmented (DOB / BANK / ORG / short-name diverse via
# augment_en_data) + Wikipedia hardneg (#64 analogue). Mirrors ja's
# convert-hf-v02-tiny-aug-ext. Required for the en model to break past the
# v0.10 collapse (ADDR/DOB/BANK 0% F1 with the wheel-installed en_ner_en).
convert-hf-en-v01-tiny-aug-ext: augment-en mine-external-hardneg-en
	uv run python -c "import json; \
a=json.load(open('data/raw/en/augmented.json',encoding='utf-8')); \
b=json.load(open('data/raw/en/external_hardneg.json',encoding='utf-8')); \
json.dump(a+b,open('data/raw/en/augmented_with_external.json','w',encoding='utf-8'),ensure_ascii=False)"
	uv run python -m pleno_ner_training.convert_to_hf_dataset \
		--input data/raw/en/augmented_with_external.json \
		--output data/hf/en-v01-tiny-aug-ext \
		--tokenizer microsoft/deberta-v3-xsmall \
		--include-negatives

# Train deberta-v3-xsmall fine-tune. RunPod GPU expected (CLAUDE.md: do not
# train on local machine). Hyperparameters mirror ja-v02-tiny so deltas
# isolate the data variable, not the optimizer.
train-hf-en-v01-tiny:
	uv run python -m pleno_ner_training.train_hf \
		--dataset data/hf/en-v01-tiny \
		--model microsoft/deberta-v3-xsmall \
		--output output/hf-en-v01-tiny \
		--fp16 --num-epochs 20 --batch-size 16 \
		--learning-rate 3e-5 --warmup-ratio 0.15

train-hf-en-v01-tiny-hardneg-ext:
	uv run python -m pleno_ner_training.train_hf \
		--dataset data/hf/en-v01-tiny-hardneg-ext \
		--model microsoft/deberta-v3-xsmall \
		--output output/hf-en-v01-tiny-hardneg-ext \
		--fp16 --num-epochs 20 --batch-size 16 \
		--learning-rate 3e-5 --warmup-ratio 0.15

# Train counterpart for the augmented + hardneg-ext dataset (en analogue of
# train-hf-v02-tiny-aug-ext). Same hyperparameters as the other tiny variants
# so deltas isolate the dataset variable, not the optimizer.
train-hf-en-v01-tiny-aug-ext:
	uv run python -m pleno_ner_training.train_hf \
		--dataset data/hf/en-v01-tiny-aug-ext \
		--model microsoft/deberta-v3-xsmall \
		--output output/hf-en-v01-tiny-aug-ext \
		--fp16 --num-epochs 20 --batch-size 16 \
		--learning-rate 3e-5 --warmup-ratio 0.15

export-onnx-en-v01-tiny:
	uv run python -m pleno_ner_training.export_onnx \
		--model output/hf-en-v01-tiny \
		--output output/hf-en-v01-tiny-onnx

# Push to HF Hub. Repo `0xhikae/en-ner-onnx` is the en analogue of
# `0xhikae/ja-ner-onnx` consumed by downstream HF token-classification backends.
push-hf-en-v01-tiny:
	uv run python -m pleno_ner_training.export_onnx \
		--model output/hf-en-v01-tiny \
		--output output/hf-en-v01-tiny-onnx \
		--push-to 0xhikae/en-ner-onnx

# End-to-end (assumes en raw + RunPod GPU). The `mine-external-hardneg-en`
# step in convert-hf-en-v01-tiny-hardneg-ext makes this network-dependent;
# split into two runs for offline reproducibility.
all-hf-en-v01-tiny: convert-hf-en-v01-tiny train-hf-en-v01-tiny export-onnx-en-v01-tiny push-hf-en-v01-tiny

# === 共通 ===

clean:
	rm -rf $(OUTPUT_DIR) $(DATA_RAW) $(DATA_PROCESSED)

all: generate convert train evaluate package
