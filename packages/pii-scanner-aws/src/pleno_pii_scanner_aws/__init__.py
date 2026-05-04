"""AWS S3 connector wheel for pleno-pii-scanner.

Public surface re-exported here so third-party code can `from
pleno_pii_scanner_aws import SPEC, S3Connector, S3Config, BucketSpec,
AccountSpec, AwsBaseIdentity, AwsSessionFactory` without reaching into
submodules.

Entry-point registration:

    [project.entry-points."pleno_pii_scanner.connectors"]
    aws-s3 = "pleno_pii_scanner_aws:SPEC"

The core CLI (`pleno-pii-scanner scan aws-s3`) calls
`pleno_pii_scanner.sources.create("aws-s3", config)` which in turn
invokes `SPEC.factory(config)` defined in `s3.py`.
"""

from pleno_pii_scanner_aws.auth import (
    AccountSpec,
    AwsBaseIdentity,
    AwsCredentials,
    AwsSessionFactory,
    HopRunner,
    StubHopRunner,
)
from pleno_pii_scanner_aws.s3 import (
    DEFAULT_CHUNK_BYTES,
    DEFAULT_MAX_DOC_BYTES,
    KIND,
    SPEC,
    BucketSpec,
    S3Config,
    S3Connector,
)
from pleno_pii_scanner_aws.sampling import (
    DEFAULT_RESERVOIR_SIZE,
    ReservoirSampler,
    SamplingDecision,
    reservoir_sample,
    should_sample,
)

__version__ = "0.1.0"

__all__ = [
    "AccountSpec",
    "AwsBaseIdentity",
    "AwsCredentials",
    "AwsSessionFactory",
    "BucketSpec",
    "DEFAULT_CHUNK_BYTES",
    "DEFAULT_MAX_DOC_BYTES",
    "DEFAULT_RESERVOIR_SIZE",
    "HopRunner",
    "KIND",
    "ReservoirSampler",
    "S3Config",
    "S3Connector",
    "SPEC",
    "SamplingDecision",
    "StubHopRunner",
    "__version__",
    "reservoir_sample",
    "should_sample",
]
