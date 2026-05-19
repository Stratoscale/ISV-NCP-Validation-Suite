#!/usr/bin/env python3
"""List tenants (IAM Groups) and verify a target group exists.

zcompute does not support the AWS resource-groups API.
Tenant lifecycle is implemented via IAM Groups (see create_tenant.py).

Output JSON:
{
    "success": true,
    "platform": "control_plane",
    "tenants": [
        {"tenant_name": "isv-tenant-test-a1b2c3d4", "tenant_id": "arn:..."}
    ],
    "target_tenant": "isv-tenant-test-a1b2c3d4",
    "found_target": true,
    "count": 1
}
"""

import argparse
import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.client import get_client  # noqa: E402

from botocore.exceptions import ClientError


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--group-name", help="Group name to verify exists")
    args = parser.parse_args()

    iam = get_client("iam", region=args.region)

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "tenants": [],
    }

    try:
        paginator = iam.get_paginator("list_groups")
        for page in paginator.paginate():
            for g in page.get("Groups", []):
                result["tenants"].append({
                    "tenant_name": g["GroupName"],
                    "tenant_id": g["Arn"],
                })

        if args.group_name:
            result["target_tenant"] = args.group_name
            result["found_target"] = any(
                t["tenant_name"] == args.group_name for t in result["tenants"]
            )

        result["count"] = len(result["tenants"])
        result["success"] = True

    except ClientError as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
