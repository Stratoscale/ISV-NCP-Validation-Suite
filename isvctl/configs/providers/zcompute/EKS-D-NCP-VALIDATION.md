# EKS-D on zcompute — NVIDIA NCP Kubernetes Validation

**Author:** Amit Orenshtein, Zadara Storage  
**Date:** May 2026  
**Status:** In Progress — 24/24 collected tests passing, NIM tests pending NGC_API_KEY

---

## What We Built

We deployed a pre-baked EKS-D (Amazon EKS Distro) Kubernetes cluster on zcompute and ran the full NVIDIA NCP Kubernetes validation suite against it.

### The Cluster

| Component | Details |
|-----------|---------|
| Kubernetes distribution | EKS-D v1.30.4 (same codebase as AWS EKS) |
| API server | `https://192.168.0.179:6443` |
| Control plane | 1× Ubuntu 22.04 node (`k8s-master-sa-1`) |
| CPU workers | 1× ASG-managed node (`k8s-worker-1`) |
| GPU workers | 2× HGX nodes (`hgx-worker`, `hgx-worker-2`) |
| GPU hardware | 8× NVIDIA H100 SXM5 80GB per node (16 total) |
| Driver version | 535.161.08 (installed by GPU Operator) |
| Container runtime | containerd 1.7.22 |
| CNI | Flannel |
| Storage | AWS-compatible EBS CSI (`ebs.csi.aws.com`) |

### What Is EKS-D

EKS-D is Amazon's open-source Kubernetes distribution — the exact same Kubernetes build that powers AWS EKS, but packaged so you can run it anywhere. On zcompute, we deploy it as a self-managed cluster using kubeadm. This gives us:

- Standard Kubernetes API (kubectl works exactly as expected)
- AWS-compatible cloud controller (reads zcompute EC2/EBS APIs)
- EBS CSI driver pointing at zcompute's storage endpoints
- Full NVIDIA GPU Operator support

---

## What We Installed

### 1. EKS-D Cluster
Deployed using kubeadm with the EKS-D Kubernetes 1.30 packages. The cluster uses:
- **Flannel** for pod networking
- **AWS Cloud Controller Manager** configured for zcompute endpoints
- **AWS Load Balancer Controller** for service exposure
- **EBS CSI Driver** for persistent storage via zcompute's EBS-compatible API
- **Cluster Autoscaler** for the CPU worker ASG

### 2. NVIDIA GPU Operator
Installed via Helm into the `nvidia-gpu-operator` namespace. Manages:
- NVIDIA driver installation (535.161.08) on GPU nodes
- NVIDIA Container Toolkit (enables `nvidia` container runtime class)
- Device Plugin (exposes `nvidia.com/gpu` resource to Kubernetes)
- GPU Feature Discovery (auto-labels GPU nodes)
- DCGM Exporter (GPU metrics)
- MIG Manager

### 3. GPU Worker Nodes
The two HGX nodes (`hgx-worker`, `hgx-worker-2`) were joined as **dedicated nodes** — not managed by the ASG. This is intentional: GPU nodes have long boot times and require special provisioning, so static dedicated nodes are more reliable than auto-scaling.

Each GPU node was:
- Joined with `kubeadm join`
- The AWS CCM was patched to not delete dedicated nodes
- Added to the cluster security group in the Zadara UI
- Labeled: `accelerator=nvidia`, `nvidia.com/gpu.present=true`
- Tainted: `nvidia.com/gpu=present:NoSchedule`

### 4. MPI Operator
Installed for multi-node NCCL tests. Manages MPIJob resources that run distributed GPU benchmarks across both HGX nodes.

---

## Test Results

### Summary

| Result | Count |
|--------|-------|
| ✅ PASSED | 24 |
| ⏭ SKIPPED (excluded / not applicable) | 8 |
| ❌ FAILED | 0 |

**Overall: PASS** on all 24 collected tests.

---

### Detailed Results

#### Node & GPU Health

| Check | Result | Notes |
|-------|--------|-------|
| All 4 nodes Ready | ✅ | |
| nvidia-smi on all GPU nodes | ✅ | Both HGX nodes |
| Driver version 535.161.08 | ✅ | On both GPU nodes |
| 16 total GPUs (`nvidia.com/gpu`) | ✅ | 8 per node × 2 nodes |
| GPU Operator running (21 pods) | ✅ | In `nvidia-gpu-operator` |
| GPU node labels set | ✅ | `nvidia.com/gpu.present=true` |
| MIG configuration valid | ✅ | MIG capable, disabled |
| No pods in error | ✅ | |
| No pending pods | ✅ | |

#### GPU Workloads

| Check | Result | Notes |
|-------|--------|-------|
| **Single-node NCCL** | ✅ **110.14 GB/s** | H100 NVLink intra-node bandwidth |
| **Multi-node NCCL** | ✅ | 2 nodes × 8 GPUs, MPI across both HGX nodes |
| GPU stress test | ✅ | Both nodes |
| NIM inference | ⏭ Excluded | Requires NGC_API_KEY |
| NIM Llama 1B benchmark | ⏭ Excluded | Requires NGC_API_KEY |
| NIM Llama 3B benchmark | ⏭ Excluded | Requires NGC_API_KEY |

#### Storage (EBS CSI via zcompute)

