# zCompute × NVIDIA NCP Validation Suite — Compatibility Report

**Last updated:** 2026-05-20
**Author:** Zadara Team
**Suite version:** NVIDIA ISV-NCP-Validation-Suite (experimental preview)
**zCompute clusters under test:**
- `<zcompute-ip>` — HGX GPU cluster (primary certification target)

---

## What We Are Doing

NVIDIA's NCP (NVIDIA Cloud Partner) certification program validates that a cloud
provider's infrastructure can reliably run NVIDIA GPU workloads — AI training,
inference, and GPU-accelerated applications.

This effort maps the NVIDIA ISV-NCP-Validation-Suite onto Zadara's zCompute
platform, which exposes AWS-compatible API endpoints. All provider-specific
work lives in `providers/zcompute/`.

---

## zCompute API Endpoints

```
https://<zcompute-ip>/api/v2/aws/<service>/
```

| Service | Endpoint | Status |
|---------|----------|--------|
| EC2 | `/api/v2/aws/ec2/` | ✅ Working |
| IAM | `/api/v2/aws/iam/` | ✅ Working |
| STS | `/api/v2/aws/sts/` | ✅ Working |
| S3 | N/A | ❌ No endpoint |
| Route53 | N/A | ❌ Not available |
| CloudWatch/CloudTrail | N/A | ❌ Not available |

**SSL:** Self-signed certificates — all boto3 clients use `verify=False` via
botocore URLLib3Session patch.

**Region:** `symphony` (single AZ, type `local-zone`).

---

## Confirmed Working API Operations

| Service | Operation | Notes |
|---------|-----------|-------|
| STS | `GetCallerIdentity` | |
| EC2 | `DescribeRegions` | Returns `symphony` |
| EC2 | `RunInstances` | Returns empty `Instances[]` — patched to find instance by key name |
| EC2 | `DescribeInstances` | Ignores `vpc-id` and `InstanceIds` filters — returns all project instances |
| EC2 | `StartInstances` / `StopInstances` / `RebootInstances` | |
| EC2 | `TerminateInstances` | May return `InternalServerError` for pending instances — retry needed |
| EC2 | `CreateVpc` / `DeleteVpc` | VPC starts `pending` — poll for `available` |
| EC2 | `CreateSubnet` / `DeleteSubnet` | Subnet starts `pending` — poll for `available` |
| EC2 | `CreateSecurityGroup` / `DeleteSecurityGroup` | `TagSpecifications` not supported |
| EC2 | `AuthorizeSecurityGroupIngress/Egress` / `RevokeSecurityGroupIngress/Egress` | |
| EC2 | `CreateVpcPeeringConnection` / `AcceptVpcPeeringConnection` / `DeleteVpcPeeringConnection` | |
| EC2 | `DescribeVpcPeeringConnections` | Returns `InternalFailure` — symp CLI fallback used |
| EC2 | `AllocateAddress` / `AssociateAddress` / `DisassociateAddress` / `ReleaseAddress` | EIPs use 172.28.x.x range |
| EC2 | `CreateInternetGateway` / `AttachInternetGateway` / `DeleteInternetGateway` | |
| EC2 | `CreateRouteTable` / `CreateRoute` / `AssociateRouteTable` | |
| EC2 | `CreateKeyPair` / `DeleteKeyPair` | `TagSpecifications` not supported; returns RSA PKCS#1 format (not OpenSSH) |
| EC2 | `DescribeKeyPairs` | Returns empty `KeyPairs[]` instead of `InvalidKeyPair.NotFound` — patched |
| EC2 | `DescribeAvailabilityZones` | Returns single AZ: `symphony` |
| EC2 | `DescribeImages` | Returns account images |
| EC2 | `ModifyVpcAttribute` | Works |
| EC2 | `ModifySubnetAttribute` (MapPublicIpOnLaunch) | Returns `AuthFailure` — silently ignored |
| EC2 | `CreateVolume` / `DeleteVolume` | Confirmed working (bare metal probe, 2026-07-09) |
| EC2 | `AttachVolume` / `DetachVolume` | Confirmed working end-to-end against a running instance (2026-07-12) — reverses an earlier `403 Forbidden` finding, which appears to have been transient |
| IAM | `ListUsers`, `CreateUser`, `DeleteUser`, `GetUser` | |
| IAM | `CreateAccessKey`, `DeleteAccessKey`, `ListAccessKeys` | |
| IAM | `CreateGroup`, `ListGroups`, `DeleteGroup` | Used as tenant proxy |

