# zcompute × NVIDIA NCP Validation Suite — Compatibility Report

**Last updated:** 2026-05-13 (run 9)
**Author:** Amit Orenshtein, Zadara Storage
**Suite version:** NVIDIA ISV-NCP-Validation-Suite (experimental preview)
**zcompute clusters under test:**
- `172.16.10.110` — non-GPU cluster (dry-run / compatibility probing)
- `172.29.0.20` — HGX GPU cluster (target for full NCP validation run)

---

## What We Are Doing

NVIDIA's NCP (NVIDIA Cloud Partner) certification program validates that a cloud
provider's infrastructure can reliably run NVIDIA GPU workloads — AI training,
inference, and GPU-accelerated applications.

This effort maps the NVIDIA ISV-NCP-Validation-Suite onto Zadara's zcompute
platform, which exposes AWS-compatible API endpoints. The goal is to:

1. Identify which NCP test suites zcompute can pass today
2. Document gaps where zcompute's AWS-compatible APIs are incomplete
3. Build a `providers/zcompute/` configuration that runs the suite against real clusters
4. Work toward a passing NCP certification run on the HGX cluster

We are proceeding suite by suite, starting from the simplest (control-plane)
and working toward the most complex (VM, Kubernetes, bare metal).

---

## zcompute API Endpoint Format

Each AWS-compatible service is exposed at its own URL path:

```
https://<cluster-ip>/api/v2/aws/<service>/
```

| Service     | Endpoint                                    | Status     |
|-------------|---------------------------------------------|------------|
| EC2         | `/api/v2/aws/ec2/`                          | ✅ Working  |
| IAM         | `/api/v2/aws/iam/`                          | ✅ Working  |
| STS         | `/api/v2/aws/ec2/` (co-hosted, also `/sts/` and `/iam/`) | ✅ Working |
| ELB         | `/api/v2/aws/elbv2/`                        | Not tested |
| ASG         | `/api/v2/aws/autoscaling/`                  | Not tested |
| CloudWatch  | `/api/v2/aws/cloudwatch/`                   | Not tested |
| SNS         | `/api/v2/aws/sns/`                          | Not tested |
| Route53     | `/api/v2/aws/route53/`                      | Not tested |
| ACM         | `/api/v2/aws/acm/`                          | Not tested |
| S3          | N/A                                         | ❌ No endpoint |

**SSL:** zcompute uses self-signed certificates. All boto3 clients use `verify=False`.

**Region:** `symphony` (zcompute-specific, not a standard AWS region name).

**Access key format:** zcompute generates 32-character hex key IDs
(e.g. `b699fd17e0e74f2c8b1b70e4813485c2`), not the standard AWS `AKIA...` format.
boto3 accepts these without issue.

---

## Tested API Operations

### Confirmed Working ✅

| Service | Operation | Notes |
|---------|-----------|-------|
| STS | `GetCallerIdentity` | Works on EC2, STS, and IAM endpoints |
| EC2 | `DescribeRegions` | Returns `symphony` as the region |
| EC2 | `RunInstances` | |
| EC2 | `DescribeInstances` | NetworkInterfaces empty at launch, populated once running |
| EC2 | `StopInstances` | |
| EC2 | `StartInstances` | ~4 min; goes through stopped→pending→stopped→pending→running |
| EC2 | `RebootInstances` | |
| EC2 | `TerminateInstances` | |
| EC2 | `CreateTags` / `DescribeTags` | |
| EC2 | `AllocateAddress` / `AssociateAddress` / `ReleaseAddress` | EIPs use internal IP range (172.28.x.x) |
| EC2 | `CreateVpc` / `DeleteVpc` | VPC starts `pending`, poll for `available` before subnets |
| EC2 | `CreateSubnet` / `DeleteSubnet` | |
| EC2 | `CreateSecurityGroup` / `DeleteSecurityGroup` | |
| EC2 | `AuthorizeSecurityGroupIngress` / `RevokeSecurityGroupIngress` | |
| EC2 | `CreateInternetGateway` / `AttachInternetGateway` / `DetachInternetGateway` | |
| EC2 | `CreateVpcPeeringConnection` | Must delete peering before deleting VPC |
| EC2 | `CreateKeyPair` / `DeleteKeyPair` | Key IDs are 32-char hex, not AKIA format |
| EC2 | `DescribeAvailabilityZones` | Returns single AZ: `symphony` (type: `local-zone`) |
| EC2 | `DescribeInstanceTypes` | Returns all types; GPU field returns `{}` (empty) |
| IAM | `ListUsers`, `CreateUser`, `DeleteUser` | |
| IAM | `CreateAccessKey`, `DeleteAccessKey`, `ListAccessKeys` | |
| IAM | `GetUser` | No propagation delay — new keys usable immediately |
| IAM | `CreateGroup`, `ListGroups`, `GetGroup`, `DeleteGroup` | Used as tenant proxy |
| IAM | `ListAttachedUserPolicies` | Every new user gets `MemberFullAccess` auto-attached |

