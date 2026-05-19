#!/usr/bin/env python3
"""Create a logical tenant using an IAM Group.

AWS uses 'resource-groups' for tenant lifecycle. zcompute does not support
the resource-groups API. Instead, we model a tenant as an IAM Group, which:
  - Is fully supported by zcompute's AWS-compatible IAM
  - Demonstrates the same create/list/describe/delete CRUD lifecycle
  - Can logically contain users (tenants/sub-accounts) in the real world

Output JSON:
{
    "success": true,
    "platform": "control_plane",
    "tenant_name": "isv-tenant-test-a1b2c3d4",
    "tenant_id": "arn:..."
}
"""

import argparse
import json
import os
import sys
import uuid
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.client import get_client  # noqa: E402

from botocore.exceptions import ClientError


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--name-prefix", default="isv-tenant-test")
    args = parser.parse_args()

    iam = get_client("iam", region=args.region)
    group_name = f"{args.name_prefix}-{uuid.uuid4().hex[:8]}"

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "tenant_name": group_name,
    }

    try:
        response = iam.create_group(GroupName=group_name)
        result["tenant_id"] = response["Group"]["Arn"]
        result["implementation_note"] = (
            "Tenant modeled as IAM Group (zcompute does not support resource-groups API)"
        )
        result["success"] = True

    except ClientError as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
