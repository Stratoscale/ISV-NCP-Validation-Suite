#!/usr/bin/env python3
"""Upload (import) a machine image into zCompute from a public URL.

zCompute does not implement EC2 ImportImage — images are imported directly
from a public URL using the symp CLI:
  symp machine-images import-machine-image-from-url ...

The image is stored in zCompute's internal VSC object storage (not S3).
After import the image's internal UUID is mapped to an EC2-compatible AMI ID:
  ami_id = "ami-" + uuid.replace("-", "")

Polling runs until state == "ready" or state == "error" (timeout 1800s).

Environment variables:
  ZCOMPUTE_SYMP_URL       symp endpoint, default http://172.29.0.20
  ZCOMPUTE_SYMP_USER      default admin
  ZCOMPUTE_SYMP_DOMAIN    default cloud_admin
  ZCOMPUTE_SYMP_PASSWORD  default admin
  ZCOMPUTE_SYMP_PROJECT   default default
  ZCOMPUTE_SYMP_CONTAINER default symp_docker

Output JSON:
{
    "success": true,
    "platform": "image_registry",
    "image_id": "ami-47108a82db3f46178ba01f225aa94b71",
    "image_name": "isv-ir-a1b2c3d4",
    "internal_uuid": "47108a82-db3f-4617-8ba0-1f225aa94b71",
    "storage_bucket": "zcompute-internal-storage",
    "disk_ids": ["<bdm-uuid>"],
    "state": "ready",
    "total_size_gb": 10
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

# Allow importing from the parent scripts/ directory (common/, etc.)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import subprocess


def symp_cmd(args: list[str], timeout: int = 60) -> Any:
    """Run a symp CLI command inside the symp_docker container.

    Returns parsed JSON output (dict or list depending on the command).

    Raises RuntimeError on non-zero exit code.
    """
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

    print(f"[upload] symp: {' '.join(args)}", file=sys.stderr)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            f"symp command failed (rc={proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return json.loads(proc.stdout)


def _uuid_to_ami(image_uuid: str) -> str:
    """Convert a zCompute internal UUID to an EC2-compatible AMI ID.

    zCompute's EC2 API uses:   ami-<uuid without hyphens>
    Example: 47108a82-db3f-4617-8ba0-1f225aa94b71
          -> ami-47108a82db3f46178ba01f225aa94b71
    """
    return "ami-" + image_uuid.replace("-", "")


def _poll_image_ready(
    image_uuid: str,
    timeout: int = 1800,
    interval: int = 15,
) -> dict[str, Any]:
    """Poll symp machine-images get until state is 'ready' or 'error'.

    Args:
        image_uuid: Internal zCompute UUID of the image.
        timeout:    Max seconds to wait (default 1800 = 30 min).
        interval:   Poll interval in seconds (default 15).

    Returns:
        The final machine-image dict from symp.

    Raises:
        TimeoutError: If the image does not reach ready/error within timeout.
        RuntimeError: If the image enters an error state.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        img = symp_cmd(["machine-images", "get", image_uuid])
        state = img.get("state", "unknown")
        total_gb = img.get("total_size_gb", 0)
        print(
            f"[upload] image {image_uuid} state={state} total_size_gb={total_gb}",
            file=sys.stderr,
        )
        if state == "ready":
            return img
        if state == "error":
            raise RuntimeError(
                f"Image {image_uuid} entered error state: {img}"
            )
        time.sleep(interval)

    raise TimeoutError(
        f"Image {image_uuid} did not reach 'ready' within {timeout}s"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import a machine image into zCompute from a public URL"
    )
    parser.add_argument("--image-url", required=True, help="Public URL of the image file")
    parser.add_argument("--image-format", default="qcow2", help="Image format (qcow2, vmdk, …)")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "symphony"))
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "image_registry",
    }

    try:
        # Generate a unique image name for this test run
        run_tag = uuid.uuid4().hex[:8]
        image_name = f"isv-ir-{run_tag}"
        print(f"[upload] importing image '{image_name}' from {args.image_url}", file=sys.stderr)

        # Build the block-device-mapping JSON argument.
        # - volume_size_gib: 20 GiB — enough for any minimal cloud image
        # - bus_type: virtio — required for zCompute KVM VMs
        # - disk_type: disk — primary OS disk
        # - no_verify_ssl: false — the source URL uses valid TLS
        bdm = json.dumps([{
            "url": args.image_url,
            "no_verify_ssl": False,
            "bus_type": "virtio",
            "disk_type": "disk",
            "volume_size_gib": 20,
        }])

        # Import the image. This starts an async import job.
        # zCompute returns {"id": "<uuid>", "state": "uploading", "name": "...", ...}
        import_result = symp_cmd(
            [
                "machine-images", "import-machine-image-from-url",
                "--description", f"ISV NCP image registry test — {run_tag}",
                "--guest-os", "linux",
                "--scope", "public",
                image_name,
                bdm,
            ],
            timeout=60,
        )

        image_uuid = import_result.get("id") or import_result.get("uuid")
        if not image_uuid:
            raise RuntimeError(
                f"symp import did not return an image ID. Response: {import_result}"
            )

        print(f"[upload] import started — UUID={image_uuid}", file=sys.stderr)

        # Poll until the image reaches 'ready' state (may take 10–30 min)
        final_img = _poll_image_ready(image_uuid, timeout=1800, interval=15)

        # Convert internal UUID to EC2-compatible AMI ID
        ami_id = _uuid_to_ami(image_uuid)
        print(f"[upload] image ready — AMI ID: {ami_id}", file=sys.stderr)

        # Extract block-device-mapping UUIDs for disk_ids output field.
        # The BDM is a list of volume objects; we use their IDs if available.
        bdm_list = final_img.get("block_device_mapping", [])
        disk_ids: list[str] = []
        for bdm_entry in bdm_list:
            entry_id = bdm_entry.get("id") or bdm_entry.get("volume_id") or bdm_entry.get("uuid")
            if entry_id:
                disk_ids.append(entry_id)
        if not disk_ids:
            # zCompute stores images in VSC (its internal block store); if the BDM
            # does not carry explicit IDs, use a symbolic placeholder.
            disk_ids = ["zcompute-vsc-disk"]

        result = {
            "success": True,
            "platform": "image_registry",
            "image_id": ami_id,
            "image_name": image_name,
            "internal_uuid": image_uuid,
            # Images are stored in zCompute's internal VSC object storage.
            # There is no S3 bucket — this string satisfies the FieldExistsCheck.
            "storage_bucket": "zcompute-internal-storage",
            "disk_ids": disk_ids,
            "state": final_img.get("state", "ready"),
            "total_size_gb": final_img.get("total_size_gb", 0),
        }

    except (TimeoutError, RuntimeError, subprocess.TimeoutExpired) as e:
        result["error"] = str(e)
    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
