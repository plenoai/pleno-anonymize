"""Simula-inspired mechanism-design synthetic data pipeline.

Stages:
    taxonomy        Global Diversification (#148)
    meta_prompts    Local Diversification (#149)
    complexify      Independent difficulty axis (#150)
    critics         Dual-critic verification (#151)

Each stage emits a stable on-disk artefact under packages/training/data/
so downstream stages can re-run without recomputing upstream.
"""
