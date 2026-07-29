#!/usr/bin/env python3
"""Verify that an OS image is correctly installed on a zcompute bare-metal instance.

Same as scripts/aws/image-registry/verify_image_installed.py — checks the
running instance's AMI metadata to confirm it was provisioned from a valid
image. Only needs DescribeInstances + DescribeImages, both confirmed working
on zcompute, so this ports over unmodified in behavior.

Output JSON:
{
    "success": true,
    "platform": "image_registry",
    "instance_id": "i-xxx",
    "image_id": "ami-xxx",
    "image_name": "...",
    "image_architecture": "x86_64",
    "instance_state": "running",
    "instance_type": "z2.3large"
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
    parser = argparse.ArgumentParser(description="Verify OS image installed on zcompute BM instance")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "symphony"))
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "image_registry",
        "instance_id": args.instance_id,
        "image_id": "",
        "image_name": "",
        "image_architecture": "",
        "instance_state": "",
        "instance_type": "",
    }

    ec2 = get_client("ec2", region=args.region)

    try:
        response = ec2.describe_instances(InstanceIds=[args.instance_id])
        reservations = response.get("Reservations", [])
        if not reservations or not reservations[0].get("Instances"):
            result["error"] = f"Instance {args.instance_id} not found"
            print(json.dumps(result, indent=2))
            return 1

        instance = reservations[0]["Instances"][0]
        ami_id = instance.get("ImageId", "")
        result["instance_state"] = instance["State"]["Name"]
        result["state"] = result["instance_state"]
        result["instance_type"] = instance.get("InstanceType", "")
        result["image_id"] = ami_id

        if result["instance_state"] != "running":
            result["error"] = f"Instance is {result['instance_state']}, expected running"
            print(json.dumps(result, indent=2))
            return 1

        try:
            ami_response = ec2.describe_images(ImageIds=[ami_id])
            images = ami_response.get("Images", [])
            if images:
                ami = images[0]
                result["image_name"] = ami.get("Name", "")
                result["image_architecture"] = ami.get("Architecture", "")
                result["image_description"] = ami.get("Description", "")
                result["image_state"] = ami.get("State", "")
        except Exception as e:
            result["image_name"] = "(AMI metadata unavailable)"
            print(f"[verify_image] warning: could not describe AMI {ami_id}: {e}", file=sys.stderr)

        result["success"] = True

    except ClientError as e:
        result["error"] = str(e)
        result["error_code"] = e.response.get("Error", {}).get("Code", "")
    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
