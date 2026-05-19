#!/usr/bin/env python3
"""Delete IAM access key and user (teardown).

Direct port of the AWS implementation - zcompute IAM supports
iam:DeleteAccessKey and iam:DeleteUser identically.

Output JSON:
{
    "success": true,
    "platform": "control_plane",
    "deleted_key": "...",
    "deleted_user": "isv-access-key-test-a1b2c3d4"
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
    parser.add_argument("--username", required=True)
    parser.add_argument("--access-key-id", required=True)
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
        iam.delete_access_key(UserName=args.username, AccessKeyId=args.access_key_id)
        result["deleted_key"] = args.access_key_id

        iam.delete_user(UserName=args.username)
        result["deleted_user"] = args.username
        result["success"] = True

    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchEntity":
            result["success"] = True
            result["already_deleted"] = True
        else:
            result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
