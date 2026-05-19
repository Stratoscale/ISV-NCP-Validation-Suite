#!/usr/bin/env python3
"""Describe a zcompute VM instance.

Returns key instance attributes including state, IPs, VPC/subnet,
instance type, and SSH configuration.

zcompute-specific notes:
  - PublicIpAddress may be empty string, "None", or None — normalised to null.
  - LaunchTime may be a datetime or string — serialised with str().

Output JSON:
{
    "success": true,
    "platform": "vm",
    "instance_id": "i-xxx",
    "state": "running",
    "public_ip": "172.28.x.x",
    "private_ip": "172.31.x.x",
    "vpc_id": "vpc-xxx",
    "subnet_id": "subnet-xxx",
    "instance_type": "zh1.52xlarge",
    "ami_id": "ami-xxx",
    "key_name": "isv-test-key",
    "key_file": "/tmp/isv-test-key.pem",
    "ssh_user": "ubuntu",
    "launch_time": "2024-01-01 00:00:00+00:00",
    "tags": {"Name": "...", "CreatedBy": "isvtest"}
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


def _normalise_ip(raw: Any) -> str | None:
    """Return None if the IP is absent, empty, or the string 'None'."""
    if raw is None:
        return None
    s = str(raw).strip()
    if s in ("", "None", "null"):
        return None
    return s


def main() -> int:
    parser = argparse.ArgumentParser(description="Describe a zcompute VM instance")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "symphony"))
    parser.add_argument("--key-file", default=None)
    parser.add_argument("--ssh-user", default="ubuntu")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "instance_id": args.instance_id,
    }

    ec2 = get_client("ec2", region=args.region)

    try:
        resp = ec2.describe_instances(InstanceIds=[args.instance_id])
        reservations = resp.get("Reservations", [])
        if not reservations or not reservations[0].get("Instances"):
            result["error"] = f"Instance {args.instance_id} not found"
            print(json.dumps(result, indent=2))
            return 1

        inst = reservations[0]["Instances"][0]

        # Serialise LaunchTime safely — may be datetime or string.
        launch_time_raw = inst.get("LaunchTime")
        launch_time = str(launch_time_raw) if launch_time_raw is not None else None

        # Normalise tag list to a flat dict.
        tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}

        result.update(
            {
                "success": True,
                "state": inst["State"]["Name"],
                "public_ip": _normalise_ip(inst.get("PublicIpAddress")),
                "private_ip": inst.get("PrivateIpAddress"),
                "vpc_id": inst.get("VpcId"),
                "subnet_id": inst.get("SubnetId"),
                "instance_type": inst.get("InstanceType"),
                "ami_id": inst.get("ImageId"),
                "key_name": inst.get("KeyName"),
                "key_file": args.key_file,
                "ssh_user": args.ssh_user,
                "launch_time": launch_time,
                "tags": tags,
            }
        )

    except ClientError as e:
        result["error"] = str(e)
        result["error_code"] = e.response.get("Error", {}).get("Code", "")
    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
