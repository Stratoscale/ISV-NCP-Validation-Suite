#!/usr/bin/env python3
"""Launch a zcompute bare-metal GPU instance for BMaaS validation.

Same lifecycle as scripts/vm/launch_instance.py, adapted for bare-metal
instance types (e.g. g4dn.metal — the zcompute BM instance type, backed by
NVIDIA GB200 hardware once the lab cluster grants access). Larger root
volume than the VM path since BM workloads typically need more local disk.

zcompute-specific notes (same as VM path):
  - No boto3 waiters — uses custom polling.
  - Root device is /dev/vda (not /dev/sda).
  - No auto public IP — EIP must be allocated and associated manually.
  - NVIDIA modules must be loaded via modprobe after SSH.
  - CreateSecurityGroup / CreateKeyPair do not support TagSpecifications
    (common/ec2.py helpers already omit them); RunInstances does accept
    TagSpecifications (confirmed working by the VM suite).
  - GPU driver/CUDA install commands in setup_gpu_dependencies() target
    x86_64 Ubuntu packages — will need revisiting once real GB200 (ARM64
    Grace-Blackwell) lab access is available.
  - Pass --skip-gpu-setup when launching a non-GPU stand-in instance type
    (e.g. z2.3large, used for integration-testing this suite's lifecycle
    mechanics before real BM/GB200 support exists) to skip the NVIDIA
    modprobe / Docker+CUDA+NCT install work entirely.
  - RunInstances is launched with UserData from cloud-init.yaml (colocated
    with this script, or override via --user-data-file /
    ZCOMPUTE_BM_USER_DATA_FILE) — enrolls the instance into the lab
    WireGuard VPN mesh so it's reachable, and sets up SSH access. UserData
    support on zcompute's RunInstances is unconfirmed until tested live;
    result["user_data_applied"] records whether it was sent (not whether
    zcompute actually honored it — that still needs to be verified against
    the launched instance).

Environment:
    ZCOMPUTE_BM_INSTANCE_ID  - if set, reuse this instance instead of launching
    ZCOMPUTE_BM_KEY_FILE     - PEM file for the reused instance

Output JSON:
{
    "success": true, "platform": "bm",
    "instance_id": "i-xxx", "instance_type": "g4dn.metal",
    "public_ip": "172.28.x.x", "private_ip": "172.31.x.x",
    "state": "running", "ami_id": "ami-xxx",
    "key_name": "isv-bm-test-key", "key_file": "/tmp/isv-bm-test-key.pem",
    "vpc_id": "vpc-xxx", "subnet_id": "subnet-xxx",
    "security_group_id": "sg-xxx", "eip_allocation_id": "eipalloc-xxx"
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
from common.ec2 import (  # noqa: E402
    allocate_and_associate_eip,
    create_key_pair,
    create_security_group,
    load_nvidia_modules,
    poll_instance_state,
    setup_gpu_dependencies,
    wait_for_public_ip,
)
from common.ssh_utils import wait_for_ssh  # noqa: E402

# ZM.gpu.gb200.a06.ibx4.dpux2 is zcompute's instance-type alias for the
# real GB200-backed BM hardware (equivalent to AWS's g4dn.metal in the
# canonical suite).
DEFAULT_INSTANCE_TYPE = os.environ.get("ZCOMPUTE_BM_INSTANCE_TYPE", "ZM.gpu.gb200.a06.ibx4.dpux2")
DEFAULT_AMI_ID = os.environ.get("ZCOMPUTE_BM_AMI_ID", "")
DEFAULT_KEY_NAME = "isv-bm-test-key"
DEFAULT_SSH_USER = "ubuntu"
DEFAULT_USER_DATA_FILE = os.environ.get(
    "ZCOMPUTE_BM_USER_DATA_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "cloud-init.yaml"),
)


def _get_default_vpc_and_subnet(ec2: Any) -> tuple[str, str]:
    """Auto-discover the first available VPC and one of its subnets.

    zCompute ignores vpc-id filters on describe_subnets, so we fetch
    all subnets and correlate them with VPCs in Python.
    """
    vpc_id = os.environ.get("ZCOMPUTE_BM_VPC_ID", "").strip()
    if not vpc_id:
        vpcs = ec2.describe_vpcs().get("Vpcs", [])
        if not vpcs:
            raise RuntimeError("No VPCs found in zCompute project")
        vpc_id = vpcs[0]["VpcId"]
        print(f"[launch] auto-discovered VPC: {vpc_id}", file=sys.stderr)

    all_subnets = ec2.describe_subnets().get("Subnets", [])
    subnets = [s for s in all_subnets if s.get("VpcId") == vpc_id]
    if not subnets:
        raise RuntimeError(f"No subnets found in VPC {vpc_id}")
    return vpc_id, subnets[0]["SubnetId"]


def _reuse_instance(ec2: Any, instance_id: str, key_file: str) -> dict[str, Any]:
    """Describe and optionally start an existing instance, then return its details."""
    print(
        f"[launch] reusing existing instance {instance_id} (ZCOMPUTE_BM_INSTANCE_ID)",
        file=sys.stderr,
    )
    resp = ec2.describe_instances(InstanceIds=[instance_id])
    inst = resp["Reservations"][0]["Instances"][0]
    state = inst["State"]["Name"]

    if state == "stopped":
        print("[launch] instance is stopped; starting it ...", file=sys.stderr)
        ec2.start_instances(InstanceIds=[instance_id])
        state = poll_instance_state(ec2, instance_id, ["running"], timeout=2700, interval=30)
    elif state != "running":
        state = poll_instance_state(ec2, instance_id, ["running"], timeout=2700, interval=30)

    resp = ec2.describe_instances(InstanceIds=[instance_id])
    inst = resp["Reservations"][0]["Instances"][0]

    public_ip = inst.get("PublicIpAddress")
    if not public_ip or public_ip in ("", "None"):
        public_ip = None

    return {
        "success": True,
        "platform": "bm",
        "instance_id": instance_id,
        "instance_type": inst.get("InstanceType", ""),
        "public_ip": public_ip,
        "private_ip": inst.get("PrivateIpAddress"),
        "state": state,
        "ami_id": inst.get("ImageId", ""),
        "key_name": inst.get("KeyName", ""),
        "key_file": key_file,
        "vpc_id": inst.get("VpcId", ""),
        "subnet_id": inst.get("SubnetId", ""),
        "security_group_id": None,
        "eip_allocation_id": None,
        "reused": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch a zcompute bare-metal instance")
    parser.add_argument("--name", default="isv-bm-test-gpu")
    parser.add_argument("--instance-type", default=DEFAULT_INSTANCE_TYPE)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "symphony"))
    parser.add_argument("--ami-id", default=DEFAULT_AMI_ID)
    parser.add_argument("--vpc-id", default=None)
    parser.add_argument("--subnet-id", default=None)
    parser.add_argument("--key-name", default=DEFAULT_KEY_NAME)
    parser.add_argument("--ssh-user", default=DEFAULT_SSH_USER)
    parser.add_argument(
        "--volume-size",
        type=int,
        default=200,
        help="Root volume size in GiB (default: 200, larger for BM workloads)",
    )
    parser.add_argument(
        "--skip-gpu-setup",
        action="store_true",
        help="Skip NVIDIA module load / Docker+CUDA+NCT install after SSH comes up "
             "(for non-GPU stand-in instance types used for integration testing)",
    )
    parser.add_argument(
        "--user-data-file",
        default=DEFAULT_USER_DATA_FILE,
        help="cloud-config file passed as RunInstances UserData (default: "
             "cloud-init.yaml colocated with this script). Pass an empty "
             "string to launch with no user-data.",
    )
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "bm"}

    existing_id = os.environ.get("ZCOMPUTE_BM_INSTANCE_ID", "").strip()
    existing_key = os.environ.get("ZCOMPUTE_BM_KEY_FILE", "").strip()

    ec2 = get_client("ec2", region=args.region)

    try:
        if existing_id and existing_key:
            result = _reuse_instance(ec2, existing_id, existing_key)
            public_ip = result.get("public_ip")
            if public_ip:
                ssh_ready = wait_for_ssh(
                    public_ip, args.ssh_user, existing_key, max_attempts=60, interval=15
                )
                result["ssh_ready"] = ssh_ready
                if ssh_ready and not args.skip_gpu_setup:
                    nvidia_ok = load_nvidia_modules(public_ip, args.ssh_user, existing_key)
                    result["nvidia_modules_loaded"] = nvidia_ok
            return 0 if result.get("success") else 1

        if not args.ami_id:
            raise RuntimeError(
                "No AMI specified. Set --ami-id or ZCOMPUTE_BM_AMI_ID "
                "(no bare-metal-ready image exists yet on this cluster)."
            )

        if args.vpc_id and args.subnet_id:
            vpc_id, subnet_id = args.vpc_id, args.subnet_id
        else:
            vpc_id, subnet_id = _get_default_vpc_and_subnet(ec2)
            if args.vpc_id:
                vpc_id = args.vpc_id
        print(f"[launch] using VPC {vpc_id}, subnet {subnet_id}", file=sys.stderr)

        key_file = create_key_pair(ec2, args.key_name)

        # zCompute returns RSA PKCS#1 keys; paramiko-based checks require
        # OpenSSH format. Convert in-place.
        try:
            from cryptography.hazmat.backends import default_backend
            from cryptography.hazmat.primitives import serialization

            with open(key_file, "rb") as _f:
                _pem = _f.read()
            _key = serialization.load_pem_private_key(_pem, password=None, backend=default_backend())
            _openssh = _key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.OpenSSH,
                encryption_algorithm=serialization.NoEncryption(),
            )
            with open(key_file, "wb") as _f:
                _f.write(_openssh)
            print("[launch] key converted to OpenSSH format", file=sys.stderr)
        except Exception as _e:
            print(f"[launch] WARNING: key format conversion failed (non-fatal): {_e}", file=sys.stderr)

        sg_name = f"{args.name}-sg"
        sg_id = create_security_group(ec2, vpc_id, sg_name)

        user_data = ""
        if args.user_data_file:
            with open(args.user_data_file, encoding="utf-8") as _f:
                user_data = _f.read()
            print(f"[launch] loaded user-data from {args.user_data_file}", file=sys.stderr)

        print(
            f"[launch] launching {args.instance_type} from {args.ami_id} ...",
            file=sys.stderr,
        )
        run_kwargs: dict[str, Any] = dict(
            ImageId=args.ami_id,
            InstanceType=args.instance_type,
            MinCount=1,
            MaxCount=1,
            KeyName=args.key_name,
            SubnetId=subnet_id,
            SecurityGroupIds=[sg_id],
            BlockDeviceMappings=[
                {
                    "DeviceName": "/dev/vda",
                    "Ebs": {
                        "VolumeSize": args.volume_size,
                        "VolumeType": "gp3",
                        "DeleteOnTermination": True,
                    },
                }
            ],
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "Name", "Value": args.name},
                        {"Key": "Platform", "Value": "bare-metal"},
                        {"Key": "CreatedBy", "Value": "isvtest"},
                    ],
                }
            ],
        )
        if user_data:
            # boto3 base64-encodes UserData automatically; pass the raw text.
            run_kwargs["UserData"] = user_data

        run_resp = ec2.run_instances(**run_kwargs)
        instance_id = run_resp["Instances"][0]["InstanceId"]
        private_ip = run_resp["Instances"][0].get("PrivateIpAddress")
        print(f"[launch] instance {instance_id} launched", file=sys.stderr)

        # Poll until running — auto-activate if instance falls to shutoff.
        # Bare-metal power-on (BMC/IPMI, full POST) is slower than VM boot,
        # so this budget is larger than the VM launch script's.
        _deadline = time.monotonic() + 2700  # 45 min total budget
        state = "pending"
        while time.monotonic() < _deadline:
            try:
                _resp = ec2.describe_instances(InstanceIds=[instance_id])
                _instances = [
                    i
                    for r in _resp.get("Reservations", [])
                    for i in r.get("Instances", [])
                    if i["InstanceId"] == instance_id
                ]
                state = _instances[0]["State"]["Name"] if _instances else state
            except Exception:
                pass

            if state == "running":
                print(f"[launch] instance {instance_id} is running", file=sys.stderr)
                break
            elif state in ("shutoff", "stopped"):
                print(
                    f"[launch] instance {instance_id} is {state} — sending start command",
                    file=sys.stderr,
                )
                try:
                    ec2.start_instances(InstanceIds=[instance_id])
                except Exception as _e:
                    print(f"[launch] WARNING: start_instances failed: {_e}", file=sys.stderr)
            else:
                print(f"[launch] waiting for running (current: {state}) ...", file=sys.stderr)
            time.sleep(60)
        else:
            raise RuntimeError(
                f"Instance {instance_id} did not reach 'running' within 45 min (last state: {state})"
            )

        allocation_id, public_ip = allocate_and_associate_eip(ec2, instance_id)

        confirmed_ip = wait_for_public_ip(ec2, instance_id, timeout=120, interval=5)
        if confirmed_ip:
            public_ip = confirmed_ip

        ssh_ready = wait_for_ssh(public_ip, args.ssh_user, key_file, max_attempts=60, interval=15)

        result = {
            "success": True,
            "platform": "bm",
            "instance_id": instance_id,
            "instance_type": args.instance_type,
            "public_ip": public_ip,
            "private_ip": private_ip,
            "state": state,
            "ami_id": args.ami_id,
            "key_name": args.key_name,
            "key_file": key_file,
            "vpc_id": vpc_id,
            "subnet_id": subnet_id,
            "security_group_id": sg_id,
            "eip_allocation_id": allocation_id,
            "ssh_ready": ssh_ready,
            "nvidia_modules_loaded": False,
            "gpu_deps": {},
            "user_data_applied": bool(user_data),
        }

        if ssh_ready and args.skip_gpu_setup:
            print("[launch] --skip-gpu-setup set; skipping NVIDIA module load and GPU dependency install", file=sys.stderr)
        elif ssh_ready:
            try:
                result["nvidia_modules_loaded"] = load_nvidia_modules(public_ip, args.ssh_user, key_file)
            except Exception as e:
                print(f"[launch] WARNING: load_nvidia_modules failed (non-fatal): {e}", file=sys.stderr)

            try:
                print("[launch] installing GPU dependencies (Docker, NCT, CUDA) ...", file=sys.stderr)
                result["gpu_deps"] = setup_gpu_dependencies(public_ip, args.ssh_user, key_file)
            except Exception as e:
                print(f"[launch] WARNING: setup_gpu_dependencies failed (non-fatal): {e}", file=sys.stderr)

    except ClientError as e:
        result["error"] = str(e)
        result["error_code"] = e.response.get("Error", {}).get("Code", "")
    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