| Check | Result | Notes |
|-------|--------|-------|
| Block storage (ebs-sc) | ✅ | Provisioning and binding works |
| Storage quota API | ✅ | ResourceQuota enforcement verified |
| Tenant-scoped CSI credentials | ✅ | Secrets properly isolated |
| Dynamic provisioning | ✅ | PVCs created and bound |
| Shared filesystem | ⏭ Skipped | Not available (no EFS equivalent) |
| NFS | ⏭ Skipped | Not configured |

#### Observability

| Check | Result | Notes |
|-------|--------|-------|
| API server metrics | ✅ | 362 Prometheus metrics |
| Control plane logs | ✅ | kube-apiserver, scheduler, controller-manager |

#### Other

| Check | Result | Notes |
|-------|--------|-------|
| CNCF conformance | ⏭ Excluded | Takes 1-2 hours, to run separately |
| OIDC issuer | ⏭ Excluded | Not configured on this cluster |
| Network policy | ⏭ Excluded | Flannel does not enforce NetworkPolicy |
| API ACL (external probe) | ⏭ Excluded | Needs external vantage point |
| Node pool CRUD | ⏭ Excluded | Static cluster — no managed node pool API |
| Dual-stack networking | ✅ (skipped subtests) | Single-stack IPv4 — auto-detected as expected |

---

## What Is Still Missing

Here is what needs to happen before the EKS-D validation is complete for NCP certification, in plain terms:

### 1. NIM Tests — Waiting for NGC_API_KEY
**What:** Three tests that deploy NVIDIA's NIM (NVIDIA Inference Microservices) containers and run real inference benchmarks on the H100s.
- `K8sNimInferenceWorkload` — deploys a NIM container and checks it responds
- `K8sNimHelmWorkload-1b` — runs Llama 3.2 1B model, measures throughput
- `K8sNimHelmWorkload-3b` — runs Llama 3.2 3B model, measures throughput

**Why it matters:** This is the core of NVIDIA's certification — proving their models run correctly on your GPU infrastructure.

**What we need:** NVIDIA provides an NGC API key. Once we have it, these tests run as-is with no code changes. Just set `export NGC_API_KEY=<key>` and rerun.

---

### 2. CNCF Conformance — Needs to Run
**What:** Runs the official CNCF Kubernetes conformance test suite against the cluster. Proves the cluster is a valid, spec-compliant Kubernetes deployment.

**Why it matters:** CNCF conformance is a well-recognized certification that validates the entire Kubernetes API works correctly.

**What we need:** Just time. The test takes 1-2 hours. We excluded it for now to avoid long runtimes, but it can be enabled by removing `K8sCncfConformanceCheck` from the exclude list.

---

### 3. Node Pool Management — Needs Engineering Work
**What:** NVIDIA wants to see that you can dynamically create and delete a Kubernetes node group (add nodes, scale up, scale down, remove).

**Why it matters:** Real customers need to scale GPU workloads up and down. This proves zcompute can provision new nodes on demand for a Kubernetes cluster.

**What we need:** Write scripts that:
1. Launch a new CPU VM via zcompute EC2 API
2. Bootstrap and join it to the cluster automatically
3. Label and taint it as requested
4. Remove it cleanly when done

Note: **GPU nodes do not need to be in the ASG for this.** The test uses regular CPU instances, not GPU. The GPU nodes remain dedicated static workers.

---

### 4. OIDC Issuer — Configuration Work
**What:** OIDC (OpenID Connect) allows Kubernetes pods to securely authenticate to external services without static credentials. EKS-D supports it but it needs to be configured.

**Why it matters:** Important for production security — pods should get short-lived credentials automatically, not use long-lived API keys.

**What we need:** Configure the EKS-D cluster with an OIDC issuer endpoint. Roughly 1-2 hours of setup work.

---

### 5. Network Policy — Needs CNI Change
**What:** Kubernetes Network Policy lets you control which pods can talk to which other pods. Flannel (our current CNI) does not enforce these rules.

**Why it matters:** Security. A provider should be able to isolate workloads from each other at the network level.

**What we need:** Replace Flannel with Calico or Cilium. This is a CNI migration — significant change, roughly 1 day of work, requires careful planning since it affects all pod networking.

---

### 6. API Network ACL — Low Priority
**What:** Proves that the Kubernetes API server rejects connections from unauthorized networks.

**Why it matters:** The control plane should not be accessible from the public internet without authentication.

**What we need:** An external IP address (outside the cluster's allow-list) to probe from. This is an operational setup, not a code change.

---

## Architecture Notes for Future Reference

### Why EKS-D Instead of Vanilla Kubernetes
EKS-D gives us the AWS-compatible integrations (Cloud Controller Manager, EBS CSI, Load Balancer Controller) that let us use zcompute's AWS-compatible APIs directly. This means storage (EBS), networking, and cloud metadata all work the same way as on AWS, without writing custom integrations.

### Why Dedicated GPU Nodes (Not ASG)
HGX GPU nodes (`zh1.52xlarge`) take 4-5 minutes to boot and require a two-stage hardware provisioning process (see cluster setup guide). Putting them in an ASG would cause timeouts during scale-up. Instead:
- GPU nodes are joined once as dedicated nodes
- They remain available permanently
- The node pool management test (when implemented) will use regular CPU instances from the ASG, not GPU nodes

### Pre-pulling Large Images
The NVIDIA workload images are large (7-12GB). First-run timeouts are expected. After the first run, images are cached on all nodes and subsequent runs complete quickly. This is documented behavior, not a zcompute issue.
