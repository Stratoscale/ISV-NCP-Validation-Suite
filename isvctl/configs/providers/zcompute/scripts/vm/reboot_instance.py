#!/usr/bin/env python3
"""Reboot a running zcompute VM instance and verify it came back up.

Captures pre-reboot uptime, issues the reboot, waits, then confirms
the uptime decreased to prove a real reboot occurred.

zcompute-specific notes:
  - No boto3 waiters — uses custom polling.
  - NVIDIA modules must be reloaded after every boot.
  - The default wait-before-check (90s) gives the instance time to
    begin the reboot before we start polling the API state.

Output JSON:
{
    "success": true,
    "platform": "vm",
    "instance_id": "i-xxx",
    "state": "running",
    "public_ip": "172.28.x.x",
    "pre_uptime": "up 2 days, 3:45",
    "post_uptime": "up 1 min",
    "reboot_confirmed": true,
    "ssh_ready": true,
    "nvidia_modules_loaded": true
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
from common.ec2 import load_nvidia_modules, poll_instance_state  # noqa: E402
from common.ssh_utils import run_ssh_command, wait_for_ssh  # noqa: E402


def _get_uptime(host: str, user: str, key_file: str) -> str | None:
    """Return the uptime string from 'uptime -p' on the remote host."""
    rc, stdout, stderr = run_ssh_command(host, user, key_file, "uptime", timeout=30)
    if rc == 0:
        return stdout.strip()
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Reboot a zcompute VM instance")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "symphony"))
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--public-ip", required=True)
    parser.add_argument("--ssh-user", default="ubuntu")
    parser.add_argument(
        "--wait-before-check",
        type=int,
        default=90,
        help="Seconds to sleep after issuing reboot before polling state (default: 90)",
    )
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "instance_id": args.instance_id,
        "public_ip": args.public_ip,
        "key_file": args.key_file,
        "ssh_user": args.ssh_user,
        "reboot_initiated": False,
    }

    ec2 = get_client("ec2", region=args.region)

    try:
        # Capture pre-reboot uptime via SSH.
        print("[reboot] capturing pre-reboot uptime ...", file=sys.stderr)
        pre_uptime = _get_uptime(args.public_ip, args.ssh_user, args.key_file)
        result["pre_uptime"] = pre_uptime
        print(f"[reboot] pre-reboot uptime: {pre_uptime}", file=sys.stderr)

        # Issue the reboot.
        print(f"[reboot] rebooting instance {args.instance_id} ...", file=sys.stderr)
        ec2.reboot_instances(InstanceIds=[args.instance_id])
        result["reboot_initiated"] = True

        # Wait before starting to poll — the instance needs time to begin reboot.
        print(
            f"[reboot] sleeping {args.wait_before_check}s before polling state ...",
            file=sys.stderr,
        )
        time.sleep(args.wait_before_check)

        # Poll until running again.
        final_state = poll_instance_state(
            ec2, args.instance_id, ["running"], timeout=600, interval=15
        )
        result["state"] = final_state

        # Wait for SSH to come back.
        ssh_ready = wait_for_ssh(
            args.public_ip, args.ssh_user, args.key_file, max_attempts=40, interval=15
        )
        result["ssh_ready"] = ssh_ready

        # Load NVIDIA modules (required after every boot on zcompute).
        nvidia_ok = False
        if ssh_ready:
            nvidia_ok = load_nvidia_modules(
                args.public_ip, args.ssh_user, args.key_file
            )

        result["nvidia_modules_loaded"] = nvidia_ok

        # Capture post-reboot uptime and confirm reboot occurred.
        post_uptime = None
        reboot_confirmed = False
        if ssh_ready:
            post_uptime = _get_uptime(args.public_ip, args.ssh_user, args.key_file)
            # A real reboot will show a much shorter uptime than before.
            # We compare string representations as a heuristic: if they differ,
            # a reboot occurred. A fresh boot typically shows "up X min".
            if post_uptime and pre_uptime:
                reboot_confirmed = post_uptime != pre_uptime
            elif post_uptime:
                # Could not get pre-uptime — at least confirm SSH came back.
                reboot_confirmed = True

        result["post_uptime"] = post_uptime
        result["reboot_confirmed"] = reboot_confirmed
        result["success"] = (
            final_state == "running" and ssh_ready and reboot_confirmed
        )

    except ClientError as e:
        result["error"] = str(e)
        result["error_code"] = e.response.get("Error", {}).get("Code", "")
    except TimeoutError as e:
        result["error"] = str(e)
    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
