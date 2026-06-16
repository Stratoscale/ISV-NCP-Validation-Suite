#!/usr/bin/env python3
"""TrafficFlowCheck — zCompute implementation.

Mirrors NVIDIA's original test exactly: one VPC, three VMs, two security
groups. SSH replaces SSM as the remote execution channel — same commands,
same pass/fail criteria.

zCompute enforces security groups on private IP traffic between VMs in the
same subnet, so no special topology workaround is needed.

Topology
--------
  VPC (10.84.0.0/16)
   └─ subnet (10.84.1.0/24)
       ├─ source       [sg_source: SSH inbound]     — SSH in here; has EIP
       ├─ target_allow [sg_allow:  ICMP + SSH]      — ping private IP must succeed
       └─ target_deny  [sg_deny:   SSH only]        — ping private IP must fail (SG drops ICMP)

Sub-tests (all run from inside source via SSH):
  traffic_allowed  — ping target_allow private IP → must succeed (SG allows ICMP)
  traffic_blocked  — ping target_deny  private IP → must fail   (SG blocks ICMP)
  internet_icmp    — ping 8.8.8.8                 → must succeed
  internet_http    — curl https://google.com       → must succeed

Usage:
    python3 traffic_flow_test.py --region symphony
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from typing import Any

import paramiko
from botocore.exceptions import ClientError

_HERE = __import__("pathlib").Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))  # providers/zcompute/scripts/

from common.client import get_client               # noqa: E402
from common.ec2 import allocate_and_associate_eip  # noqa: E402

# ── Constants ──────────────────────────────────────────────────────────────────

_RUN_TAG = f"isv-net-flow-{uuid.uuid4().hex[:8]}"

_VPC_CIDR    = "10.84.0.0/16"
_SUBNET_CIDR = "10.84.1.0/24"

_VM_LAUNCH_TIMEOUT = 600


# ── Network creation ───────────────────────────────────────────────────────────

def _poll_vpc_available(ec2: Any, vpc_id: str, timeout: int = 120) -> None:
    """Poll until VPC leaves 'pending' and reaches 'available'."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = ec2.describe_vpcs(VpcIds=[vpc_id])["Vpcs"][0]["State"]
        if state == "available":
            return
        print(f"[net-flow] VPC {vpc_id} state={state}, waiting ...", file=sys.stderr)
        time.sleep(5)
    raise RuntimeError(f"VPC {vpc_id} did not become 'available' within {timeout}s")


def _create_network(ec2: Any) -> dict[str, str]:
    """Create VPC + IGW + subnet + route table. Return resource IDs."""
    vpc_id = ec2.create_vpc(CidrBlock=_VPC_CIDR)["Vpc"]["VpcId"]
    _poll_vpc_available(ec2, vpc_id)
    ec2.create_tags(Resources=[vpc_id], Tags=[
        {"Key": "Name",      "Value": f"isv-net-flow-vpc-{_RUN_TAG}"},
        {"Key": "CreatedBy", "Value": "isvtest"},
        {"Key": "RunTag",    "Value": _RUN_TAG},
    ])
    print(f"[net-flow] created VPC {vpc_id}", file=sys.stderr)

    igw_id = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
    ec2.create_tags(Resources=[igw_id], Tags=[
        {"Key": "Name",      "Value": f"isv-net-flow-igw-{_RUN_TAG}"},
        {"Key": "CreatedBy", "Value": "isvtest"},
        {"Key": "RunTag",    "Value": _RUN_TAG},
    ])

    az_name = ec2.describe_availability_zones()["AvailabilityZones"][0]["ZoneName"]
    subnet_id = ec2.create_subnet(
        VpcId=vpc_id, CidrBlock=_SUBNET_CIDR, AvailabilityZone=az_name
    )["Subnet"]["SubnetId"]
    ec2.create_tags(Resources=[subnet_id], Tags=[
        {"Key": "Name",      "Value": f"isv-net-flow-subnet-{_RUN_TAG}"},
        {"Key": "CreatedBy", "Value": "isvtest"},
        {"Key": "RunTag",    "Value": _RUN_TAG},
    ])
    try:
        ec2.modify_subnet_attribute(SubnetId=subnet_id, MapPublicIpOnLaunch={"Value": True})
    except ClientError:
        pass  # AuthFailure in zCompute — EIPs used instead

    rtb_id = ec2.create_route_table(VpcId=vpc_id)["RouteTable"]["RouteTableId"]
    ec2.create_route(RouteTableId=rtb_id, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id)
    ec2.associate_route_table(RouteTableId=rtb_id, SubnetId=subnet_id)
    ec2.create_tags(Resources=[rtb_id], Tags=[
        {"Key": "Name",      "Value": f"isv-net-flow-rtb-{_RUN_TAG}"},
        {"Key": "CreatedBy", "Value": "isvtest"},
        {"Key": "RunTag",    "Value": _RUN_TAG},
    ])

    print(f"[net-flow] network ready: subnet={subnet_id}", file=sys.stderr)
    return {"vpc_id": vpc_id, "subnet_id": subnet_id, "igw_id": igw_id, "rtb_id": rtb_id}