### Confirmed NOT Working ❌

| Service | Operation | Error | Impact |
|---------|-----------|-------|--------|
| IAM | `UpdateAccessKey` | `NotImplementedException` | Cannot disable access keys — **certification blocker** |
| IAM | `ListUserPolicies` | `AuthFailure` | Skipped in delete script — no inline policies on test users |
| EC2 | `GetConsoleOutput` | `500 InternalFailure` | Serial console not available — excluded from VM suite |

---

## Test Suite Status

### ⚠️ Control Plane (`control-plane.yaml`) — Run Complete, Partial Pass

**Adaptations:** Per-service endpoint URLs, IAM Groups as tenant proxy, disable/reject steps set to `continue_on_failure`.
**Exclusions:** `AccessKeyDisabledCheck`, `AccessKeyRejectedCheck` (UpdateAccessKey not implemented).

| Check | Result |
|-------|--------|
| API health (STS/EC2/IAM) | ✅ |
| Access key create + authenticate | ✅ |
| Access key disable | ❌ NotImplementedException (skipped) |
| Access key rejection verify | ❌ Skipped (depends on disable) |
| Tenant CRUD (via IAM Groups) | ✅ |
| Teardown | ✅ |

---

### ✅ IAM (`iam.yaml`) — Full Pass

All operations working. `ListUserPolicies` failure handled gracefully (skipped in delete script).

---

### ⬜ Network (`network.yaml`) — Probing Complete, Build Pending

Core APIs confirmed working (VPC, subnets, SGs, IGW, EIP, peering). Key zcompute behaviors:
- VPC starts `pending` — poll for `available` before creating subnets
- Single AZ (`symphony`) — `require_multi_az` must be `false`
- VPC can't be deleted while peering connections exist (delete peering first)
- Several tests (traffic, DHCP, DNS, floating IP) require running VMs — deferred until after VM suite

---

### 🔄 VM (`vm.yaml`) — In Progress (multiple runs, iterating)

**HGX cluster:** `172.29.0.20` · instance type `zh1.52xlarge` (208 vCPUs, ~1.87TB RAM, 8× H100 SXM5 80GB) · AMI `ami-8269e586aa484003948818fadcbb475a` (Ubuntu 24.04)

**zcompute-specific behaviors discovered:**
- No auto public IP — must allocate + associate EIP after launch
- `PublicIpAddress` is `""` at launch (not null)
- Root device is `/dev/vda` (not `/dev/sda`)
- Start cycle: `stopped → pending → stopped → pending → running` (~4-5 min)
- GPU resources take 4-5 min to release after stop — `start_instance` waits 5 min before retrying
- NVIDIA modules (`nvidia`, `nvidia-uvm`, `nvidia-modeset`) not auto-loaded at boot — loaded via SSH + persisted to `/etc/modules`
- Docker and CUDA toolkit not on base image — installed at launch time via SSH (~15 min)
- boto3 waiters not supported — all polling is custom

**Current validation status (best run: run 8, 20/24 pass):**

