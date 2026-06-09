#!/usr/bin/env python3
"""Image CRUD operations for zCompute image registry validation.

All operations use the symp CLI (machine-images commands) instead of the
EC2 API. The EC2 describe-images API does not reliably return images that
were created or imported via symp in zCompute.

Operations:
  get    - symp machine-images get <uuid>
  list   - symp machine-images list
  create - symp machine-images create-machine-image-from-vm
  delete - symp machine-images delete <uuid>

Environment variables:
  ZCOMPUTE_PREBAKED_VM_ID  UUID of a running/stopped VM to snapshot (default hardcoded)
  ZCOMPUTE_SYMP_*          symp CLI credentials

Output JSON:
{
    "success": true,
    "platform": "image_registry",
    "image_id": "<input ami-id>",
    "operations": {
        "get":    {"passed": true, "image_name": "...", "state": "ready"},
        "list":   {"passed": true, "total_images": N},
        "create": {"passed": true, "new_image_id": "ami-...", "new_image_uuid": "..."},
        "delete": {"passed": true}
    }
}
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Default pre-baked VM ID to snapshot for the 'create' test.
# Override with ZCOMPUTE_PREBAKED_VM_ID env var.
_DEFAULT_PREBAKED_VM_ID = "b867e0dd-780f-44b3-aaae-f4c2a4607fad"


# ── symp CLI helper ────────────────────────────────────────────────────────────

def symp_cmd(args: list[str], timeout: int = 60) -> Any:
    """Run a symp CLI command inside the symp_docker container and return JSON."""
    url = os.environ.get("ZCOMPUTE_SYMP_URL", "http://172.29.0.20")
    user = os.environ.get("ZCOMPUTE_SYMP_USER", "amitor")
    domain = os.environ.get("ZCOMPUTE_SYMP_DOMAIN", "amitor")
    password = os.environ.get("ZCOMPUTE_SYMP_PASSWORD", "S123456!")
    project = os.environ.get("ZCOMPUTE_SYMP_PROJECT", "ISV")
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


# ── ID conversion helpers ──────────────────────────────────────────────────────

def _ami_to_uuid(ami_id: str) -> str:
    """Convert EC2 AMI ID to internal zCompute UUID.

    ami-12f9907ef7c34419b3f4989f9ff91b4b  →  12f9907e-f7c3-4419-b3f4-989f9ff91b4b
    """
    h = ami_id.removeprefix("ami-")
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def _uuid_to_ami(image_uuid: str) -> str:
    """Convert internal zCompute UUID to EC2-compatible AMI ID."""
    return "ami-" + image_uuid.replace("-", "")


# ── Image readiness polling ────────────────────────────────────────────────────

def _poll_image_ready(image_uuid: str, timeout: int = 600, interval: int = 15) -> dict:
    """Poll symp machine-images get until state is 'ready' or 'error'."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        img = symp_cmd(["machine-images", "get", image_uuid])
        state = img.get("state", "unknown")
        print(f"[crud] image {image_uuid} state={state}", file=sys.stderr)
        if state == "ready":
            return img
        if state == "error":
            raise RuntimeError(f"Image {image_uuid} entered error state")
        time.sleep(interval)
    raise TimeoutError(f"Image {image_uuid} did not reach 'ready' within {timeout}s")


# ── CRUD operations ────────────────────────────────────────────────────────────

def op_get(image_id: str) -> dict[str, Any]:
    """Get a specific image by EC2 AMI ID using symp machine-images get.

    The AMI ID is converted back to the internal UUID for the symp call.
    """
    uuid_val = _ami_to_uuid(image_id)
    img = symp_cmd(["machine-images", "get", uuid_val], timeout=30)
    state = img.get("state", "")
    name = img.get("name", "")
    print(f"[crud] get: found {image_id} name={name} state={state}", file=sys.stderr)
    return {"passed": True, "image_name": name, "state": state}


def op_list() -> dict[str, Any]:
    """List all machine images visible to the current project via symp."""
    images = symp_cmd(["machine-images", "list"], timeout=30)
    total = len(images) if isinstance(images, list) else 0
    print(f"[crud] list: {total} images found", file=sys.stderr)
    return {"passed": True, "total_images": total}


def op_create(run_tag: str) -> dict[str, Any]:
    """Create a new image by snapshotting a VM via symp machine-images create-machine-image-from-vm.

    EC2 CreateImage is not implemented in zCompute — we use the symp API instead.
    The --no-reboot flag avoids disrupting the source VM.
    """
    vm_id = os.environ.get("ZCOMPUTE_PREBAKED_VM_ID", _DEFAULT_PREBAKED_VM_ID).strip()
    new_name = f"isv-ir-crud-{run_tag}"
    print(f"[crud] create: snapshotting VM {vm_id} -> image '{new_name}'", file=sys.stderr)

    create_resp = symp_cmd(
        [
            "machine-images", "create-machine-image-from-vm",
            "--description", f"ISV NCP CRUD test snapshot — {run_tag}",
            "--scope", "project",
            "--no-reboot",
            new_name,
            vm_id,
        ],
        timeout=60,
    )

    new_uuid = create_resp.get("id") or create_resp.get("uuid")
    if not new_uuid:
        raise RuntimeError(f"create-machine-image-from-vm returned no ID: {create_resp}")

    # Wait for snapshot to finish
    _poll_image_ready(new_uuid, timeout=600, interval=15)

    new_ami_id = _uuid_to_ami(new_uuid)
    print(f"[crud] create: new image ready — {new_ami_id}", file=sys.stderr)
    return {"passed": True, "new_image_id": new_ami_id, "new_image_uuid": new_uuid}


def op_delete(new_image_uuid: str) -> dict[str, Any]:
    """Delete the image created by op_create using symp machine-images delete."""
    symp_cmd(["machine-images", "delete", new_image_uuid], timeout=30)
    print(f"[crud] delete: deleted {new_image_uuid} via symp", file=sys.stderr)
    return {"passed": True}


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Image CRUD validation for zCompute")
    parser.add_argument("--image-id", required=True, help="EC2 AMI ID from upload_image step")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "symphony"))
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "image_registry",
        "image_id": args.image_id,
        "operations": {},
    }

    run_tag = uuid.uuid4().hex[:8]
    ops: dict[str, Any] = {}
    new_image_uuid: str = ""

    # ── 1. get ────────────────────────────────────────────────────────────────
    try:
        ops["get"] = op_get(args.image_id)
    except Exception as e:
        ops["get"] = {"passed": False, "error": str(e)}

    # ── 2. list ───────────────────────────────────────────────────────────────
    try:
        ops["list"] = op_list()
    except Exception as e:
        ops["list"] = {"passed": False, "error": str(e)}

    # ── 3. create ─────────────────────────────────────────────────────────────
    try:
        create_result = op_create(run_tag)
        ops["create"] = create_result
        if create_result.get("passed"):
            new_image_uuid = create_result.get("new_image_uuid", "")
    except Exception as e:
        ops["create"] = {"passed": False, "error": str(e)}

    # ── 4. delete ─────────────────────────────────────────────────────────────
    if new_image_uuid:
        try:
            ops["delete"] = op_delete(new_image_uuid)
        except Exception as e:
            ops["delete"] = {"passed": False, "error": str(e)}
    else:
        ops["delete"] = {"passed": False, "error": "Skipped — create did not succeed"}

    result["operations"] = ops
    result["success"] = all(op.get("passed") for op in ops.values())

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