# ── Security groups ────────────────────────────────────────────────────────────

def _create_security_groups(ec2: Any, vpc_id: str) -> dict[str, str]:
    """Create three security groups matching NVIDIA's original test.

    sg_source — source VM: inbound SSH so we can connect.
    sg_allow  — target_allow: inbound ICMP + SSH permitted.
    sg_deny   — target_deny: SSH only, NO inbound ICMP.

    zCompute enforces SGs on private IP traffic within the same subnet, so
    pings from source to target_deny's private IP will be dropped by sg_deny.

    TagSpecifications is not supported in zCompute's CreateSecurityGroup —
    tags are added separately via create_tags.
    """
    def _make(name: str, desc: str) -> str:
        sg_id = ec2.create_security_group(
            GroupName=name, Description=desc, VpcId=vpc_id
        )["GroupId"]
        ec2.create_tags(Resources=[sg_id], Tags=[
            {"Key": "Name",      "Value": name},
            {"Key": "CreatedBy", "Value": "isvtest"},
            {"Key": "RunTag",    "Value": _RUN_TAG},
        ])
        return sg_id

    sg_source = _make(f"isv-net-flow-src-{_RUN_TAG}",   "ISV traffic-flow source VM")
    ec2.authorize_security_group_ingress(GroupId=sg_source, IpPermissions=[
        {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
         "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "SSH"}]},
    ])

    sg_allow = _make(f"isv-net-flow-allow-{_RUN_TAG}", "ISV traffic-flow allow ICMP")
    ec2.authorize_security_group_ingress(GroupId=sg_allow, IpPermissions=[
        {"IpProtocol": "icmp", "FromPort": -1, "ToPort": -1,
         "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "ICMP from anywhere"}]},
        {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
         "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "SSH"}]},
    ])

    sg_deny = _make(f"isv-net-flow-deny-{_RUN_TAG}",  "ISV traffic-flow deny ICMP")
    ec2.authorize_security_group_ingress(GroupId=sg_deny, IpPermissions=[
        # SSH only — no inbound ICMP rule → pings dropped by SG
        {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
         "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "SSH"}]},
    ])

    print(f"[net-flow] SGs: source={sg_source} allow={sg_allow} deny={sg_deny}", file=sys.stderr)
    return {"sg_source": sg_source, "sg_allow": sg_allow, "sg_deny": sg_deny}


# ── VM launch ──────────────────────────────────────────────────────────────────

def _launch_vm(
    ec2: Any,
    subnet_id: str,
    sg_id: str,
    name: str,
    assign_eip: bool = False,
) -> dict[str, Any]:
    """Launch a VM, wait for running state, optionally assign an EIP.

    Only the source VM needs an EIP (so we can SSH into it from outside).
    Target VMs are only pinged on their private IPs from within the VPC.

    Handles zCompute quirks:
      - run_instances may return empty Instances[] → poll by Name tag.
      - Instance may land in 'shutoff' → call start_instances to recover.
    """
    ami_id        = os.environ.get("ZCOMPUTE_TEST_AMI_ID", "")
    instance_type = os.environ.get("ZCOMPUTE_TEST_INSTANCE_TYPE", "z2.3large")
    key_name      = f"isv-net-flow-key-{_RUN_TAG}"

    if not ami_id:
        raise RuntimeError("ZCOMPUTE_TEST_AMI_ID is not set.")

    # Key pair — shared across all VMs in this run; created once, reused.
    key_file = f"/tmp/{key_name}.pem"
    try:
        ec2.describe_key_pairs(KeyNames=[key_name])
        if not os.path.exists(key_file):
            ec2.delete_key_pair(KeyName=key_name)
            raise ClientError(
                {"Error": {"Code": "InvalidKeyPair.NotFound", "Message": "gone"}},
                "DescribeKeyPairs",
            )
    except ClientError as e:
        if e.response["Error"]["Code"] == "InvalidKeyPair.NotFound":
            kp = ec2.create_key_pair(KeyName=key_name)
            with open(key_file, "w") as fh:
                fh.write(kp["KeyMaterial"])
            os.chmod(key_file, 0o600)
            print(f"[net-flow] created key pair {key_name}", file=sys.stderr)
        else:
            raise

    launch_time = time.time()
    print(f"[net-flow] launching VM '{name}' ...", file=sys.stderr)

    run_resp = ec2.run_instances(
        ImageId=ami_id,
        InstanceType=instance_type,
        MinCount=1, MaxCount=1,
        KeyName=key_name,
        SubnetId=subnet_id,
        SecurityGroupIds=[sg_id],
        BlockDeviceMappings=[{
            "DeviceName": "/dev/vda",
            "Ebs": {"VolumeSize": 100, "VolumeType": "gp2", "DeleteOnTermination": True},
        }],
        TagSpecifications=[{
            "ResourceType": "instance",
            "Tags": [
                {"Key": "Name",      "Value": name},
                {"Key": "CreatedBy", "Value": "isvtest"},
                {"Key": "RunTag",    "Value": _RUN_TAG},
            ],
        }],
    )

    instances_list = run_resp.get("Instances", [])
    if instances_list:
        instance_id = instances_list[0]["InstanceId"]
        private_ip  = instances_list[0].get("PrivateIpAddress")
    else:
        print(f"[net-flow] run_instances returned empty list — polling by name ...", file=sys.stderr)
        instance_id = _find_instance_by_name(ec2, name, after_timestamp=launch_time)
        private_ip  = None

    deadline = time.monotonic() + _VM_LAUNCH_TIMEOUT
    state = "pending"
    while time.monotonic() < deadline:
        try:
            desc = ec2.describe_instances(InstanceIds=[instance_id])
            matching = [
                i for r in desc.get("Reservations", [])
                for i in r.get("Instances", [])
                if i["InstanceId"] == instance_id
            ]
            if matching:
                state = matching[0]["State"]["Name"]
                if not private_ip:
                    private_ip = matching[0].get("PrivateIpAddress")
        except Exception as exc:
            print(f"[net-flow] describe error (non-fatal): {exc}", file=sys.stderr)

        if state == "running":
            print(f"[net-flow] {instance_id} ({name}) is running", file=sys.stderr)
            break
        elif state in ("shutoff", "stopped"):
            print(f"[net-flow] {instance_id} in {state} — calling start_instances ...", file=sys.stderr)
            try:
                ec2.start_instances(InstanceIds=[instance_id])
            except Exception as exc:
                print(f"[net-flow] start_instances failed (non-fatal): {exc}", file=sys.stderr)
        else:
            print(f"[net-flow] {instance_id} state={state} ...", file=sys.stderr)
        time.sleep(20)
    else:
        raise RuntimeError(
            f"Instance {instance_id} ({name}) did not reach 'running' in {_VM_LAUNCH_TIMEOUT}s"
        )

    public_ip     = None
    allocation_id = None
    if assign_eip:
        allocation_id, public_ip = allocate_and_associate_eip(ec2, instance_id)

    return {
        "instance_id":       instance_id,
        "private_ip":        private_ip,
        "public_ip":         public_ip,
        "eip_allocation_id": allocation_id,
        "key_name":          key_name,
    }


def _find_instance_by_name(
    ec2: Any, name: str, after_timestamp: float, timeout: int = 120
) -> str:
    """Fallback: find instance by Name tag when run_instances returns empty list."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for reservation in ec2.describe_instances().get("Reservations", []):
            for inst in reservation.get("Instances", []):
                tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                if tags.get("Name") != name:
                    continue
                launch_dt = inst.get("LaunchTime")
                if launch_dt:
                    if isinstance(launch_dt, str):
                        import dateutil.parser
                        ts = dateutil.parser.parse(launch_dt).timestamp()
                    else:
                        ts = launch_dt.timestamp()
                    if ts < after_timestamp - 5:
                        continue
                if iid := inst.get("InstanceId"):
                    return iid
        print(f"[net-flow] waiting for instance '{name}' ...", file=sys.stderr)
        time.sleep(10)
    raise RuntimeError(f"Could not find instance with Name='{name}' after {timeout}s")


# ── SSH helpers ────────────────────────────────────────────────────────────────

def _wait_ssh(public_ip: str, timeout: int = 300) -> None:
    """Block until TCP port 22 is reachable on public_ip."""
    import socket
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((public_ip, 22), timeout=5):
                print(f"[net-flow] SSH ready on {public_ip}", file=sys.stderr)
                return
        except OSError:
            time.sleep(10)
    raise RuntimeError(f"SSH not reachable on {public_ip} after {timeout}s")


def _ssh_run(public_ip: str, key_file: str, command: str, timeout: int = 30) -> tuple[int, str, str]:
    """Execute command on remote VM via SSH. Returns (exit_code, stdout, stderr)."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        public_ip, username="ubuntu", key_filename=key_file,
        timeout=30, allow_agent=False, look_for_keys=False,
    )
    try:
        _, out, err = client.exec_command(command, timeout=timeout)
        exit_code = out.channel.recv_exit_status()
        return exit_code, out.read().decode(), err.read().decode()
    finally:
        client.close()


# ── Sub-tests ──────────────────────────────────────────────────────────────────

def _test_traffic_allowed(source_public_ip: str, key_file: str, target_private_ip: str) -> dict[str, Any]:
    """Ping target_allow's private IP — sg_allow permits ICMP, must succeed."""
    print(f"[net-flow] traffic_allowed: ping {target_private_ip} (SG allows ICMP)", file=sys.stderr)
    try:
        exit_code, stdout, _ = _ssh_run(
            source_public_ip, key_file, f"ping -c 3 -W 3 {target_private_ip}", timeout=20
        )
        passed = exit_code == 0
        return {
            "passed": passed,
            "target": target_private_ip,
            "output": stdout.strip(),
            "message": f"ping {target_private_ip}: {'OK' if passed else 'FAILED (SG should allow)'}",
        }
    except Exception as exc:
        return {"passed": False, "error": str(exc)}


def _test_traffic_blocked(source_public_ip: str, key_file: str, target_private_ip: str) -> dict[str, Any]:
    """Ping target_deny's private IP — sg_deny has no ICMP rule, must fail.

    zCompute enforces SGs on intra-subnet private IP traffic, so the SG drop
    is genuine — not routing isolation.
    """
    print(f"[net-flow] traffic_blocked: ping {target_private_ip} (SG denies ICMP, should fail)", file=sys.stderr)
    try:
        exit_code, stdout, _ = _ssh_run(
            source_public_ip, key_file, f"ping -c 2 -W 2 {target_private_ip}", timeout=15
        )
        blocked = exit_code != 0
        return {
            "passed": blocked,
            "target": target_private_ip,
            "ping_exit_code": exit_code,
            "output": stdout.strip(),
            "message": (
                f"ping {target_private_ip} failed (exit {exit_code}) — SG blocking confirmed"
                if blocked
                else f"ping {target_private_ip} SUCCEEDED — SG is NOT blocking ICMP"
            ),
        }
    except Exception as exc:
        return {"passed": False, "error": str(exc)}


def _test_internet_icmp(source_public_ip: str, key_file: str) -> dict[str, Any]:
    """Ping 8.8.8.8 from source — confirms outbound internet ICMP via IGW."""
    print("[net-flow] internet_icmp: ping 8.8.8.8", file=sys.stderr)
    try:
        exit_code, stdout, _ = _ssh_run(
            source_public_ip, key_file, "ping -c 3 -W 3 8.8.8.8", timeout=20
        )
        passed = exit_code == 0
        return {
            "passed": passed,
            "target": "8.8.8.8",
            "output": stdout.strip(),
            "message": "ping 8.8.8.8 succeeded" if passed else f"ping 8.8.8.8 failed (exit {exit_code})",
        }
    except Exception as exc:
        return {"passed": False, "error": str(exc)}


def _test_internet_http(source_public_ip: str, key_file: str) -> dict[str, Any]:
    """Curl https://google.com from source — confirms outbound internet HTTP/S."""
    print("[net-flow] internet_http: curl https://google.com", file=sys.stderr)
    try:
        exit_code, stdout, _ = _ssh_run(
            source_public_ip, key_file,
            "curl -s -L --max-time 10 -o /dev/null -w '%{http_code}' https://google.com",
            timeout=20,
        )
        http_code_str = stdout.strip().strip("'")
        try:
            http_code = int(http_code_str)
        except ValueError:
            http_code = 0
        passed = 200 <= http_code < 400
        return {
            "passed": passed,
            "target": "https://google.com",
            "http_status": http_code,
            "message": (
                f"curl returned HTTP {http_code}" if passed
                else f"curl failed or unexpected status {http_code}"
            ),
        }
    except Exception as exc:
        return {"passed": False, "error": str(exc)}


# ── Cleanup ────────────────────────────────────────────────────────────────────

def _cleanup(ec2: Any, resources: dict[str, Any]) -> None:
    """Best-effort cleanup. Called from finally — must not raise."""

    def _try(desc: str, fn: Any, *args: Any, **kwargs: Any) -> None:
        try:
            fn(*args, **kwargs)
            print(f"[net-flow] cleanup: deleted {desc}", file=sys.stderr)
        except Exception as exc:
            print(f"[net-flow] cleanup WARNING: {desc}: {exc}", file=sys.stderr)

    for iid in resources.get("instance_ids", []):
        _try(f"instance {iid}", ec2.terminate_instances, InstanceIds=[iid])

    if resources.get("instance_ids"):
        print("[net-flow] cleanup: waiting 20s for instances to terminate ...", file=sys.stderr)
        time.sleep(20)
        deadline = time.monotonic() + 120
        for iid in resources.get("instance_ids", []):
            while time.monotonic() < deadline:
                try:
                    st = ec2.describe_instances(InstanceIds=[iid])[
                        "Reservations"][0]["Instances"][0]["State"]["Name"]
                    if st in ("terminated", "shutting-down"):
                        break
                except Exception:
                    break
                time.sleep(10)

    for alloc_id in resources.get("eip_allocation_ids", []):
        try:
            resp = ec2.describe_addresses(AllocationIds=[alloc_id])
            if assoc_id := resp["Addresses"][0].get("AssociationId"):
                _try(f"EIP association {assoc_id}", ec2.disassociate_address, AssociationId=assoc_id)
        except Exception:
            pass
        _try(f"EIP {alloc_id}", ec2.release_address, AllocationId=alloc_id)

    for sg_id in resources.get("sg_ids", []):
        _try(f"SG {sg_id}", ec2.delete_security_group, GroupId=sg_id)

    rtb_id    = resources.get("rtb_id")
    subnet_id = resources.get("subnet_id")
    igw_id    = resources.get("igw_id")
    vpc_id    = resources.get("vpc_id")

    if rtb_id:
        try:
            for assoc in ec2.describe_route_tables(RouteTableIds=[rtb_id])[
                    "RouteTables"][0].get("Associations", []):
                if not assoc.get("Main"):
                    _try(
                        f"RTB assoc {assoc['RouteTableAssociationId']}",
                        ec2.disassociate_route_table,
                        AssociationId=assoc["RouteTableAssociationId"],
                    )
        except Exception:
            pass
        _try(f"route table {rtb_id}", ec2.delete_route_table, RouteTableId=rtb_id)

    if subnet_id:
        _try(f"subnet {subnet_id}", ec2.delete_subnet, SubnetId=subnet_id)

    if igw_id and vpc_id:
        _try(f"IGW attachment {igw_id}", ec2.detach_internet_gateway,
             InternetGatewayId=igw_id, VpcId=vpc_id)
    if igw_id:
        _try(f"IGW {igw_id}", ec2.delete_internet_gateway, InternetGatewayId=igw_id)

    if vpc_id:
        _try(f"VPC {vpc_id}", ec2.delete_vpc, VpcId=vpc_id)

    if key_name := resources.get("key_name"):
        _try(f"key pair {key_name}", ec2.delete_key_pair, KeyName=key_name)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="zCompute TrafficFlowCheck (SSH-based)")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "symphony"))
    args = parser.parse_args()

    ec2 = get_client("ec2", region=args.region)

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "traffic_flow",
        "tests": {
            "traffic_allowed": {"passed": False},
            "traffic_blocked": {"passed": False},
            "internet_icmp":   {"passed": False},
            "internet_http":   {"passed": False},
        },
    }

    resources: dict[str, Any] = {
        "instance_ids":       [],
        "eip_allocation_ids": [],
        "sg_ids":             [],
    }

    try:
        # ── Step 1: Network ───────────────────────────────────────────────────
        print(f"[net-flow] creating network (run_tag={_RUN_TAG}) ...", file=sys.stderr)
        net = _create_network(ec2)
        resources.update({
            "vpc_id":    net["vpc_id"],
            "subnet_id": net["subnet_id"],
            "igw_id":    net["igw_id"],
            "rtb_id":    net["rtb_id"],
        })

        # ── Step 2: Security groups ───────────────────────────────────────────
        sgs = _create_security_groups(ec2, net["vpc_id"])
        resources["sg_ids"] = [sgs["sg_source"], sgs["sg_allow"], sgs["sg_deny"]]

        # ── Step 3: Launch 3 VMs ──────────────────────────────────────────────
        # Only source needs an EIP; targets are pinged on their private IPs.
        print("[net-flow] launching source VM ...", file=sys.stderr)
        vm_source = _launch_vm(
            ec2, net["subnet_id"], sgs["sg_source"],
            f"isv-net-flow-source-{_RUN_TAG}", assign_eip=True,
        )
        resources["instance_ids"].append(vm_source["instance_id"])
        resources["eip_allocation_ids"].append(vm_source["eip_allocation_id"])
        resources["key_name"] = vm_source["key_name"]

        print("[net-flow] launching target_allow VM ...", file=sys.stderr)
        vm_allow = _launch_vm(
            ec2, net["subnet_id"], sgs["sg_allow"],
            f"isv-net-flow-allow-{_RUN_TAG}", assign_eip=False,
        )
        resources["instance_ids"].append(vm_allow["instance_id"])

        print("[net-flow] launching target_deny VM ...", file=sys.stderr)
        vm_deny = _launch_vm(
            ec2, net["subnet_id"], sgs["sg_deny"],
            f"isv-net-flow-deny-{_RUN_TAG}", assign_eip=False,
        )
        resources["instance_ids"].append(vm_deny["instance_id"])

        # ── Step 4: Wait for SSH on source ────────────────────────────────────
        key_file = f"/tmp/{vm_source['key_name']}.pem"
        _wait_ssh(vm_source["public_ip"])

        # ── Step 5: Sub-tests ─────────────────────────────────────────────────
        for label, fn, target in [
            ("traffic_allowed", _test_traffic_allowed, vm_allow["private_ip"]),
            ("traffic_blocked", _test_traffic_blocked, vm_deny["private_ip"]),
            ("internet_icmp",   _test_internet_icmp,   None),
            ("internet_http",   _test_internet_http,   None),
        ]:
            try:
                if target is not None:
                    result["tests"][label] = fn(vm_source["public_ip"], key_file, target)
                else:
                    result["tests"][label] = fn(vm_source["public_ip"], key_file)
            except Exception as exc:
                result["tests"][label] = {"passed": False, "error": str(exc)}

        result["success"] = all(t.get("passed", False) for t in result["tests"].values())

    except Exception as exc:
        result["error"] = str(exc)
        print(f"[net-flow] FATAL: {exc}", file=sys.stderr)

    finally:
        print("[net-flow] cleaning up ...", file=sys.stderr)
        _cleanup(ec2, resources)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