## Confirmed NOT Working

| Service | Operation | Error | Impact |
|---------|-----------|-------|--------|
| IAM | `UpdateAccessKey` | `NotImplementedException` | **CRITICAL** — cannot disable keys (known platform limitation) |
| IAM | `ListUserPolicies` | `AuthFailure` | Skipped — no inline policies on test users |
| EC2 | `GetConsoleOutput` | `500 InternalFailure` | Serial console not available |
| EC2 | `CreatePlacementGroup` / `DescribePlacementGroups` / `DeletePlacementGroup` | `AuthFailure` | Bare metal `TopologyPlacementCheck` blocked — not implemented |
| EC2 | `CreateLaunchTemplate` / `DescribeLaunchTemplates` / `DeleteLaunchTemplate` | `AuthFailure` | Confirmed 2026-07-29 (twice, valid creds) — same "not implemented" signature as placement groups, not transient. Blocks `verify_config_installable.py`'s Launch-Template dry-run approach; zcompute version substitutes a direct `RunInstances(DryRun=True)` instead, output explicitly marks `launch_template_supported: false` |
| EC2 | `DescribeNetworkAcls` / `CreateNetworkAcl` | `AuthFailure` | **CRITICAL** — NACLs not supported, SG-only model |
| EC2 | `DescribeVpcPeeringConnections` | `InternalFailure` | Workaround: symp CLI fallback |
| boto3 | All waiters | `WaiterError` / `NotSupported` | Replaced with poll loops in ssl_wrapper.py and scripts |

---

## Test Suite Status

### ⚠️ Control Plane — PARTIAL PASS (9/11)

| Check | Result | Notes |
|-------|--------|-------|
| API Health (STS/EC2/IAM) | ✅ | |
| AccessKeyCreatedCheck / TenantCreatedCheck / AuthenticatedCheck | ✅ | |
| AccessKeyDisabledCheck | ❌ BLOCKED | `UpdateAccessKey` not implemented (known platform limitation) |
| AccessKeyRejectedCheck | ⛔ EXCLUDED | Depends on disable |
| TenantListedCheck / TenantInfoCheck / StepSuccessCheck ×2 | ✅ | |

---

### ✅ IAM — FULL PASS (5/5)

All checks passing.

---

### ⚠️ VM — PARTIAL PASS (24/24 collected, as of 2026-05-20)

**Instance:** `zh1.52xlarge` (208 vCPUs, ~1.87TB RAM, 8× H100 SXM5 80GB)
**AMI:** `<ami-id>` (Ubuntu 24.04 server cloudimg)

| Check | Result | Notes |
|-------|--------|-------|
| InstanceStateCheck / InstanceCreatedCheck / CloudInitCheck | ✅ | EIP allocated for public IP |
| InstanceListCheck / InstanceTagCheck | ✅ | |
| ConnectivityCheck / OsCheck (ssh, start, reboot) | ✅ | |
| VcpuPinningCheck / PciBusCheck / HostSoftwareCheck / DriverCheck / CpuInfoCheck | ✅ | |
| ContainerRuntimeCheck | ✅ | `nvidia_docker` subtest fails (NVIDIA GPG key expired) |
| InstanceStopCheck / InstanceStartCheck / InstanceRebootCheck | ✅ | |
| StableIdentifierCheck ×2 | ✅ | |
| GpuCheck (initial / post-start / post-reboot) | ⏳ IN PROGRESS | NVML driver/library version mismatch being fixed |
| SerialConsoleCheck / ConsoleRbacCheck | ⛔ EXCLUDED | `GetConsoleOutput` returns 500 |
| NimHealthCheck / NimModelCheck / NimInferenceCheck | ⏳ IN PROGRESS | NGC key with NIM entitlement received, testing |