| Check | Status | Notes |
|-------|--------|-------|
| InstanceStateCheck (launch) | ✅ | |
| InstanceCreatedCheck | ✅ | |
| CloudInitCheck | ✅ | cloud-init + metadata service both pass |
| InstanceListCheck | ✅ | |
| InstanceTagCheck | ✅ | |
| ConnectivityCheck (SSH) | ✅ | |
| OsCheck | ✅ | Ubuntu confirmed |
| VcpuPinningCheck | ✅ | 208 vCPUs confirmed |
| PciBusCheck | ✅ | 8 GPUs on PCI confirmed |
| CpuInfoCheck | ✅ | |
| ContainerRuntimeCheck | ✅ | Docker + NVIDIA Container Toolkit installed |
| InstanceStopCheck | ✅ | |
| InstanceStartCheck | ✅ | |
| StableIdentifierCheck (×2) | ✅ | |
| ConnectivityCheck (start + reboot SSH) | ✅ | |
| OsCheck (start + reboot SSH) | ✅ | |
| InstanceRebootCheck | ✅ | |
| InstanceStateCheck (reboot) | ✅ | |
| HostSoftwareCheck | ✅ | nvidia_driver subtest passes |
| GpuCheck (×3) | ⏳ | nvidia-smi found but driver not communicating at validation time; fix: wait loop after modprobe (deployed, not yet tested) |
| DriverCheck | ⏳ | cuda_toolkit subtest — CUDA nvcc install via NVIDIA repo (deployed, timed out in run 9, reduced package size for run 10) |
| SerialConsoleCheck | ⛔ EXCLUDED | `GetConsoleOutput` returns 500 — not supported |
| ConsoleRbacCheck | ⛔ EXCLUDED | Not implemented in zcompute |
| NIM tests | ⛔ EXCLUDED | Requires NGC_API_KEY — deferred |

**Outstanding for full VM pass:**
- `GpuCheck` ×3 — driver wait loop deployed (needs run 10 to validate)
- `DriverCheck cuda_toolkit` — nvcc install via smaller package deployed (needs run 10)

**Image plan:** Once a run passes completely, snapshot the configured VM as a new image.
That image will have NVIDIA modules, Docker, NVIDIA Container Toolkit, and CUDA toolkit
pre-installed — reducing launch time from ~25 min to ~10 min for the certification run.

---

### ⬜ Kubernetes (`k8s.yaml`) — Not started
### ⬜ Security (`security.yaml`) — Not started
### ⬜ Image Registry (`image-registry.yaml`) — Not started
### ⬜ Bare Metal (`bare_metal.yaml`) — Not started (may not apply to zcompute)

---

## Known Gaps & Issues to Flag

### 🔴 Critical — Likely blocks certification

| # | Issue | Detail |
|---|-------|--------|
| 1 | `iam:UpdateAccessKey` not implemented | Cannot disable access keys. Credential lifecycle management is a core NCP security requirement. Must be implemented in zcompute IAM before certification. |

### 🟡 Notable — May affect certification scope

| # | Issue | Detail |
|---|-------|--------|
| 2 | No S3 endpoint | Image registry suite uses S3. zcompute has no S3-compatible endpoint. Needs alternative approach. |
| 3 | Single AZ only | zcompute has one AZ (`symphony`, type `local-zone`). Multi-AZ requirements in network suite must be overridden to `false`. Certification may require multi-AZ capability. |
| 4 | Serial console unavailable | `GetConsoleOutput` returns 500. `SerialConsoleCheck` excluded from VM suite. |
| 5 | GPU instance type not in EKS | No EKS equivalent — Kubernetes suite requires zcompute's native K8s provisioning mechanism. |
| 6 | Multi-step GPU node provisioning | GPU nodes require a two-stage admin provisioning process with forced power cycle. Non-standard for cloud providers. NVIDIA may evaluate this as an operational requirement. |
| 7 | Docker/CUDA not pre-installed | Base Ubuntu 24.04 image lacks Docker, NVIDIA Container Toolkit, and CUDA toolkit. Currently installed at launch time (~15 min overhead). Proper fix: GPU-ready image. |

### 🟢 Resolved

