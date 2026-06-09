#!/usr/bin/env python3
"""zCompute network connectivity test — replaces SSM-based NetworkConnectivityCheck.

NVIDIA's upstream NetworkConnectivityCheck uses SSM (AWS Systems Manager) to run
commands inside VMs. zCompute does not have SSM agents, so we replace it with
the native zCompute guestnet-admin-tool, which lets the management plane issue
arping commands to any tenant VM via its DHCP server — bypassing security groups.

What this script does:
  1. Creates a fresh VPC + subnet + security group (ICMP + SSH allowed).
  2. Launches 2 VMs (ZCOMPUTE_TEST_AMI_ID / ZCOMPUTE_TEST_INSTANCE_TYPE).
  3. Allocates EIPs so each VM has a public IP.
  4. Waits for both VMs to reach 'running' state.
  5. Translates EC2 instance IDs → internal zCompute UUIDs via 'symp vm list'.
  6. Calls 'guestnet-admin-tool ping-vm create --command-type arping' for each VM.
  7. Polls 'ping-vm get' until status leaves 'pending' (max 30 s).
  8. Records success = status == 'succeeded' AND output contains 'Received'.
  9. Cleans up ALL resources in a finally block (never raises from cleanup).
  10. Outputs the JSON structure expected by NetworkConnectivityCheck.

Output JSON:
{
    "success": true,
    "platform": "network",
    "test_name": "network_connectivity",
    "instances": [
        {"private_ip": "10.0.1.x", "public_ip": "172.28.x.x"},
        {"private_ip": "10.0.1.y", "public_ip": "172.28.x.y"}
    ],
    "tests": {
        "vm1_reachable": {"passed": true},
        "vm2_reachable": {"passed": true}
    }
}

zCompute quirks handled:
  - No boto3 waiters — replaced with poll loops throughout.
  - No auto-assigned public IP — EIPs allocated and associated manually.
  - run_instances may return empty Instances[] — falls back to poll by key name + LaunchTime.
  - describe_instances may ignore InstanceIds filter — post-filtered in Python.
  - TagSpecifications not supported in CreateSecurityGroup — stripped and re-tagged.
  - Instances sometimes land in 'shutoff' state — we send start_instances to recover.
  - symp vm list returns objects with 'id' (internal UUID) and 'name' fields.

Environment variables:
  ZCOMPUTE_TEST_AMI_ID            - AMI to launch (required)
  ZCOMPUTE_TEST_INSTANCE_TYPE     - Instance type (required, e.g. z2.3large)
  ZCOMPUTE_BASE_URL               - https://172.29.0.20 (required)
  AWS_ACCESS_KEY_ID               - required
  AWS_SECRET_ACCESS_KEY           - required
  AWS_REGION                      - default: symphony

  ZCOMPUTE_SYMP_URL               - symp endpoint, default http://172.29.0.20
  ZCOMPUTE_SYMP_USER              - default admin
  ZCOMPUTE_SYMP_DOMAIN            - default cloud_admin
  ZCOMPUTE_SYMP_PASSWORD          - default admin
  ZCOMPUTE_SYMP_PROJECT           - default default
  ZCOMPUTE_SYMP_CONTAINER         - default symp_docker

Usage:
    python3 network_connectivity_test.py --region symphony
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from typing import Any

from botocore.exceptions import ClientError

# Bring zcompute common modules onto the path regardless of cwd.
# This script lives at scripts/network/ and common/ is one level up.
_HERE = __import__("pathlib").Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))   # providers/zcompute/scripts/

from common.client import get_client          # noqa: E402
from common.ec2 import allocate_and_associate_eip  # noqa: E402

# ── Constants ─────────────────────────────────────────────────────────────────

# Unique tag suffix shared across all resources in this run so cleanup can
# reliably identify and remove them even if the run is aborted mid-way.
_RUN_TAG = f"isv-net-conn-{uuid.uuid4().hex[:8]}"

# VPC CIDR chosen to avoid collisions with other test scripts that use
# 10.85-10.99. NetworkConnectivityCheck gets its own /16 block.
_VPC_CIDR = "10.83.0.0/16"
_SUBNET_CIDR = "10.83.1.0/24"

# How long to wait for a VM to reach 'running' state (seconds).
_VM_LAUNCH_TIMEOUT = 600

# How long to poll guestnet-admin-tool for a ping result (seconds).
_PING_POLL_TIMEOUT = 90
_PING_POLL_INTERVAL = 3


# ── symp CLI helper ───────────────────────────────────────────────────────────

def _symp_cmd(args: list[str], timeout: int = 30) -> Any:
    """Run a symp CLI command via the symp_docker container and return parsed JSON.

    All symp calls go through 'sudo docker exec <container> symp -q -k ...',
    matching the pattern used by backend_switch_fabric_test.py.

    We pass '-f json' as the last argument so symp always returns machine-
    readable output rather than the human-friendly table format.

    Args:
        args:    symp subcommand + arguments (without auth flags or -f json).
        timeout: subprocess timeout in seconds.

    Returns:
        Parsed JSON — either a list or a dict depending on the command.

    Raises:
        RuntimeError: If the subprocess exits non-zero.
        json.JSONDecodeError: If symp output is not valid JSON.
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


