#!/usr/bin/env python3
"""Delete tenant (IAM Group) - teardown.

Output JSON:
{
    "success": true,
    "platform": "control_plane",
    "deleted_group": "isv-tenant-test-a1b2c3d4"
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
    parser.add_argument("--skip-destroy", action="store_true")
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "control_plane"}

    if args.skip_destroy:
        result["success"] = True
        result["skipped"] = True
        print(json.dumps(result, indent=2))
        return 0

    iam = get_client("iam", region=args.region)

    try:
        iam.delete_group(GroupName=args.group_name)
        result["deleted_group"] = args.group_name
        result["success"] = True

    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("NoSuchEntity", "NoSuchEntityException"):
            result["success"] = True
            result["already_deleted"] = True
        else:
            result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
