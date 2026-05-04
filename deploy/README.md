# Deploy artifacts for `pleno-pii-scanner`

This directory ships the production deployment surface for the
enterprise scanner CLI:

```
deploy/
├── docker/
│   └── scanner.Dockerfile         # multi-arch CLI image (CronJob payload)
├── k8s/
│   ├── cronjob.yaml               # raw manifest example
│   ├── serviceaccount.yaml        # SA + PVC + ConfigMap + Secret stub
│   └── networkpolicy.yaml         # default-deny egress + public-HTTPS allow
└── helm/
    └── pleno-pii-scanner/         # Helm chart wrapping the same shape
```

The FastAPI server image (`/Dockerfile` at repo root) is unrelated —
that one targets fly.io for the public anonymization endpoint. This
directory is for operators running the scanner CLI **inside their own
network**, on Kubernetes, against their own data sources.

## Build the image

```sh
docker buildx build \
  -f deploy/docker/scanner.Dockerfile \
  -t ghcr.io/plenoai/pii-scanner:dev \
  --platform linux/amd64,linux/arm64 \
  --push .
```

## Helm install

```sh
helm install pii-scanner deploy/helm/pleno-pii-scanner \
  --namespace security --create-namespace \
  --values my-overrides.yaml
```

Minimum `my-overrides.yaml`:

```yaml
schedule: "0 2 * * *"
scanConfig: |
  [github]
  app_id = "123456"
  private_key_env = "GITHUB_APP_PRIVATE_KEY"
  organizations = ["acme-corp"]

secrets:
  findingsMasterKey: "<32-byte base64>"
  githubAppPrivateKey: |
    -----BEGIN RSA PRIVATE KEY-----
    ...
    -----END RSA PRIVATE KEY-----

# If you've enabled a self-managed connector (GitLab/Bitbucket Server,
# Azure DevOps Server, internal PostgreSQL, etc.), allow the relevant
# private subnets through NetworkPolicy:
networkPolicy:
  allowedPrivateCIDRs:
    - 10.20.0.0/16

# Cloud auth — pick the right SA annotation for your cluster:
serviceAccount:
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::123456789012:role/pii-scanner
```

## Cloud workload identity

Each cloud connector uses the platform-native auth path; bind the
ServiceAccount to:

| connector            | binding                                |
|----------------------|----------------------------------------|
| pii-scanner-aws      | IRSA (EKS) — `eks.amazonaws.com/role-arn` |
| pii-scanner-gcs      | GKE Workload Identity — `iam.gke.io/gcp-service-account` |
| pii-scanner-azure-*  | Azure Workload Identity — `azure.workload.identity/client-id` |

For the long-lived-token connectors (GitHub App, Slack xoxa, Notion
integration, etc.), populate the corresponding env-var secret and
reference it from the connector's TOML config.
