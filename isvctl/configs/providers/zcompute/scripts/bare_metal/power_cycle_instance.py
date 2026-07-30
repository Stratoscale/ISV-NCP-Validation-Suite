#!/usr/bin/env python3
"""Power-cycle a zcompute bare-metal instance (hard stop + start).

Unlike reboot (OS-level restart), this performs a full hardware
power-cycle: force-stop the instance, wait for 'stopped', then start it
and wait for recovery. Exercises firmware init, BIOS POST, and a cold OS
boot — validating recovery from complete power loss.

zcompute-specific notes:
  - stop_instances(Force=True) is UNVERIFIED on zcompute as of this
    writing — our API probe only exercised Force=True's read-only
    surface, not a real force-stop against bare-metal hardware (no BM
    instance type existed yet to test against). If the service silently
    ignores the flag, this degrades to an ordinary graceful stop, which
    still exercises most of what InstancePowerCycleCheck cares about.
  - Same GPU-resource-release retry loop as start_instance.py.

Output JSON:
{
    "success": true,
    "platform": "bm",
    "instance_id": "i-xxx",
    "state": "running",
    "public_ip": "172.28.x.x",
    "key_file": "/tmp/isv-bm-test-key.pem",
    "power_cycle_initiated": true,
    "power_was_off": true,
    "ssh_ready": true,
    "recovery_seconds": 900
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
from common.ec2 import load_nvidia_modules, poll_instance_state, wait_for_public_ip  # noqa: E402
from common.ssh_utils import wait_for_ssh  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Power-cycle a zcompute bare-metal instance")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "symphony"))
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--ssh-user", default="ubuntu")
    parser.add_argument(
        "--pre-start-delay", type=int, default=600,
        help="Seconds to wait after power-off before issuing start (default: 600)",
    )
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": args.instance_id,
        "state": "",
        "region": args.region,
        "key_file": args.key_file,
        "ssh_user": args.ssh_user,
        "power_cycle_initiated": False,
        "power_was_off": False,
        "ssh_ready": False,
        "recovery_seconds": None,
    }

    ec2 = get_client("ec2", region=args.region)

    try:
        print("[power-cycle] verifying instance is running before power-cycle ...", file=sys.stderr)
        resp = ec2.describe_instances(InstanceIds=[args.instance_id])
        inst = resp["Reservations"][0]["Instances"][0]
        current_state = inst["State"]["Name"]

        if current_state != "running":
            result["error"] = f"Instance is {current_state}, expected running"
            result["state"] = current_state
            print(json.dumps(result, indent=2))
            return 1

        start_time = time.monotonic()

        print(f"[power-cycle] force-stopping instance {args.instance_id} ...", file=sys.stderr)
        try:
            ec2.stop_instances(InstanceIds=[args.instance_id], Force=True)
        except ClientError as e:
            print(
                f"[power-cycle] Force=True rejected ({e.response.get('Error', {}).get('Code')}), "
                "falling back to a plain stop_instances call",
                file=sys.stderr,
            )
            ec2.stop_instances(InstanceIds=[args.instance_id])
        result["power_cycle_initiated"] = True

        # Bare-metal hardware power-down can take up to ~30 min.
        poll_instance_state(ec2, args.instance_id, ["stopped"], timeout=1800, interval=30)
        result["power_was_off"] = True
        print("[power-cycle] instance stopped (powered off)", file=sys.stderr)

        if args.pre_start_delay > 0:
            print(
                f"[power-cycle] waiting {args.pre_start_delay}s for GPU resource release ...",
                file=sys.stderr,
            )
            time.sleep(args.pre_start_delay)

        # Same bounce-back retry logic as start_instance.py.
        MAX_START_RETRIES = 4
        RESOURCE_WAIT_SECONDS = 300
        final_state = "stopped"

        for attempt in range(MAX_START_RETRIES):
            print(
                f"[power-cycle] starting instance (cold boot, attempt {attempt + 1}/{MAX_START_RETRIES}) ...",
                file=sys.stderr,
            )
            ec2.start_instances(InstanceIds=[args.instance_id])

            final_state = poll_instance_state(
                ec2, args.instance_id, ["running", "stopped"], timeout=2700, interval=30
            )
            if final_state == "running":
                print("[power-cycle] instance is running.", file=sys.stderr)
                break

            if attempt < MAX_START_RETRIES - 1:
                print(
                    f"[power-cycle] instance returned to stopped; waiting "
                    f"{RESOURCE_WAIT_SECONDS}s before retry ...",
                    file=sys.stderr,
                )
                time.sleep(RESOURCE_WAIT_SECONDS)

        result["state"] = final_state

        # Poll for the EIP to reappear post-power-cycle — private_ip is
        # still the actual SSH target, but public_ip presence confirms the
        # network survived (EIP association is what causes NICo to attach
        # a network interface at all — confirmed 2026-07-29).
        resp = ec2.describe_instances(InstanceIds=[args.instance_id])
        inst = resp["Reservations"][0]["Instances"][0]
        result["private_ip"] = inst.get("PrivateIpAddress")

        fresh_ip = inst.get("PublicIpAddress") or wait_for_public_ip(ec2, args.instance_id)
        if fresh_ip and fresh_ip not in ("", "None"):
            result["public_ip"] = fresh_ip
        else:
            result["error"] = "Instance has no public IP after power-cycle (timed out polling)"
            print(json.dumps(result, indent=2))
            return 1

        print("[power-cycle] waiting for SSH to be ready ...", file=sys.stderr)
        ssh_ready = wait_for_ssh(result["private_ip"], args.ssh_user, args.key_file, max_attempts=80, interval=15)
        result["ssh_ready"] = ssh_ready
        result["recovery_seconds"] = int(time.monotonic() - start_time)

        if not ssh_ready:
            result["error"] = "SSH not ready after power-cycle"
            print(json.dumps(result, indent=2))
            return 1

        nvidia_ok = load_nvidia_modules(result["private_ip"], args.ssh_user, args.key_file)
        result["nvidia_modules_loaded"] = nvidia_ok

        result["success"] = final_state == "running"
        print(
            f"[power-cycle] completed successfully! (recovery={result['recovery_seconds']}s)",
            file=sys.stderr,
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
