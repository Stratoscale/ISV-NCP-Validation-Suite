#!/usr/bin/env python3
"""Verify that an instance's config can provision a zcompute bare-metal instance.

Unlike the AWS reference (which creates a real Launch Template from the
instance's config, then dry-run-launches from it), this script does NOT
create a Launch Template: CreateLaunchTemplate/DescribeLaunchTemplates are
confirmed NOT implemented on zcompute (AuthFailure - the same "not
implemented" signature confirmed for CreatePlacementGroup, verified twice
with working credentials on 2026-07-29).

To avoid silently reporting support zcompute doesn't have, the Launch
Template stage is explicitly marked `launch_template_supported: false` /
`config_creation_status: "skipped_not_supported"` in the output - it is
never attempted, not attempted-and-hidden. As a substitute proof that the
instance's current config can actually launch, this runs a direct
RunInstances(DryRun=True) using the instance's own AMI/instance-type/subnet/
security-groups - no Launch Template resource is created or referenced.

Output JSON:
{
    "success": true,
    "platform": "image_registry",
    "instance_id": "i-xxx",
    "launch_template_supported": false,
    "config_creation_status": "skipped_not_supported",
    "config_id": "",
    "config_name": "",
    "dry_run_method": "run_instances_direct",
    "dry_run_passed": true,
    "instance_state": "running"
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
    parser = argparse.ArgumentParser(description="Verify a zcompute BM instance's config can provision")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "symphony"))
    args = parser.parse_args()

    ec2 = get_client("ec2", region=args.region)

    result: dict[str, Any] = {
        "success": False,
        "platform": "image_registry",
        "instance_id": args.instance_id,
        "launch_template_supported": False,
        "config_creation_status": "skipped_not_supported",
        "config_id": "",
        "config_name": "",
        "dry_run_method": "run_instances_direct",
        "dry_run_passed": False,
        "instance_state": "",
    }

    print(
        "[verify_config] CreateLaunchTemplate is not implemented on zcompute "
        "(confirmed AuthFailure) - skipping launch-template creation entirely, "
        "using a direct RunInstances(DryRun=True) instead",
        file=sys.stderr,
    )

    try:
        response = ec2.describe_instances(InstanceIds=[args.instance_id])
        reservations = response.get("Reservations", [])
        if not reservations or not reservations[0].get("Instances"):
            result["error"] = f"Instance {args.instance_id} not found"
            print(json.dumps(result, indent=2))
            return 1

        instance = reservations[0]["Instances"][0]
        result["instance_state"] = instance["State"]["Name"]

        ami_id = instance["ImageId"]
        instance_type = instance["InstanceType"]
        key_name = instance.get("KeyName", "")
        sg_ids = [sg["GroupId"] for sg in instance.get("SecurityGroups", [])]
        subnet_id = instance.get("SubnetId", "")

        run_kwargs: dict[str, Any] = {
            "ImageId": ami_id,
            "InstanceType": instance_type,
            "SubnetId": subnet_id,
            "MinCount": 1,
            "MaxCount": 1,
            "DryRun": True,
        }
        if key_name:
            run_kwargs["KeyName"] = key_name
        if sg_ids:
            run_kwargs["SecurityGroupIds"] = sg_ids

        try:
            ec2.run_instances(**run_kwargs)
            # Real AWS always raises DryRunOperation on a successful dry run;
            # if zcompute returns 200 instead, treat that as passed too.
            result["dry_run_passed"] = True
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "DryRunOperation":
                result["dry_run_passed"] = True
            else:
                result["error"] = f"Dry-run failed: {e.response['Error']['Message']}"
                result["error_code"] = code
                print(json.dumps(result, indent=2))
                return 1

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
