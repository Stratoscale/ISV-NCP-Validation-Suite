#!/usr/bin/env python3
"""Reinstall a zcompute bare-metal instance from its configured stock OS.

Same manual root-volume-swap approach as AWS's bare_metal/reinstall_instance.py
(stop -> detach root volume -> create volume from AMI snapshot -> attach as
root -> start), adapted to zcompute's client/no-waiter conventions.

zcompute-specific notes:
  - CreateVolume / DeleteVolume confirmed working via direct API probe.
  - AttachVolume onto a RUNNING instance returned a 403 ForbiddenException
    in that same probe (distinct from the AuthFailure "not implemented"
    pattern — looks like a real permission/state restriction from the
    vm-manager-api backend). This script attaches only after stopping the
    instance, which is the untested case — has NOT been verified end-to-end
    on zcompute. Keep `skip: true` in the config until it has been run
    successfully at least once.
  - No boto3 waiters — custom polling via common/ec2.poll_instance_state
    plus local volume-state polling here (no equivalent helper yet).

Output JSON:
{
    "success": true,
    "platform": "bm",
    "instance_id": "i-xxx",
    "state": "running",
    "public_ip": "",
    "key_file": "/tmp/isv-bm-test-key.pem",
    "ssh_ready": true,
    "reinstall_method": "root_volume_swap"
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
from common.ssh_utils import wait_for_ssh  # noqa: E402


def _poll_volume_state(ec2: Any, volume_id: str, target_states: list[str], timeout: int = 300, interval: int = 10) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = ec2.describe_volumes(VolumeIds=[volume_id])
        state = resp["Volumes"][0]["State"]
        if state in target_states:
            return state
        time.sleep(interval)
    raise TimeoutError(f"Volume {volume_id} did not reach {target_states} within {timeout}s")


def get_ami_root_snapshot(ec2: Any, ami_id: str) -> tuple[str, str]:
    images = ec2.describe_images(ImageIds=[ami_id])
    if not images["Images"]:
        raise RuntimeError(f"AMI {ami_id} not found")

    image = images["Images"][0]
    root_device = image["RootDeviceName"]

    for bdm in image.get("BlockDeviceMappings", []):
        if bdm.get("DeviceName") == root_device and "Ebs" in bdm:
            return bdm["Ebs"]["SnapshotId"], root_device

    raise RuntimeError(f"No root snapshot found in AMI {ami_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Reinstall a zcompute bare-metal instance from stock OS")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "symphony"))
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--ssh-user", default="ubuntu")
    parser.add_argument("--ami-id", default=None)
    parser.add_argument("--volume-size", type=int, default=200)
    args = parser.parse_args()

    ec2 = get_client("ec2", region=args.region)

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": args.instance_id,
        "region": args.region,
        "key_file": args.key_file,
        "ssh_user": args.ssh_user,
        "ssh_ready": False,
        "reinstall_method": "root_volume_swap",
    }

    old_volume_id = None

    try:
        print("[reinstall] getting instance details ...", file=sys.stderr)
        resp = ec2.describe_instances(InstanceIds=[args.instance_id])
        inst = resp["Reservations"][0]["Instances"][0]

        if inst["State"]["Name"] != "running":
            result["error"] = f"Instance is {inst['State']['Name']}, expected running"
            print(json.dumps(result, indent=2))
            return 1

        ami_id = args.ami_id or inst.get("ImageId")
        if not ami_id:
            result["error"] = "Cannot determine AMI ID for reinstall"
            print(json.dumps(result, indent=2))
            return 1

        result["ami_id"] = ami_id
        az = inst["Placement"]["AvailabilityZone"]
        root_device = inst.get("RootDeviceName", "/dev/vda")

        for bdm in inst.get("BlockDeviceMappings", []):
            if bdm.get("DeviceName") == root_device:
                old_volume_id = bdm["Ebs"]["VolumeId"]
                break

        if not old_volume_id:
            result["error"] = f"Cannot find root volume for device {root_device}"
            print(json.dumps(result, indent=2))
            return 1

        print(f"[reinstall] AMI: {ami_id}, root device: {root_device}, old volume: {old_volume_id}", file=sys.stderr)

        print("[reinstall] getting AMI root snapshot ...", file=sys.stderr)
        snapshot_id, _ = get_ami_root_snapshot(ec2, ami_id)
        print(f"[reinstall] snapshot: {snapshot_id}", file=sys.stderr)

        print(f"[reinstall] stopping instance {args.instance_id} ...", file=sys.stderr)
        ec2.stop_instances(InstanceIds=[args.instance_id])
        poll_instance_state(ec2, args.instance_id, ["stopped"], timeout=1800, interval=30)
        print("[reinstall] instance stopped", file=sys.stderr)

        print(f"[reinstall] detaching old root volume {old_volume_id} ...", file=sys.stderr)
        ec2.detach_volume(VolumeId=old_volume_id, InstanceId=args.instance_id, Force=True)
        _poll_volume_state(ec2, old_volume_id, ["available"])
        print("[reinstall] old volume detached", file=sys.stderr)

        print(f"[reinstall] creating new root volume from snapshot {snapshot_id} ...", file=sys.stderr)
        new_volume = ec2.create_volume(
            SnapshotId=snapshot_id,
            AvailabilityZone=az,
            VolumeType="gp3",
            Size=args.volume_size,
        )
        new_volume_id = new_volume["VolumeId"]
        result["new_volume_id"] = new_volume_id
        _poll_volume_state(ec2, new_volume_id, ["available"])
        print(f"[reinstall] new volume created: {new_volume_id}", file=sys.stderr)

        print(f"[reinstall] attaching new volume as {root_device} ...", file=sys.stderr)
        ec2.attach_volume(VolumeId=new_volume_id, InstanceId=args.instance_id, Device=root_device)
        _poll_volume_state(ec2, new_volume_id, ["in-use"])
        print("[reinstall] new volume attached", file=sys.stderr)

        print(f"[reinstall] starting instance {args.instance_id} ...", file=sys.stderr)
        ec2.start_instances(InstanceIds=[args.instance_id])
        poll_instance_state(ec2, args.instance_id, ["running"], timeout=2700, interval=30)

        resp = ec2.describe_instances(InstanceIds=[args.instance_id])
        inst = resp["Reservations"][0]["Instances"][0]

        result["state"] = inst["State"]["Name"]
        # No EIP on this cluster — private_ip is the reachable SSH target,
        # assigned immediately at boot (no wait_for_public_ip poll needed).
        result["private_ip"] = inst.get("PrivateIpAddress")
        result["public_ip"] = ""

        if not result["private_ip"]:
            result["error"] = "Instance has no private IP after reinstall"
            print(json.dumps(result, indent=2))
            return 1

        print("[reinstall] waiting for SSH ...", file=sys.stderr)
        ssh_ready = wait_for_ssh(result["private_ip"], args.ssh_user, args.key_file, max_attempts=60, interval=15)
        result["ssh_ready"] = ssh_ready

        if not ssh_ready:
            result["error"] = "SSH not ready after reinstall"
            print(json.dumps(result, indent=2))
            return 1

        result["success"] = True
        print("[reinstall] completed successfully!", file=sys.stderr)

        if old_volume_id:
            print(f"[reinstall] cleaning up old volume {old_volume_id} ...", file=sys.stderr)
            try:
                ec2.delete_volume(VolumeId=old_volume_id)
                print("[reinstall] old volume deleted", file=sys.stderr)
            except ClientError as e:
                print(f"[reinstall] warning: could not delete old volume: {e}", file=sys.stderr)

    except ClientError as e:
        result["error"] = str(e)
        result["error_code"] = e.response.get("Error", {}).get("Code", "")
    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
