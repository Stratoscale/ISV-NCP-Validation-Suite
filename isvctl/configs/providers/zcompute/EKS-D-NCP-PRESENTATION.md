# zcompute × NVIDIA NCP Certification
## Kubernetes Validation — Status Report

**Presenter:** Amit Orenshtein, Zadara Storage  
**Date:** May 2026

---

## Background: What Is NCP Certification?

NVIDIA's **NCP (NVIDIA Cloud Partner)** program certifies that a cloud provider's infrastructure can run NVIDIA GPU workloads reliably — AI training, inference, and GPU-accelerated applications. To get certified, a cloud provider must run NVIDIA's official validation test suite and demonstrate that everything passes.

The certification covers multiple areas: virtual machines, networking, storage, identity, and Kubernetes. This document focuses on the **Kubernetes validation**, which is the most complex and most important part for AI workloads.

---

## What We Built

### The Cluster

We deployed a full **EKS-D (Amazon EKS Distro) Kubernetes 1.30** cluster running entirely on zcompute.

**EKS-D** is Amazon's open-source Kubernetes distribution — the exact same Kubernetes build that powers AWS EKS, but packaged so it can run anywhere. By using EKS-D on zcompute, we get all the AWS-compatible integrations (storage, load balancing, cloud controller) pointing at zcompute's own APIs instead of Amazon's.

**Cluster topology:**

| Node | Role | Hardware |
|------|------|----------|
| `k8s-master-sa-1` | Control plane | Standard VM |
| `k8s-worker-1` | CPU worker (auto-scaling) | Standard VM |
| `hgx-worker` | GPU worker (dedicated) | zh1.52xlarge — 8× H100 SXM5 80GB |
| `hgx-worker-2` | GPU worker (dedicated) | zh1.52xlarge — 8× H100 SXM5 80GB |

**Total GPU capacity: 16× H100 SXM5 80GB across 2 dedicated nodes.**

### Infrastructure Stack

Everything running in this cluster was deployed and validated:

- **EKS-D Kubernetes 1.30** — the cluster itself
- **Flannel** — pod networking (CNI)
- **AWS Cloud Controller Manager** — connects Kubernetes to zcompute EC2 APIs
- **AWS EBS CSI Driver** — connects Kubernetes persistent storage to zcompute's block storage API
- **AWS Load Balancer Controller** — manages load balancers via zcompute
- **NVIDIA GPU Operator** — automatically installs GPU drivers, container runtime, and device plugin on GPU nodes
- **MPI Operator** — enables distributed multi-node GPU workloads (required for multi-node NCCL tests)

---

## What We Validated

We ran NVIDIA's official ISV NCP Validation Suite — 24 tests covering every aspect of GPU Kubernetes infrastructure.

---

## Results: 24/24 Passing ✅

**Every single collected test passes.**

---

### GPU Hardware Validation

These tests prove that the H100 GPUs are correctly exposed to Kubernetes and working.

| Test | Result | What It Proved |
|------|--------|----------------|
| All nodes Ready | ✅ | 4 nodes healthy |
| nvidia-smi works | ✅ | GPU driver installed and responsive on both HGX nodes |
| Driver version correct | ✅ | 535.161.08 verified on both GPU nodes |
| 16 total GPUs visible | ✅ | `nvidia.com/gpu: 16` reported to Kubernetes scheduler |
| GPU pods can access GPUs | ✅ | Containers can request and use GPUs |
| GPU Operator running | ✅ | 21 operator pods healthy in `nvidia-gpu-operator` namespace |
| GPU node labels set | ✅ | Nodes correctly labeled for GPU workload scheduling |
| MIG configuration valid | ✅ | H100 MIG (multi-instance GPU) capability detected and configured correctly |

---

### GPU Workload Benchmarks

These tests run real GPU workloads and measure performance.

| Test | Result | What It Proved |
|------|--------|----------------|
| **NCCL Single-Node** | ✅ **110.14 GB/s** | H100 NVLink bandwidth within one node — exceeds the 100 GB/s requirement |
| **NCCL Multi-Node** | ✅ | Distributed GPU communication working across both HGX nodes over the network |
| GPU Stress Test | ✅ | Both GPU nodes sustained a GPU memory stress workload successfully |

> **NCCL** (NVIDIA Collective Communications Library) is the core communication library used by every major AI training framework (PyTorch, TensorFlow, JAX). Passing these benchmarks proves that distributed AI training will work on this cluster.

---

### Storage Validation

These tests prove that persistent storage works correctly for Kubernetes workloads via zcompute's AWS-compatible EBS storage API.

| Test | Result | What It Proved |
|------|--------|----------------|
| Block storage provisioning | ✅ | PVCs created and bound via `ebs.csi.aws.com` |
| Storage quota enforcement | ✅ | Kubernetes ResourceQuota for storage works correctly |
| Tenant-isolated credentials | ✅ | CSI secrets are properly scoped per tenant, not shared |
| Dynamic provisioning | ✅ | New volumes created on demand and attached automatically |

---

### Cluster Health & Observability

| Test | Result | What It Proved |
|------|--------|----------------|
| No pods in error state | ✅ | Cluster is healthy end-to-end |
| No pending pods | ✅ | Scheduler has no unresolved workloads |
| API server Prometheus metrics | ✅ | 362 metrics available — full observability |
| Control plane logs | ✅ | kube-apiserver, kube-scheduler, controller-manager all logging correctly |

---

## What Is Still Missing

Four items remain before the Kubernetes portion of NCP certification is complete.

---

### 1. NIM Inference Tests — Blocked on NGC API Key

