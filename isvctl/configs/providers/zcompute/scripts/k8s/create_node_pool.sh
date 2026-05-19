#!/bin/bash
# Node Pool Create/Update — zcompute EKS-D stub
#
# EKS-D on zcompute uses a static cluster with ASG-managed workers.
# This stub satisfies the node_pool output schema so the orchestrator
# can proceed. K8sNodePoolCheck is excluded in k8s.yaml so it won't fail.
set -eo pipefail

ACTION="${NODE_POOL_ACTION:-Creating}"
POOL_NAME="${TF_VAR_test_pool_name:-isv-test-pool}"
DESIRED="${TF_VAR_test_pool_desired_size:-1}"
LABELS="${TF_VAR_test_pool_labels_json:-'{}'}"
TAINTS="${TF_VAR_test_pool_taints_json:-'[]'}"
INSTANCE_TYPES="${TF_VAR_test_pool_instance_types:-'[]'}"
NODE_TYPE="${TF_VAR_test_pool_node_type:-cpu}"

echo "$ACTION test node pool '$POOL_NAME' (stub — EKS-D static cluster)" >&2

# Schema requires: node_pool_name, label_selector, expected_replicas (int)
# expected_labels_json, expected_taints_json, expected_instance_types_json must be JSON strings
LABELS_STR=$(echo "$LABELS" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read().strip()))")
TAINTS_STR=$(echo "$TAINTS" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read().strip()))")
TYPES_STR=$(echo "$INSTANCE_TYPES" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read().strip()))")

cat <<EOF
{
  "success": true,
  "platform": "kubernetes",
  "node_pool_name": "${POOL_NAME}",
  "label_selector": "isv.ncp.validation/workload=data-ingest",
  "expected_replicas": ${DESIRED},
  "expected_labels_json": ${LABELS_STR},
  "expected_taints_json": ${TAINTS_STR},
  "expected_instance_types_json": ${TYPES_STR},
  "node_type": "${NODE_TYPE}",
  "note": "Static EKS-D cluster — node pool CRUD not applicable"
}
EOF
