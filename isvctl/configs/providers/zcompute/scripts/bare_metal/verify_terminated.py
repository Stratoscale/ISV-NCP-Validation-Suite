#!/usr/bin/env python3
"""Verify a zcompute bare-metal instance has been terminated after teardown.

Post-teardown sanitization check: confirms the instance is 'terminated' (or
gone) and that its security group / key pair were removed.

Output JSON:
{
    "success": true,
    "platform": "bm",
    "instance_id": "i-xxx",
    "state": "terminated",
    "resources_destroyed": true,
    "checks": {
        "instance": "terminated",
        "security_group": "deleted",
        "key_pair": "deleted"
    }
}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.client import get_client  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify zcompute bare-metal instance terminated")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "symphony"))
    parser.add_argument("--security-group-id", nargs="?", default=None)
    parser.add_argument("--key-name", nargs="?", default=None)
    args = parser.parse_args()

    if args.security_group_id is not None and not args.security_group_id.strip():
        args.security_group_id = None
    if args.key_name is not None and not args.key_name.strip():
        args.key_name = None

    if os.environ.get("ZCOMPUTE_BM_SKIP_TEARDOWN") == "true":
        print(
            json.dumps(
                {
                    "success": True,
                    "platform": "bm",
                    "instance_id": args.instance_id,
                    "message": "Verification skipped (ZCOMPUTE_BM_SKIP_TEARDOWN=true)",
                    "checks": {},
                },
                indent=2,
            )
        )
        return 0

    ec2 = get_client("ec2", region=args.region)

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": args.instance_id,
        "region": args.region,
        "resources_destroyed": False,
        "checks": {},
    }

    issues: list[str] = []

    try:
        resp = ec2.describe_instances(InstanceIds=[args.instance_id])
        reservations = resp.get("Reservations", [])
        if not reservations or not reservations[0].get("Instances"):
            result["state"] = "not_found"
            result["checks"]["instance"] = "not_found"
        else:
            inst = reservations[0]["Instances"][0]
            state = inst["State"]["Name"]
            result["state"] = state
            result["checks"]["instance"] = state
            if state not in ("terminated", "shutting-down"):
                issues.append(f"Instance {args.instance_id} is {state}, expected terminated")
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "InvalidInstanceID.NotFound":
            result["state"] = "not_found"
            result["checks"]["instance"] = "not_found"
        else:
            issues.append(f"Error checking instance: {e}")

    if args.security_group_id:
        try:
            ec2.describe_security_groups(GroupIds=[args.security_group_id])
            result["checks"]["security_group"] = "exists"
            issues.append(f"Security group {args.security_group_id} still exists")
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "InvalidGroup.NotFound":
                result["checks"]["security_group"] = "deleted"
            else:
                issues.append(f"Error checking SG: {e}")

    if args.key_name:
        try:
            resp = ec2.describe_key_pairs(KeyNames=[args.key_name])
            # zcompute returns an empty KeyPairs[] rather than
            # InvalidKeyPair.NotFound when the key is gone.
            if resp.get("KeyPairs"):
                result["checks"]["key_pair"] = "exists"
                issues.append(f"Key pair {args.key_name} still exists")
            else:
                result["checks"]["key_pair"] = "deleted"
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "InvalidKeyPair.NotFound":
                result["checks"]["key_pair"] = "deleted"
            else:
                issues.append(f"Error checking key pair: {e}")

    if issues:
        result["error"] = "; ".join(issues)
    else:
        result["success"] = True
        result["resources_destroyed"] = True

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