**Key zCompute VM behaviors:**
- No auto-assigned public IP — EIP allocated at launch, released at teardown
- `RunInstances` returns empty `Instances[]` — patched to find instance by key name + LaunchTime
- Instance may go to `shutoff` — monitoring loop detects and auto-starts
- NVIDIA modules not auto-loaded at boot — `load_nvidia_modules()` runs via SSH after launch
- Docker, CUDA, NVIDIA Container Toolkit not on base image — installed at launch (~15 min)
- SSH key returned in RSA PKCS#1 format — converted to OpenSSH via `ssh-keygen`
- Driver install order critical: load modules BEFORE adding CUDA apt repo (CUDA repo ships newer nvidia-utils that mismatches kernel module)

---

### ✅ Network — 15/15 collected checks PASS, all phases PASS (as of 2026-06-07, test run #208)

All test phases (setup/test/teardown) pass cleanly.

| Check | Result | Notes |
|-------|--------|-------|
| NetworkProvisionedCheck | ✅ | setup phase |
| VpcCrudCheck | ✅ | |
| SubnetConfigCheck | ✅ | Single AZ, `require_multi_az: false` |
| VpcIsolationCheck | ✅ | symp CLI fallback for peering describe |
| SgCrudCheck | ✅ | `TagSpecifications` removed, `create_tags` used after |
| SecurityBlockingCheck | ✅ | NACLs skipped (SG-only model) |
| VpcIpConfigCheck | ✅ | `auto_assign_ip_mode: instance` |
| DhcpIpManagementCheck | ✅ | EIP allocated; SSH verified DHCP lease, IP match, DNS |
| SgWorkloadScopingCheck | ✅ | Updated 2026-06-07 — previously listed as excluded, now passes |
| SgNodeScopingCheck | ✅ | Updated 2026-06-07 — previously listed as excluded, now passes |
| StablePrivateIpCheck | ✅ | IP stable across stop/start |
| FloatingIpCheck | ✅ | EIP switch ~1.7s (limit 10s) |
| LocalizedDnsCheck | ✅ | Updated 2026-06-07 — previously listed as excluded ("Route 53 not available"), now passes via symp/internal DNS |
| VpcPeeringCheck | ✅ | |
| BackendSwitchFabricCheck | ✅ | Updated 2026-06-07 — previously listed as excluded, now passes. Uses `symp gpunet-pool switch list` (`scripts/network/backend_switch_fabric_test.py`) — reports actual leaf/spine/core switch IDs for a node, zCompute's fabric is single-tier so all three tiers report the same switch |
| NvlinkDomainCheck | ✅ | Updated 2026-06-07 — previously listed as excluded, now passes. Uses `symp gpunet-pool gpunet list` (`scripts/network/nvlink_domain_test.py`) — reports the NVLink/GPU fabric domain ID for a node |
| NetworkConnectivityCheck / TrafficFlowCheck | ⛔ EXCLUDED | Require SSM agent |
| SgSubnetScopingCheck / SgServiceScopingCheck | ⛔ EXCLUDED | NACLs + VPC endpoints not supported |
| SdnLogging ×3 | ⛔ EXCLUDED | Not in released_tests.json |
| ByoipCheck | ⛔ EXCLUDED | Not applicable |

**All network fixes live in `scripts/network/ssl_wrapper.py`.**

