#!/usr/bin/env python3
"""Delete IAM user and associated access keys (teardown).

zcompute-specific notes:
  - iam:ListUserPolicies returns AuthFailure consistently — inline policy
    cleanup is skipped entirely.
  - iam:DeleteUser works even with attached policies (no DeleteConflict),
    so we delete the user directly after clearing its keys.
  - Every user gets MemberFullAccess auto-attached — we don't attempt to
    detach it since delete_user handles it implicitly.

Output JSON:
{
    "success": true,
    "platform": "iam",
    "resources_destroyed": true,
    "resources_deleted": ["access_key:...", "user:isv-test-user-a1b2c3d4"]
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
    parser.add_argument("--skip-destroy", action="store_true")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "iam",
        "resources_destroyed": False,
        "resources_deleted": [],
    }

    if args.skip_destroy:
        result["success"] = True
        result["message"] = "Destroy skipped (--skip-destroy flag)"
        print(json.dumps(result, indent=2))
        return 0

    iam = get_client("iam")

    try:
        # Delete all access keys first
        keys_response = iam.list_access_keys(UserName=args.username)
        for key in keys_response.get("AccessKeyMetadata", []):
            iam.delete_access_key(
                UserName=args.username,
                AccessKeyId=key["AccessKeyId"],
            )
            result["resources_deleted"].append(f"access_key:{key['AccessKeyId']}")

        # Delete the user directly.
        # zcompute allows delete_user even with attached policies (no DeleteConflict).
        # iam:ListUserPolicies is not implemented (AuthFailure), so inline policy
        # cleanup is intentionally skipped.
        iam.delete_user(UserName=args.username)
        result["resources_deleted"].append(f"user:{args.username}")
        result["resources_destroyed"] = True
        result["success"] = True
        result["message"] = "User and access keys deleted"

    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "NoSuchEntity":
            result["success"] = True
            result["resources_destroyed"] = True
            result["message"] = "User not found (already deleted)"
        else:
            result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
