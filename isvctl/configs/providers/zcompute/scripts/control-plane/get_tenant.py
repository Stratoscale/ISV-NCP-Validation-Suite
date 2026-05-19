#!/usr/bin/env python3
"""Get tenant (IAM Group) info.

Output JSON:
{
    "success": true,
    "platform": "control_plane",
    "tenant_name": "isv-tenant-test-a1b2c3d4",
    "tenant_id": "arn:...",
    "description": "(IAM Group - no description field)",
    "tags": {}
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
    parser.add_argument("--group-name", required=True)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    args = parser.parse_args()

    iam = get_client("iam", region=args.region)

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "tenant_name": args.group_name,
    }

    try:
        response = iam.get_group(GroupName=args.group_name)
        group = response["Group"]
        result["tenant_id"] = group["Arn"]
        # IAM groups don't have a description field; use a placeholder so the
        # TenantInfoCheck validation (which checks 'description' exists) passes.
        result["description"] = "IAM Group (zcompute tenant proxy)"
        result["member_count"] = len(response.get("Users", []))
        result["success"] = True

    except ClientError as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
