#!/usr/bin/env python3
"""Tear down a zcompute VM instance and optionally clean up associated resources.

Flow:
  1. Get instance details (security groups, key name).
  2. Terminate the instance.
  3. Poll until 'terminated' (no boto3 waiters — custom poll).
  4. If --eip-allocation-id: release the EIP.
  5. If --delete-security-group: delete the security group (with retry).
  6. If --delete-key-pair: delete the key pair and remove the local PEM file.

zcompute-specific notes:
  - No boto3 waiters — custom polling until 'terminated'.
  - Security group deletion may fail immediately after instance termination
    because ENIs are still being cleaned up; a short retry delay is applied.

Output JSON:
{
    "success": true,
    "platform": "vm",
    "instance_id": "i-xxx",
    "state": "terminated",
    "eip_released": true,
    "security_group_deleted": true,
    "key_pair_deleted": true
}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.client import get_client  # noqa: E402
from common.ec2 import poll_instance_state  # noqa: E402


def _delete_security_group(ec2: Any, sg_id: str, max_retries: int = 5, retry_delay: int = 15) -> bool:
    """Attempt to delete a security group, retrying if dependencies still exist."""
    for attempt in range(1, max_retries + 1):
        try:
            ec2.delete_security_group(GroupId=sg_id)
            print(f"[teardown] security group {sg_id} deleted", file=sys.stderr)
            return True
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("DependencyViolation", "InvalidGroup.InUse"):
                print(
                    f"[teardown] SG {sg_id} still has dependencies "
                    f"(attempt {attempt}/{max_retries}); retrying in {retry_delay}s ...",
                    file=sys.stderr,
                )
                if attempt < max_retries:
                    time.sleep(retry_delay)
            else:
                print(
                    f"[teardown] failed to delete SG {sg_id}: {e}",
                    file=sys.stderr,
                )
                return False
    print(f"[teardown] gave up deleting SG {sg_id} after {max_retries} attempts", file=sys.stderr)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Tear down a zcompute VM instance")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "symphony"))
    parser.add_argument("--eip-allocation-id", default=None)
    parser.add_argument(
        "--delete-key-pair",
        action="store_true",
        default=False,
        help="Delete the EC2 key pair and local PEM file",
    )
    parser.add_argument(
        "--delete-security-group",
        action="store_true",
        default=False,
        help="Delete the security group(s) attached to the instance",
    )
    parser.add_argument(
        "--skip-destroy",
        action="store_true",
        default=False,
        help="Skip termination (useful for dry-run / debugging)",
    )
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "instance_id": args.instance_id,
        "state": None,
        "eip_released": False,
        "security_group_deleted": False,
        "key_pair_deleted": False,
    }

    ec2 = get_client("ec2", region=args.region)

    try:
        # Fetch instance details before termination.
        resp = ec2.describe_instances(InstanceIds=[args.instance_id])
        reservations = resp.get("Reservations", [])
        if not reservations or not reservations[0].get("Instances"):
            result["error"] = f"Instance {args.instance_id} not found"
            print(json.dumps(result, indent=2))
            return 1

        inst = reservations[0]["Instances"][0]
        current_state = inst["State"]["Name"]
        key_name = inst.get("KeyName", "")
        sg_ids = [sg["GroupId"] for sg in inst.get("SecurityGroups", [])]

        print(
            f"[teardown] instance {args.instance_id} state: {current_state}, "
            f"key: {key_name}, SGs: {sg_ids}",
            file=sys.stderr,
        )

        if args.skip_destroy:
            print("[teardown] --skip-destroy set; skipping termination", file=sys.stderr)
            result["state"] = current_state
            result["success"] = True
            print(json.dumps(result, indent=2))
            return 0

        # Terminate the instance (idempotent — skip if already terminated).
        if current_state not in ("terminated", "shutting-down"):
            print(f"[teardown] terminating {args.instance_id} ...", file=sys.stderr)
            ec2.terminate_instances(InstanceIds=[args.instance_id])
        else:
            print(
                f"[teardown] instance already in state '{current_state}'; skipping terminate",
                file=sys.stderr,
            )

        # Poll until terminated.
        final_state = poll_instance_state(
            ec2, args.instance_id, ["terminated"], timeout=600, interval=15
        )
        result["state"] = final_state

        # Release EIP.
        if args.eip_allocation_id:
            try:
                ec2.release_address(AllocationId=args.eip_allocation_id)
                result["eip_released"] = True
                print(
                    f"[teardown] EIP {args.eip_allocation_id} released",
                    file=sys.stderr,
                )
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                print(
                    f"[teardown] failed to release EIP {args.eip_allocation_id}: "
                    f"{code} — {e}",
                    file=sys.stderr,
                )

        # Delete security group(s).
        if args.delete_security_group and sg_ids:
            for sg_id in sg_ids:
                deleted = _delete_security_group(ec2, sg_id)
                if deleted:
                    result["security_group_deleted"] = True

        # Delete key pair and local PEM.
        if args.delete_key_pair and key_name:
            try:
                ec2.delete_key_pair(KeyName=key_name)
                result["key_pair_deleted"] = True
                print(f"[teardown] key pair '{key_name}' deleted from EC2", file=sys.stderr)
            except ClientError as e:
                print(
                    f"[teardown] failed to delete key pair '{key_name}': {e}",
                    file=sys.stderr,
                )

            # Remove local PEM file.
            pem_path = f"/tmp/{key_name}.pem"
            if os.path.exists(pem_path):
                os.remove(pem_path)
                print(f"[teardown] removed local PEM {pem_path}", file=sys.stderr)

        result["success"] = final_state == "terminated"

    except ClientError as e:
        result["error"] = str(e)
        result["error_code"] = e.response.get("Error", {}).get("Code", "")
    except TimeoutError as e:
        result["error"] = str(e)
    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
