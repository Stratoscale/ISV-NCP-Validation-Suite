# EKS-D Cluster Setup for NVIDIA NCP Certification

**Audience:** Engineers standing up a new EKS-D cluster on zcompute that will run the NCP validation suite.

This document covers every prerequisite that must be in place before running `isvctl test run`. It distinguishes between what the test suite scripts do automatically (cluster inventory queries) and what must be set up once on the cluster itself.

---

## Overview

The validation suite scripts (`setup.sh`, `teardown.sh`) are **read-only** — they query a pre-existing cluster. All of the following must be done before running the suite:

| # | Prerequisite | Required for | Notes |
|---|-------------|-------------|-------|
| 1 | [EKS-D cluster](#1-eksd-cluster-bootstrap) | Everything | kubeadm-based, standard |
| 2 | [Calico CNI](#2-calico-cni-networkpolicy-support) | `K8sNetworkPolicyCheck` | Replaces Flannel |
| 3 | [AWS-compatible addons](#3-aws-compatible-addons) | Storage, load balancers | Cloud Controller, EBS CSI |
| 4 | [NVIDIA GPU Operator](#4-nvidia-gpu-operator) | All GPU checks | Helm chart |
| 5 | [MPI Operator](#5-mpi-operator) | `K8sNcclMultiNodeWorkload` | Kubeflow |
| 6 | [OIDC issuer](#6-oidc-service-account-issuer) | `K8sOidcIssuerCheck` | One API server flag |
| 7 | [GPU node joining](#7-gpu-node-joining-hgx-specific) | GPU checks | HGX-specific steps |
| 8 | [RDMA NIC fix](#8-rdma-nic-ip-fix) | `K8sNcclMultiNodeWorkload` | Fix duplicate IP |
| 9 | [Image pre-pull](#9-pre-pull-large-images) | Workloads (avoids timeouts) | Run once per node |

---

## 1. EKS-D Cluster Bootstrap

### kubeadm init

Use EKS-D 1.30 packages. The control-plane init must include the OIDC service account issuer flag (covered in §6 — set it here from the start, it is much easier than patching later).

```bash
# On the control-plane node
sudo kubeadm init \
  --kubernetes-version v1.30.4 \
  --pod-network-cidr 10.244.0.0/16 \
  --service-cidr 10.96.0.0/12 \
  --apiserver-advertise-address <CONTROL_PLANE_IP> \
  --extra-config=apiserver.service-account-issuer=https://<CONTROL_PLANE_IP>:6443 \
  --extra-config=apiserver.service-account-key-file=/etc/kubernetes/pki/sa.pub

# Copy kubeconfig
mkdir -p $HOME/.kube
sudo cp /etc/kubernetes/admin.conf $HOME/.kube/config
sudo chown $(id -u):$(id -g) $HOME/.kube/config
```

If the cluster already exists and `--service-account-issuer` was not set, see §6 for how to add it retroactively.

### Worker nodes

```bash
# On each worker node — token from `kubeadm token create --print-join-command`
sudo kubeadm join <CONTROL_PLANE_IP>:6443 \
  --token <token> \
  --discovery-token-ca-cert-hash sha256:<hash>
```

---

## 2. Calico CNI (NetworkPolicy support)

Flannel does **not** enforce NetworkPolicy. Use Calico.

### Fresh cluster (Calico from day 1)

```bash
# Install Calico (matches pod-network-cidr 10.244.0.0/16 from kubeadm init)
kubectl create -f https://raw.githubusercontent.com/projectcalico/calico/v3.28.0/manifests/tigera-operator.yaml

cat <<EOF | kubectl apply -f -
apiVersion: operator.tigera.io/v1
kind: Installation
metadata:
  name: default
spec:
  calicoNetwork:
    ipPools:
    - blockSize: 26
      cidr: 10.244.0.0/16
      encapsulation: VXLANCrossSubnet
      natOutgoing: Enabled
      nodeSelector: all()
EOF

# Wait for Calico to be ready
kubectl wait --for=condition=Available deployment/calico-kube-controllers -n calico-system --timeout=300s
```

### Existing cluster running Flannel → Calico migration

> ⚠️  This is disruptive. Plan a maintenance window. All pod IPs will change.

```bash
# 1. Delete Flannel
kubectl delete -f https://raw.githubusercontent.com/flannel-io/flannel/master/Documentation/kube-flannel.yml
# Remove Flannel network interface on every node
for node_ip in <NODE_IPS>; do
  ssh ubuntu@$node_ip "sudo ip link delete flannel.1 2>/dev/null; sudo rm -f /run/flannel/subnet.env"
done

# 2. Install Calico (same commands as fresh cluster above)
kubectl create -f https://raw.githubusercontent.com/projectcalico/calico/v3.28.0/manifests/tigera-operator.yaml
# ... (same Installation CR as above)

# 3. Restart all pods to get new IPs
kubectl delete pods --all --all-namespaces
kubectl wait --for=condition=Ready pods --all --all-namespaces --timeout=300s
```

### Verify NetworkPolicy works

```bash
# Deploy a test policy and verify it is enforced
cat <<EOF | kubectl apply -f -
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: deny-all-test
  namespace: default
spec:
  podSelector: {}
  policyTypes: [Ingress]
EOF
kubectl get networkpolicy deny-all-test
kubectl delete networkpolicy deny-all-test
```

---

## 3. AWS-Compatible Addons

These components allow EKS-D to use zcompute's AWS-compatible APIs for storage and load balancing.

### AWS Cloud Controller Manager

```bash
# Configure with zcompute endpoint
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: cloud-config
  namespace: kube-system
data:
  cloud.conf: |
    [Global]
    zone=symphony
    [ServiceOverride "ec2"]
    Service=ec2
    Region=symphony
    URL=https://<ZCOMPUTE_IP>/api/v2/aws/ec2/
    SigningRegion=symphony
EOF

# Install cloud controller manager (use the zcompute-patched build)
helm install aws-cloud-controller-manager \
  aws-cloud-controller-manager/aws-cloud-controller-manager \
  --namespace kube-system \
  --set args[0]="--cloud-provider=aws" \
  --set args[1]="--cloud-config=/etc/kubernetes/cloud.conf"
```

### EBS CSI Driver

```bash
# Create secret with zcompute credentials
kubectl create secret generic aws-secret \
  --namespace kube-system \
  --from-literal "key_id=${AWS_ACCESS_KEY_ID}" \
  --from-literal "access_key=${AWS_SECRET_ACCESS_KEY}"

# Install EBS CSI driver pointing at zcompute
helm install aws-ebs-csi-driver aws-ebs-csi-driver/aws-ebs-csi-driver \
  --namespace kube-system \
  --set controller.env[0].name=AWS_EC2_ENDPOINT \
  --set controller.env[0].value=https://<ZCOMPUTE_IP>/api/v2/aws/ec2/ \
  --set controller.extraVolumeTags.environment=ncp-validation

# Create default StorageClass
cat <<EOF | kubectl apply -f -
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: ebs-sc
  annotations:
    storageclass.kubernetes.io/is-default-class: "true"
provisioner: ebs.csi.aws.com
volumeBindingMode: WaitForFirstConsumer
reclaimPolicy: Delete
EOF
```

---

## 4. NVIDIA GPU Operator

Manages driver installation, device plugin, feature discovery, DCGM, and MIG.

```bash
# Add Helm repo
helm repo add nvidia https://helm.ngc.nvidia.com/nvidia
helm repo update

# Install GPU Operator
helm install gpu-operator nvidia/gpu-operator \
  --namespace nvidia-gpu-operator \
  --create-namespace \
  --version v24.6.0 \
  --set driver.enabled=true \
  --set driver.version="535.161.08" \
  --set toolkit.enabled=true \
  --set devicePlugin.enabled=true \
  --set gfd.enabled=true \
  --set migManager.enabled=true \
  --set dcgmExporter.enabled=true \
  --set nodeStatusExporter.enabled=true \
  --set operator.defaultRuntime=containerd

# Wait for GPU Operator to be fully ready (may take 5-10 min on first run)
kubectl wait --for=condition=Ready pods --all -n nvidia-gpu-operator --timeout=600s

# Verify GPUs are visible
kubectl get nodes -l nvidia.com/gpu.present=true
kubectl describe node hgx-worker | grep nvidia.com/gpu
```

---

## 5. MPI Operator

Required for `K8sNcclMultiNodeWorkload` (multi-node NCCL AllReduce test).

```bash
kubectl apply -f https://raw.githubusercontent.com/kubeflow/mpi-operator/v0.5.0/deploy/v2beta1/mpi-operator.yaml

# Verify
kubectl wait --for=condition=Available deployment/mpi-operator -n mpi-operator --timeout=120s
kubectl api-resources --api-group=kubeflow.org | grep mpijobs
```

---

## 6. OIDC Service Account Issuer

`K8sOidcIssuerCheck` calls `kubectl get --raw /.well-known/openid-configuration`. This endpoint is served by the API server automatically once `--service-account-issuer` is set to an HTTPS URL.

### If bootstrapping from scratch

Already covered in §1 — pass `--extra-config=apiserver.service-account-issuer=https://<IP>:6443` to `kubeadm init`.

### If patching an existing cluster

```bash
# Edit the kube-apiserver static pod manifest on the control-plane node
sudo vi /etc/kubernetes/manifests/kube-apiserver.yaml

# Add these two lines under the command: section:
#   - --service-account-issuer=https://<CONTROL_PLANE_IP>:6443
#   - --service-account-key-file=/etc/kubernetes/pki/sa.pub
#
# kubelet will restart kube-apiserver automatically within ~30 seconds.
```

### Verify

```bash
kubectl get --raw /.well-known/openid-configuration | python3 -m json.tool
# Expected: JSON with "issuer", "jwks_uri", "response_types_supported", etc.
```

### Remove the exclusion in the zcompute config

Once configured, remove `K8sOidcIssuerCheck` from the `exclude.tests` list in
`isvctl/configs/providers/zcompute/config/k8s.yaml`.

---

## 7. GPU Node Joining (HGX-specific)

HGX nodes (`zh1.52xlarge`) require special handling due to long boot times and the GPU Operator driver installation cycle.

```bash
# On the HGX node — load NVIDIA kernel modules first
sudo modprobe nvidia nvidia-uvm nvidia-modeset

# Join the cluster
sudo kubeadm join <CONTROL_PLANE_IP>:6443 \
  --token <token> \
  --discovery-token-ca-cert-hash sha256:<hash>

# On the control-plane — wait for GPU Operator to install driver on new node
# This takes 4-8 minutes on first join (driver compilation)
kubectl wait --for=condition=Ready node/hgx-worker --timeout=600s

# Verify
kubectl get node hgx-worker -o jsonpath='{.status.capacity.nvidia\.com/gpu}'
# Expected: 8
```

### Preventing the Cloud Controller Manager from deleting GPU nodes

EKS-D's AWS CCM will try to delete nodes it cannot find via EC2 DescribeInstances if the dedicated GPU nodes are not in its region. Patch the CCM to exclude them:

```bash
kubectl patch deployment aws-cloud-controller-manager -n kube-system \
  --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--node-monitor-period=0s"}]'
# OR: add the node label to exclude it from CCM lifecycle management
kubectl label node hgx-worker node.kubernetes.io/exclude-from-external-load-balancers=true
```

---

## 8. RDMA NIC IP Fix

The HGX nodes have 8 Mellanox ConnectX RoCE NICs (`rocep75s0` through `rocep52s0`). On the current cluster, `enp75s0np0` (the first NIC) has the same IP (`10.20.0.16`) on both GPU nodes, which prevents NCCL from using it for inter-node RDMA. The other 7 NICs work correctly.

The NCP validation suite manifests already work around this via `NCCL_IB_HCA=^rocep75s0` (exclude the broken NIC). However, the proper fix is to assign the correct IPs so all 8 NICs can be used.

### Check current state

```bash
ssh ubuntu@192.168.0.190 "ip addr show | grep 10.20"  # hgx-worker
ssh ubuntu@192.168.0.235 "ip addr show | grep 10.20"  # hgx-worker-2
# The enp75s0np0 interface should show DIFFERENT IPs on each node (e.g. .16 and .17)
# If both show .16, the fix below is needed
```

### Fix (must be done on the nodes by the cluster admin)

```bash
# On hgx-worker-2: remove the duplicate .16 and assign the correct .17
ssh ubuntu@192.168.0.235 "
  sudo ip addr del 10.20.0.16/31 dev enp75s0np0
  sudo ip addr add 10.20.0.17/31 dev enp75s0np0
"
# Make persistent (Ubuntu 22.04 with netplan)
ssh ubuntu@192.168.0.235 "
  sudo tee /etc/netplan/99-rdma-fix.yaml <<EOF
network:
  version: 2
  ethernets:
    enp75s0np0:
      addresses: [10.20.0.0/31, 10.20.0.17/31]
EOF
  sudo netplan apply
"
```

Once this is fixed, remove `NCCL_IB_HCA=^rocep75s0` from the MPIJob manifest
(`isvtest/src/isvtest/workloads/manifests/k8s/nccl_allreduce_mpijob.yaml`)
so all 8 NICs contribute to bandwidth. Peak bus bandwidth should increase from ~115 GB/s
to ~130+ GB/s.

---

## 9. Pre-Pull Large Images

NVIDIA workload images are 7-15 GB. Pull them once on all GPU nodes to avoid timeout failures on the first run.

```bash
# Run on each GPU node (hgx-worker, hgx-worker-2)
for node in hgx-worker hgx-worker-2; do
  echo "=== Pre-pulling on $node ==="
  kubectl debug node/$node --image=nvcr.io/nvidia/hpc-benchmarks:25.04 \
    --profile=general -- sleep 1 2>/dev/null || true
done

# Alternative: DaemonSet approach (runs on all GPU nodes simultaneously)
cat <<EOF | kubectl apply -f -
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: image-prepull
  namespace: default
spec:
  selector:
    matchLabels:
      app: image-prepull
  template:
    metadata:
      labels:
        app: image-prepull
    spec:
      nodeSelector:
        nvidia.com/gpu.present: "true"
      tolerations:
        - key: nvidia.com/gpu
          operator: Exists
          effect: NoSchedule
      initContainers:
        - name: pull-hpc-benchmarks
          image: nvcr.io/nvidia/hpc-benchmarks:25.04
          command: [echo, "pulled"]
          resources:
            limits:
              nvidia.com/gpu: "1"
      containers:
        - name: done
          image: busybox
          command: [sleep, infinity]
  updateStrategy:
    type: RollingUpdate
EOF

kubectl wait --for=condition=Ready pods -l app=image-prepull --timeout=1800s
kubectl delete daemonset image-prepull
```

---

## 10. Final Verification Checklist

Run these before starting the NCP suite:

```bash
# 1. All nodes Ready
kubectl get nodes

# 2. GPU Operator pods healthy (should be ~21 pods)
kubectl get pods -n nvidia-gpu-operator

# 3. MPI Operator running
kubectl get pods -n mpi-operator

# 4. GPUs allocatable on both nodes
kubectl get nodes -l nvidia.com/gpu.present=true \
  -o custom-columns=NAME:.metadata.name,GPUS:.status.allocatable."nvidia\.com/gpu"

# 5. OIDC discovery working
kubectl get --raw /.well-known/openid-configuration | python3 -c "import sys,json; d=json.load(sys.stdin); print('issuer:', d['issuer'])"

# 6. NetworkPolicy enforced (Calico)
kubectl get pods -n calico-system

# 7. StorageClass available
kubectl get sc

# 8. Runtime class registered
kubectl get runtimeclass nvidia

# 9. RDMA devices visible on GPU nodes
ssh ubuntu@192.168.0.190 "ls /dev/infiniband/"
ssh ubuntu@192.168.0.235 "ls /dev/infiniband/"

# 10. RDMA IPs are unique across nodes (different IPs on enp75s0np0)
ssh ubuntu@192.168.0.190 "ip addr show enp75s0np0 | grep inet"
ssh ubuntu@192.168.0.235 "ip addr show enp75s0np0 | grep inet"
# These should show DIFFERENT addresses
```

---

## Environment Variables Required at Runtime

```bash
export KUBECONFIG=/path/to/kubeconfig         # Required: points at the EKS-D cluster
export ZCOMPUTE_BASE_URL=https://<IP>         # Required: zcompute endpoint
export AWS_ACCESS_KEY_ID=<key>                # Required: zcompute credentials
export AWS_SECRET_ACCESS_KEY=<secret>         # Required
export AWS_REGION=symphony                    # Required
export PYTHONPATH=isvctl/src:isvtest/src:isvreporter/src  # Required on Mac (uv editable install)
export NGC_API_KEY=<key>                      # Optional: enables NIM workload tests
```

---

## What Remains for Full NCP Certification

After completing all steps above, the remaining gaps are:

| Gap | Effort | Notes |
|-----|--------|-------|
| **NIM tests** | Minutes | Set `NGC_API_KEY` and remove from exclude list |
| **CNCF conformance** | 1-2 hours | Remove `K8sCncfConformanceCheck` from exclude list, run once |
| **API Network ACL** | 30 min | Need an external IP outside the cluster's allow-list to probe from |
| **Node pool CRUD** | 1 day | Write scripts to dynamically join/remove CPU worker nodes via zcompute EC2 API |
| **All 8 RoCE NICs** | 30 min | Fix the `rocep75s0` IP conflict (§8), then remove `NCCL_IB_HCA=^rocep75s0` from the manifest |