# ── VPC / subnet / SG creation ───────────────────────────────────────────────

def _poll_vpc_available(ec2: Any, vpc_id: str, timeout: int = 120) -> None:
    """Poll until VPC leaves 'pending' state and becomes 'available'.

    zCompute transitions VPCs through 'pending' before 'available'. boto3
    waiters are not supported, so we poll manually.

    Args:
        ec2:     boto3 EC2 client.
        vpc_id:  VPC ID to poll.
        timeout: Maximum seconds to wait.

    Raises:
        RuntimeError: If VPC does not reach 'available' within timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = ec2.describe_vpcs(VpcIds=[vpc_id])
        state = resp["Vpcs"][0]["State"]
        if state == "available":
            return
        print(f"[net-conn] VPC {vpc_id} state={state}, waiting ...", file=sys.stderr)
        time.sleep(5)
    raise RuntimeError(f"VPC {vpc_id} did not reach 'available' within {timeout}s")


def _create_network(ec2: Any) -> dict[str, str]:
    """Create a VPC, subnet, internet gateway, route table, and security group.

    Returns a dict with vpc_id, subnet_id, sg_id, igw_id, rtb_id.

    Each resource is tagged with _RUN_TAG so cleanup can find it reliably.
    We build a minimal but correct network topology: one VPC, one subnet,
    one IGW wired into the route table, and one SG that allows ICMP from
    everywhere (required for ping tests) plus TCP/22 for optional SSH.
    """
    tag_suffix = _RUN_TAG

    # ── VPC ──────────────────────────────────────────────────────────────────
    vpc_resp = ec2.create_vpc(CidrBlock=_VPC_CIDR)
    vpc_id = vpc_resp["Vpc"]["VpcId"]
    print(f"[net-conn] created VPC {vpc_id}", file=sys.stderr)

    # Wait for VPC to become available (zCompute-specific poll loop).
    _poll_vpc_available(ec2, vpc_id)

    ec2.create_tags(
        Resources=[vpc_id],
        Tags=[
            {"Key": "Name", "Value": f"isv-net-conn-vpc-{tag_suffix}"},
            {"Key": "CreatedBy", "Value": "isvtest"},
            {"Key": "RunTag", "Value": tag_suffix},
        ],
    )

    # ── Internet Gateway ─────────────────────────────────────────────────────
    igw_resp = ec2.create_internet_gateway()
    igw_id = igw_resp["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
    ec2.create_tags(
        Resources=[igw_id],
        Tags=[{"Key": "Name", "Value": f"isv-net-conn-igw-{tag_suffix}"},
              {"Key": "CreatedBy", "Value": "isvtest"},
              {"Key": "RunTag", "Value": tag_suffix}],
    )

    # ── Subnet ───────────────────────────────────────────────────────────────
    # Single AZ only — zCompute has exactly one AZ ('symphony').
    azs = ec2.describe_availability_zones()
    az_name = azs["AvailabilityZones"][0]["ZoneName"]

    subnet_resp = ec2.create_subnet(
        VpcId=vpc_id, CidrBlock=_SUBNET_CIDR, AvailabilityZone=az_name
    )
    subnet_id = subnet_resp["Subnet"]["SubnetId"]
    ec2.create_tags(
        Resources=[subnet_id],
        Tags=[{"Key": "Name", "Value": f"isv-net-conn-subnet-{tag_suffix}"},
              {"Key": "CreatedBy", "Value": "isvtest"},
              {"Key": "RunTag", "Value": tag_suffix}],
    )

    # MapPublicIpOnLaunch is unsupported in zCompute (returns AuthFailure) —
    # we use explicit EIP allocation instead, so silently ignore failures here.
    try:
        ec2.modify_subnet_attribute(SubnetId=subnet_id, MapPublicIpOnLaunch={"Value": True})
    except ClientError:
        pass

    # ── Route table ──────────────────────────────────────────────────────────
    rtb_resp = ec2.create_route_table(VpcId=vpc_id)
    rtb_id = rtb_resp["RouteTable"]["RouteTableId"]
    ec2.create_route(
        RouteTableId=rtb_id, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id
    )
    ec2.associate_route_table(RouteTableId=rtb_id, SubnetId=subnet_id)
    ec2.create_tags(
        Resources=[rtb_id],
        Tags=[{"Key": "Name", "Value": f"isv-net-conn-rtb-{tag_suffix}"},
              {"Key": "CreatedBy", "Value": "isvtest"},
              {"Key": "RunTag", "Value": tag_suffix}],
    )

    # ── Security Group ───────────────────────────────────────────────────────
    # TagSpecifications is not supported in zCompute's CreateSecurityGroup —
    # we create the SG without tags first, then add them via create_tags.
    sg_resp = ec2.create_security_group(
        GroupName=f"isv-net-conn-sg-{tag_suffix}",
        Description="ISV NCP network connectivity test",
        VpcId=vpc_id,
    )
    sg_id = sg_resp["GroupId"]
    ec2.create_tags(
        Resources=[sg_id],
        Tags=[{"Key": "Name", "Value": f"isv-net-conn-sg-{tag_suffix}"},
              {"Key": "CreatedBy", "Value": "isvtest"},
              {"Key": "RunTag", "Value": tag_suffix}],
    )

    # Allow ICMP from anywhere (needed for arping to reach the VMs) and SSH.
    ec2.authorize_security_group_ingress(
        GroupId=sg_id,
        IpPermissions=[
            # ICMP (all types) — guestnet-admin-tool arping needs this
            {
                "IpProtocol": "icmp",
                "FromPort": -1,
                "ToPort": -1,
                "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "ICMP for arping"}],
            },
            # SSH — optional, useful for debugging
            {
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "SSH"}],
            },
        ],
    )

    print(f"[net-conn] network ready: vpc={vpc_id} subnet={subnet_id} sg={sg_id}", file=sys.stderr)
    return {
        "vpc_id": vpc_id,
        "subnet_id": subnet_id,
        "sg_id": sg_id,
        "igw_id": igw_id,
        "rtb_id": rtb_id,
    }


# ── VM launch ─────────────────────────────────────────────────────────────────

def _launch_vm(ec2: Any, subnet_id: str, sg_id: str, name: str) -> dict[str, Any]:
    """Launch a single VM and wait for it to reach 'running' state.

    Returns a dict with instance_id, private_ip, public_ip, eip_allocation_id.

    zCompute quirks handled here:
      - run_instances occasionally returns an empty Instances[] list; we fall
        back to polling describe_instances filtered by key name + launch time.
      - Instances can land in 'shutoff' instead of 'running'; we call
        start_instances to recover.
      - No auto-assigned public IP; EIP allocated and associated manually.
    """
    ami_id = os.environ.get("ZCOMPUTE_TEST_AMI_ID", "")
    instance_type = os.environ.get("ZCOMPUTE_TEST_INSTANCE_TYPE", "z2.3large")
    key_name = f"isv-net-conn-key-{_RUN_TAG}"

    if not ami_id:
        raise RuntimeError(
            "ZCOMPUTE_TEST_AMI_ID is not set. "
            "Export it to a valid zCompute AMI ID before running this script."
        )

    # ── Key pair ─────────────────────────────────────────────────────────────
    # Re-use the key if it already exists (idempotent across the two VM launches).
    key_file = f"/tmp/{key_name}.pem"
    try:
        ec2.describe_key_pairs(KeyNames=[key_name])
        # Key exists in EC2 — check that we have the PEM locally.
        if not os.path.exists(key_file):
            # PEM is gone; delete the cloud key so we can recreate it.
            ec2.delete_key_pair(KeyName=key_name)
            raise ClientError(
                {"Error": {"Code": "InvalidKeyPair.NotFound", "Message": "gone"}},
                "DescribeKeyPairs",
            )
        print(f"[net-conn] reusing key pair {key_name}", file=sys.stderr)
    except ClientError as e:
        if e.response["Error"]["Code"] == "InvalidKeyPair.NotFound":
            # CreateKeyPair does not support TagSpecifications in zCompute —
            # create the key pair first, then tag it separately.
            kp_resp = ec2.create_key_pair(KeyName=key_name)
            with open(key_file, "w") as fh:
                fh.write(kp_resp["KeyMaterial"])
            os.chmod(key_file, 0o600)
            print(f"[net-conn] created key pair {key_name}", file=sys.stderr)
        else:
            raise

    launch_time = time.time()

    # ── Launch VM ────────────────────────────────────────────────────────────
    print(f"[net-conn] launching VM '{name}' ({instance_type}) ...", file=sys.stderr)
    run_resp = ec2.run_instances(
        ImageId=ami_id,
        InstanceType=instance_type,
        MinCount=1,
        MaxCount=1,
        KeyName=key_name,
        SubnetId=subnet_id,
        SecurityGroupIds=[sg_id],
        # Root device for zCompute KVM-based VMs is /dev/vda, not /dev/sda.
        BlockDeviceMappings=[
            {
                "DeviceName": "/dev/vda",
                "Ebs": {"VolumeSize": 100, "VolumeType": "gp2", "DeleteOnTermination": True},
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

    # zCompute sometimes returns an empty Instances[] even on success.
    # Fall back to polling describe_instances by name + key to find the instance.
    instances_list = run_resp.get("Instances", [])
    if instances_list:
        instance_id = instances_list[0]["InstanceId"]
        private_ip = instances_list[0].get("PrivateIpAddress")
    else:
        print(
            f"[net-conn] run_instances returned empty Instances[] — polling by name ...",
            file=sys.stderr,
        )
        instance_id = _find_instance_by_name(ec2, name, after_timestamp=launch_time)
        private_ip = None  # will be filled in after polling

    print(f"[net-conn] instance {instance_id} launched for '{name}'", file=sys.stderr)

    # ── Poll for running state ────────────────────────────────────────────────
    # zCompute occasionally puts instances into 'shutoff' instead of 'running'.
    # Detect this and call start_instances to recover.
    deadline = time.monotonic() + _VM_LAUNCH_TIMEOUT
    state = "pending"
    while time.monotonic() < deadline:
        try:
            desc = ec2.describe_instances(InstanceIds=[instance_id])
            # post-filter in Python because zCompute may return all instances
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
            print(f"[net-conn] describe_instances error (non-fatal): {exc}", file=sys.stderr)

        if state == "running":
            print(f"[net-conn] {instance_id} is running", file=sys.stderr)
            break
        elif state in ("shutoff", "stopped"):
            # zCompute scheduling quirk: instance parked in shutoff.
            # Send start command and continue polling.
            print(
                f"[net-conn] {instance_id} in {state} — sending start_instances ...",
                file=sys.stderr,
            )
            try:
                ec2.start_instances(InstanceIds=[instance_id])
            except Exception as exc:
                print(f"[net-conn] start_instances failed (non-fatal): {exc}", file=sys.stderr)
        else:
            print(f"[net-conn] {instance_id} state={state}, waiting ...", file=sys.stderr)

        time.sleep(20)
    else:
        raise RuntimeError(
            f"Instance {instance_id} did not reach 'running' within {_VM_LAUNCH_TIMEOUT}s "
            f"(last state: {state})"
        )

    # ── Allocate and associate EIP ────────────────────────────────────────────
    # zCompute does not auto-assign public IPs; an EIP must be allocated and
    # explicitly associated with the instance.
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
    """Fall-back: find an instance by Name tag when run_instances returns no IDs.

    zCompute sometimes returns an empty Instances[] from run_instances.
    We poll describe_instances (which returns ALL project instances, not filtered
    by VPC or subnet) and match on Name tag + launch time to avoid false positives
    from other tests running concurrently.

    Args:
        ec2:             boto3 EC2 client.
        name:            Value of the 'Name' tag.
        after_timestamp: Unix timestamp before which we ignore instances.
        timeout:         Maximum seconds to search.

    Returns:
        EC2 instance ID string.

    Raises:
        RuntimeError: If no matching instance is found within timeout.
    """
    import datetime

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        all_instances = ec2.describe_instances()
        for reservation in all_instances.get("Reservations", []):
            for inst in reservation.get("Instances", []):
                # Match on Name tag
                tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                if tags.get("Name") != name:
                    continue
                # Ignore instances launched before this run to avoid false positives.
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
                    print(f"[net-conn] found instance {iid} by name tag", file=sys.stderr)
                    return iid
        print(f"[net-conn] waiting for instance '{name}' to appear ...", file=sys.stderr)
        time.sleep(10)

    raise RuntimeError(f"Could not find instance with Name='{name}' after {timeout}s")


# ── zCompute UUID translation ─────────────────────────────────────────────────

def _get_zcompute_vm_uuid(vm_name: str) -> str | None:
    """Translate an EC2 Name-tag to the internal zCompute VM UUID.

    guestnet-admin-tool ping-vm requires the internal zCompute UUID, not
    the EC2 instance ID (i-xxx). We get it by calling 'symp vm list -f json',
    which returns objects with 'id' (UUID) and 'name' fields.

    We match on 'name' because that is what zCompute surfaces from the EC2
    Name tag in the EC2-compatible API.

    Args:
        vm_name: The Name tag value set on the EC2 instance.

    Returns:
        zCompute UUID string, or None if not found.
    """
    try:
        vms = _symp_cmd(["vm", "list"], timeout=30)
        # vms is a list of dicts; each has 'id' and 'name'
        for vm in vms:
            if vm.get("name") == vm_name:
                return vm.get("id")
        print(
            f"[net-conn] WARNING: could not find zCompute UUID for VM '{vm_name}'",
            file=sys.stderr,
        )
        return None
    except Exception as exc:
        print(f"[net-conn] WARNING: symp vm list failed: {exc}", file=sys.stderr)
        return None


# ── guestnet-admin-tool ping ─────────────────────────────────────────────────

def _ping_vm(zcompute_uuid: str) -> dict[str, Any]:
    """Arping a VM from its DHCP server using guestnet-admin-tool.

    arping works at Layer 2 (ARP-level) and bypasses tenant security groups
    entirely — it originates from the DHCP server on the management plane,
    not from another tenant VM. This makes it the ideal connectivity check
    for zCompute where SSM is unavailable.

    Flow:
      1. POST: ping-vm create --command-type arping <uuid>  → returns {"id": ..., "status": "pending"}
      2. GET: ping-vm get <ping-id>  → poll until status != "pending"

    A result of status=="succeeded" AND "Received" in output means the VM is
    live and the DHCP/L2 fabric is healthy.

    Args:
        zcompute_uuid: Internal zCompute VM UUID.

    Returns:
        Dict with keys: passed (bool), status (str), output (str), error (str or None).
    """
    result: dict[str, Any] = {"passed": False, "status": "unknown", "output": "", "error": None}

    try:
        # ── Step 1: Create the ping job ───────────────────────────────────────
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

        print(
            f"[net-conn] ping-vm job {ping_id} created for {zcompute_uuid}, polling ...",
            file=sys.stderr,
        )

        # ── Step 2: Poll until the job leaves 'pending' ───────────────────────
        deadline = time.monotonic() + _PING_POLL_TIMEOUT
        ping_status = "pending"
        ping_output = ""

        while time.monotonic() < deadline:
            get_resp = _symp_cmd(
                ["guestnet-admin-tool", "ping-vm", "get", ping_id],
                timeout=15,
            )
            ping_status = get_resp.get("status", "unknown")
            ping_output = get_resp.get("output", "")

            if ping_status != "pending":
                break
            time.sleep(_PING_POLL_INTERVAL)
        else:
            # Timed out while still pending — treat as a failed ping.
            result["error"] = f"ping-vm job {ping_id} still pending after {_PING_POLL_TIMEOUT}s"
            result["status"] = "timeout"
            return result

        result["status"] = ping_status
        result["output"] = ping_output

        # ── Determine success ─────────────────────────────────────────────────
        # status=="succeeded" alone is the primary indicator; we also check that
        # the arping output contains "Received" to guard against a spurious success
        # response from the API (has not been observed, but is cheap to verify).
        if ping_status == "succeeded" and "Received" in ping_output:
            result["passed"] = True
        else:
            result["error"] = (
                f"ping-vm status={ping_status!r}, output={ping_output!r}"
            )

    except Exception as exc:
        result["error"] = str(exc)

    return result


# ── Resource cleanup ──────────────────────────────────────────────────────────

def _cleanup(ec2: Any, resources: dict[str, Any]) -> None:
    """Best-effort cleanup of all resources created by this script.

    Called from a finally block — MUST NOT raise. Each deletion is wrapped in
    its own try/except so a failure on one resource does not skip the others.

    Deletion order matters because zCompute enforces dependency constraints:
      VMs → EIPs → SG → subnet → route table → IGW (detach + delete) → VPC

    Args:
        ec2:       boto3 EC2 client.
        resources: Dict populated during resource creation, keyed by resource type.
    """

    def _try(desc: str, fn: Any, *args: Any, **kwargs: Any) -> None:
        try:
            fn(*args, **kwargs)
            print(f"[net-conn] cleanup: deleted {desc}", file=sys.stderr)
        except Exception as exc:
            print(f"[net-conn] cleanup WARNING: could not delete {desc}: {exc}", file=sys.stderr)

    # ── Terminate VMs ─────────────────────────────────────────────────────────
    for iid in resources.get("instance_ids", []):
        _try(f"instance {iid}", ec2.terminate_instances, InstanceIds=[iid])

    # Wait briefly for instances to begin termination before deleting dependent
    # resources (SG may be "in use" while VMs are still being terminated).
    if resources.get("instance_ids"):
        print("[net-conn] cleanup: waiting 20s for instances to terminate ...", file=sys.stderr)
        time.sleep(20)
        # Poll for termination (allow up to 120s)
        deadline = time.monotonic() + 120
        for iid in resources.get("instance_ids", []):
            while time.monotonic() < deadline:
                try:
                    desc = ec2.describe_instances(InstanceIds=[iid])
                    state = desc["Reservations"][0]["Instances"][0]["State"]["Name"]
                    if state in ("terminated", "shutting-down"):
                        break
                except Exception:
                    break  # instance is likely gone
                time.sleep(10)

    # ── Release EIPs ──────────────────────────────────────────────────────────
    for alloc_id in resources.get("eip_allocation_ids", []):
        # Disassociate first (may have already been done by instance termination)
        try:
            assoc_resp = ec2.describe_addresses(AllocationIds=[alloc_id])
            assoc_id = assoc_resp["Addresses"][0].get("AssociationId")
            if assoc_id:
                _try(f"EIP association {assoc_id}", ec2.disassociate_address, AssociationId=assoc_id)
        except Exception:
            pass
        _try(f"EIP {alloc_id}", ec2.release_address, AllocationId=alloc_id)

    # ── Delete Security Group ─────────────────────────────────────────────────
    sg_id = resources.get("sg_id")
    if sg_id:
        _try(f"security group {sg_id}", ec2.delete_security_group, GroupId=sg_id)

    # ── Delete Route Table ────────────────────────────────────────────────────
    rtb_id = resources.get("rtb_id")
    if rtb_id:
        # Disassociate from subnet first
        try:
            rtb_desc = ec2.describe_route_tables(RouteTableIds=[rtb_id])
            for assoc in rtb_desc["RouteTables"][0].get("Associations", []):
                if not assoc.get("Main"):
                    _try(
                        f"route table association {assoc['RouteTableAssociationId']}",
                        ec2.disassociate_route_table,
                        AssociationId=assoc["RouteTableAssociationId"],
                    )
        except Exception:
            pass
        _try(f"route table {rtb_id}", ec2.delete_route_table, RouteTableId=rtb_id)

    # ── Delete Subnet ─────────────────────────────────────────────────────────
    subnet_id = resources.get("subnet_id")
    if subnet_id:
        _try(f"subnet {subnet_id}", ec2.delete_subnet, SubnetId=subnet_id)

    # ── Detach and delete Internet Gateway ────────────────────────────────────
    igw_id = resources.get("igw_id")
    vpc_id = resources.get("vpc_id")
    if igw_id and vpc_id:
        _try(
            f"IGW attachment {igw_id}/{vpc_id}",
            ec2.detach_internet_gateway,
            InternetGatewayId=igw_id,
            VpcId=vpc_id,
        )
    if igw_id:
        _try(f"IGW {igw_id}", ec2.delete_internet_gateway, InternetGatewayId=igw_id)

    # ── Delete VPC ────────────────────────────────────────────────────────────
    if vpc_id:
        _try(f"VPC {vpc_id}", ec2.delete_vpc, VpcId=vpc_id)

    # ── Delete Key Pair ───────────────────────────────────────────────────────
    key_name = resources.get("key_name")
    if key_name:
        _try(f"key pair {key_name}", ec2.delete_key_pair, KeyName=key_name)


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    """Create VMs, ping them via guestnet-admin-tool, clean up, output JSON."""
    parser = argparse.ArgumentParser(
        description="zCompute NetworkConnectivityCheck replacement (guestnet-admin-tool)"
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION", "symphony"),
        help="AWS region (default: symphony)",
    )
    args = parser.parse_args()

    ec2 = get_client("ec2", region=args.region)

    # Result skeleton — matches the shape expected by NetworkConnectivityCheck.
    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "network_connectivity",
        "instances": [],
        "tests": {
            "vm1_reachable": {"passed": False},
            "vm2_reachable": {"passed": False},
        },
    }

    # Track all created resources so cleanup knows what to delete.
    resources: dict[str, Any] = {
        "instance_ids": [],
        "eip_allocation_ids": [],
    }

    try:
        # ── Step 1: Create network infrastructure ─────────────────────────────
        print(f"[net-conn] creating network (run_tag={_RUN_TAG}) ...", file=sys.stderr)
        net = _create_network(ec2)
        resources.update({
            "vpc_id": net["vpc_id"],
            "subnet_id": net["subnet_id"],
            "sg_id": net["sg_id"],
            "igw_id": net["igw_id"],
            "rtb_id": net["rtb_id"],
        })

        # ── Step 2: Launch two VMs ─────────────────────────────────────────────
        print("[net-conn] launching VM 1 ...", file=sys.stderr)
        vm1 = _launch_vm(ec2, net["subnet_id"], net["sg_id"], f"isv-net-conn-vm1-{_RUN_TAG}")
        resources["instance_ids"].append(vm1["instance_id"])
        resources["eip_allocation_ids"].append(vm1["eip_allocation_id"])
        resources["key_name"] = vm1["key_name"]

        print("[net-conn] launching VM 2 ...", file=sys.stderr)
        vm2 = _launch_vm(ec2, net["subnet_id"], net["sg_id"], f"isv-net-conn-vm2-{_RUN_TAG}")
        resources["instance_ids"].append(vm2["instance_id"])
        resources["eip_allocation_ids"].append(vm2["eip_allocation_id"])

        # Populate the 'instances' field for NetworkConnectivityCheck
        result["instances"] = [
            {"private_ip": vm1["private_ip"], "public_ip": vm1["public_ip"]},
            {"private_ip": vm2["private_ip"], "public_ip": vm2["public_ip"]},
        ]

        # ── Step 3: Translate EC2 instance IDs → zCompute internal UUIDs ──────
        # guestnet-admin-tool ping-vm operates on the internal zCompute UUID, not
        # the EC2 instance ID.  We match on the Name tag we set at launch.
        vm1_uuid = _get_zcompute_vm_uuid(f"isv-net-conn-vm1-{_RUN_TAG}")
        vm2_uuid = _get_zcompute_vm_uuid(f"isv-net-conn-vm2-{_RUN_TAG}")

        if not vm1_uuid:
            result["tests"]["vm1_reachable"] = {
                "passed": False,
                "error": "Could not resolve zCompute UUID for VM1",
            }
        if not vm2_uuid:
            result["tests"]["vm2_reachable"] = {
                "passed": False,
                "error": "Could not resolve zCompute UUID for VM2",
            }

        # ── Step 4: Ping VMs via guestnet-admin-tool ───────────────────────────
        # Wait 30s after VMs are running to allow DHCP to assign IPs and the
        # network stack to initialize. Without this, arping from the DHCP server
        # finds the VM's L2 address before the guest network stack is ready and
        # the ping-vm job stays in "processing" indefinitely.
        print("[net-conn] waiting 30s for VM network stack to initialize ...", file=sys.stderr)
        time.sleep(30)

        # Each ping is executed independently; one failure does not skip the other.
        if vm1_uuid:
            print(f"[net-conn] pinging VM1 (uuid={vm1_uuid}) ...", file=sys.stderr)
            ping1 = _ping_vm(vm1_uuid)
            result["tests"]["vm1_reachable"] = {
                "passed": ping1["passed"],
                "status": ping1["status"],
                "output": ping1.get("output", ""),
            }
            if ping1.get("error"):
                result["tests"]["vm1_reachable"]["error"] = ping1["error"]

        if vm2_uuid:
            print(f"[net-conn] pinging VM2 (uuid={vm2_uuid}) ...", file=sys.stderr)
            ping2 = _ping_vm(vm2_uuid)
            result["tests"]["vm2_reachable"] = {
                "passed": ping2["passed"],
                "status": ping2["status"],
                "output": ping2.get("output", ""),
            }
            if ping2.get("error"):
                result["tests"]["vm2_reachable"]["error"] = ping2["error"]

        # ── Step 5: Overall success ────────────────────────────────────────────
        result["success"] = all(
            t.get("passed", False) for t in result["tests"].values()
        )

    except Exception as exc:
        result["error"] = str(exc)
        print(f"[net-conn] FATAL: {exc}", file=sys.stderr)

    finally:
        # ── Step 6: Cleanup (always runs, never raises) ────────────────────────
        print("[net-conn] cleaning up resources ...", file=sys.stderr)
        _cleanup(ec2, resources)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
