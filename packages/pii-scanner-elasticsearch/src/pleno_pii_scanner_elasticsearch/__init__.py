"""Elasticsearch / OpenSearch SourceConnector for pleno-pii-scanner."""

from pleno_pii_scanner_elasticsearch.connector import (
    ElasticsearchConfig,
    ElasticsearchConnector,
    SPEC,
)

__all__ = ["ElasticsearchConfig", "ElasticsearchConnector", "SPEC"]
