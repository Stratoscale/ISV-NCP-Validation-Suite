# zCompute Provider — NVIDIA NCP Validation Suite

NCP Validation Suite provider for Zadara's zCompute platform.

zCompute exposes AWS-compatible EC2, IAM, and STS API endpoints. All AWS scripts
are reused via `ssl_wrapper.py` (network suite) or direct boto3 with per-service
endpoint overrides (VM/K8s suites). The main differences from AWS are documented
in `COMPATIBILITY_REPORT.md`.

## Quick Start

### 1. Set environment variables

All credentials live in `~/suite-zcompute.env` Source it before every run:

```bash
source ~/suite-zcompute.env
```

### 2. Install dependencies

```bash
cd ISV-NCP-Validation-Suite
uv sync
```

### 3. Run a suite

```bash
# VM suite (GPU lifecycle + NIM)
uv run isvctl test run -f isvctl/configs/providers/zcompute/config/vm.yaml

# Network suite
uv run isvctl test run -f isvctl/configs/providers/zcompute/config/network.yaml

# Kubernetes / EKS-D suite
uv run isvctl test run -f isvctl/configs/providers/zcompute/config/k8s.yaml
```

### 4. Clean up stale resources after failed runs

```bash
cd isvctl/configs/providers/zcompute/config
python3 ../scripts/network/cleanup_stale_resources.py
```
## Directory Structure

```
providers/zcompute/
├── config/          ← Suite configs (our overrides of NVIDIA's suite definitions)
├── scripts/
│   ├── vm/          ← launch_instance.py, start_instance.py, reboot_instance.py, etc.
│   ├── network/     ← ssl_wrapper.py, create_vpc.py, vpc_crud_test.py, cleanup_stale_resources.py
│   ├── control-plane/
│   ├── k8s/
│   └── common/      ← ec2.py (load_nvidia_modules, setup_gpu_dependencies, EIP utils)
├── (suite.env)      ← Credentials file — keep at ~/suite-zcompute.env, not here
├── CLUSTER-SETUP.md ← EKS-D cluster setup runbook
└── COMPATIBILITY_REPORT.md ← Detailed API compatibility notes and test history
```