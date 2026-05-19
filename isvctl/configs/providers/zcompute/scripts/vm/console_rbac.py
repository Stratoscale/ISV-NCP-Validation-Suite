#!/usr/bin/env python3
"""Report EC2 IAM console RBAC simulation support for zcompute.

zcompute does not implement the EC2 IAM console RBAC simulation feature.
This script returns a static result that the check runner can use to
exclude this test from the validation suite via config.

Output JSON:
{
    "success": true,
    "platform": "vm",
    "console_rbac_supported": false,
    "note": "zcompute does not implement EC2 IAM console RBAC simulation"
}
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Report EC2 IAM console RBAC support for zcompute"
    )
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "symphony"))
    parser.parse_known_args()  # Accept and ignore unknown args for interface compatibility.

    result = {
        "success": True,
        "platform": "vm",
        "console_rbac_supported": False,
        "note": "zcompute does not implement EC2 IAM console RBAC simulation",
    }

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
