#!/usr/bin/env python3
"""Test that a newly created access key can authenticate against zcompute.

Creates a fresh boto3 Session with the test credentials and calls
STS get_caller_identity through the zcompute endpoint.

Output JSON:
{
    "success": true,
    "platform": "control_plane",
    "authenticated": true,
    "identity_id": "arn:...",
    "account_id": "123456"
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--access-key-id", required=True)
    parser.add_argument("--secret-access-key", required=True)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--wait", type=int, default=5, help="Seconds to wait for key propagation")
    parser.add_argument("--retries", type=int, default=3)
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "authenticated": False,
    }

    if args.wait > 0:
        time.sleep(args.wait)

    last_error = None
    for attempt in range(args.retries):
        try:
            session = boto3.Session(
                aws_access_key_id=args.access_key_id,
                aws_secret_access_key=args.secret_access_key,
                region_name=args.region,
            )
            sts = get_session_client(session, "sts", args.region)
            identity = sts.get_caller_identity()

            result["authenticated"] = True
            result["identity_id"] = identity.get("Arn", "unknown")
            result["account_id"] = identity.get("Account", "unknown")
            result["success"] = True
            break

        except (ClientError, NoCredentialsError) as e:
            last_error = str(e)
            if attempt < args.retries - 1:
                time.sleep(2 ** (attempt + 1))

    if not result["success"]:
        result["error"] = last_error

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
