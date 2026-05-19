#!/usr/bin/env python3
"""Create IAM user and access key for testing.

This is a direct port of the AWS implementation. zcompute's IAM is
AWS-compatible for user and access-key lifecycle operations, so this
script works unchanged except for the endpoint routing via ZCOMPUTE_ENDPOINT.

Output JSON:
{
    "success": true,
    "platform": "control_plane",
    "username": "isv-access-key-test-a1b2c3d4",
    "access_key_id": "...",
    "secret_access_key": "...",
    "user_id": "arn:..."
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
    parser.add_argument("--username-prefix", default="isv-access-key-test")
    args = parser.parse_args()

    iam = get_client("iam", region=args.region)
    username = f"{args.username_prefix}-{uuid.uuid4().hex[:8]}"

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "username": username,
    }

    try:
        user_response = iam.create_user(
            UserName=username,
            Tags=[{"Key": "CreatedBy", "Value": "isvtest"}],
        )
        result["user_id"] = user_response["User"]["Arn"]

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
