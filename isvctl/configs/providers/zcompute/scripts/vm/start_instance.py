#!/usr/bin/env python3
"""Start a stopped zcompute VM instance.

zcompute-specific notes:
  - No boto3 waiters — uses custom polling.
  - StartInstances goes: stopped -> pending -> stopped -> pending -> running.
    The intermediate 'stopped' must not break polling; poll_instance_state
    handles this by only stopping when the target state is reached.
  - Needs up to 5+ min timeout for the full transition.
  - Public IP must be re-fetched after start; it may have changed.
  - NVIDIA modules must be reloaded after every boot.

Output JSON:
{
    "success": true,
    "platform": "vm",
    "instance_id": "i-xxx",
    "previous_state": "stopped",
    "state": "running",
    "public_ip": "172.28.x.x",
    "private_ip": "172.31.x.x",
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
from common.ec2 import (  # noqa: E402
    load_nvidia_modules,
    poll_instance_state,
    wait_for_public_ip,
)
from common.ssh_utils import wait_for_ssh  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Start a stopped zcompute VM instance")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "symphony"))
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--ssh-user", default="ubuntu")
    parser.add_argument(
        "--pre-start-delay", type=int, default=300,
        help="Seconds to wait before issuing start_instances, to allow GPU "
             "resource release after a stop (default: 300 = 5 min).",
    )
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "instance_id": args.instance_id,
        "start_initiated": False,
        "key_file": args.key_file,
        "ssh_user": args.ssh_user,
    }

    ec2 = get_client("ec2", region=args.region)

    try:
        # Verify current state.
        resp = ec2.describe_instances(InstanceIds=[args.instance_id])
        inst = resp["Reservations"][0]["Instances"][0]
        current_state = inst["State"]["Name"]
        result["previous_state"] = current_state
        print(
            f"[start] instance {args.instance_id} current state: {current_state}",
            file=sys.stderr,
        )

        if current_state == "running":
            result["state"] = "running"
            result["success"] = True
            result["note"] = "Instance was already running"
            # Still fetch IP and check SSH.
        elif current_state not in ("stopped", "stopping"):
            result["error"] = (
                f"Cannot start instance in state '{current_state}'; "
                "expected 'stopped'."
            )
            print(json.dumps(result, indent=2))
            return 1
        else:
            # If stopping, wait until fully stopped first.
            if current_state == "stopping":
                print("[start] waiting for instance to finish stopping ...", file=sys.stderr)
                poll_instance_state(
                    ec2, args.instance_id, ["stopped"], timeout=300, interval=15
                )

            # Wait for GPU resources to be released before starting.
            # zcompute takes 4-5 min to release GPU resources after a stop.
            # Without this delay, start_instances succeeds but the VM can't
            # boot and returns to stopped state.
            if args.pre_start_delay > 0:
                print(
                    f"[start] waiting {args.pre_start_delay}s for GPU resource "
                    "release after stop ...",
                    file=sys.stderr,
                )
                time.sleep(args.pre_start_delay)

            # Issue the start with retry for GPU resource release.
            # If the VM returns to 'stopped' after being 'pending', the
            # resource pool wasn't ready — wait and retry start_instances.
            MAX_START_RETRIES = 4
            RESOURCE_WAIT_SECONDS = 120  # wait between retries
            final_state = "stopped"

            for attempt in range(MAX_START_RETRIES):
                print(
                    f"[start] calling start_instances (attempt {attempt + 1}/{MAX_START_RETRIES}) ...",
                    file=sys.stderr,
                )
                ec2.start_instances(InstanceIds=[args.instance_id])
                result["start_initiated"] = True

                # Poll until running or back to stopped (resource unavailable).
                # zcompute path: stopped -> pending -> stopped -> pending -> running
                final_state = poll_instance_state(
                    ec2, args.instance_id, ["running", "stopped"], timeout=360, interval=15
                )

                if final_state == "running":
                    print("[start] instance is running.", file=sys.stderr)
                    break

                # VM returned to stopped — resources not yet released.
                if attempt < MAX_START_RETRIES - 1:
                    print(
                        f"[start] instance returned to stopped (GPU resources not yet available). "
                        f"Waiting {RESOURCE_WAIT_SECONDS}s before retry ...",
                        file=sys.stderr,
                    )
                    time.sleep(RESOURCE_WAIT_SECONDS)
                else:
                    print("[start] exhausted retries; instance could not start.", file=sys.stderr)

            result["state"] = final_state

        # Get public IP (may take a moment to appear).
        public_ip = wait_for_public_ip(ec2, args.instance_id, timeout=120, interval=5)

        # Re-fetch full instance details.
        resp = ec2.describe_instances(InstanceIds=[args.instance_id])
        inst = resp["Reservations"][0]["Instances"][0]
        private_ip = inst.get("PrivateIpAddress")

        if not public_ip:
            raw = inst.get("PublicIpAddress")
            if raw and raw not in ("", "None"):
                public_ip = raw

        result["public_ip"] = public_ip
        result["private_ip"] = private_ip
        result["state"] = inst["State"]["Name"]

        # Wait for SSH.
        ssh_ready = False
        nvidia_ok = False
        if public_ip:
            ssh_ready = wait_for_ssh(
                public_ip, args.ssh_user, args.key_file, max_attempts=40, interval=15
            )
            if ssh_ready:
                nvidia_ok = load_nvidia_modules(public_ip, args.ssh_user, args.key_file)

        result["ssh_ready"] = ssh_ready
        result["nvidia_modules_loaded"] = nvidia_ok
        result["success"] = result.get("state") == "running"

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
