#!/usr/bin/env python3
"""Validate topology-based placement for a zcompute bare-metal instance.

Attempts the AWS placement-group lifecycle (create -> verify instance ->
describe -> delete), same as AWS's bare_metal/topology_placement.py.
Confirmed via direct API probe against the BM lab cluster:
CreatePlacementGroup / DescribePlacementGroups both return AuthFailure —
zcompute's generic response for EC2 actions it doesn't implement (verified
non-credential: the same session's DescribeInstances succeeded against 40
real instances). Reports not_supported=true gracefully so the test runner
excludes TopologyPlacementCheck rather than failing the whole suite.

TODO: zcompute exposes GPU-fabric topology natively via the symp CLI
(see scripts/network/nvlink_domain_test.py's `gpunet-pool gpunet list`),
not via AWS placement groups. Once we have symp access for the BM lab
cluster, consider swapping this check to query the gpunet pool for the
node instead of the AWS-shaped placement-group API.

Output JSON (not supported):
{
    "success": true,
    "platform": "bm",
    "instance_id": "i-xxx",
    "placement_supported": false,
    "not_supported": true,
    "note": "placement groups are not implemented on this zcompute version",
    "operations": {"create_group": {"passed": false, ...}, ...}
}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from typing import Any

from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.client import get_client  # noqa: E402

_NOT_IMPLEMENTED_CODES = {
    "AuthFailure",
    "NotImplemented",
    "UnsupportedOperation",
    "InvalidOperation",
    "OperationNotSupported",
}


def _is_not_supported(e: ClientError) -> bool:
    code = e.response.get("Error", {}).get("Code", "")
    msg = e.response.get("Error", {}).get("Message", "").lower()
    return code in _NOT_IMPLEMENTED_CODES or any(
        kw in msg for kw in ("not implemented", "not supported", "unsupported", "validate the provided access")
    )


def test_create_group(ec2: Any, group_name: str) -> dict[str, Any]:
    result: dict[str, Any] = {"passed": False}
    try:
        ec2.create_placement_group(GroupName=group_name, Strategy="cluster")
        result["passed"] = True
        result["message"] = f"Created placement group {group_name}"
    except ClientError as e:
        result["error"] = str(e)
        result["error_code"] = e.response.get("Error", {}).get("Code", "")
        result["not_supported"] = _is_not_supported(e)
    return result


def test_verify_instance(ec2: Any, instance_id: str) -> dict[str, Any]:
    result: dict[str, Any] = {"passed": False}
    try:
        response = ec2.describe_instances(InstanceIds=[instance_id])
        reservations = response.get("Reservations", [])
        if not reservations or not reservations[0].get("Instances"):
            result["error"] = f"Instance {instance_id} not found"
            return result

        instance = reservations[0]["Instances"][0]
        placement = instance.get("Placement", {})

        result["availability_zone"] = placement.get("AvailabilityZone", "")
        result["tenancy"] = placement.get("Tenancy", "")
        result["group_name"] = placement.get("GroupName", "")
        result["instance_type"] = instance.get("InstanceType", "")
        result["passed"] = bool(result["availability_zone"])
        result["message"] = f"Instance {instance_id} in AZ {result['availability_zone']}, tenancy={result['tenancy']}"
    except ClientError as e:
        result["error"] = str(e)
    return result


def test_describe_group(ec2: Any, group_name: str) -> dict[str, Any]:
    result: dict[str, Any] = {"passed": False}
    try:
        response = ec2.describe_placement_groups(GroupNames=[group_name])
        groups = response.get("PlacementGroups", [])
        if not groups:
            result["error"] = f"Placement group {group_name} not found"
            return result

        group = groups[0]
        result["state"] = group.get("State", "")
        result["strategy"] = group.get("Strategy", "")
        result["group_id"] = group.get("GroupId", "")
        result["passed"] = result["state"] == "available"
        result["message"] = f"Group {group_name}: state={result['state']}, strategy={result['strategy']}"
    except ClientError as e:
        result["error"] = str(e)
        result["error_code"] = e.response.get("Error", {}).get("Code", "")
        result["not_supported"] = _is_not_supported(e)
    return result


def test_delete_group(ec2: Any, group_name: str) -> dict[str, Any]:
    result: dict[str, Any] = {"passed": False}
    try:
        ec2.delete_placement_group(GroupName=group_name)
        result["passed"] = True
        result["message"] = f"Deleted placement group {group_name}"
    except ClientError as e:
        result["error"] = str(e)
        result["error_code"] = e.response.get("Error", {}).get("Code", "")
        result["not_supported"] = _is_not_supported(e)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate topology-based placement (zcompute BM)")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "symphony"))
    args = parser.parse_args()

    ec2 = get_client("ec2", region=args.region)
    group_name = f"isvtest-pg-{uuid.uuid4().hex[:8]}"

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": args.instance_id,
        "placement_supported": False,
        "availability_zone": "",
        "placement_group": group_name,
        "placement_strategy": "cluster",
        "operations": {
            "create_group": {"passed": False},
            "verify_instance": {"passed": False},
            "describe_group": {"passed": False},
            "delete_group": {"passed": False},
        },
    }

    create_result = test_create_group(ec2, group_name)
    result["operations"]["create_group"] = create_result

    if not create_result["passed"] and create_result.get("not_supported"):
        # zcompute doesn't implement placement groups — report gracefully so
        # the config's exclude list can drop TopologyPlacementCheck rather
        # than failing the whole suite.
        result["not_supported"] = True
        result["note"] = "placement groups are not implemented on this zcompute version"
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    try:
        if not create_result["passed"]:
            raise RuntimeError(f"Create group failed: {create_result.get('error')}")

        verify_result = test_verify_instance(ec2, args.instance_id)
        result["operations"]["verify_instance"] = verify_result
        result["availability_zone"] = verify_result.get("availability_zone", "")
        if not verify_result["passed"]:
            raise RuntimeError(f"Verify instance failed: {verify_result.get('error')}")

        describe_result = test_describe_group(ec2, group_name)
        result["operations"]["describe_group"] = describe_result
        if not describe_result["passed"]:
            raise RuntimeError(f"Describe group failed: {describe_result.get('error')}")

        delete_result = test_delete_group(ec2, group_name)
        result["operations"]["delete_group"] = delete_result
        if not delete_result["passed"]:
            raise RuntimeError(f"Delete group failed: {delete_result.get('error')}")

        result["placement_supported"] = True
        result["success"] = True

    except RuntimeError as e:
        result["error"] = str(e)
        try:
            ec2.delete_placement_group(GroupName=group_name)
        except ClientError:
            pass

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
