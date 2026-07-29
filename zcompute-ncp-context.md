# Zadara zCompute — NVIDIA NCP Certification: Full Context

## What This Project Is

NVIDIA's ISV-NCP-Validation-Suite validates cloud providers for GPU workload certification.
Zadara is certifying **zCompute** (EC2-compatible cloud) using a forked suite with a full
compatibility layer for zCompute's API quirks.

## Repositories & Locations

| Location | Path |
|---|---|
| Mac repo | `~/Documents/terraforms/ISV-NCP-Validation-Suite` |
| Toolbox repo | `~/ISV-NCP-Validation-Suite` |
| GitHub fork | `github.com:amit-orenshtein-zadara/ISV-NCP-Validation-Suite` |
| Upstream | `github.com:NVIDIA/ISV-NCP-Validation-Suite` |
| Confluence | `zadara.atlassian.net/wiki/spaces/ZEQ/pages/3846733974` |

## Environment Setup

```bash
source ~/suite.env
```

Key vars: `AWS_ENDPOINT_URL_EC2`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_REGION=symphony`, `ZCOMPUTE_BASE_URL=https://172.29.0.20`,
`ZCOMPUTE_TEST_AMI_ID`, `ZCOMPUTE_TEST_INSTANCE_TYPE`, `NGC_API_KEY`, `KUBECONFIG`.

**New zCompute lab** (bare metal API testing): `ZCOMPUTE_BASE_URL=https://172.16.10.140`

## How to Run Suites

```bash
cd ~/ISV-NCP-Validation-Suite
source ~/suite.env

uv run isvctl test run -f isvctl/configs/providers/zcompute/config/vm.yaml -v 2>&1 | tee /tmp/ncp-vm-run<N>.log
uv run isvctl test run -f isvctl/configs/providers/zcompute/config/network.yaml -v 2>&1 | tee /tmp/ncp-net-run<N>.log
uv run isvctl test run -f isvctl/configs/providers/zcompute/config/k8s.yaml -v 2>&1 | tee /tmp/ncp-k8s-run<N>.log
uv run isvctl test run -f isvctl/configs/providers/zcompute/config/k8s_nccl_only.yaml -v 2>&1 | tee /tmp/ncp-k8s-nccl-run<N>.log
```

## Current Certification Status

| Suite | Status | Notes |
|---|---|---|
| Control Plane | PARTIAL PASS | `UpdateAccessKey` not implemented — NK-19406 |
| IAM | FULL PASS | |
| VM | PARTIAL PASS | GpuCheck ✓ driver 575.57.08, NIM passing |
| Network | PARTIAL PASS | NACLs not supported (critical gap) |
| K8s (EKS-D) | IN PROGRESS | K8sNcclMultiNodeWorkload blocking — avg busbw 17.60 < 30 GB/s |
| Security | NOT STARTED | |
| Image Registry | NOT STARTED | No S3 endpoint |
| Bare Metal | NOT STARTED — next focus | See below |

## Bare Metal Suite — Current State

### Test List (v0.6.8 — 37 tests)

This is the active target version. Full details in `Baremetal-Test-Summary.xlsx` in the repo root.

**Passed on zCompute (21 — shared with VM suite, already working):**
CloudInitCheck, ConnectivityCheck, ContainerRuntimeCheck, CpuInfoCheck, DriverCheck,
GpuCheck, HostSoftwareCheck, InstanceListCheck, InstanceRebootCheck, InstanceStartCheck,
InstanceStateCheck, InstanceStopCheck, InstanceTagCheck, NimHealthCheck, NimInferenceCheck,
NimModelCheck, OsCheck, PciBusCheck, StableIdentifierCheck, StepSuccessCheck, VcpuPinningCheck

