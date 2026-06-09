#!/usr/bin/env python3
"""Image CRUD operations for zCompute image registry validation.

Exercises get, list, create, and delete operations on machine images.

Notes:
  - get / list / delete use the EC2-compatible API via boto3
  - create uses symp machine-images create-machine-image-from-vm because
    zCompute does not implement the EC2 CreateImage API
  - The "create" operation requires a pre-baked VM that is powered OFF.
    Set ZCOMPUTE_PREBAKED_VM_ID to override the default VM UUID.

Environment variables:
  ZCOMPUTE_PREBAKED_VM_ID  UUID of a stopped VM to snapshot (default hardcoded)
  ZCOMPUTE_SYMP_*          symp CLI credentials (see upload_image.py)

Output JSON:
{
    "success": true,
    "platform": "image_registry",
    "image_id": "<input ami-id>",
    "operations": {
        "get":    {"passed": true, "image_name": "..."},
        "list":   {"passed": true, "total_images": N},
        "create": {"passed": true, "new_image_id": "ami-..."},
        "delete": {"passed": true}
    }
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

import subprocess

# Allow importing from parent scripts/ directory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.client import get_client  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402


# Default pre-baked VM ID. This VM must exist and be in stopped/shutoff state.
# Override with ZCOMPUTE_PREBAKED_VM_ID env var.
_DEFAULT_PREBAKED_VM_ID = "b867e0dd-780f-44b3-aaae-f4c2a4607fad"


def symp_cmd(args: list[str], timeout: int = 60) -> Any:
    """Run a symp CLI command inside the symp_docker container."""
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

    print(f"[crud] symp: {' '.join(args)}", file=sys.stderr)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            f"symp command failed (rc={proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return json.loads(proc.stdout)


def _uuid_to_ami(image_uuid: str) -> str:
    """Convert a zCompute internal UUID to an EC2-compatible AMI ID."""
    return "ami-" + image_uuid.replace("-", "")


def _poll_image_ready(
    image_uuid: str,
    timeout: int = 600,
    interval: int = 15,
) -> dict[str, Any]:
    """Poll symp machine-images get until state is 'ready' or 'error'."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        img = symp_cmd(["machine-images", "get", image_uuid])
        state = img.get("state", "unknown")
        print(f"[crud] image {image_uuid} state={state}", file=sys.stderr)
        if state == "ready":
            return img
        if state == "error":
            raise RuntimeError(f"Image {image_uuid} entered error state: {img}")
        time.sleep(interval)
    raise TimeoutError(
        f"Image {image_uuid} did not reach 'ready' within {timeout}s"
    )


def op_get(ec2: Any, image_id: str) -> dict[str, Any]:
    """Get a specific image by EC2 AMI ID."""
    resp = ec2.describe_images(ImageIds=[image_id])
    images = resp.get("Images", [])
    if not images:
        return {"passed": False, "error": f"Image {image_id} not found"}
    img = images[0]
    print(
        f"[crud] get: found {image_id} name={img.get('Name', '')} state={img.get('State', '')}",
        file=sys.stderr,
    )
    return {"passed": True, "image_name": img.get("Name", ""), "state": img.get("State", "")}


def op_list(ec2: Any) -> dict[str, Any]:
    """List all images visible to the project."""
    # zCompute: --owners self may return empty; do not filter by owner
    resp = ec2.describe_images()
    images = resp.get("Images", [])
    total = len(images)
    print(f"[crud] list: {total} images found", file=sys.stderr)
    return {"passed": True, "total_images": total}


def op_create(run_tag: str) -> dict[str, Any]:
    """Create a new image by snapshotting a pre-baked stopped VM via symp.

    EC2 CreateImage is not implemented in zCompute — we use
    symp machine-images create-machine-image-from-vm instead.
    The source VM must be stopped/shutoff.
    """
    vm_id = os.environ.get("ZCOMPUTE_PREBAKED_VM_ID", _DEFAULT_PREBAKED_VM_ID).strip()
    new_name = f"isv-ir-crud-{run_tag}"
    print(
        f"[crud] create: snapshotting VM {vm_id} -> image '{new_name}'",
        file=sys.stderr,
    )

    create_result = symp_cmd(
        [
            "machine-images", "create-machine-image-from-vm",
            "--description", f"ISV NCP CRUD test snapshot — {run_tag}",
            "--scope", "project",
            new_name,
            vm_id,
        ],
        timeout=60,
    )

    new_uuid = create_result.get("id") or create_result.get("uuid")
    if not new_uuid:
        raise RuntimeError(
            f"create-machine-image-from-vm did not return an ID. Response: {create_result}"
        )

    # Wait for the snapshot to finish
    _poll_image_ready(new_uuid, timeout=600, interval=15)

    new_ami_id = _uuid_to_ami(new_uuid)
    print(f"[crud] create: new image ready — {new_ami_id}", file=sys.stderr)
    return {"passed": True, "new_image_id": new_ami_id, "new_image_uuid": new_uuid}


def op_delete(ec2: Any, new_image_id: str) -> dict[str, Any]:
    """Deregister the image that was created in op_create."""
    try:
        ec2.deregister_image(ImageId=new_image_id)
        print(f"[crud] delete: deregistered {new_image_id}", file=sys.stderr)
        return {"passed": True}
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        # InvalidAMIID.NotFound is acceptable — image may already be gone
        if code in ("InvalidAMIID.NotFound", "InvalidAMIID.Unavailable"):
            print(f"[crud] delete: {new_image_id} already gone ({code})", file=sys.stderr)
            return {"passed": True}
        return {"passed": False, "error": str(e)}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Image CRUD validation for zCompute image registry"
    )
    parser.add_argument(
        "--image-id", required=True,
        help="EC2 AMI ID of the image uploaded by upload_image.py (ami-xxx)"
    )
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "symphony"))
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "image_registry",
        "image_id": args.image_id,
        "operations": {},
    }

    ec2 = get_client("ec2", region=args.region)
    run_tag = uuid.uuid4().hex[:8]
    ops: dict[str, Any] = {}
    new_image_id: str | None = None

    try:
        # ── 1. get ────────────────────────────────────────────────────────────
        try:
            ops["get"] = op_get(ec2, args.image_id)
        except Exception as e:
            ops["get"] = {"passed": False, "error": str(e)}

        # ── 2. list ───────────────────────────────────────────────────────────
        try:
            ops["list"] = op_list(ec2)
        except Exception as e:
            ops["list"] = {"passed": False, "error": str(e)}

        # ── 3. create ─────────────────────────────────────────────────────────
        try:
            create_result = op_create(run_tag)
            ops["create"] = create_result
            if create_result.get("passed"):
                new_image_id = create_result.get("new_image_id")
        except Exception as e:
            ops["create"] = {"passed": False, "error": str(e)}

        # ── 4. delete ─────────────────────────────────────────────────────────
        if new_image_id:
            try:
                ops["delete"] = op_delete(ec2, new_image_id)
            except Exception as e:
                ops["delete"] = {"passed": False, "error": str(e)}
        else:
            # Create failed — nothing to delete
            ops["delete"] = {
                "passed": False,
                "error": "Skipped — create did not produce a new image ID",
            }

        result["operations"] = ops

        # Overall success: all four operations must pass
        all_passed = all(op.get("passed") for op in ops.values())
        result["success"] = all_passed

    except Exception as e:
        result["error"] = str(e)
        result["operations"] = ops

    print(json.dumps(result, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
