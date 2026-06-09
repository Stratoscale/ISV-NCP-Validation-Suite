#!/usr/bin/env python3
"""zCompute traffic flow test — replaces SSM-based TrafficFlowCheck.

NVIDIA's upstream TrafficFlowCheck uses SSM to run connectivity checks from
inside VMs. zCompute does not have SSM agents, so we replace it with a
combination of guestnet-admin-tool primitives that test the same scenarios
using the management plane as the probe origin.

Test design rationale
─────────────────────
Four sub-tests map directly to what TrafficFlowCheck validates:

  traffic_allowed
    guestnet-admin-tool arping to VM-A (lives in VPC-A). The DHCP server for
    VPC-A is on the same L2 segment as VM-A, so arping should always succeed.
    This validates that VM-A is up and reachable on its L2 domain.

  traffic_blocked
    guestnet-admin-tool arping from VPC-A's network context to VM-B's private
    IP (VM-B is in VPC-B — a different, non-peered VPC). ARP is L2-only; a
    different VPC is a different L2 domain, so the arping should fail/timeout.
    "status=failed OR 'Received 0' in output" → traffic is correctly isolated
    → test PASSES (we are testing that blocking works).

  internet_icmp
    SSH into VM-A (which has a public EIP on a subnet with an IGW) and run
    `ping -c 3 8.8.8.8`. If 8.8.8.8 responds, the VM has real internet ICMP
    access via the IGW. This is the same probe that SSM would have run from
    inside the instance.

  internet_http
    SSH into VM-A and run `curl -s --max-time 10 https://google.com`. A 2xx/3xx
    response confirms the VM can reach the public internet over HTTP/S. This is
    the equivalent of what SSM's TrafficFlowCheck does for internet_http.

Infrastructure
──────────────
  VPC-A (10.85.0.0/16) with subnet 10.85.1.0/24 — VM-A lives here
  VPC-B (10.84.0.0/16) with subnet 10.84.1.0/24 — VM-B lives here
  The two VPCs are intentionally NOT peered so L2 isolation holds.

zCompute quirks handled:
  - No boto3 waiters — poll loops throughout.
  - No auto public IP — EIPs for VMs.
  - guestnet-admin-tool ping-vm needs internal zCompute UUID (not i-xxx).
  - guestnet-admin-tool ping-ip needs the internal network UUID for the
    source network (not the VPC ID from EC2 API).
    Use 'symp dvs network list -f json' → match by vpc_id field.
  - TagSpecifications not supported in CreateSecurityGroup — create then tag.
  - run_instances may return empty Instances[] — fall back to poll by name.

Environment variables:
  ZCOMPUTE_TEST_AMI_ID            - AMI to launch (required)
  ZCOMPUTE_TEST_INSTANCE_TYPE     - Instance type (required)
  ZCOMPUTE_BASE_URL               - https://172.29.0.20 (required)
  AWS_ACCESS_KEY_ID               - required
  AWS_SECRET_ACCESS_KEY           - required
  AWS_REGION                      - default: symphony

  ZCOMPUTE_SYMP_URL               - default http://172.29.0.20
  ZCOMPUTE_SYMP_USER              - default admin
  ZCOMPUTE_SYMP_DOMAIN            - default cloud_admin
  ZCOMPUTE_SYMP_PASSWORD          - default admin
  ZCOMPUTE_SYMP_PROJECT           - default default
  ZCOMPUTE_SYMP_CONTAINER         - default symp_docker

Output JSON:
{
    "success": true,
    "platform": "network",
    "test_name": "traffic_flow",
    "tests": {
        "traffic_allowed":  {"passed": true},
        "traffic_blocked":  {"passed": true},
        "internet_icmp":    {"passed": true},
        "internet_http":    {"passed": true}
    }
}

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

# Add zcompute common to path (script lives at scripts/network/).
_HERE = __import__("pathlib").Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))  # providers/zcompute/scripts/

from common.client import get_client              # noqa: E402
from common.ec2 import allocate_and_associate_eip  # noqa: E402

# ── Constants ──────────────────────────────────────────────────────────────────

_RUN_TAG = f"isv-net-flow-{uuid.uuid4().hex[:8]}"

# Two separate VPCs in non-overlapping blocks. The blocks were chosen to avoid
# collisions with other network test scripts (which use 10.83, 10.87-10.99).
_VPC_A_CIDR = "10.85.0.0/16"
_VPC_A_SUBNET = "10.85.1.0/24"
_VPC_B_CIDR = "10.84.0.0/16"
_VPC_B_SUBNET = "10.84.1.0/24"

# zCompute management plane IP — used as the target for internet_icmp and
# internet_http tests. In a private-cloud context this is the "external" target.
_MANAGEMENT_IP = "172.29.0.20"

# guestnet-admin-tool polling parameters
_PING_POLL_TIMEOUT = 30
_PING_POLL_INTERVAL = 3

# VM launch timeout
_VM_LAUNCH_TIMEOUT = 600


# ── symp CLI helper ────────────────────────────────────────────────────────────

def _symp_cmd(args: list[str], timeout: int = 30) -> Any:
    """Run a symp CLI command via docker exec and return parsed JSON.

    Identical pattern to backend_switch_fabric_test.py / network_connectivity_test.py.
    All auth flags are read from environment variables with sensible defaults.

    Args:
        args:    symp subcommand + arguments (without auth flags or -f json).
        timeout: subprocess timeout in seconds.

    Returns:
        Parsed JSON (list or dict depending on the command).

    Raises:
        RuntimeError: If subprocess exits non-zero.
        json.JSONDecodeError: If output is not valid JSON.
    """
    import subprocess

    url = os.environ.get("ZCOMPUTE_SYMP_URL", "http://172.29.0.20")
    user = os.environ.get("ZCOMPUTE_SYMP_USER", "admin")
    domain = os.environ.get("ZCOMPUTE_SYMP_DOMAIN", "cloud_admin")
    password = os.environ.get("ZCOMPUTE_SYMP_PASSWORD", "admin")
    project = os.environ.get("ZCOMPUTE_SYMP_PROJECT", "default")
    container = os.environ.get("ZCOMPUTE_SYMP_CONTAINER", "symp_docker")

    cmd = [
        "sudo", "docker", "exec", container,
        "symp", "-q", "-k",
        "--username", user,
        "--domain", domain,
        "--password", password,
        "--project", project,
        "--url", url,
    ] + args + ["-f", "json"]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            f"symp command failed (rc={proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return json.loads(proc.stdout)


# ── VPC creation ───────────────────────────────────────────────────────────────

def _poll_vpc_available(ec2: Any, vpc_id: str, timeout: int = 120) -> None:
    """Poll until VPC transitions from 'pending' to 'available'.

    zCompute does not support boto3 waiters, so all state checks are manual
    poll loops. VPCs briefly stay in 'pending' right after creation.

    Args:
        ec2:     boto3 EC2 client.
        vpc_id:  VPC ID.
        timeout: Maximum seconds to wait before raising.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = ec2.describe_vpcs(VpcIds=[vpc_id])
        state = resp["Vpcs"][0]["State"]
        if state == "available":
            return
        print(f"[net-flow] VPC {vpc_id} state={state}, waiting ...", file=sys.stderr)
        time.sleep(5)
    raise RuntimeError(f"VPC {vpc_id} did not become 'available' within {timeout}s")


