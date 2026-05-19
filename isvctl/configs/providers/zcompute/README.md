# zcompute Provider

NCP Validation Suite provider for Zadara zcompute clusters.

zcompute exposes AWS-compatible EC2, IAM, and STS API endpoints, so the
existing AWS scripts can be reused with minimal changes. The key difference
is that all boto3 clients must be pointed at the zcompute endpoint URL
instead of the real AWS endpoints.

## Quick Start

### 1. Set environment variables

```bash
export ZCOMPUTE_ENDPOINT=https://api.yourzone.zadarastorage.com
export AWS_ACCESS_KEY_ID=<your_zcompute_access_key>
export AWS_SECRET_ACCESS_KEY=<your_zcompute_secret_key>
export AWS_REGION=<your_zcompute_region>
```

### 2. Install dependencies

```bash
cd ISV-NCP-Validation-Suite
uv sync
```

### 3. Run the control-plane suite (start here)

```bash
uv run isvctl test run -f isvctl/configs/providers/zcompute/config/control-plane.yaml
```

### 4. Dry-run (validates config, no API calls)

```bash
uv run isvctl test run -f isvctl/configs/providers/zcompute/config/control-plane.yaml --dry-run
```

## Available Test Suites

| Suite | Config | Status |
|-------|--------|--------|
| Control Plane | `config/control-plane.yaml` | Ready |
| IAM | `config/iam.yaml` | TODO |
| Network | `config/network.yaml` | TODO |
| VM | `config/vm.yaml` | TODO |
| Kubernetes | `config/k8s.yaml` | TODO |
| Bare Metal | `config/bare_metal.yaml` | TODO |
| Image Registry | `config/image-registry.yaml` | TODO |

## Known Differences from AWS

| Feature | AWS | zcompute |
|---------|-----|----------|
| Tenant API | `resource-groups` | Not supported — implemented via IAM Groups |
| Services | ec2, s3, iam, sts, eks, ... | ec2, iam, sts (S3 may use separate endpoint) |
| AMI format | `ami-xxxxxxxx` | zcompute image IDs (TBD) |
| Instance types | `g4dn.xlarge`, `p3.2xlarge`, ... | zcompute flavor names (TBD) |
| EKS | Native | Not supported — requires custom K8s provider |

## Endpoint Configuration

All scripts read the endpoint from environment variables via
`scripts/common/client.py`:

1. `ZCOMPUTE_ENDPOINT` (preferred) — e.g. `https://api.yourzone.zadarastorage.com`
2. `AWS_ENDPOINT_URL` — standard boto3 universal override (boto3 >= 1.28)

## Adding More Suites

When adding the next suite (e.g. IAM or Network):

1. Copy the AWS config from `providers/aws/config/<suite>.yaml` to `providers/zcompute/config/<suite>.yaml`
2. Update the `import:` path and `command:` paths
3. Copy the AWS scripts to `providers/zcompute/scripts/<suite>/`
4. Replace `boto3.client(...)` calls with `get_client(...)` from `common/client.py`
5. Adapt any AWS-specific resource types (instance types, AMIs, etc.) once the zcompute equivalents are known
