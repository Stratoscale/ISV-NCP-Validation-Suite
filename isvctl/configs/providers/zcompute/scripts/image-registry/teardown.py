#!/usr/bin/env python3
"""Teardown for the zCompute image registry validation suite.

Best-effort cleanup of all resources created during the image registry test run.
Never raises — individual cleanup failures are logged but do not affect the
overall success flag (teardown always returns success: true).

Cleanup order:
  1. Terminate the instance created by launch_instance.py
  2. Release the associated EIP
  3. Delete the key pair + local PEM file
  4. Delete the security group
  5. Deregister the image uploaded by upload_image.py (and its symp record)

Also performs a broad sweep for any stale isv-ir-* resources left over from
interrupted runs (instances, EIPs, key pairs, images).

Environment:
  ZCOMPUTE_SYMP_*  symp CLI credentials (see upload_image.py)

Output JSON:
{
    "success": true,
    "platform": "image_registry",
    "test_name": "teardown",
    "resources_deleted": []
}
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

# Allow importing from parent scripts/ directory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.client import get_client  # noqa: E402


def symp_cmd(args: list[str], timeout: int = 60) -> Any:
    """Run a symp CLI command. Returns parsed JSON or None on error."""
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
        print(
            f"[teardown] symp {args[:3]} failed: {proc.stderr.strip() or proc.stdout.strip()}",
            file=sys.stderr,
        )
        return None
    try:
        return json.loads(proc.stdout)
    except Exception:
        return None


def _safe(fn_name: str, fn: Any, *a: Any, **kw: Any) -> bool:
    """Call fn(*a, **kw), log any exception, and always return True."""
    try:
        fn(*a, **kw)
        return True
    except Exception as e:
        print(f"[teardown] {fn_name} failed (non-fatal): {e}", file=sys.stderr)
        return False


# ── Individual cleanup helpers ────────────────────────────────────────────────

def _terminate_instance(ec2: Any, instance_id: str) -> None:
    if not instance_id or instance_id == "null":
        return
    try:
        resp = ec2.describe_instances(InstanceIds=[instance_id])
        inst_list = [
            i for r in resp.get("Reservations", [])
            for i in r.get("Instances", [])
            if i["InstanceId"] == instance_id
        ]
        if not inst_list:
            print(f"[teardown] instance {instance_id} not found — skipping", file=sys.stderr)
            return
        state = inst_list[0]["State"]["Name"]
        if state in ("terminated", "shutting-down"):
            print(f"[teardown] instance {instance_id} already {state}", file=sys.stderr)
            return
    except ClientError:
        return

    print(f"[teardown] terminating instance {instance_id}", file=sys.stderr)
    ec2.terminate_instances(InstanceIds=[instance_id])
    # Poll until terminated (best effort — don't block teardown too long)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        try:
            resp = ec2.describe_instances(InstanceIds=[instance_id])
            inst_list = [
                i for r in resp.get("Reservations", [])
                for i in r.get("Instances", [])
                if i["InstanceId"] == instance_id
            ]
            if not inst_list:
                break
            state = inst_list[0]["State"]["Name"]
            print(f"[teardown] instance {instance_id} state={state}", file=sys.stderr)
            if state == "terminated":
                break
        except Exception:
            break
        time.sleep(15)


def _release_eip(ec2: Any, allocation_id: str) -> None:
    if not allocation_id or allocation_id == "null":
        return
    print(f"[teardown] releasing EIP {allocation_id}", file=sys.stderr)
    ec2.release_address(AllocationId=allocation_id)


def _delete_key_pair(ec2: Any, key_name: str) -> None:
    if not key_name or key_name == "null":
        return
    print(f"[teardown] deleting key pair '{key_name}'", file=sys.stderr)
    ec2.delete_key_pair(KeyName=key_name)
    # Remove local PEM
    for path in [f"/tmp/{key_name}.pem", f"/tmp/{key_name}"]:
        try:
            if os.path.exists(path):
                os.remove(path)
                print(f"[teardown] removed {path}", file=sys.stderr)
        except Exception:
            pass


def _delete_security_group(ec2: Any, sg_id: str, max_retries: int = 4) -> None:
    if not sg_id or sg_id == "null":
        return
    for attempt in range(1, max_retries + 1):
        try:
            ec2.delete_security_group(GroupId=sg_id)
            print(f"[teardown] deleted security group {sg_id}", file=sys.stderr)
            return
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("DependencyViolation", "InvalidGroup.InUse"):
                if attempt < max_retries:
                    print(
                        f"[teardown] SG {sg_id} still has dependencies "
                        f"(attempt {attempt}/{max_retries}), retrying in 15s ...",
                        file=sys.stderr,
                    )
                    time.sleep(15)
            elif code in ("InvalidGroup.NotFound",):
                print(f"[teardown] SG {sg_id} already gone", file=sys.stderr)
                return
            else:
                raise


def _deregister_image(ec2: Any, image_id: str) -> None:
    """Deregister an EC2 image and delete its symp machine-image record."""
    if not image_id or image_id == "null":
        return
    print(f"[teardown] deregistering image {image_id}", file=sys.stderr)
    try:
        ec2.deregister_image(ImageId=image_id)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code not in ("InvalidAMIID.NotFound", "InvalidAMIID.Unavailable"):
            raise

    # Also delete via symp to remove the internal machine-image record.
    # Convert ami-xxx back to UUID: strip "ami-" prefix and reinsert hyphens
    # at positions 8-4-4-4-12.
    hex_str = image_id.replace("ami-", "")
    if len(hex_str) == 32:
        internal_uuid = (
            f"{hex_str[0:8]}-{hex_str[8:12]}-{hex_str[12:16]}"
            f"-{hex_str[16:20]}-{hex_str[20:32]}"
        )
        print(
            f"[teardown] deleting symp machine-image {internal_uuid}",
            file=sys.stderr,
        )
        symp_cmd(["machine-images", "delete", internal_uuid])


def _sweep_stale_instances(ec2: Any) -> None:
    """Terminate any running/stopped instances tagged isv-ir-* or named isv-ir-*."""
    try:
        resp = ec2.describe_instances()
        for res in resp.get("Reservations", []):
            for inst in res.get("Instances", []):
                iid = inst["InstanceId"]
                state = inst["State"]["Name"]
                if state in ("terminated", "shutting-down"):
                    continue
                tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                name = tags.get("Name", "")
                if name.startswith("isv-ir-"):
                    print(
                        f"[teardown] sweep: terminating stale instance {iid} ({name})",
                        file=sys.stderr,
                    )
                    _safe("terminate_stale", ec2.terminate_instances, InstanceIds=[iid])
    except Exception as e:
        print(f"[teardown] sweep_instances failed: {e}", file=sys.stderr)


def _sweep_stale_key_pairs(ec2: Any) -> None:
    """Delete key pairs matching isv-ir-*."""
    try:
        resp = ec2.describe_key_pairs()
        for kp in resp.get("KeyPairs", []):
            kname = kp.get("KeyName", "")
            if kname.startswith("isv-ir-"):
                print(
                    f"[teardown] sweep: deleting stale key pair '{kname}'",
                    file=sys.stderr,
                )
                _safe("delete_stale_key", ec2.delete_key_pair, KeyName=kname)
    except Exception as e:
        print(f"[teardown] sweep_key_pairs failed: {e}", file=sys.stderr)


def _sweep_stale_images_symp() -> None:
    """Delete any symp machine-images named isv-ir-* that are still registered."""
    try:
        images = symp_cmd(["machine-images", "list"])
        if not images:
            return
        for img in images:
            name = img.get("name", "")
            img_id = img.get("id") or img.get("uuid")
            if name.startswith("isv-ir-") and img_id:
                print(
                    f"[teardown] sweep: deleting stale image '{name}' ({img_id})",
                    file=sys.stderr,
                )
                symp_cmd(["machine-images", "delete", img_id])
    except Exception as e:
        print(f"[teardown] sweep_images_symp failed: {e}", file=sys.stderr)


def _sweep_stale_eips(ec2: Any) -> None:
    """Release any unassociated EIPs (broad sweep — only unassociated ones)."""
    try:
        resp = ec2.describe_addresses()
        for addr in resp.get("Addresses", []):
            # Only release if not currently associated with anything
            if not addr.get("AssociationId") and not addr.get("InstanceId"):
                alloc_id = addr.get("AllocationId")
                if alloc_id:
                    print(
                        f"[teardown] sweep: releasing unassociated EIP {alloc_id}",
                        file=sys.stderr,
                    )
                    _safe("release_stale_eip", ec2.release_address, AllocationId=alloc_id)
    except Exception as e:
        print(f"[teardown] sweep_eips failed: {e}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Teardown for zCompute image registry validation"
    )
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "symphony"))
    # These come from the step outputs via config template substitution
    parser.add_argument("--image-id", nargs="?", const="", default="", help="AMI ID from upload_image step")
    parser.add_argument("--instance-id", nargs="?", const="", default="", help="Instance ID from launch_instance step")
    parser.add_argument("--eip-allocation-id", nargs="?", const="", default="", help="EIP allocation ID")
    parser.add_argument("--key-name", nargs="?", const="", default="", help="Key pair name")
    parser.add_argument("--security-group-id", nargs="?", const="", default="", help="Security group ID")
    parser.add_argument(
        "--skip-destroy",
        action="store_true",
        default=False,
        help="Skip actual deletion (dry-run / debugging)",
    )
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": True,
        "platform": "image_registry",
        "test_name": "teardown",
        "resources_deleted": [],
    }

    if args.skip_destroy:
        print("[teardown] --skip-destroy set; skipping all cleanup", file=sys.stderr)
        print(json.dumps(result, indent=2))
        return 0

    ec2 = get_client("ec2", region=args.region)

    # ── Targeted cleanup from step outputs ────────────────────────────────────
    _safe("terminate_instance", _terminate_instance, ec2, args.instance_id)
    _safe("release_eip", _release_eip, ec2, args.eip_allocation_id)
    _safe("delete_key_pair", _delete_key_pair, ec2, args.key_name)
    _safe("delete_security_group", _delete_security_group, ec2, args.security_group_id)
    _safe("deregister_image", _deregister_image, ec2, args.image_id)

    # ── Broad sweep: clean up any leftover isv-ir-* resources ─────────────────
    # This catches resources from interrupted runs that didn't reach teardown.
    _sweep_stale_instances(ec2)
    _sweep_stale_key_pairs(ec2)
    _sweep_stale_images_symp()
    # Note: only sweep unassociated EIPs to avoid releasing EIPs from other suites
    _sweep_stale_eips(ec2)

    print("[teardown] cleanup complete", file=sys.stderr)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
