#!/usr/bin/env python3
"""Create IAM user and access key for testing.

zcompute-specific notes:
  - Access key IDs are 32-char hex strings (not AKIA... format) — boto3 accepts this fine.
  - New keys are usable immediately, no propagation delay needed.
  - Every new user gets MemberFullAccess auto-attached by zcompute.

Output JSON:
{
    "success": true,
    "platform": "iam",
    "username": "isv-test-user-a1b2c3d4",
    "user_arn": "arn:aws:iam::...:user/isv-test-user-a1b2c3d4",
    "user_id": "...",
    "access_key_id": "...",
    "secret_access_key": "..."
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
    parser.add_argument("--username", default="isv-test-user")
    parser.add_argument("--create-access-key", action="store_true", default=True)
    args = parser.parse_args()

    username = f"{args.username}-{uuid.uuid4().hex[:8]}"
    iam = get_client("iam")

    result: dict[str, Any] = {
        "success": False,
        "platform": "iam",
        "username": username,
    }

    try:
        response = iam.create_user(
            UserName=username,
            Tags=[{"Key": "CreatedBy", "Value": "isvtest"}],
        )
        result["user_arn"] = response["User"]["Arn"]
        result["user_id"] = response["User"]["UserId"]

        if args.create_access_key:
            key_response = iam.create_access_key(UserName=username)
            result["access_key_id"] = key_response["AccessKey"]["AccessKeyId"]
            result["secret_access_key"] = key_response["AccessKey"]["SecretAccessKey"]

        result["success"] = True

    except ClientError as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
