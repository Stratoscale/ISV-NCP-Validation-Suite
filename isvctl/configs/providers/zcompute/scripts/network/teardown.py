#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
"""Teardown VPC and all associated resources — zCompute variant.

Wraps the upstream AWS teardown with two zCompute-specific fixes:
  1. SSL verification disabled (self-signed cert on zCompute endpoint).
  2. delete_vpc tolerates "already deleting" state — zCompute sometimes leaves
     a VPC in "deleting" state after a previous attempt; treat this as success.
  3. instance_terminated waiter replaced with poll loop (no boto3 waiters).

Usage:
    python teardown.py --vpc-id vpc-xxx --region symphony
"""

import argparse
import json
import os
import sys
import time
from typing import Any

import boto3
import urllib3
from botocore.config import Config
from botocore.exceptions import ClientError

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_here = __import__("pathlib").Path(__file__).resolve()
sys.path.insert(0, str(_here.parents[1]))   # zcompute/scripts
sys.path.insert(0, str(_here.parents[3] / "aws" / "scripts"))  # aws/scripts

from common.client import get_client  # zcompute client (verify=False)


# ── patched helpers ──────────────────────────────────────────────────────────

def _delete_with_retry(func, resource_type: str, max_retries: int = 5, **kwargs) -> bool:
    """Delete resource with retry — extended for zCompute quirks."""
    for attempt in range(max_retries):
        try:
            func(**kwargs)
            return True
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            error_msg = str(e)
            if error_code == "DependencyViolation":
                if attempt < max_retries - 1:
                    time.sleep(5 * (attempt + 1))
                    continue
            elif error_code in [
                "InvalidGroup.NotFound",
                "InvalidSubnetID.NotFound",
                "InvalidRouteTableID.NotFound",
                "InvalidInternetGatewayID.NotFound",
                "InvalidVpcID.NotFound",
                "InvalidVpc.NotFound",
            ]:
                return True  # Already deleted
            elif error_code == "InvalidParameterValue" and "deleting" in error_msg.lower():
                # zCompute: VPC is already being deleted — treat as success
                return True
            raise
    return False