**Relevant to bare-metal/NVL72 scoping (2026-07-15):** `BackendSwitchFabricCheck`/
`NvlinkDomainCheck` query actual GPU fabric topology facts via `symp`
(no create/delete lifecycle, unlike AWS's placement-group API) — these are a
better-fit proof of "GPU fabric topology awareness" than bare metal's
AWS-shaped `TopologyPlacementCheck` for a fixed-topology single-NVL72 target,
where there's no scheduling decision to request in the first place.

---

### ⚠️ Kubernetes (EKS-D) — PARTIAL PASS (24/24 collected, as of 2026-05-19)

**Cluster:** EKS-D v1.30.4 — 1 control plane + 1 CPU worker + 2× HGX GPU workers (16× H100 total)

Single-node NCCL: **110 GB/s** | Multi-node NCCL: **31 GB/s avg / 116 GB/s peak** over RoCE

All 24 collected tests pass. 8 excluded pending engineering work:
`K8sOidcIssuerCheck`, `K8sNetworkPolicyCheck`, `K8sApiNetworkAclCheck`,
`K8sCncfConformanceCheck`, `K8sNodePoolCheck`, NIM ×3.

---

### ⬜ Security — NOT STARTED
### ⬜ Image Registry — NOT STARTED (no S3 endpoint)

### 🔄 Bare Metal — IN PROGRESS (blocked on real BM/GB200 instance type)

zCompute has no bare-metal instance type today — `g4dn.metal`-equivalent support
(backed by NVIDIA GB200 hardware) is planned but not yet available on the lab
cluster. Work so far:

- **Provider scripts written**: `providers/zcompute/scripts/bare_metal/*.py`
  ports all 12 AWS reference scripts (launch/describe/tags/stop/start/reboot/
  power-cycle/topology/serial-console/teardown/reinstall), applying the same
  zcompute fix patterns already proven in the VM suite (no waiters, manual
  EIP, RSA→OpenSSH key conversion, no `TagSpecifications` on
  `CreateSecurityGroup`/`CreateKeyPair`).
- **Lifecycle mechanics validated twice** against the lab cluster
  (172.16.10.140) using `z2.3large` as a temporary non-GPU stand-in (2026-07-09):
  launch, tags, stop/start/reboot/power-cycle (recovered in 642s both runs),
  teardown, and sanitization all passed. GPU/NIM/InfiniBand checks excluded
  (no GPU hardware on this stand-in type) — see `config/bare_metal.yaml` for
  the temporary exclude list to remove once real hardware lands.
- **AttachVolume/DetachVolume re-verified working** (2026-07-12) against a
  disposable test instance, reversing the earlier 403 finding. `reinstall_instance`
  is kept `skip: true` regardless, to match the AWS reference config's own
  default (parity over enabling it just because nothing blocks it anymore).
- **AWS reference suite run as the certification target** (2026-07-10, real
  `g4dn.metal`, `eu-west-1`): full pass on everything except `deploy_nim`
  (NIM health check timeout — looks like an `nvidia-fabricmanager` service
  issue on the AMI, unrelated to bare-metal infra) and `InfiniBandCheck`
  (skipped, no IB hardware on this instance type). This is the reference
  zCompute's bare-metal implementation needs to match.
- **`verify_image`/`verify_config` steps added** (2026-07-29), matching the
  AWS reference's `image-registry`-sourced steps (these validate the BM
  instance's own image/config, not the separate Image Registry suite, which
  remains untouched/not started). `verify_image_installed.py` ports over
  unmodified (`DescribeInstances`/`DescribeImages` only). `verify_config_installable.py`
  required adapting: `CreateLaunchTemplate` is confirmed not implemented on
  zcompute, so the zcompute version skips Launch Template creation entirely
  (never attempted) and substitutes a direct `RunInstances(DryRun=True)`
  using the instance's own config — output explicitly marks
  `launch_template_supported: false` so nothing looks like it's passing
  Launch Template support that doesn't exist.
- **Confirmed zcompute API gaps specific to bare metal** (see tables above):
  `GetConsoleOutput` (serial console), the `CreatePlacementGroup` family
  (topology placement), and the `CreateLaunchTemplate` family (install
  config validation) — all return the same generic "not implemented"
  `AuthFailure`/`500` errors, not hardware-dependent, need engineering work
  independent of GB200 availability.
- **Lab cluster note**: `172.16.10.140` was wiped at some point after
  2026-07-12; API sanity checks from 2026-07-29 onward use `172.16.10.120`
  instead (different AMIs/instance types available there — `zp2.xlarge`/
  `zp2.3xlarge` in use, no `z2.3large`).

---

## Known Gaps

| # | Gap | Severity | Status |
|---|-----|----------|--------|
| 1 | `iam:UpdateAccessKey` not implemented | 🔴 CRITICAL | Known platform limitation |
| 2 | NACLs not supported (SG-only model) | 🔴 CRITICAL | Needs engineering ticket |
| 3 | NGC API key with NIM entitlement | 🟠 HIGH | Received, testing |
| 4 | No S3 endpoint | 🟠 HIGH | Open |
| 5 | OIDC not configured (K8s) | 🟡 MEDIUM | Runbook ready, ~30 min |
| 6 | NetworkPolicy not enforced (Flannel→Calico) | 🟡 MEDIUM | Runbook ready, ~1 day |
| 7 | NVIDIA Container Toolkit GPG key expired | 🟡 MEDIUM | `nvidia_docker` subtest fails |
| 8 | rocep75s0 duplicate IP on HGX nodes | 🟡 MEDIUM | Workaround: excluded NIC |
| 9 | Single AZ only (`symphony`) | 🟢 MITIGATED | `require_multi_az: false` |
| 10 | Serial console unavailable | 🔵 LOW | `GetConsoleOutput` returns 500 |
| 11 | No bare-metal/GB200 instance type on zcompute | 🟠 HIGH | Blocks GPU-dependent bare-metal validation; planned |
| 12 | Placement groups not implemented (bare metal) | 🟡 MEDIUM | `AuthFailure` — needs engineering ticket |
| 13 | Launch Templates not implemented (`config_installable`) | 🟡 MEDIUM | `AuthFailure`, confirmed 2026-07-29 — worked around with direct `RunInstances(DryRun=True)`, still needs engineering ticket if real Launch Template support matters elsewhere |