**What NIM is:**
NIM (NVIDIA Inference Microservices) are NVIDIA's own ready-to-use AI model containers. They package large language models (like Meta's Llama) with everything needed to run them — you deploy the container on your GPU cluster and it immediately starts serving AI requests. This is NVIDIA's flagship product for cloud AI inference.

**What the tests do:**
Three tests deploy NIM containers on the H100 cluster and run real inference benchmarks:
- Deploy a NIM container and verify it responds to requests
- Run the Llama 3.2 1B model and measure throughput (tokens per second)
- Run the Llama 3.2 3B model and measure throughput

**Why this is the most important missing item:**
This is the core of what NCP certification is about. NVIDIA wants to know: *does our product (NIM) actually work on your cloud?* Passing these tests is proof that a customer can come to zcompute, deploy NIM, and get a working AI inference service on day one.

**What's needed to unblock:**
An **NGC API Key** from NVIDIA. NGC (NVIDIA GPU Cloud) is NVIDIA's private container registry where NIM images are stored. Without authentication, we cannot pull the images. Once NVIDIA provides the key, these three tests run with zero additional code changes. The infrastructure is ready.

**Effort:** Immediate — no engineering work required once the key is provided.

---

### 2. CNCF Conformance — Needs to Run

**What CNCF is:**
The **Cloud Native Computing Foundation** is the independent organization that governs Kubernetes. They maintain the official Kubernetes standard and run a conformance certification program. Every major cloud provider — AWS, Google, Azure, Oracle — holds CNCF Kubernetes conformance certification.

**What the test does:**
CNCF provides a test suite that runs hundreds of tests covering every part of the Kubernetes specification — how pods behave, how services route traffic, how storage gets attached, how authentication works, and much more. Passing proves that the cluster behaves exactly as Kubernetes is defined to behave — no missing features, no broken behaviors.

**Why it matters:**
Enterprise customers need to know that Kubernetes on zcompute is standard Kubernetes — not a custom variant that might break their applications. CNCF conformance is the recognized proof of that. It also gives NVIDIA confidence that the cluster underpinning their certification tests is a genuine, spec-compliant Kubernetes environment.

**What's needed:**
Nothing technical — just time. The test is already integrated into our suite. We excluded it from development runs because it takes 1-2 hours. For the formal submission, we run it once without interruptions.

**Effort:** 1-2 hours to run, then review results.

---

### 3. Dynamic Node Pool Management — Engineering Work Required

**What this means:**
NVIDIA wants to see that you can **automatically** add and remove groups of worker nodes to and from the Kubernetes cluster through an API — not manually. A customer should be able to say *"I need 10 GPU nodes for a training job"* and have the cluster scale up automatically, then scale back down when the job is done.

**Why it matters:**
Running GPU nodes 24/7 is expensive. The economics of cloud AI depend on elasticity — scale up when you need compute, scale down when you don't. This test proves that zcompute can do this in a Kubernetes-native way, which is how every production AI platform works.

**What we have vs. what we need:**
Our GPU nodes are connected to the cluster as permanent dedicated nodes. This is correct for the GPU benchmarks — but it doesn't demonstrate dynamic scaling. We need to implement scripts that:
1. Automatically launch a new worker VM via zcompute's EC2 API
2. Bootstrap it with the correct Kubernetes configuration
3. Have it join the cluster and become Ready without manual intervention
4. Remove it cleanly when requested

Note: this uses **regular CPU worker nodes** for the test itself — not GPU nodes. The GPU nodes remain dedicated and permanent. The test simply proves that the cloud platform can dynamically manage worker node capacity.

**Effort:** 1-2 days of engineering work.

---

### 4. OIDC Issuer — Configuration Required

**What OIDC means:**
OIDC (OpenID Connect) is a security standard that solves a specific problem: *how does an application running inside Kubernetes securely prove its identity to an external service — like a database, a storage bucket, or an API?*

Without OIDC, the typical approach is to store a static password or API key inside the cluster as a secret. This is a security risk — secrets can be leaked, they don't expire, and rotating them is painful.

With OIDC, each application automatically receives a short-lived cryptographic token that proves exactly what it is, which cluster it's from, and when the token expires. No passwords, no secrets, no rotation needed.

**Why it matters for NCP:**
Enterprise AI workloads need to access external services securely — pulling model weights from storage, accessing databases, writing results to object storage. OIDC is the modern security standard for doing this in Kubernetes. A cloud provider without it is not suitable for production enterprise deployments.

**What's needed:**
This is a configuration task on the EKS-D cluster, not a zcompute limitation. EKS-D fully supports OIDC. We need to configure an OIDC issuer endpoint on the cluster's API server. This is a standard EKS-D setup step.

**Effort:** 2-4 hours of configuration.

---

## Remaining Work Summary

| Item | What It Is (Simple) | Effort | Blocked By |
|------|---------------------|--------|------------|
| **NIM Tests** | Run NVIDIA's own AI models on our GPUs | Zero — infrastructure ready | NGC API Key from NVIDIA |
| **CNCF Conformance** | Prove we run standard Kubernetes | Run it (1-2 hours) | Time |
| **Node Pool Management** | Prove we can auto-scale worker nodes | 1-2 days engineering | Nothing — just needs to be built |
| **OIDC** | Secure identity for applications | 2-4 hours configuration | Nothing — just needs to be configured |

---

## Key Takeaway

The Kubernetes infrastructure on zcompute **passes all GPU, storage, and observability validation** in NVIDIA's NCP test suite. The H100 hardware performs at full specification. The two remaining technical items (node pool management, OIDC) are configuration and engineering tasks — not gaps in zcompute's fundamental capabilities. The NIM tests are ready to run the moment NVIDIA provides an NGC API key.