| # | Issue | Resolution |
|---|-------|------------|
| A | NVIDIA modules not auto-loading | Fixed: `load_nvidia_modules()` persists to `/etc/modules` + wait loop until driver ready |
| B | `stop_initiated`/`start_initiated`/`reboot_initiated` missing from output | Fixed: added to all lifecycle scripts |
| C | `key_file`/`ssh_user` missing from start/reboot output | Fixed: added to result dicts |
| D | `console_rbac.py` rejecting `--instance-id` arg | Fixed: changed to `parse_known_args()` |
| E | `serial_console` returning error on 500 | Fixed: `InternalFailure` treated as not-supported |
| F | `start_instance` timing out (GPU resource release delay) | Fixed: 5 min pre-start delay + retry logic |
| G | `nvidia-smi: command not found` in paramiko sessions | Fixed: `find` to locate binary + force symlink to `/usr/local/bin/` + reinstall nvidia-utils if missing |
| H | `ContainerRuntimeCheck` failing | Fixed: Docker + NVIDIA Container Toolkit installed at launch |
| I | SSH known_hosts conflict when IPs reused | Fixed: `UserKnownHostsFile=/dev/null` in all SSH calls |
| J | CUDA install timeout (900s for 3GB package) | Fixed: switched to `cuda-nvcc-12-6` (smaller), timeout raised to 1800s |

---

## GPU Cluster Prerequisites (One-Time Admin Setup)

Before any GPU VM can be launched on a zcompute cluster, the following
cluster-level provisioning must be completed by an administrator.

Full guide: https://zadara.atlassian.net/wiki/spaces/ZEC/pages/3203399742/

**Summary:**
1. Install zCompute, join GPU nodes as worker nodes
2. Tag GPU nodes `to_be_gpu`, add taint rule to prevent premature scheduling
3. Create GPU account/project via GUI (Identity)
4. Create GPU Network via API (VNI ID: 4096–16,777,215)
5. Create and configure switches via API (LLDP discovery, rail group)
6. Run two-stage GPU node provisioning: Stage 1 (firmware, huge pages → power cycle ~10 min), Stage 2 (NVLink/GPU fabric ports)
7. Disable taint rule, remove from maintenance

**NCP implication:** Multi-step manual provisioning with forced power cycle is non-standard. NVIDIA will evaluate whether this meets their operational requirements.

---

### ✅ Kubernetes (`k8s.yaml`) — FULL PASS (23/25, 2 expected skips)

**EKS-D v1.30.4 cluster on zcompute, 1× hgx-worker (8× H100 SXM5 80GB)**

| Check | Result |
|-------|--------|
| K8sNodeCountCheck | ✅ 3 nodes |
| K8sNodeReadyCheck | ✅ All Ready |
| K8sNvidiaSmiCheck | ✅ |
| K8sDriverVersionCheck | ✅ 535.161.08 |
| K8sGpuPodAccessCheck | ✅ |
| K8sGpuCapacityCheck | ✅ 8 GPUs total |
| K8sGpuOperatorNamespaceCheck | ✅ nvidia-gpu-operator |
| K8sGpuOperatorPodsCheck | ✅ 13 running pods |
| K8sGpuLabelsCheck | ✅ |
| K8sPodHealthCheck | ✅ |
| K8sMigConfigCheck | ✅ MIG capable, disabled |
| K8sDualStackNodeCheck | ✅ Single-stack (auto-skip) |
| K8sCsiStorageTypesCheck | ✅ Block (ebs-sc) |
| K8sCsiStorageQuotaApiCheck | ✅ |
| K8sCsiTenantScopedCredentialsCheck | ✅ |
| K8sCsiProvisioningModesCheck | ✅ Dynamic |
| **K8sNcclWorkload** | ✅ **110.14 GB/s** (H100 NVLink) |
| K8sApiServerMetricsCheck | ✅ 362 metrics |
| K8sControlPlaneLogsCheck | ✅ All 3 components |
| K8sGpuStressWorkload | ✅ GPU stress test passed on all 1 nodes |
| K8sNcclMultiNodeWorkload | ⏭ SKIPPED — MPI Operator not installed (expected, 1 GPU node) |
| NIM tests | ⏭ EXCLUDED — NGC_API_KEY needed |

**NCCL result: 110.14 GB/s average bus bandwidth** — well above the 100 GB/s threshold.

---

## Run Log