---

## Run Log

| Date | Suite | Result | Notes |
|------|-------|--------|-------|
| 2026-05-11 | control-plane | ⚠️ PARTIAL PASS | 9/11 — UpdateAccessKey gap |
| 2026-05-11 | iam | ✅ FULL PASS | 5/5 |
| 2026-05-12–13 | vm runs 1–9 | ❌→⚠️ | Iterating on GPU/Docker/CUDA setup |
| 2026-05-14 | vm run 10 | ⚠️ 21/24 | DriverCheck ✅, GpuCheck×3 remaining |
| 2026-05-15 | k8s run 1 | ⚠️ 22/25 | GPU stress image pull timeout |
| 2026-05-15 | k8s run 2 | ✅ 23/25 | 2 expected skips (MPI multi-node, 1 GPU node) |
| 2026-05-15–16 | k8s (multi-node NCCL) | ✅ 24/24 | MPI Operator added, RoCE configured, 116 GB/s peak |
| 2026-05-19 | network (full suite) | ✅ 10/10 | All phases PASS including DHCP/stable IP/floating IP |
| 2026-05-20 | vm (NIM enabled) | ⚠️ 21+/27 | NIM: Payment Required (old key). GpuCheck NVML mismatch being fixed |
| 2026-07-09 | bare-metal (z2.3large, run 1) | ⚠️ | `CpuInfoCheck` failed (`pci_nvidia` subtest, no `gpu` marker) — everything else passed |
| 2026-07-09 | bare-metal (z2.3large, run 2) | ✅ | Re-run confirms `CpuInfoCheck` exclusion fix; full pass |
| 2026-07-10 | bare-metal (AWS `g4dn.metal`, reference run 1) | ⚠️ | `reboot_instance` waiter timeout (15min budget too tight) |
| 2026-07-10 | bare-metal (AWS `g4dn.metal`, reference run 2) | ⚠️ | Reboot waiter bumped to 45min, passed. `deploy_nim` failed (fabric-manager) — everything else passed |
| 2026-07-12 | bare-metal API sanity check (zcompute) | ✅ | `AttachVolume`/`DetachVolume` confirmed working end-to-end, reversing earlier 403 finding |

---

## Next Steps

1. ✅ Control-plane — partial pass (UpdateAccessKey gap documented)
2. ✅ IAM — full pass
3. ✅ Network — 10/10 collected, all phases pass
4. ✅ K8s — 24/24 collected, multi-node NCCL over RoCE
5. 🔄 VM — fix GpuCheck NVML version mismatch (driver load order fix deployed)
6. 🔄 VM/K8s NIM tests — NGC key with NIM entitlement received, testing
7. 🔴 `iam:UpdateAccessKey` — escalate to zCompute engineering (known platform limitation)
8. 🔴 NACLs — file engineering ticket
9. ⬜ OIDC configuration (K8s) — runbook ready
10. ⬜ Calico migration (K8s NetworkPolicy) — runbook ready
11. ⬜ Security suite
12. ⬜ Image Registry suite
13. 🔄 Bare Metal — provider scripts + lifecycle mechanics validated on `z2.3large`; AWS `g4dn.metal` reference run passed (NIM excluded); blocked on real BM/GB200 instance type for GPU-dependent validation
14. ⬜ Bare Metal — validate `reinstall_instance` (root volume swap) now that `AttachVolume` is confirmed working
15. 🔴 Placement groups (bare metal `TopologyPlacementCheck`) — file engineering ticket
