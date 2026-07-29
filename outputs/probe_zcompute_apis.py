#!/usr/bin/env python3
"""Probe zCompute EC2-compatible API for bare-metal (BM) suite readiness.

Focused on the API calls the AWS bare_metal provider scripts need that are
NOT already exercised (and confirmed) by the VM/Network suites — see
isvctl/configs/providers/zcompute/COMPATIBILITY_REPORT.md for the baseline.

Already confirmed by VM/Network suites (not re-tested here):
    RunInstances, DescribeInstances, StartInstances, StopInstances,
    RebootInstances, TerminateInstances, DescribeImages, CreateKeyPair,
    DescribeKeyPairs, DeleteKeyPair, CreateSecurityGroup,
    AuthorizeSecurityGroupIngress, DescribeSecurityGroups,
    DeleteSecurityGroup, DescribeVpcs, DescribeSubnets

New / untested for bare metal, probed here:
    DescribeInstanceTypeOfferings, GetConsoleOutput,
    CreatePlacementGroup, DescribePlacementGroups, DeletePlacementGroup,
    CreateVolume, AttachVolume, DetachVolume, DeleteVolume,
    StopInstances(Force=True), DetachVolume(Force=True)

Usage:
    ZCOMPUTE_BASE_URL=https://172.16.10.140 \
    AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_REGION=symphony \
    python3 outputs/probe_zcompute_apis.py

Prints a JSON report to stdout; human-readable progress to stderr.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from typing import Any

import boto3
import urllib3
from botocore.exceptions import ClientError

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def get_client(service: str) -> Any:
    base = os.environ.get("ZCOMPUTE_BASE_URL", "").rstrip("/")
    if not base:
        raise RuntimeError("ZCOMPUTE_BASE_URL is not set")
    region = os.environ.get("AWS_REGION", "symphony")
    return boto3.client(
        service,
        region_name=region,
        endpoint_url=f"{base}/api/v2/aws/{service}/",
        verify=False,
    )


def record(report: dict[str, Any], call: str, status: str, detail: str = "") -> None:
    report["calls"][call] = {"status": status, "detail": detail}
    print(f"[{status.upper():^12}] {call} — {detail}", file=sys.stderr)


def probe_describe_instance_type_offerings(ec2: Any, report: dict[str, Any]) -> None:
    call = "DescribeInstanceTypeOfferings"
    try:
        resp = ec2.describe_instance_type_offerings(LocationType="availability-zone")
        offerings = resp.get("InstanceTypeOfferings", [])
        if offerings:
            record(report, call, "supported", f"{len(offerings)} offerings returned")
        else:
            record(report, call, "degraded", "call succeeded but returned empty list (AZ/instance-type filtering unusable)")
    except ClientError as e:
        record(report, call, "not_supported", str(e))
    except Exception as e:
        record(report, call, "error", str(e))


def probe_placement_groups(ec2: Any, report: dict[str, Any]) -> None:
    group_name = f"isv-probe-pg-{uuid.uuid4().hex[:8]}"

    try:
        ec2.create_placement_group(GroupName=group_name, Strategy="cluster")
        record(report, "CreatePlacementGroup", "supported", f"created {group_name}")
    except ClientError as e:
        record(report, "CreatePlacementGroup", "not_supported", str(e))
        record(report, "DescribePlacementGroups", "skipped", "create failed, nothing to describe")
        record(report, "DeletePlacementGroup", "skipped", "create failed, nothing to delete")
        return
    except Exception as e:
        record(report, "CreatePlacementGroup", "error", str(e))
        return

    try:
        resp = ec2.describe_placement_groups(GroupNames=[group_name])
        groups = resp.get("PlacementGroups", [])
        state = groups[0].get("State") if groups else "not_found"
        record(report, "DescribePlacementGroups", "supported" if groups else "degraded", f"state={state}")
    except ClientError as e:
        record(report, "DescribePlacementGroups", "not_supported", str(e))
    except Exception as e:
        record(report, "DescribePlacementGroups", "error", str(e))

    try:
        ec2.delete_placement_group(GroupName=group_name)
        record(report, "DeletePlacementGroup", "supported", f"deleted {group_name}")
    except ClientError as e:
        record(report, "DeletePlacementGroup", "not_supported", str(e))
    except Exception as e:
        record(report, "DeletePlacementGroup", "error", str(e))


def probe_volumes(ec2: Any, report: dict[str, Any]) -> None:
    az = os.environ.get("AWS_REGION", "symphony")
    try:
        resp = ec2.describe_availability_zones()
        zones = resp.get("AvailabilityZones", [])
        if zones:
            az = zones[0]["ZoneName"]
    except Exception:
        pass

    volume_id = None
    try:
        resp = ec2.create_volume(AvailabilityZone=az, VolumeType="gp3", Size=8)
        volume_id = resp.get("VolumeId")
        record(report, "CreateVolume", "supported", f"created {volume_id} in {az}")
    except ClientError as e:
        record(report, "CreateVolume", "not_supported", str(e))
    except Exception as e:
        record(report, "CreateVolume", "error", str(e))

    if not volume_id:
        record(report, "AttachVolume", "skipped", "no volume created")
        record(report, "DetachVolume", "skipped", "no volume created")
        record(report, "DeleteVolume", "skipped", "no volume created")
        return

    # Poll briefly for the volume to become available before delete.
    for _ in range(12):
        try:
            resp = ec2.describe_volumes(VolumeIds=[volume_id])
            state = resp["Volumes"][0]["State"]
            if state == "available":
                break
        except Exception:
            pass
        time.sleep(5)

    record(
        report,
        "AttachVolume",
        "not_probed",
        "requires a running instance — not tested standalone (see instance-dependent section)",
    )
    record(
        report,
        "DetachVolume",
        "not_probed",
        "requires a running instance — not tested standalone (see instance-dependent section)",
    )

    try:
        ec2.delete_volume(VolumeId=volume_id)
        record(report, "DeleteVolume", "supported", f"deleted {volume_id}")
    except ClientError as e:
        record(report, "DeleteVolume", "not_supported", str(e))
    except Exception as e:
        record(report, "DeleteVolume", "error", str(e))


def find_any_instance(ec2: Any) -> str | None:
    try:
        resp = ec2.describe_instances()
        for res in resp.get("Reservations", []):
            for inst in res.get("Instances", []):
                if inst["State"]["Name"] not in ("terminated", "shutting-down"):
                    return inst["InstanceId"]
    except Exception:
        pass
    return None


def probe_instance_dependent(ec2: Any, report: dict[str, Any]) -> None:
    instance_id = os.environ.get("ZCOMPUTE_PROBE_INSTANCE_ID", "").strip() or find_any_instance(ec2)

    if not instance_id:
        for call in ("GetConsoleOutput", "StopInstances(Force=True)", "AttachVolume", "DetachVolume"):
            record(
                report,
                call,
                "not_probed",
                "no running/stopped instance available on this cluster to test against "
                "(set ZCOMPUTE_PROBE_INSTANCE_ID to test against a specific instance)",
            )
        return

    report["probed_against_instance"] = instance_id

    try:
        resp = ec2.get_console_output(InstanceId=instance_id, Latest=True)
        output = resp.get("Output", "")
        record(report, "GetConsoleOutput", "supported" if output else "degraded", f"output_length={len(output)}")
    except ClientError as e:
        record(report, "GetConsoleOutput", "not_supported", str(e))
    except Exception as e:
        record(report, "GetConsoleOutput", "error", str(e))

    # Force=True stop/detach are NOT exercised destructively against a real
    # instance by this probe (would disrupt whatever else is using it).
    record(
        report,
        "StopInstances(Force=True)",
        "not_probed",
        f"skipped destructive test against live instance {instance_id} — needs a dedicated disposable instance",
    )
    record(
        report,
        "AttachVolume",
        "not_probed",
        f"skipped destructive test against live instance {instance_id} — needs a dedicated disposable instance",
    )
    record(
        report,
        "DetachVolume",
        "not_probed",
        f"skipped destructive test against live instance {instance_id} — needs a dedicated disposable instance",
    )


def main() -> int:
    report: dict[str, Any] = {
        "zcompute_base_url": os.environ.get("ZCOMPUTE_BASE_URL", ""),
        "region": os.environ.get("AWS_REGION", "symphony"),
        "calls": {},
    }

    ec2 = get_client("ec2")

    probe_describe_instance_type_offerings(ec2, report)
    probe_placement_groups(ec2, report)
    probe_volumes(ec2, report)
    probe_instance_dependent(ec2, report)

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
