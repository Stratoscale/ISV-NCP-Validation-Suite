#!/bin/bash
# K8s Inventory for zcompute EKS-D cluster
#
# Queries the live EKS-D cluster and outputs inventory JSON for the NCP suite.
# The cluster is pre-deployed (static) — this script only reads, never provisions.
#
# Requirements:
#   - kubectl in PATH
#   - KUBECONFIG pointing at the EKS-D cluster (e.g. config-eksd-nkqa11)
#   - jq (for node name array)
#
# Usage:
#   export KUBECONFIG=path/to/config-eksd-nkqa11
#   ./setup.sh

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Use kubectl (standard for EKS-D)
if [[ "${KUBECTL:-}" =~ [^[:space:]] ]]; then
    :  # already set in environment
elif command -v kubectl &>/dev/null; then
    KUBECTL="kubectl"
else
    echo "Error: kubectl not found. Install kubectl or set KUBECTL env var." >&2
    exit 1
fi

CLUSTER_NAME=$($KUBECTL config current-context 2>/dev/null || echo "eksd-zcompute")
# zadara-vm-chart deploys GPU Operator into zadara-system, not the upstream default
DEFAULT_GPU_NS="zadara-system"
REQUIRE_JQ="true"

# Source shared inventory logic (copied from my-isv provider)
source "$SCRIPT_DIR/_common.sh"
