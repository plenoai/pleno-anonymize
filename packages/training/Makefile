.PHONY: generate convert train train-cnn evaluate package clean

MODEL ?= gpt-5.4-mini
DOCS_PER_TEMPLATE ?= 20
BATCHES_PER_TEMPLATE ?= 50
MAX_WORKERS ?= 5
GPU_ID ?= 0

DATA_RAW = data/raw
DATA_PROCESSED = data/processed
OUTPUT_DIR = output
MODELS_DIR = ../models

generate:
	uv run python -m pleno_ner_training.generate_data \
		--output-dir $(DATA_RAW) \
		--docs-per-template $(DOCS_PER_TEMPLATE) \
		--batches-per-template $(BATCHES_PER_TEMPLATE) \
		--model $(MODEL) \
		--max-workers $(MAX_WORKERS)

convert:
	uv run python -m pleno_ner_training.convert_to_docbin \
		--input $(DATA_RAW)/generated.json \
		--output-dir $(DATA_PROCESSED)

train:
	uv run python -m spacy train configs/train_transformer.cfg \
		--output $(OUTPUT_DIR) \
		--paths.train $(DATA_PROCESSED)/train.spacy \
		--paths.dev $(DATA_PROCESSED)/dev.spacy \
		--gpu-id $(GPU_ID)

train-cnn:
	uv run python -m spacy train configs/train_cnn.cfg \
		--output $(OUTPUT_DIR) \
		--paths.train $(DATA_PROCESSED)/train.spacy \
		--paths.dev $(DATA_PROCESSED)/dev.spacy

evaluate:
	uv run python -m pleno_ner_training.evaluate \
		--model $(OUTPUT_DIR)/model-best \
		--test-data $(DATA_PROCESSED)/test.spacy \
		--output-json $(OUTPUT_DIR)/scores.json

package:
	mkdir -p $(MODELS_DIR)
	uv run python -m spacy package $(OUTPUT_DIR)/model-best $(MODELS_DIR) \
		--name ner_ja \
		--version 0.1.0 \
		--build wheel

clean:
	rm -rf $(OUTPUT_DIR) $(DATA_RAW) $(DATA_PROCESSED)

all: generate convert train evaluate package
