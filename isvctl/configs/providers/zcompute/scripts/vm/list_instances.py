#!/usr/bin/env python3
"""List zcompute VM instances in a VPC.

Optionally checks that a specific target instance is present in the list.

zcompute-specific notes:
  - Filters by vpc-id to scope results to the HGX cluster VPC.
  - PublicIpAddress normalised to None if empty / "None".

Output JSON:
{
    "success": true,
    "platform": "vm",
    "instances": [
        {
            "instance_id": "i-xxx",
            "state": "running",
            "instance_type": "zh1.52xlarge",
            "public_ip": "172.28.x.x",
            "private_ip": "172.31.x.x",
            "vpc_id": "vpc-xxx",
            "subnet_id": "subnet-xxx",
            "key_name": "isv-test-key",
            "launch_time": "...",
            "tags": {"Name": "...", "CreatedBy": "isvtest"}
        }
    ],
    "total_count": 1,
    "target_found": true
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

DEFAULT_VPC_ID = "vpc-0b7f00012d3046f391a4e99399e456af"


def _normalise_ip(raw: Any) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    return None if s in ("", "None", "null") else s


def main() -> int:
    parser = argparse.ArgumentParser(description="List zcompute VM instances")
    parser.add_argument("--vpc-id", default=DEFAULT_VPC_ID)
    parser.add_argument("--instance-id", default=None, help="Target instance to look for")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "symphony"))
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "instances": [],
        "total_count": 0,
        "target_found": False,
    }

    ec2 = get_client("ec2", region=args.region)

    try:
        filters = []
        if args.vpc_id:
            filters.append({"Name": "vpc-id", "Values": [args.vpc_id]})

        kwargs: dict[str, Any] = {}
        if filters:
            kwargs["Filters"] = filters

        resp = ec2.describe_instances(**kwargs)

        instances = []
        for reservation in resp.get("Reservations", []):
            for inst in reservation.get("Instances", []):
                # Skip terminated instances.
                if inst["State"]["Name"] == "terminated":
                    continue

                launch_time_raw = inst.get("LaunchTime")
                launch_time = str(launch_time_raw) if launch_time_raw is not None else None
                tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}

                instances.append(
                    {
                        "instance_id": inst["InstanceId"],
                        "state": inst["State"]["Name"],
                        "instance_type": inst.get("InstanceType"),
                        "public_ip": _normalise_ip(inst.get("PublicIpAddress")),
                        "private_ip": inst.get("PrivateIpAddress"),
                        "vpc_id": inst.get("VpcId"),
                        "subnet_id": inst.get("SubnetId"),
                        "key_name": inst.get("KeyName"),
                        "launch_time": launch_time,
                        "tags": tags,
                    }
                )

        target_found = False
        if args.instance_id:
            target_found = any(
                i["instance_id"] == args.instance_id for i in instances
            )

        result.update(
            {
                "success": True,
                "instances": instances,
                "total_count": len(instances),
                "target_found": target_found,
                "vpc_id": args.vpc_id,
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
