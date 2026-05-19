#!/bin/bash
# K8s Teardown — no-op for zcompute EKS-D
# The cluster is externally managed; we never destroy it from the suite.
set -eo pipefail
echo "Teardown: EKS-D cluster is externally managed — no action taken." >&2
echo '{"success": true, "platform": "kubernetes", "message": "static cluster, teardown skipped"}'