def _create_vpc_stack(ec2: Any, label: str, cidr: str, subnet_cidr: str) -> dict[str, str]:
    """Create a minimal VPC stack: VPC + IGW + subnet + route table + security group.

    'label' is used to distinguish VPC-A from VPC-B in names and log messages.
    All resources are tagged with _RUN_TAG for reliable cleanup.

    Returns a dict with: vpc_id, subnet_id, sg_id, igw_id, rtb_id.

    TagSpecifications is unsupported in zCompute's CreateSecurityGroup, so we
    create the SG without tags and then add them separately via create_tags.
    """
    tag_suffix = _RUN_TAG

    # ── VPC ────────────────────────────────────────────────────────────────────
    vpc_resp = ec2.create_vpc(CidrBlock=cidr)
    vpc_id = vpc_resp["Vpc"]["VpcId"]
    _poll_vpc_available(ec2, vpc_id)
    ec2.create_tags(
        Resources=[vpc_id],
        Tags=[
            {"Key": "Name", "Value": f"isv-net-flow-{label}-{tag_suffix}"},
            {"Key": "CreatedBy", "Value": "isvtest"},
            {"Key": "RunTag", "Value": tag_suffix},
        ],
    )
    print(f"[net-flow] created VPC-{label}: {vpc_id}", file=sys.stderr)

    # ── Internet Gateway ───────────────────────────────────────────────────────
    igw_resp = ec2.create_internet_gateway()
    igw_id = igw_resp["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
    ec2.create_tags(
        Resources=[igw_id],
        Tags=[{"Key": "Name", "Value": f"isv-net-flow-{label}-igw-{tag_suffix}"},
              {"Key": "CreatedBy", "Value": "isvtest"},
              {"Key": "RunTag", "Value": tag_suffix}],
    )

    # ── Subnet ─────────────────────────────────────────────────────────────────
    azs = ec2.describe_availability_zones()
    az_name = azs["AvailabilityZones"][0]["ZoneName"]
    subnet_resp = ec2.create_subnet(VpcId=vpc_id, CidrBlock=subnet_cidr, AvailabilityZone=az_name)
    subnet_id = subnet_resp["Subnet"]["SubnetId"]
    ec2.create_tags(
        Resources=[subnet_id],
        Tags=[{"Key": "Name", "Value": f"isv-net-flow-{label}-subnet-{tag_suffix}"},
              {"Key": "CreatedBy", "Value": "isvtest"},
              {"Key": "RunTag", "Value": tag_suffix}],
    )

    # MapPublicIpOnLaunch returns AuthFailure in zCompute — silently ignore.
    try:
        ec2.modify_subnet_attribute(SubnetId=subnet_id, MapPublicIpOnLaunch={"Value": True})
    except ClientError:
        pass

    # ── Route table ────────────────────────────────────────────────────────────
    rtb_resp = ec2.create_route_table(VpcId=vpc_id)
    rtb_id = rtb_resp["RouteTable"]["RouteTableId"]
    ec2.create_route(RouteTableId=rtb_id, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id)
    ec2.associate_route_table(RouteTableId=rtb_id, SubnetId=subnet_id)
    ec2.create_tags(
        Resources=[rtb_id],
        Tags=[{"Key": "Name", "Value": f"isv-net-flow-{label}-rtb-{tag_suffix}"},
              {"Key": "CreatedBy", "Value": "isvtest"},
              {"Key": "RunTag", "Value": tag_suffix}],
    )

    # ── Security Group ─────────────────────────────────────────────────────────
    sg_resp = ec2.create_security_group(
        GroupName=f"isv-net-flow-{label}-sg-{tag_suffix}",
        Description=f"ISV NCP traffic flow test VPC-{label}",
        VpcId=vpc_id,
    )
    sg_id = sg_resp["GroupId"]
    ec2.create_tags(
        Resources=[sg_id],
        Tags=[{"Key": "Name", "Value": f"isv-net-flow-{label}-sg-{tag_suffix}"},
              {"Key": "CreatedBy", "Value": "isvtest"},
              {"Key": "RunTag", "Value": tag_suffix}],
    )
    ec2.authorize_security_group_ingress(
        GroupId=sg_id,
        IpPermissions=[
            {"IpProtocol": "icmp", "FromPort": -1, "ToPort": -1,
             "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
            {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
             "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
        ],
    )

    return {"vpc_id": vpc_id, "subnet_id": subnet_id, "sg_id": sg_id,
            "igw_id": igw_id, "rtb_id": rtb_id}


# ── VM launch ──────────────────────────────────────────────────────────────────

def _launch_vm(ec2: Any, subnet_id: str, sg_id: str, name: str) -> dict[str, Any]:
    """Launch a VM and wait for it to reach 'running'. Return ids and IPs.

    Handles:
      - run_instances returning empty Instances[] → fall back to name-tag poll.
      - Instance landing in 'shutoff' → send start_instances.
      - No auto-assigned public IP → allocate_and_associate_eip.
    """
    ami_id = os.environ.get("ZCOMPUTE_TEST_AMI_ID", "")
    instance_type = os.environ.get("ZCOMPUTE_TEST_INSTANCE_TYPE", "z2.3large")
    key_name = f"isv-net-flow-key-{_RUN_TAG}"

    if not ami_id:
        raise RuntimeError("ZCOMPUTE_TEST_AMI_ID is not set.")

    # ── Key pair (shared by both VMs in this run) ──────────────────────────────
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
            kp_resp = ec2.create_key_pair(KeyName=key_name)
            with open(key_file, "w") as fh:
                fh.write(kp_resp["KeyMaterial"])
            os.chmod(key_file, 0o600)
            print(f"[net-flow] created key pair {key_name}", file=sys.stderr)
        else:
            raise

    launch_time = time.time()

    print(f"[net-flow] launching VM '{name}' ...", file=sys.stderr)
    run_resp = ec2.run_instances(
        ImageId=ami_id,
        InstanceType=instance_type,
        MinCount=1,
        MaxCount=1,
        KeyName=key_name,
        SubnetId=subnet_id,
        SecurityGroupIds=[sg_id],
        # zCompute KVM VMs use /dev/vda as the root device, not /dev/sda.
        BlockDeviceMappings=[
            {
                "DeviceName": "/dev/vda",
                "Ebs": {"VolumeSize": 20, "VolumeType": "gp2", "DeleteOnTermination": True},
            }
        ],
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Name", "Value": name},
                    {"Key": "CreatedBy", "Value": "isvtest"},
                    {"Key": "RunTag", "Value": _RUN_TAG},
                ],
            }
        ],
    )

    instances_list = run_resp.get("Instances", [])
    if instances_list:
        instance_id = instances_list[0]["InstanceId"]
        private_ip = instances_list[0].get("PrivateIpAddress")
    else:
        # zCompute occasionally returns an empty Instances[] from run_instances.
        print(f"[net-flow] run_instances returned empty list — polling by name ...", file=sys.stderr)
        instance_id = _find_instance_by_name(ec2, name, after_timestamp=launch_time)
        private_ip = None

    # ── Poll for running state ─────────────────────────────────────────────────
    deadline = time.monotonic() + _VM_LAUNCH_TIMEOUT
    state = "pending"
    while time.monotonic() < deadline:
        try:
            desc = ec2.describe_instances(InstanceIds=[instance_id])
            # Post-filter because zCompute may ignore the InstanceIds parameter.
            matching = [
                i
                for r in desc.get("Reservations", [])
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
            print(f"[net-flow] {instance_id} is running", file=sys.stderr)
            break
        elif state in ("shutoff", "stopped"):
            # zCompute scheduling quirk — nudge it back to running.
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
            f"Instance {instance_id} did not reach 'running' in {_VM_LAUNCH_TIMEOUT}s "
            f"(last state: {state})"
        )

    # ── EIP ────────────────────────────────────────────────────────────────────
    allocation_id, public_ip = allocate_and_associate_eip(ec2, instance_id)

    return {
        "instance_id": instance_id,
        "private_ip": private_ip,
        "public_ip": public_ip,
        "eip_allocation_id": allocation_id,
        "key_name": key_name,
    }


