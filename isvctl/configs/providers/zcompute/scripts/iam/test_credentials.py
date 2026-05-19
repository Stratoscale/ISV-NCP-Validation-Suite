#!/usr/bin/env python3
"""Test that newly created IAM credentials authenticate against zcompute.

zcompute-specific notes:
  - No propagation delay — new keys work immediately (no retry loop needed,
    but we keep a short retry for safety).
  - STS GetCallerIdentity is available on the EC2 endpoint.
  - IAM GetUser works with the new user's own credentials.

Output JSON:
{
    "success": true,
    "platform": "iam",
    "account_id": "...",
    "arn": "arn:aws:iam::...",
    "tests": {
        "sts_identity": {"passed": true},
        "iam_access":   {"passed": true}
    }
}
"""

import argparse
import json
import os
import sys
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.client import get_session_client  # noqa: E402

MAX_RETRIES = 3
RETRY_DELAY = 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--access-key-id", required=True)
    parser.add_argument("--secret-access-key", required=True)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "symphony"))
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "iam",
        "tests": {},
    }

    session = boto3.Session(
        aws_access_key_id=args.access_key_id,
        aws_secret_access_key=args.secret_access_key,
        region_name=args.region,
    )

    # Test STS GetCallerIdentity
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            sts = get_session_client(session, "sts", args.region)
            identity = sts.get_caller_identity()
            result["account_id"] = identity.get("Account", "unknown")
            result["arn"] = identity.get("Arn", "unknown")
            result["user_id"] = identity.get("UserId", "unknown")
            result["tests"]["sts_identity"] = {"passed": True}
            if attempt > 0:
                result["tests"]["sts_identity"]["retries"] = attempt
            break
        except (ClientError, NoCredentialsError) as e:
            last_error = str(e)
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
    else:
        result["tests"]["sts_identity"] = {"passed": False, "error": last_error}
        print(json.dumps(result, indent=2))
        return 1

    # Test IAM GetUser with new credentials
    try:
        iam = get_session_client(session, "iam", args.region)
        iam.get_user()
        result["tests"]["iam_access"] = {"passed": True}
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "AccessDenied":
            # Credentials work but limited permissions — acceptable
            result["tests"]["iam_access"] = {"passed": True, "note": "AccessDenied (expected)"}
        else:
            result["tests"]["iam_access"] = {"passed": False, "error": str(e)}

    result["success"] = result["tests"]["sts_identity"]["passed"]
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
