#!/usr/bin/env python3
"""Launch a zCompute VM from an uploaded image for image registry validation.

Launches a small VM from the image imported by upload_image.py, allocates
an EIP, and waits for SSH connectivity. The instance uses ubuntu as the
SSH user (Ubuntu minimal cloud images default).

zCompute-specific notes:
  - No boto3 waiters — custom polling throughout
  - No auto public IP — EIP must be allocated and associated manually
  - Root device is /dev/vda (virtio, not /dev/sda)
  - describe_instances ignores InstanceIds filter in some versions —
    results are post-filtered by InstanceId in Python
  - run_instances may return empty Instances[] — fallback polls by key name
  - TagSpecifications stripped and replaced with post-creation create_tags

Environment:
  ZCOMPUTE_TEST_VPC_ID  If set, reuse this VPC instead of creating one

Output JSON:
{
    "success": true,
    "platform": "image_registry",
    "instance_id": "i-xxx",
    "public_ip": "172.28.x.x",
    "private_ip": "10.86.x.x",
    "key_name": "isv-ir-key-xxx",
    "key_path": "/tmp/isv-ir-key-xxx.pem",
    "state": "running",
    "image_id": "<input ami-id>",
    "vpc_id": "vpc-xxx",
    "subnet_id": "subnet-xxx",
    "security_group_id": "sg-xxx",
    "eip_allocation_id": "eipalloc-xxx"
}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

# Allow importing from parent scripts/ directory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.client import get_client  # noqa: E402
from common.ec2 import (  # noqa: E402
    allocate_and_associate_eip,
    create_key_pair,
    create_security_group,
    poll_instance_state,
    wait_for_public_ip,
)
from common.ssh_utils import wait_for_ssh  # noqa: E402

DEFAULT_SSH_USER = "ubuntu"


def _get_or_create_vpc_and_subnet(ec2: Any) -> tuple[str, str, bool]:
    """Return (vpc_id, subnet_id, created_new).

    Uses ZCOMPUTE_TEST_VPC_ID if set; otherwise auto-discovers the first
    available VPC. zCompute ignores vpc-id filters on describe_subnets so
    we fetch all subnets and correlate in Python.

    Returns (vpc_id, subnet_id, created_new) where created_new is True if
    this function created a new VPC (for teardown to clean up).
    """
    vpc_id = os.environ.get("ZCOMPUTE_TEST_VPC_ID", "").strip()
    created_new = False

    if not vpc_id:
        vpcs = ec2.describe_vpcs().get("Vpcs", [])
        if vpcs:
            vpc_id = vpcs[0]["VpcId"]
            print(f"[launch] auto-discovered VPC: {vpc_id}", file=sys.stderr)
        else:
            # Create a minimal VPC for the test
            print("[launch] no VPCs found — creating 10.86.0.0/16 VPC", file=sys.stderr)
            vpc_resp = ec2.create_vpc(CidrBlock="10.86.0.0/16")
            vpc_id = vpc_resp["Vpc"]["VpcId"]
            created_new = True
            # Poll until available (zCompute VPCs start in 'pending')
            for _ in range(20):
                vpc_info = ec2.describe_vpcs(VpcIds=[vpc_id])["Vpcs"][0]
                if vpc_info.get("State") == "available":
                    break
                time.sleep(5)
            print(f"[launch] created VPC: {vpc_id}", file=sys.stderr)

            # Create subnet
            subnet_resp = ec2.create_subnet(
                VpcId=vpc_id, CidrBlock="10.86.0.0/24"
            )
            subnet_id = subnet_resp["Subnet"]["SubnetId"]
            # Poll until available
            for _ in range(20):
                sn_info = ec2.describe_subnets(SubnetIds=[subnet_id])["Subnets"][0]
                if sn_info.get("State") == "available":
                    break
                time.sleep(5)
            print(f"[launch] created subnet: {subnet_id}", file=sys.stderr)
            return vpc_id, subnet_id, created_new

    # Find a subnet in the VPC
    all_subnets = ec2.describe_subnets().get("Subnets", [])
    subnets = [s for s in all_subnets if s.get("VpcId") == vpc_id]
    if not subnets:
        raise RuntimeError(f"No subnets found in VPC {vpc_id}")
    return vpc_id, subnets[0]["SubnetId"], created_new


def _find_instance_by_key(ec2: Any, key_name: str, launched_after: float) -> str | None:
    """Fallback: find instance by key name and recent launch time.

    zCompute's run_instances occasionally returns empty Instances[].
    We can recover by describing all instances and finding the one just
    launched (matching key name and launch time within a few minutes).
    """
    try:
        resp = ec2.describe_instances()
        for res in resp.get("Reservations", []):
            for inst in res.get("Instances", []):
                if inst.get("KeyName") == key_name:
                    # Accept instances launched in the last 5 minutes
                    lt = inst.get("LaunchTime")
                    if lt:
                        import datetime
                        lt_ts = lt.timestamp() if hasattr(lt, "timestamp") else 0
                        if lt_ts >= launched_after - 300:
                            return inst["InstanceId"]
    except Exception as e:
        print(f"[launch] fallback instance search failed: {e}", file=sys.stderr)
    return None


def _convert_key_to_openssh(key_file: str) -> None:
    """Convert RSA PKCS#1 key to OpenSSH format (required by paramiko/ssh)."""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.backends import default_backend
        with open(key_file, "rb") as fh:
            pem = fh.read()
        key = serialization.load_pem_private_key(pem, password=None, backend=default_backend())
        openssh = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=serialization.NoEncryption(),
        )
        with open(key_file, "wb") as fh:
            fh.write(openssh)
        print("[launch] key converted to OpenSSH format", file=sys.stderr)
    except Exception as e:
        print(f"[launch] WARNING: key format conversion failed (non-fatal): {e}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Launch a zCompute VM from an image registry image"
    )
    parser.add_argument(
        "--image-id", required=True,
        help="EC2 AMI ID to launch (ami-xxx, from upload_image.py output)"
    )
    parser.add_argument("--instance-type", default="z2.3large")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "symphony"))
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "image_registry"}

    ec2 = get_client("ec2", region=args.region)
    run_tag = uuid.uuid4().hex[:8]
    key_name = f"isv-ir-key-{run_tag}"

    try:
        # Discover or create VPC + subnet
        vpc_id, subnet_id, _created_vpc = _get_or_create_vpc_and_subnet(ec2)
        print(f"[launch] using VPC {vpc_id}, subnet {subnet_id}", file=sys.stderr)

        # Create key pair
        key_file = create_key_pair(ec2, key_name)
        _convert_key_to_openssh(key_file)

        # Create security group with SSH ingress
        sg_name = f"isv-ir-sg-{run_tag}"
        sg_id = create_security_group(ec2, vpc_id, sg_name)

        # Launch the instance
        print(
            f"[launch] launching {args.instance_type} from {args.image_id} ...",
            file=sys.stderr,
        )
        launch_time = time.time()
        run_resp = ec2.run_instances(
            ImageId=args.image_id,
            InstanceType=args.instance_type,
            MinCount=1,
            MaxCount=1,
            KeyName=key_name,
            SubnetId=subnet_id,
            SecurityGroupIds=[sg_id],
            BlockDeviceMappings=[
                {
                    "DeviceName": "/dev/vda",
                    "Ebs": {
                        "VolumeSize": 20,
                        "VolumeType": "gp2",
                        "DeleteOnTermination": True,
                    },
                }
            ],
            # TagSpecifications not supported in all zCompute versions —
            # tags are applied via create_tags after launch instead.
        )

        instances = run_resp.get("Instances", [])
        if instances:
            instance_id = instances[0]["InstanceId"]
            private_ip = instances[0].get("PrivateIpAddress")
        else:
            # zCompute sometimes returns empty Instances[] — find via key name
            print(
                "[launch] run_instances returned empty Instances[] — scanning for new instance",
                file=sys.stderr,
            )
            time.sleep(10)
            instance_id = _find_instance_by_key(ec2, key_name, launch_time)
            if not instance_id:
                raise RuntimeError(
                    "Could not determine instance ID after run_instances (empty response)"
                )
            private_ip = None

        print(f"[launch] instance {instance_id} launched", file=sys.stderr)

        # Tag the instance (post-creation, since TagSpecifications may be ignored)
        try:
            ec2.create_tags(
                Resources=[instance_id],
                Tags=[
                    {"Key": "Name", "Value": f"isv-ir-vm-{run_tag}"},
                    {"Key": "CreatedBy", "Value": "isvtest-image-registry"},
                ],
            )
        except Exception as e:
            print(f"[launch] WARNING: tagging failed (non-fatal): {e}", file=sys.stderr)

        # Poll until running — handle shutoff/stopped intermediate states
        deadline = time.monotonic() + 900  # 15 min budget
        state = "pending"
        while time.monotonic() < deadline:
            try:
                resp = ec2.describe_instances(InstanceIds=[instance_id])
                inst_list = [
                    i for r in resp.get("Reservations", [])
                    for i in r.get("Instances", [])
                    if i["InstanceId"] == instance_id
                ]
                if inst_list:
                    state = inst_list[0]["State"]["Name"]
                    if not private_ip:
                        private_ip = inst_list[0].get("PrivateIpAddress")
            except Exception:
                pass

            if state == "running":
                print(f"[launch] instance {instance_id} is running", file=sys.stderr)
                break
            elif state in ("shutoff", "stopped"):
                # zCompute occasionally drops new instances to shutoff
                print(
                    f"[launch] instance {instance_id} is {state} — sending start",
                    file=sys.stderr,
                )
                try:
                    ec2.start_instances(InstanceIds=[instance_id])
                except Exception as e:
                    print(f"[launch] WARNING: start_instances failed: {e}", file=sys.stderr)
            else:
                print(f"[launch] waiting for running (current: {state}) ...", file=sys.stderr)
            time.sleep(30)
        else:
            raise RuntimeError(
                f"Instance {instance_id} did not reach 'running' within 15 min "
                f"(last state: {state})"
            )

        # Allocate and associate EIP (no auto-assignment in zCompute)
        allocation_id, public_ip = allocate_and_associate_eip(ec2, instance_id)

        # Wait for the public IP to be reflected in describe_instances
        confirmed_ip = wait_for_public_ip(ec2, instance_id, timeout=120, interval=5)
        if confirmed_ip:
            public_ip = confirmed_ip

        print(f"[launch] public IP: {public_ip}", file=sys.stderr)

        # Wait for SSH (Ubuntu minimal cloud images boot quickly)
        ssh_ready = wait_for_ssh(
            public_ip, DEFAULT_SSH_USER, key_file, max_attempts=40, interval=15
        )

        result = {
            "success": True,
            "platform": "image_registry",
            "instance_id": instance_id,
            "public_ip": public_ip,
            "private_ip": private_ip,
            "key_name": key_name,
            # key_path satisfies FieldExistsCheck for "key_path"
            "key_path": key_file,
            "key_file": key_file,  # used by teardown
            "state": state,
            "image_id": args.image_id,
            "instance_type": args.instance_type,
            "vpc_id": vpc_id,
            "subnet_id": subnet_id,
            "security_group_id": sg_id,
            "eip_allocation_id": allocation_id,
            "ssh_ready": ssh_ready,
        }

    except ClientError as e:
        result["error"] = str(e)
        result["error_code"] = e.response.get("Error", {}).get("Code", "")
    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