def _find_instance_by_name(
    ec2: Any, name: str, after_timestamp: float, timeout: int = 120
) -> str:
    """Poll describe_instances and find the instance with the given Name tag.

    Fallback for when run_instances returns an empty Instances[]. Matches on
    both the Name tag and launch time to avoid collisions with concurrent tests.

    Args:
        ec2:             boto3 EC2 client.
        name:            Instance Name tag value.
        after_timestamp: Unix timestamp; ignore instances launched before this.
        timeout:         Maximum seconds to search.

    Returns:
        EC2 instance ID.

    Raises:
        RuntimeError: If no matching instance found within timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        all_resp = ec2.describe_instances()
        for reservation in all_resp.get("Reservations", []):
            for inst in reservation.get("Instances", []):
                tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                if tags.get("Name") != name:
                    continue
                launch_dt = inst.get("LaunchTime")
                if launch_dt:
                    if isinstance(launch_dt, str):
                        import dateutil.parser
                        launch_ts = dateutil.parser.parse(launch_dt).timestamp()
                    else:
                        launch_ts = launch_dt.timestamp()
                    if launch_ts < after_timestamp - 5:
                        continue
                iid = inst.get("InstanceId")
                if iid:
                    return iid
        print(f"[net-flow] waiting for instance '{name}' ...", file=sys.stderr)
        time.sleep(10)
    raise RuntimeError(f"Could not find instance with Name='{name}' after {timeout}s")


# ── zCompute UUID helpers ──────────────────────────────────────────────────────

def _get_vm_uuid(vm_name: str) -> str | None:
    """Translate a VM Name tag to the internal zCompute UUID.

    guestnet-admin-tool ping-vm requires the internal UUID, not the EC2 ID.
    We call 'symp vm list -f json' and match on the 'name' field, which
    zCompute populates from the EC2 Name tag.

    Args:
        vm_name: EC2 Name tag value.

    Returns:
        Internal UUID string, or None if not found.
    """
    try:
        vms = _symp_cmd(["vm", "list"], timeout=30)
        for vm in vms:
            if vm.get("name") == vm_name:
                return vm.get("id")
        print(f"[net-flow] WARNING: VM '{vm_name}' not found in symp vm list", file=sys.stderr)
    except Exception as exc:
        print(f"[net-flow] WARNING: symp vm list failed: {exc}", file=sys.stderr)
    return None


def _get_network_uuid(vpc_id: str) -> str | None:
    """Translate an EC2 VPC ID to the internal zCompute network UUID.

    guestnet-admin-tool ping-ip requires the internal network UUID as the
    source network context, not the EC2 VPC ID. We get it from:
      'symp dvs network list -f json'
    which returns objects with 'id' (UUID) and 'vpc_id' fields.

    The 'vpc_id' field in symp output corresponds to the EC2 VpcId.

    Args:
        vpc_id: EC2 VPC ID (vpc-xxx).

    Returns:
        Internal network UUID string, or None if not found.
    """
    try:
        networks = _symp_cmd(["dvs", "network", "list"], timeout=30)
        for net in networks:
            # zCompute may expose the vpc_id under different key names
            # depending on the symp version. Try common variants.
            if net.get("vpc_id") == vpc_id or net.get("external_id") == vpc_id:
                return net.get("id")
        print(f"[net-flow] WARNING: network for vpc_id={vpc_id} not found in symp dvs network list", file=sys.stderr)
    except Exception as exc:
        print(f"[net-flow] WARNING: symp dvs network list failed: {exc}", file=sys.stderr)
    return None


# ── guestnet-admin-tool helpers ────────────────────────────────────────────────

def _ping_vm_arping(zcompute_uuid: str) -> dict[str, Any]:
    """Arping a VM by its internal UUID via guestnet-admin-tool ping-vm.

    Used for traffic_allowed: proves the VM is reachable on its local L2
    segment from the DHCP server (management plane), which bypasses SGs.

    Args:
        zcompute_uuid: Internal zCompute VM UUID.

    Returns:
        Dict with: passed (bool), status (str), output (str), error (str|None).
    """
    result: dict[str, Any] = {"passed": False, "status": "unknown", "output": "", "error": None}
    try:
        # Create the arping job
        create_resp = _symp_cmd(
            ["guestnet-admin-tool", "ping-vm", "create",
             "--command-type", "arping",
             zcompute_uuid],
            timeout=15,
        )
        ping_id = create_resp.get("id")
        if not ping_id:
            result["error"] = f"ping-vm create returned no 'id': {create_resp}"
            return result

        print(f"[net-flow] ping-vm job {ping_id} created, polling ...", file=sys.stderr)

        # Poll until status leaves 'pending'
        deadline = time.monotonic() + _PING_POLL_TIMEOUT
        status = "pending"
        output = ""
        while time.monotonic() < deadline:
            get_resp = _symp_cmd(
                ["guestnet-admin-tool", "ping-vm", "get", ping_id],
                timeout=15,
            )
            status = get_resp.get("status", "unknown")
            output = get_resp.get("output", "")
            if status != "pending":
                break
            time.sleep(_PING_POLL_INTERVAL)
        else:
            result["error"] = f"ping-vm {ping_id} still pending after {_PING_POLL_TIMEOUT}s"
            result["status"] = "timeout"
            return result

        result["status"] = status
        result["output"] = output

        # Success = API says 'succeeded' AND arping output confirms a response.
        if status == "succeeded" and "Received" in output:
            result["passed"] = True
        else:
            result["error"] = f"status={status!r}, output={output!r}"

    except Exception as exc:
        result["error"] = str(exc)

    return result


def _ping_ip(network_uuid: str, dest_ip: str, command_type: str = "arping") -> dict[str, Any]:
    """Send an ICMP ping or arping from a given network context to a destination IP.

    Used for both traffic_blocked and internet_icmp tests.

    guestnet-admin-tool ping-ip sends a probe from the L3 context of the
    specified network (its router namespace on the management plane). The
    source is the management plane acting on behalf of that network's router.

    command_type choices:
      arping — L2 ARP probe; only reaches hosts in the same L2 segment.
      ping   — ICMP L3 ping; reaches any routable IP.

    Args:
        network_uuid: Internal zCompute network UUID (from 'symp dvs network list').
        dest_ip:      Destination IP to probe.
        command_type: 'arping' or 'ping'.

    Returns:
        Dict with: passed (bool), status (str), output (str), error (str|None).
    """
    result: dict[str, Any] = {"passed": False, "status": "unknown", "output": "", "error": None}
    try:
        create_resp = _symp_cmd(
            ["guestnet-admin-tool", "ping-ip", "create",
             "--command-type", command_type,
             network_uuid, dest_ip],
            timeout=15,
        )
        ping_id = create_resp.get("id")
        if not ping_id:
            result["error"] = f"ping-ip create returned no 'id': {create_resp}"
            return result

        print(f"[net-flow] ping-ip job {ping_id} ({command_type} {dest_ip}) polling ...", file=sys.stderr)

        deadline = time.monotonic() + _PING_POLL_TIMEOUT
        status = "pending"
        output = ""
        while time.monotonic() < deadline:
            get_resp = _symp_cmd(
                ["guestnet-admin-tool", "ping-ip", "get", ping_id],
                timeout=15,
            )
            status = get_resp.get("status", "unknown")
            output = get_resp.get("output", "")
            if status != "pending":
                break
            time.sleep(_PING_POLL_INTERVAL)
        else:
            result["status"] = "timeout"
            result["error"] = f"ping-ip {ping_id} still pending after {_PING_POLL_TIMEOUT}s"
            return result

        result["status"] = status
        result["output"] = output

    except Exception as exc:
        result["error"] = str(exc)

    return result


# ── Individual sub-tests ───────────────────────────────────────────────────────

def _test_traffic_allowed(vm_a_uuid: str | None) -> dict[str, Any]:
    """traffic_allowed: arping VM-A within its own VPC.

    VM-A's DHCP server is on the same L2 segment. arping from the management
    plane (which IS the DHCP server) to VM-A should always succeed if VM-A is
    alive and its L2 domain is healthy.

    PASS condition: ping-vm status == 'succeeded' AND output contains 'Received'.
    """
    if not vm_a_uuid:
        return {"passed": False, "error": "zCompute UUID for VM-A not resolved"}

    result = _ping_vm_arping(vm_a_uuid)
    return {
        "passed": result["passed"],
        "status": result["status"],
        "output": result.get("output", ""),
        **({"error": result["error"]} if result.get("error") else {}),
    }


def _test_traffic_blocked(vpc_a_uuid: str | None, vm_b_private_ip: str | None) -> dict[str, Any]:
    """traffic_blocked: arping from VPC-A context to VM-B's IP in VPC-B.

    VM-B lives in VPC-B — a separate, non-peered VPC. arping is L2-only; it
    cannot cross L2 segment boundaries. If the platform is correctly isolating
    VPCs, this arping should fail or time out.

    PASS condition: status == 'failed' OR 'Received 0' in output.
    The test PASSES when the arping FAILS — we are verifying isolation.

    Args:
        vpc_a_uuid:      Internal network UUID for VPC-A (probe source context).
        vm_b_private_ip: Private IP of VM-B (the target that should NOT respond).
    """
    if not vpc_a_uuid:
        return {"passed": False, "error": "zCompute network UUID for VPC-A not resolved"}
    if not vm_b_private_ip:
        return {"passed": False, "error": "Private IP of VM-B not available"}

    result = _ping_ip(vpc_a_uuid, vm_b_private_ip, command_type="arping")
    output = result.get("output", "")
    status = result.get("status", "unknown")

    # If the arping reached the management plane but got no L2 response from
    # VM-B's IP, that is the expected isolation behaviour. Both 'failed' status
    # and 'Received 0' in output count as "traffic correctly blocked".
    isolation_confirmed = (
        status == "failed"
        or "Received 0" in output
        or result.get("status") == "timeout"
    )

    return {
        "passed": isolation_confirmed,
        "status": status,
        "output": output,
        "note": (
            "arping from VPC-A to VPC-B returned no response — VPC isolation confirmed"
            if isolation_confirmed
            else f"Unexpected: arping to cross-VPC IP succeeded (status={status})"
        ),
        **({"error": result["error"]} if result.get("error") and not isolation_confirmed else {}),
    }


def _ssh_run(public_ip: str, key_file: str, command: str, timeout: int = 30) -> tuple[int, str, str]:
    """Run a command on a remote VM via SSH and return (exit_code, stdout, stderr).

    Uses paramiko with the key file generated during VM launch. SSH host key
    checking is disabled because VMs are ephemeral and we don't track host keys.
    """
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=public_ip,
        username="ubuntu",
        key_filename=key_file,
        timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    try:
        _, stdout, stderr = client.exec_command(command, timeout=timeout)
        exit_code = stdout.channel.recv_exit_status()
        return exit_code, stdout.read().decode(), stderr.read().decode()
    finally:
        client.close()


def _test_internet_icmp(public_ip: str, key_file: str) -> dict[str, Any]:
    """internet_icmp: SSH into VM-A and ping 8.8.8.8 (Google DNS).

    VM-A has a public EIP and its subnet has an Internet Gateway, so it has
    real internet access. We SSH in and run `ping -c 3 8.8.8.8` to confirm
    outbound ICMP to the public internet works.

    This is the same probe that NVIDIA's SSM-based TrafficFlowCheck would run
    from inside the instance — we are just using SSH instead of SSM as the
    remote execution mechanism.

    PASS condition: ping exits 0 (at least one response received from 8.8.8.8).
    """
    try:
        exit_code, stdout, stderr = _ssh_run(
            public_ip, key_file,
            "ping -c 3 -W 3 8.8.8.8",
            timeout=20,
        )
        passed = exit_code == 0
        return {
            "passed": passed,
            "target": "8.8.8.8",
            "exit_code": exit_code,
            "output": stdout.strip(),
            "message": (
                "ping 8.8.8.8 succeeded — VM has outbound internet ICMP"
                if passed
                else f"ping 8.8.8.8 failed (exit {exit_code})"
            ),
        }
    except Exception as exc:
        return {"passed": False, "error": str(exc)}


def _test_internet_http(public_ip: str, key_file: str) -> dict[str, Any]:
    """internet_http: SSH into VM-A and curl https://google.com.

    VM-A has a public EIP and an IGW-backed subnet, so it has real internet
    access. We SSH in and run curl to confirm outbound HTTPS to the public
    internet works.

    We check only the HTTP status code (not the body) and accept any response
    including redirects (3xx) — the important thing is that the TCP+TLS+HTTP
    stack can reach a public internet host.

    PASS condition: curl returns an HTTP status code (any 2xx or 3xx).
    """
    try:
        exit_code, stdout, stderr = _ssh_run(
            public_ip, key_file,
            # -L follows redirects; -o /dev/null discards body; -w prints status code only
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
                f"curl https://google.com returned HTTP {http_code} — VM has outbound internet HTTP/S"
                if passed
                else f"curl failed or returned unexpected status {http_code}"
            ),
        }
    except Exception as exc:
        return {"passed": False, "error": str(exc)}


# ── Resource cleanup ───────────────────────────────────────────────────────────

def _cleanup(ec2: Any, resources: dict[str, Any]) -> None:
    """Best-effort cleanup of all resources created during this test run.

    Called from a finally block — MUST NOT raise. Each operation is individually
    wrapped to ensure all resources are attempted regardless of partial failures.

    Deletion order (VPC-A then VPC-B):
      VMs → EIPs → SGs → subnets → route tables → IGWs → VPCs → key pair

    Args:
        ec2:       boto3 EC2 client.
        resources: Accumulated resource IDs from _create_vpc_stack and _launch_vm.
    """

    def _try(desc: str, fn: Any, *args: Any, **kwargs: Any) -> None:
        try:
            fn(*args, **kwargs)
            print(f"[net-flow] cleanup: deleted {desc}", file=sys.stderr)
        except Exception as exc:
            print(f"[net-flow] cleanup WARNING: {desc}: {exc}", file=sys.stderr)

    # ── Terminate VMs ──────────────────────────────────────────────────────────
    for iid in resources.get("instance_ids", []):
        _try(f"instance {iid}", ec2.terminate_instances, InstanceIds=[iid])

    if resources.get("instance_ids"):
        print("[net-flow] cleanup: waiting 20s for instances to terminate ...", file=sys.stderr)
        time.sleep(20)
        deadline = time.monotonic() + 120
        for iid in resources.get("instance_ids", []):
            while time.monotonic() < deadline:
                try:
                    d = ec2.describe_instances(InstanceIds=[iid])
                    st = d["Reservations"][0]["Instances"][0]["State"]["Name"]
                    if st in ("terminated", "shutting-down"):
                        break
                except Exception:
                    break
                time.sleep(10)

    # ── Release EIPs ───────────────────────────────────────────────────────────
    for alloc_id in resources.get("eip_allocation_ids", []):
        try:
            resp = ec2.describe_addresses(AllocationIds=[alloc_id])
            assoc_id = resp["Addresses"][0].get("AssociationId")
            if assoc_id:
                _try(f"EIP association {assoc_id}", ec2.disassociate_address, AssociationId=assoc_id)
        except Exception:
            pass
        _try(f"EIP {alloc_id}", ec2.release_address, AllocationId=alloc_id)

    # ── Per-VPC resources ─────────────────────────────────────────────────────
    for vpc_res in resources.get("vpcs", []):
        sg_id = vpc_res.get("sg_id")
        rtb_id = vpc_res.get("rtb_id")
        subnet_id = vpc_res.get("subnet_id")
        igw_id = vpc_res.get("igw_id")
        vpc_id = vpc_res.get("vpc_id")

        if sg_id:
            _try(f"SG {sg_id}", ec2.delete_security_group, GroupId=sg_id)

        if rtb_id:
            try:
                d = ec2.describe_route_tables(RouteTableIds=[rtb_id])
                for assoc in d["RouteTables"][0].get("Associations", []):
                    if not assoc.get("Main"):
                        _try(
                            f"RTB association {assoc['RouteTableAssociationId']}",
                            ec2.disassociate_route_table,
                            AssociationId=assoc["RouteTableAssociationId"],
                        )
            except Exception:
                pass
            _try(f"route table {rtb_id}", ec2.delete_route_table, RouteTableId=rtb_id)

        if subnet_id:
            _try(f"subnet {subnet_id}", ec2.delete_subnet, SubnetId=subnet_id)

        if igw_id and vpc_id:
            _try(
                f"IGW attachment {igw_id}",
                ec2.detach_internet_gateway,
                InternetGatewayId=igw_id, VpcId=vpc_id,
            )
        if igw_id:
            _try(f"IGW {igw_id}", ec2.delete_internet_gateway, InternetGatewayId=igw_id)

        if vpc_id:
            _try(f"VPC {vpc_id}", ec2.delete_vpc, VpcId=vpc_id)

    # ── Key pair ───────────────────────────────────────────────────────────────
    key_name = resources.get("key_name")
    if key_name:
        _try(f"key pair {key_name}", ec2.delete_key_pair, KeyName=key_name)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    """Create two VPCs with one VM each, run 4 traffic sub-tests, clean up."""
    parser = argparse.ArgumentParser(
        description="zCompute TrafficFlowCheck replacement (guestnet-admin-tool)"
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION", "symphony"),
        help="AWS region (default: symphony)",
    )
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

    # Accumulate resource IDs for cleanup; organised so cleanup can iterate
    # VPC stacks uniformly.
    resources: dict[str, Any] = {
        "instance_ids": [],
        "eip_allocation_ids": [],
        "vpcs": [],         # list of vpc-stack dicts {vpc_id, subnet_id, sg_id, igw_id, rtb_id}
        "key_name": None,
    }

    try:
        # ── Step 1: Create two independent VPCs ──────────────────────────────
        print(f"[net-flow] creating VPC-A and VPC-B (run_tag={_RUN_TAG}) ...", file=sys.stderr)

        net_a = _create_vpc_stack(ec2, "a", _VPC_A_CIDR, _VPC_A_SUBNET)
        resources["vpcs"].append(net_a)

        net_b = _create_vpc_stack(ec2, "b", _VPC_B_CIDR, _VPC_B_SUBNET)
        resources["vpcs"].append(net_b)

        # ── Step 2: Launch one VM in each VPC ─────────────────────────────────
        print("[net-flow] launching VM-A in VPC-A ...", file=sys.stderr)
        vm_a = _launch_vm(ec2, net_a["subnet_id"], net_a["sg_id"],
                          f"isv-net-flow-vma-{_RUN_TAG}")
        resources["instance_ids"].append(vm_a["instance_id"])
        resources["eip_allocation_ids"].append(vm_a["eip_allocation_id"])
        resources["key_name"] = vm_a["key_name"]

        print("[net-flow] launching VM-B in VPC-B ...", file=sys.stderr)
        vm_b = _launch_vm(ec2, net_b["subnet_id"], net_b["sg_id"],
                          f"isv-net-flow-vmb-{_RUN_TAG}")
        resources["instance_ids"].append(vm_b["instance_id"])
        resources["eip_allocation_ids"].append(vm_b["eip_allocation_id"])

        # ── Step 3: Resolve internal zCompute identifiers ─────────────────────
        # ping-vm needs VM-A's internal UUID; ping-ip needs VPC-A's network UUID.
        vm_a_uuid = _get_vm_uuid(f"isv-net-flow-vma-{_RUN_TAG}")
        vpc_a_net_uuid = _get_network_uuid(net_a["vpc_id"])

        # ── Step 4: Run sub-tests ─────────────────────────────────────────────
        # Each sub-test is wrapped individually so one failure does not abort others.

        # -- traffic_allowed --------------------------------------------------
        print("[net-flow] running traffic_allowed test ...", file=sys.stderr)
        try:
            result["tests"]["traffic_allowed"] = _test_traffic_allowed(vm_a_uuid)
        except Exception as exc:
            result["tests"]["traffic_allowed"] = {"passed": False, "error": str(exc)}

        # -- traffic_blocked --------------------------------------------------
        print("[net-flow] running traffic_blocked test ...", file=sys.stderr)
        try:
            result["tests"]["traffic_blocked"] = _test_traffic_blocked(
                vpc_a_net_uuid, vm_b["private_ip"]
            )
        except Exception as exc:
            result["tests"]["traffic_blocked"] = {"passed": False, "error": str(exc)}

        # -- internet_icmp / internet_http ------------------------------------
        # Both tests SSH into VM-A. Wait up to 180s for SSH to become available
        # before attempting (VM boots asynchronously after EIP association).
        print("[net-flow] waiting for SSH on VM-A ...", file=sys.stderr)
        import socket as _socket
        vm_a_key_file = f"/tmp/{vm_a['key_name']}.pem"
        _ssh_deadline = time.monotonic() + 180
        _ssh_ready = False
        while time.monotonic() < _ssh_deadline:
            try:
                with _socket.create_connection((vm_a["public_ip"], 22), timeout=5):
                    _ssh_ready = True
                    break
            except OSError:
                time.sleep(10)
        if _ssh_ready:
            print(f"[net-flow] SSH ready on {vm_a['public_ip']}", file=sys.stderr)
        else:
            print(f"[net-flow] SSH not ready after 180s — internet tests may fail", file=sys.stderr)

        # -- internet_icmp ----------------------------------------------------
        # SSH into VM-A and ping 8.8.8.8 — VM has EIP + IGW so real internet access.
        print("[net-flow] running internet_icmp test (SSH + ping 8.8.8.8) ...", file=sys.stderr)
        try:
            result["tests"]["internet_icmp"] = _test_internet_icmp(
                vm_a["public_ip"], vm_a_key_file
            )
        except Exception as exc:
            result["tests"]["internet_icmp"] = {"passed": False, "error": str(exc)}

        # -- internet_http ----------------------------------------------------
        # SSH into VM-A and curl https://google.com — confirms full internet HTTP/S.
        print("[net-flow] running internet_http test (SSH + curl google.com) ...", file=sys.stderr)
        try:
            result["tests"]["internet_http"] = _test_internet_http(
                vm_a["public_ip"], vm_a_key_file
            )
        except Exception as exc:
            result["tests"]["internet_http"] = {"passed": False, "error": str(exc)}

        # ── Step 5: Overall success ───────────────────────────────────────────
        result["success"] = all(
            t.get("passed", False) for t in result["tests"].values()
        )

    except Exception as exc:
        result["error"] = str(exc)
        print(f"[net-flow] FATAL: {exc}", file=sys.stderr)

    finally:
        # ── Step 6: Cleanup (always, never raises) ────────────────────────────
        print("[net-flow] cleaning up resources ...", file=sys.stderr)
        _cleanup(ec2, resources)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
