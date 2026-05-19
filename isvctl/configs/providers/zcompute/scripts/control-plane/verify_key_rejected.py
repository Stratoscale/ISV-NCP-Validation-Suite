#!/usr/bin/env python3
"""Verify that a disabled access key is rejected by zcompute STS.

Creates a session with the disabled key and confirms that
get_caller_identity raises a ClientError (key is inactive).

Output JSON:
{
    "success": true,
    "platform": "control_plane",
    "rejected": true,
    "error_code": "InvalidClientTokenId"
}

Note: zcompute may return a different error code than AWS
(e.g. "AuthFailure" or "InvalidAccessKeyId"). The test validates
that *any* ClientError is raised, not a specific code.
"""

import argparse
import json
import os
import sys
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.client import get_session_client  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--access-key-id", required=True)
    parser.add_argument("--secret-access-key", required=True)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--wait", type=int, default=5)
    parser.add_argument("--retries", type=int, default=5)
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "rejected": False,
    }

    if args.wait > 0:
        time.sleep(args.wait)

    for attempt in range(args.retries):
        try:
            session = boto3.Session(
                aws_access_key_id=args.access_key_id,
                aws_secret_access_key=args.secret_access_key,
                region_name=args.region,
            )
            sts = get_session_client(session, "sts", args.region)
            sts.get_caller_identity()

            # Key still active - retry if we have attempts left
            if attempt < args.retries - 1:
                time.sleep(2 ** (attempt + 1))
                continue

            result["rejected"] = False
            result["error"] = "Key was NOT rejected after all retries (still active)"

        except ClientError as e:
            # Any auth error means the key was correctly blocked
            result["rejected"] = True
            result["error_code"] = e.response["Error"]["Code"]
            result["success"] = True
            break

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
