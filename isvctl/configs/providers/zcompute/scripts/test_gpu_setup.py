#!/usr/bin/env python3
"""One-off manual test: run setup_gpu_dependencies() directly against an
already-launched instance, to validate the skip-if-already-present logic
(Docker/CUDA/NCT/nvidia-utils-hold) before trusting a full fresh-instance
run. Not part of the suite — delete after use.

Usage (from isvctl/configs/providers/zcompute/scripts/):
    python3 test_gpu_setup.py --host <private_ip> --key-file <path> [--user ubuntu]
"""

from __future__ import annotations

import argparse
import json
import sys

from common.ec2 import setup_gpu_dependencies


def main() -> int:
    parser = argparse.ArgumentParser(description="Manually test setup_gpu_dependencies()")
    parser.add_argument("--host", required=True, help="Instance private IP")
    parser.add_argument("--key-file", required=True, help="SSH private key path")
    parser.add_argument("--user", default="ubuntu")
    args = parser.parse_args()

    result = setup_gpu_dependencies(args.host, args.user, args.key_file)
    print(json.dumps(result, indent=2))
    return 0 if all(result.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
