# pleno-pii-scanner-oci

Daemon-less OCI registry SourceConnector for [pleno-pii-scanner](https://github.com/plenoai/pleno-anonymize), implementing ADR-0007 §15.

## Why daemon-less

`docker-py` requires a Unix-socket mount of `/var/run/docker.sock`, which is forbidden in:
- GitHub Actions runners (no daemon)
- Kubernetes pods without `hostPath` (every CIS-benchmarked cluster)
- gVisor / Kata sandboxes (no socket forwarding)
- Lambda / Cloud Run (no privileged container)

This connector talks the [OCI Distribution Spec v1.1](https://github.com/opencontainers/distribution-spec/blob/main/spec.md) directly over HTTPS. No daemon. No `runc`. Works in every sandbox the operator can `pip install` into.

## Highest-priority finding source

Per ADR-0007 §15: **40-60% of registry findings are in `config.Env`, `Cmd`, `Entrypoint`, and image history**, not in layer file contents. Operators ship secrets baked into image build args far more often than into committed source files. This connector emits the config + history as the *first* document for every image so the regex / NER pipeline sees it before paying the layer-streaming cost.

## Layer dedup

Layer digests are content-addressed. The same `python:3.12-slim` base layer appears in hundreds of derived images. The connector caches scanned layer digests across the run so a 1000-image org scan only walks each unique base layer once.

## Streaming layer scan

Every layer is streamed via `tarfile.open(fileobj=..., mode='r|gz')` (or `mode='r|'` over zstandard's stream decompressor). Members are yielded one at a time and discarded — RSS stays under 1 GB regardless of layer size.

## Install

```bash
pip install pleno-pii-scanner pleno-pii-scanner-oci
```

## Use

```toml
# oci-prod-images.toml
references = [
    "ghcr.io/acme/api:v1.2.3",
    "registry.example.com/internal/billing@sha256:...",
]
default_platform = "linux/amd64"
max_layer_bytes = 524288000   # 500 MiB
```

```bash
pleno-pii-scanner scan run oci --config oci-prod-images.toml
```