| Date | Suite | Cluster | Result | Notes |
|------|-------|---------|--------|-------|
| 2026-05-11 | control-plane | 172.16.10.110 | ⚠️ PARTIAL PASS | 9/9 validations passed, 2 skipped (UpdateAccessKey gap), 1 step failed |
| 2026-05-11 | iam | 172.16.10.110 | ✅ FULL PASS | All 3 validations passed, clean teardown |
| 2026-05-12 | vm (run 1) | 172.29.0.20 | ❌ start_instance timeout | 600s timeout too short; console_rbac arg error |
| 2026-05-12 | vm (run 2) | 172.29.0.20 | ❌ start_instance timeout | Resource release delay not yet handled |
| 2026-05-12 | vm (run 3) | 172.29.0.20 | ❌ start_instance timeout | 1800s timeout + 5 min pre-delay deployed but not yet tested |
| 2026-05-13 | vm (run 4) | 172.29.0.20 | ⚠️ PARTIAL PASS | start/reboot/describe all pass; GPU/Docker/CUDA checks fail (image gaps) — fixes deployed |
| 2026-05-13 | vm (run 5) | 172.29.0.20 | ⚠️ 19/24 pass | Docker ✅ InstanceStart/Reboot ✅ all SSH checks ✅ — only nvidia-smi PATH issue remaining |
| 2026-05-13 | vm (run 6) | 172.29.0.20 | ⚠️ 19/24 pass | Identical to run 5 — symlink approach failed silently; new fix: find+verify+fallback-reinstall |
| 2026-05-13 | vm (run 7) | 172.29.0.20 | ⚠️ 19/24 pass | nvidia-utils reinstall added to setup_gpu_dependencies — still failing |
| 2026-05-13 | vm (run 8) | 172.29.0.20 | ⚠️ 20/24 pass | **HostSoftwareCheck ✅ DriverCheck nvidia_driver ✅** — nvidia-smi found but driver not communicating when GpuCheck runs; fix: wait loop after modprobe deployed |
| 2026-05-13 | vm (run 9) | 172.29.0.20 | ❌ launch failed | CUDA toolkit install timed out (900s); known_hosts conflict on IP reuse — both fixed |
| 2026-05-14 | vm (run 10) | 172.29.0.20 | ⚠️ 21/24 pass | DriverCheck ✅ CUDA ✅ — only GpuCheck×3 remain (driver wait loop fix deployed) |
| 2026-05-15 | k8s (run 1) | EKS-D zcompute | ⚠️ 22/25 | NCCL 110.14 GB/s ✅ — GpuStress failed (image pull timeout, 12GB image) |
| 2026-05-15 | k8s (run 2) | EKS-D zcompute | ✅ **FULL PASS** | **23/25 — 2 expected skips (MPI multi-node, no MPI Operator)** |

### vm run 8 detail — 2026-05-13 (best run: 20/24 pass)

```
[PASS] SETUP   ✅  InstanceStateCheck, InstanceCreatedCheck, CloudInitCheck

[FAIL] TEST    (all 8 steps passed; 4 validations failing)
  All steps: list ✅  tags ✅  serial_console ✅  console_rbac ✅
             stop ✅  start ✅  reboot ✅  describe ✅

  20 passing: all lifecycle, SSH, OS, vCPU, PCI, CPU, container, stable ID checks
  4 failing:
    GpuCheck (×3)      nvidia-smi found, driver not communicating → wait loop fix deployed
    DriverCheck        cuda_toolkit only → nvcc install fix deployed

[PASS] TEARDOWN  ✅
```

---

## Next Steps

1. ✅ Control-plane suite complete (partial pass — UpdateAccessKey gap documented)
2. ✅ IAM suite complete (full pass)
3. 🔄 VM suite — fixes deployed, next run should pass most checks
4. ⬜ Build GPU-ready image (snapshot after clean VM pass) — removes ~15 min Docker/CUDA overhead
5. ⬜ Build and run Network suite
6. ⬜ Build and run Security suite
7. ⬜ Assess Kubernetes suite (need zcompute K8s provisioning info)
8. ⬜ Enable NIM tests (needs NGC_API_KEY)
9. ⬜ Full certification run on HGX once all suites passing
10. 🔴 Escalate `iam:UpdateAccessKey` to zcompute engineering — certification blocker
