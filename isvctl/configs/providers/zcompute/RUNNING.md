# Running the NCP Validation Suite on zCompute

This guide covers everything needed to run each test suite against a Zadara zCompute cluster, from credential setup through uploading results to the NVIDIA portal.

---

## Prerequisites

Install once on the machine you'll run from:

- **Python 3.12+** and **[uv](https://docs.astral.sh/uv/)** (package manager)
- **kubectl** — required for the K8s suite
- **jq** — required for the K8s suite setup step
- **helm** — required for NIM workload tests in the K8s and VM suites

Then install the project:

```bash
cd ISV-NCP-Validation-Suite
uv sync
```

---

## Credentials file

Create `~/suite-zcompute.env` (outside the repo, gitignored) and source it before every run. The file has two sections: a base block shared by all suites, and suite-specific blocks below.

```bash
source ~/suite-zcompute.env
```

---

## Network Suite

**Config:** `isvctl/configs/providers/zcompute/config/network.yaml`

### Environment variables

```bash
# ── Required ──────────────────────────────────────────────────────────────────
export ZCOMPUTE_BASE_URL=https://<zcompute-ip>
export AWS_ENDPOINT_URL_EC2=https://<zcompute-ip>/api/v2/aws/ec2/
export AWS_ACCESS_KEY_ID=<key>
export AWS_SECRET_ACCESS_KEY=<secret>
export AWS_REGION=symphony

# ── Required for VM-launching tests (dhcp_ip, stable_ip, floating_ip) ─────────
export ZCOMPUTE_TEST_AMI_ID=<ami-id>             # Ubuntu image ID in your zCompute project
export ZCOMPUTE_TEST_INSTANCE_TYPE=z2.3large     # Any available VM type in your project

# ── Optional (VPC peering fallback via symp CLI) ───────────────────────────────
export SYMP_URL=https://<zcompute-ip>
export SYMP_USER=<username>
export SYMP_DOMAIN=<domain>
export SYMP_PASSWORD=<password>
export SYMP_PROJECT=<project-name>
```

> `ZCOMPUTE_TEST_AMI_ID` and `ZCOMPUTE_TEST_INSTANCE_TYPE` are **required** for the DHCP, stable-IP, and floating-IP tests. The suite will raise an error at runtime if they are unset.

### Run

```bash
uv run isvctl test run -f isvctl/configs/providers/zcompute/config/network.yaml 2>&1 | tee /tmp/network.log
```

---

## VM Suite

**Config:** `isvctl/configs/providers/zcompute/config/vm.yaml`

### What the suite does automatically

The launch step SSHes into the VM and installs Docker, NVIDIA Container Toolkit, and the CUDA toolkit (takes ~20 min on first launch). You do not need to prepare anything on the VM beyond using a supported Ubuntu base image. The base image must have the NVIDIA driver already installed and loaded.

The NIM deployment step (`deploy_nim`) logs into `nvcr.io` using your `NGC_API_KEY`, pulls the NIM container image (~30 min on first pull), and runs it. No pre-pull required.

**Total runtime: ~90 min** (launch + GPU setup + stop/start/reboot cycle + NIM deploy + teardown).

### Environment variables

```bash
# ── Required ──────────────────────────────────────────────────────────────────
export ZCOMPUTE_BASE_URL=https://<zcompute-ip>
export AWS_ACCESS_KEY_ID=<key>
export AWS_SECRET_ACCESS_KEY=<secret>
export AWS_REGION=symphony
export NGC_API_KEY=<ngc-api-key>     # Required for NIM deployment tests

# ── Optional ──────────────────────────────────────────────────────────────────
export ZCOMPUTE_TEST_AMI_ID=<ami-id>         # Required: Ubuntu GPU image ID in your zCompute project
export ZCOMPUTE_VM_INSTANCE_ID=<instance-id> # Reuse an existing running instance (skip launch)
export ZCOMPUTE_VM_KEY_FILE=/path/to/key.pem # Required when reusing an existing instance
```

> When using `ZCOMPUTE_VM_INSTANCE_ID` to reuse an existing instance, Docker and CUDA must already be installed on it (the launch step is skipped).

### Run

```bash
uv run isvctl test run -f isvctl/configs/providers/zcompute/config/vm.yaml 2>&1 | tee /tmp/vm.log
```

---

## Kubernetes Suite

**Config:** `isvctl/configs/providers/zcompute/config/k8s.yaml`

The cluster must be running before you start — the suite does **not** provision it. See `CLUSTER-SETUP.md` for the full cluster setup runbook.

### Cluster topology expected by the suite

| Node | Count | GPU |
|------|-------|-----|
| control-plane | 1 | — |
| CPU workers | 2 | — |
| GPU workers | 2 | 8× H100 SXM5 each |

- GPU Operator deployed in `zadara-system` namespace
- `nvidia` RuntimeClass present
- `nvidia.com/gpu` capacity: 8 per node, 16 total

### Required images pre-pulled on GPU nodes

These are large and must be pulled before running the suite (they will time out otherwise):

```
nvcr.io/nvidia/hpc-benchmarks:25.04
nvcr.io/nvidia/pytorch:25.04-py3
nvcr.io/nim/meta/llama-3.2-1b-instruct:latest
```

### Environment variables

```bash
# ── Required ──────────────────────────────────────────────────────────────────
export KUBECONFIG=/path/to/config-eksd-<cluster>

# ── Required for NIM workload tests ───────────────────────────────────────────
export NGC_API_KEY=<ngc-api-key>
```

### Run

```bash
uv run isvctl test run -f isvctl/configs/providers/zcompute/config/k8s.yaml 2>&1 | tee /tmp/k8s.log
```

Partial runs (useful for re-running after a failure):

```bash
# Run only the NCCL benchmark tests
uv run isvctl test run -f isvctl/configs/providers/zcompute/config/k8s_nccl_only.yaml

# Run only CNCF conformance
uv run isvctl test run -f isvctl/configs/providers/zcompute/config/k8s_cncf_only.yaml
```

---

## IAM Suite

**Config:** `isvctl/configs/providers/zcompute/config/iam.yaml`

```bash
export ZCOMPUTE_BASE_URL=https://<zcompute-ip>
export AWS_ACCESS_KEY_ID=<key>
export AWS_SECRET_ACCESS_KEY=<secret>
export AWS_REGION=symphony
```

```bash
uv run isvctl test run -f isvctl/configs/providers/zcompute/config/iam.yaml 2>&1 | tee /tmp/iam.log
```

---

## Control Plane Suite

**Config:** `isvctl/configs/providers/zcompute/config/control-plane.yaml`

```bash
export ZCOMPUTE_BASE_URL=https://<zcompute-ip>
export AWS_ACCESS_KEY_ID=<key>
export AWS_SECRET_ACCESS_KEY=<secret>
export AWS_REGION=symphony

# Required for the disable_access_key test (uses symp CLI as fallback):
export SYMP_URL=https://<zcompute-ip>
export SYMP_USER=<username>
export SYMP_DOMAIN=<domain>
export SYMP_PASSWORD=<password>
```

```bash
uv run isvctl test run -f isvctl/configs/providers/zcompute/config/control-plane.yaml 2>&1 | tee /tmp/control-plane.log
```

---

## Security Suite

**Config:** `isvctl/configs/providers/zcompute/config/security.yaml`

```bash
export ZCOMPUTE_BASE_URL=https://<zcompute-ip>
export AWS_ACCESS_KEY_ID=<key>
export AWS_SECRET_ACCESS_KEY=<secret>
export AWS_REGION=symphony
```

```bash
uv run isvctl test run -f isvctl/configs/providers/zcompute/config/security.yaml 2>&1 | tee /tmp/security.log
```

---

## Uploading Results to the NVIDIA Portal

Add `--lab-id <N>` to any run command to upload results automatically after the suite completes. The lab ID is the numeric ID of your registered lab on the NVIDIA ISV Lab Service portal.

You also need these credentials in your environment (provided by NVIDIA):

```bash
export ISV_CLIENT_ID=<client-id>
export ISV_CLIENT_SECRET=<client-secret>
export ISV_SERVICE_ENDPOINT=<endpoint-url>
export ISV_SSA_ISSUER=<issuer-url>
```

### Example

```bash
uv run isvctl test run \
  -f isvctl/configs/providers/zcompute/config/network.yaml \
  --lab-id 37 \
  2>&1 | tee /tmp/network-upload.log
```

On success, the output will show:

```
Test run created successfully
  Test Run ID: <id>
  URL: https://public-api.ncp-isv-validation-labs.nvidia.com/v1/labs/37/test-runs/<id>
...
Test results uploaded successfully
  Status: SUCCESS
```

---

## Useful CLI Options

| Flag | Description |
|------|-------------|
| `--lab-id <N>` | Upload results to NVIDIA portal after run |
| `--phase setup\|test\|teardown` | Run only one phase |
| `-v` | Verbose output |
| `2>&1 \| tee /tmp/run.log` | Save full output to a log file |

---

## Cleaning Up Stale Resources

If a run fails midway, resources may be left in your zCompute project. Clean them up with:

```bash
cd isvctl/configs/providers/zcompute/config
python3 ../scripts/network/cleanup_stale_resources.py
```
