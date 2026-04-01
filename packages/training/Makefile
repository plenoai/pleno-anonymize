.PHONY: generate convert train train-cnn evaluate benchmark-external benchmark-huggingface package clean \
       generate-en augment-en convert-en train-en evaluate-en benchmark-external-en package-en all-en \
       benchmark-v02-generate benchmark-v02-generate-en benchmark-v02-evaluate benchmark-v02-evaluate-en \
       benchmark-v03-generate benchmark-v03-generate-en benchmark-v03-evaluate benchmark-v03-evaluate-en

MODEL ?= gpt-5.4-nano
DOCS_PER_TEMPLATE ?= 20
BATCHES_PER_TEMPLATE ?= 50
MAX_WORKERS ?= 5
GPU_ID ?= 0
DOTENVX = dotenvx run -f ../../.env --

DATA_RAW = data/raw
DATA_PROCESSED = data/processed
OUTPUT_DIR = output
MODELS_DIR = ../models

# === 日本語 (既存) ===

generate:
	$(DOTENVX) uv run python -m pleno_ner_training.generate_data \
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
	uv run python -m spacy train configs/train_transformer.cfg \
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
	$(DOTENVX) uv run python -m pleno_ner_training.generate_data \
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
		--augment-count 1000

convert-v02:
	uv run python -m pleno_ner_training.convert_to_docbin \
		--language ja \
		--input $(DATA_RAW)/ja-v02/generated.json \
		--augment $(DATA_RAW)/ja-v02/augmented.json \
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
	$(DOTENVX) uv run python -m pleno_ner_training.generate_data \
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
		--augment-count 1000

convert-en:
	uv run python -m pleno_ner_training.convert_to_docbin \
		--language en \
		--input $(DATA_RAW)/en/generated.json \
		--augment $(DATA_RAW)/en/augmented.json \
		--output-dir $(DATA_PROCESSED)/en

train-en:
	uv run python -m spacy train configs/train_transformer_en.cfg \
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
	$(DOTENVX) uv run python -m pleno_ner_training.generate_benchmark \
		--language ja \
		--docs-per-template $(DOCS_PER_TEMPLATE) \
		--batches-per-template $(BENCHMARK_BATCHES) \
		--model $(MODEL) \
		--max-workers $(MAX_WORKERS)

benchmark-v02-generate-en:
	$(DOTENVX) uv run python -m pleno_ner_training.generate_benchmark \
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
		--model $(OUTPUT_DIR)/en-transformer/model-best \
		--language en --version v0.2.0

benchmark-v03-generate:
	$(DOTENVX) uv run python -m pleno_ner_training.generate_benchmark \
		--version v0.3.0 \
		--language ja \
		--docs-per-template $(DOCS_PER_TEMPLATE) \
		--batches-per-template $(BENCHMARK_BATCHES) \
		--model $(MODEL) \
		--max-workers $(MAX_WORKERS)

benchmark-v03-generate-en:
	$(DOTENVX) uv run python -m pleno_ner_training.generate_benchmark \
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
		--model $(OUTPUT_DIR)/en-transformer/model-best \
		--language en --version v0.3.0

# === 共通 ===

clean:
	rm -rf $(OUTPUT_DIR) $(DATA_RAW) $(DATA_PROCESSED)

all: generate convert train evaluate package