def _wait_for_termination(ec2: Any, instance_ids: list[str], timeout: int = 300, interval: int = 10) -> None:
    """Poll until all instances are terminated (no boto3 waiters on zCompute)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = ec2.describe_instances(InstanceIds=instance_ids)
        states = [
            inst["State"]["Name"]
            for r in resp["Reservations"]
            for inst in r["Instances"]
        ]
        if all(s == "terminated" for s in states):
            return
        time.sleep(interval)
    # Timeout — continue anyway, teardown best-effort
    print(f"[teardown] Warning: timed out waiting for instances to terminate: {instance_ids}",
          file=sys.stderr)


def teardown_vpc(ec2: Any, vpc_id: str) -> dict[str, Any]:
    """Delete VPC and all associated resources (zCompute version)."""
    deleted: dict[str, Any] = {
        "instances": [],
        "key_pairs": [],
        "security_groups": [],
        "subnets": [],
        "route_tables": [],
        "internet_gateways": [],
        "peering_connections": [],
        "vpc": None,
    }

    # Check VPC exists before doing anything
    try:
        resp = ec2.describe_vpcs(VpcIds=[vpc_id])
        vpc_state = resp["Vpcs"][0]["State"] if resp["Vpcs"] else None
    except ClientError as e:
        if "NotFound" in str(e):
            deleted["vpc"] = vpc_id
            deleted["message"] = "VPC already deleted"
            return deleted
        raise

    if vpc_state == "deleting":
        # VPC already being deleted — wait briefly then declare success
        print(f"[teardown] VPC {vpc_id} is in deleting state, waiting for completion ...",
              file=sys.stderr)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                ec2.describe_vpcs(VpcIds=[vpc_id])
            except ClientError as e:
                if "NotFound" in str(e):
                    break
            time.sleep(10)
        deleted["vpc"] = vpc_id
        deleted["message"] = "VPC was already in deleting state"
        return deleted

    # Terminate all instances in VPC
    instances = ec2.describe_instances(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "instance-state-name", "Values": ["running", "stopped", "pending"]},
        ]
    )
    # zCompute: vpc-id filter may be ignored — post-filter by VpcId
    instance_ids = [
        inst["InstanceId"]
        for r in instances["Reservations"]
        for inst in r["Instances"]
        if inst.get("VpcId") == vpc_id
    ]

    if instance_ids:
        ec2.terminate_instances(InstanceIds=instance_ids)
        _wait_for_termination(ec2, instance_ids)
        deleted["instances"] = instance_ids

    # Delete security groups (except default)
    sgs = ec2.describe_security_groups(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
    for sg in sgs["SecurityGroups"]:
        if sg["GroupName"] != "default":
            _delete_with_retry(ec2.delete_security_group, "security_group", GroupId=sg["GroupId"])
            deleted["security_groups"].append(sg["GroupId"])

    # Delete subnets
    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
    for subnet in subnets["Subnets"]:
        _delete_with_retry(ec2.delete_subnet, "subnet", SubnetId=subnet["SubnetId"])
        deleted["subnets"].append(subnet["SubnetId"])

    # Delete non-main route tables
    rtbs = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
    for rtb in rtbs["RouteTables"]:
        is_main = any(assoc.get("Main", False) for assoc in rtb.get("Associations", []))
        if not is_main:
            for assoc in rtb.get("Associations", []):
                if not assoc.get("Main", False) and assoc.get("RouteTableAssociationId"):
                    try:
                        ec2.disassociate_route_table(AssociationId=assoc["RouteTableAssociationId"])
                    except ClientError:
                        pass
            _delete_with_retry(ec2.delete_route_table, "route_table", RouteTableId=rtb["RouteTableId"])
            deleted["route_tables"].append(rtb["RouteTableId"])

    # Detach and delete internet gateways
    igws = ec2.describe_internet_gateways(Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}])
    for igw in igws["InternetGateways"]:
        try:
            ec2.detach_internet_gateway(InternetGatewayId=igw["InternetGatewayId"], VpcId=vpc_id)
        except ClientError:
            pass
        _delete_with_retry(ec2.delete_internet_gateway, "internet_gateway",
                           InternetGatewayId=igw["InternetGatewayId"])
        deleted["internet_gateways"].append(igw["InternetGatewayId"])

    # Delete VPC peering connections
    try:
        peerings = ec2.describe_vpc_peering_connections(
            Filters=[
                {"Name": "requester-vpc-info.vpc-id", "Values": [vpc_id]},
                {"Name": "status-code", "Values": ["active", "pending-acceptance"]},
            ]
        )
        for pc in peerings.get("VpcPeeringConnections", []):
            try:
                ec2.delete_vpc_peering_connection(VpcPeeringConnectionId=pc["VpcPeeringConnectionId"])
                deleted["peering_connections"].append(pc["VpcPeeringConnectionId"])
            except ClientError:
                pass
    except ClientError:
        pass

    # Delete VPC
    _delete_with_retry(ec2.delete_vpc, "vpc", VpcId=vpc_id)
    deleted["vpc"] = vpc_id

    return deleted


def main() -> int:
    parser = argparse.ArgumentParser(description="Teardown VPC (zCompute)")
    parser.add_argument("--vpc-id", required=True)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "symphony"))
    parser.add_argument("--skip-destroy", action="store_true")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "resources_destroyed": False,
        "network_id": args.vpc_id,
        "deleted": {},
    }

    skip_destroy = args.skip_destroy or os.environ.get("AWS_NETWORK_SKIP_TEARDOWN", "").lower() == "true"
    if skip_destroy:
        result["success"] = True
        result["message"] = "Destroy skipped"
        print(json.dumps(result, indent=2))
        return 0

    ec2 = get_client("ec2", region=args.region)

    try:
        deleted = teardown_vpc(ec2, args.vpc_id)
        result["deleted"] = deleted
        result["success"] = True
        result["resources_destroyed"] = True
        result["message"] = deleted.get("message", "VPC and all resources destroyed successfully")
    except ClientError as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