**Not yet run (16 — bare-metal-only or not covered by VM suite):**
BmCudaVersion, BmDriverInstalled, BmDriverVersion, BmGpuComputeCapability, BmGpuDetection,
BmGpuHealth, EthernetCheck, GpuStressCheck, InfiniBandCheck, InstancePowerCycleCheck,
NcclCheck, NvlinkCheck, SerialConsoleCheck, SerialConsoleRetentionCheck, TopologyPlacementCheck, TrainingCheck

### AWS EC2 API Calls Required (25 total, v0.6.8)

These are the exact calls extracted from the AWS bare metal provider scripts:

```
RunInstances, DescribeInstances, TerminateInstances, StopInstances, StartInstances,
RebootInstances, DescribeInstanceTypeOfferings, GetConsoleOutput, DescribeImages,
CreatePlacementGroup, DescribePlacementGroups, DeletePlacementGroup,
CreateKeyPair, DescribeKeyPairs, DeleteKeyPair,
CreateSecurityGroup, AuthorizeSecurityGroupIngress, DescribeSecurityGroups, DeleteSecurityGroup,
DescribeVpcs, DescribeSubnets,
CreateVolume, AttachVolume, DetachVolume, DeleteVolume
```

Notes:
- Tags are set via `TagSpecifications` in `RunInstances` (not a separate `CreateTags` call)
- Tags are read via `DescribeInstances` (not a separate `DescribeTags` call)
- `CreateVolume/Attach/Detach/DeleteVolume` only used by `reinstall_instance` step (skipped by default)
- All calls go through the ssl_wrapper SSL patch (zCompute self-signed cert)
- All waiters replaced with poll loops (zCompute doesn't support boto3 waiters)

## NEXT TASK: Bare Metal API Compatibility Testing on zCompute

### Goal

Build a zCompute bare metal provider (like `providers/aws/` but for zCompute) by:
1. Testing which of the 25 API calls work on zCompute as-is
2. Documenting what's broken/missing
3. Applying the same fix patterns already used in the VM/Network suites

### Probe Script

A probe script already exists at `outputs/probe_zcompute_apis.py` (Mac working dir).
It tests all non-destructive calls and lightweight create/delete pairs.
It was interrupted before running — start by running it:

```bash
ZCOMPUTE_BASE_URL=https://172.16.10.140 \
AWS_ACCESS_KEY_ID=1ed6ac742d0b410a9728a3f4e90e0d92 \
AWS_SECRET_ACCESS_KEY=ab2ec02bdb60409aaedbd2e631700a9e \
AWS_REGION=symphony \
python3 outputs/probe_zcompute_apis.py
```

Then Phase 2: test `RunInstances` + lifecycle with a small instance (need AMI ID from Phase 1 output).

### Expected Fix Patterns (based on existing VM/Network quirks)

These are confirmed quirks from the VM suite that will almost certainly apply to bare metal too:

| API Call | Expected Issue | Known Fix |
|---|---|---|
| `RunInstances` | Empty `Instances[]` response | Fallback: poll by key name + LaunchTime |
| `RunInstances` | `TagSpecifications` not supported | Strip; use post-creation `create_tags` |
| `DescribeInstances` | Ignores `InstanceIds` filter | Post-filter in Python |
| All waiters | Not supported | Replace with `_ZComputeInstanceWaiter` poll loop |
| All HTTPS calls | SSL cert mismatch | `ssl_wrapper.py` botocore patch |
| `TerminateInstances` | `InternalServerError` | Retry with backoff |
| Instance state | Stays `shutting-down` long | Accept as terminated |
| Public IP | Not auto-assigned | Allocate EIP + associate |
| `CreateKeyPair` | RSA PKCS#1 format | Convert to OpenSSH with ssh-keygen |
| `CreatePlacementGroup` | Likely `UnsupportedOperation` | zCompute has no placement groups |
| `GetConsoleOutput` | Unknown — needs testing | May not be implemented |
| `DescribeInstanceTypeOfferings` | May return empty or error | Needs testing |

### Where New Files Should Go

```
isvctl/configs/providers/zcompute/
├── config/
│   └── bare_metal.yaml          ← new (import suites/bare_metal.yaml + zcompute commands)
└── scripts/
    └── bare_metal/
        ├── launch_instance.py   ← new (based on aws version + zcompute patches)
        ├── describe_instance.py ← new
        ├── stop_instance.py     ← new
        ├── start_instance.py    ← new
        ├── reboot_instance.py   ← new
        ├── power_cycle_instance.py ← new
        ├── teardown.py          ← new
        └── verify_terminated.py ← new
```

Most of these will be thin wrappers that run the AWS scripts through `ssl_wrapper.py`
with zCompute-specific patches (same pattern as network suite).

## Project Structure

```
ISV-NCP-Validation-Suite/
├── isvctl/configs/
│   ├── suites/              ← NVIDIA canonical (don't modify)
│   └── providers/
│       ├── aws/             ← NVIDIA reference (don't modify)
│       └── zcompute/        ← ALL OUR WORK
│           ├── config/      ← vm.yaml, network.yaml, k8s.yaml, k8s_nccl_only.yaml
│           └── scripts/
│               ├── vm/      ← launch_instance.py, start_instance.py, etc.
│               ├── network/ ← ssl_wrapper.py, create_vpc.py, cleanup_stale_resources.py
│               └── common/  ← ec2.py (get_client, setup_gpu_dependencies)
├── isvtest/src/isvtest/
│   ├── workloads/
│   │   ├── nccl_allreduce_mpijob.yaml  ← NCCL MPIJob manifest (main tuning file)
│   │   └── nccl_common.py              ← parses avg_bus_bw_gbps
│   └── validations/                    ← GpuCheck, VpcCrudCheck, etc.
└── Baremetal-Test-Summary.xlsx         ← full bare metal test catalog (v0.6.8, 37 tests)
```

## zCompute API Quirks (confirmed fixes in ssl_wrapper.py)

- **SSL**: botocore patched `verify=False`
- **No boto3 waiters**: replaced with poll loops
- **`describe_instances` ignores filters**: post-filter by InstanceId/VpcId in Python
- **`run_instances` returns empty `Instances[]`**: fallback poll by key name + LaunchTime
- **`TagSpecifications` not supported**: stripped, replaced with post-creation `create_tags`
- **`TerminateInstances` InternalServerError**: retry with backoff
- **`shutting-down` state lingers**: accepted as terminated
- **No public IP auto-assigned**: EIP allocated + associated
- **NACLs return AuthFailure**: skipped (zCompute is SG-only) — CRITICAL cert gap
- **Single AZ only**: `symphony` — no multi-AZ

## EKS-D Cluster (K8s suite)

- EKS-D v1.35.2 on zCompute 172.29.0.20
- 2 GPU workers: each 8× H100 SXM5 80GB, driver 575.57.08, CUDA 12.9
- NCCL issue: avg busbw 17.60 GB/s < 30 threshold — root causes identified (see skill)

## K8sNcclMultiNodeWorkload — Blocking Issue

**Two root causes:**
1. Only mlx5_0 and mlx5_3 carry inter-node traffic (mlx5_1/2 QP fails silently)
2. Avg busbw metric averages all 22 message sizes — large msgs already hit 57–58 GB/s

**Pending:**
1. `ib_send_bw -d mlx5_1 --gid-index=3` between nodes
2. `NCCL_NET_GDR_LEVEL=2` (GPUDirect RDMA, nvidia_peermem IS loaded)
3. Determine if NCP spec requires avg or large-message busbw

## Known Gaps

| Gap | Severity |
|---|---|
| `UpdateAccessKey` not implemented | CRITICAL — NK-19406 |
| NACLs not supported | CRITICAL |
| K8s NCCL avg busbw < 30 GB/s | CRITICAL — in progress |
| No S3 endpoint (Image Registry) | HIGH |
| OIDC not configured (K8s) | MEDIUM |
| NetworkPolicy not enforced (Cilium) | MEDIUM |
