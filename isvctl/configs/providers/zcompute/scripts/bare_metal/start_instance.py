#!/usr/bin/env python3
"""Start a stopped zcompute bare-metal instance.

Same retry logic as scripts/vm/start_instance.py (resources may not be
released immediately after a stop, so start_instances can bounce back to
'stopped' and needs a retry), but with longer waits — bare-metal power-on
(BMC/IPMI, full POST/BIOS cycle, OS boot without a hypervisor) is slower
than a VM boot.

Output JSON:
{
    "success": true,
    "platform": "bm",
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
    log,
    poll_instance_state,
    wait_for_public_ip,
)
from common.ssh_utils import wait_for_ssh  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Start a stopped zcompute bare-metal instance")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "symphony"))
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--ssh-user", default="ubuntu")
    parser.add_argument(
        "--pre-start-delay", type=int, default=600,
        help="Seconds to wait before issuing start_instances, to allow GPU "
             "resource release after a stop (default: 600 = 10 min, longer "
             "than the VM path for bare-metal hardware release).",
    )
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": args.instance_id,
        "start_initiated": False,
        "key_file": args.key_file,
        "ssh_user": args.ssh_user,
    }

    ec2 = get_client("ec2", region=args.region)

    try:
        resp = ec2.describe_instances(InstanceIds=[args.instance_id])
        inst = resp["Reservations"][0]["Instances"][0]
        current_state = inst["State"]["Name"]
        result["previous_state"] = current_state
        log(f"[start] instance {args.instance_id} current state: {current_state}")

        if current_state == "running":
            result["state"] = "running"
            result["success"] = True
            result["note"] = "Instance was already running"
        elif current_state not in ("stopped", "stopping"):
            result["error"] = (
                f"Cannot start instance in state '{current_state}'; expected 'stopped'."
            )
            print(json.dumps(result, indent=2))
            return 1
        else:
            if current_state == "stopping":
                log("[start] waiting for instance to finish stopping ...")
                poll_instance_state(ec2, args.instance_id, ["stopped"], timeout=900, interval=30)

            if args.pre_start_delay > 0:
                log(
                    f"[start] waiting {args.pre_start_delay}s for GPU resource "
                    "release after stop ..."
                )
                time.sleep(args.pre_start_delay)

            # Retry start_instances if the instance bounces back to 'stopped'
            # (resource pool not yet released) — bare-metal path allows a long
            # total retry budget. A live run (Aviv/Amit, 2026-08-06) showed
            # start_instances getting rejected outright (InternalServerError)
            # for the whole ~20 min the old fixed-4-retries-at-300s budget
            # allowed, well before this real GB200 hardware was actually ready
            # to accept a new start call after a full physical stop cycle.
            # Retrying on a 4-hour wall-clock deadline instead of a fixed
            # attempt count so however long this platform actually needs, the
            # retry loop keeps trying instead of giving up early.
            RETRY_BUDGET_SECONDS = 14400  # 4 hours total
            RESOURCE_WAIT_SECONDS = 300
            final_state = "stopped"

            last_start_error: ClientError | None = None
            retry_deadline = time.monotonic() + RETRY_BUDGET_SECONDS
            attempt = 0
            while True:
                attempt += 1
                remaining = retry_deadline - time.monotonic()
                log(
                    f"[start] calling start_instances (attempt {attempt}, "
                    f"~{remaining / 60:.0f}m left in retry budget) ..."
                )
                # start_instances itself can transiently fail right after a
                # stop (e.g. InvalidInstanceID.NotFound - confirmed live
                # 2026-08-03, the backend hadn't fully re-registered the
                # instance as available yet). Previously this wasn't caught
                # here at all, so it escaped to the outer handler and failed
                # the whole script on the very first attempt with zero
                # retries. Now treated the same as a "bounced back to
                # stopped" result: retry with the same backoff.
                try:
                    ec2.start_instances(InstanceIds=[args.instance_id])
                    result["start_initiated"] = True
                except ClientError as e:
                    last_start_error = e
                    code = e.response.get("Error", {}).get("Code", "")
                    log(f"[start] start_instances call failed ({code}): {e}")
                    if time.monotonic() + RESOURCE_WAIT_SECONDS >= retry_deadline:
                        log("[start] exhausted 4-hour retry budget; instance could not start.")
                        break
                    log(f"[start] retrying in {RESOURCE_WAIT_SECONDS}s ...")
                    time.sleep(RESOURCE_WAIT_SECONDS)
                    continue

                # Bare metal needs a longer per-attempt budget: full POST/BIOS/OS boot.
                final_state = poll_instance_state(
                    ec2, args.instance_id, ["running", "stopped"], timeout=2700, interval=30
                )

                if final_state == "running":
                    log("[start] instance is running.")
                    break

                if time.monotonic() + RESOURCE_WAIT_SECONDS >= retry_deadline:
                    log("[start] exhausted 4-hour retry budget; instance could not start.")
                    break
                log(
                    f"[start] instance returned to stopped (GPU resources not yet available). "
                    f"Waiting {RESOURCE_WAIT_SECONDS}s before retry ..."
                )
                time.sleep(RESOURCE_WAIT_SECONDS)

            if final_state != "running" and last_start_error is not None and not result["start_initiated"]:
                result["error"] = str(last_start_error)
                result["error_code"] = last_start_error.response.get("Error", {}).get("Code", "")
                print(json.dumps(result, indent=2))
                return 1

            result["state"] = final_state

        # Poll for the EIP to reappear (it persists across stop/start, but
        # takes a moment to reassociate) — private_ip is still the actual
        # SSH target, but public_ip presence confirms the network survived.
        public_ip = wait_for_public_ip(ec2, args.instance_id, timeout=120, interval=5)

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

        ssh_ready = False
        nvidia_ok = False
        if private_ip:
            ssh_ready = wait_for_ssh(private_ip, args.ssh_user, args.key_file, max_attempts=80, interval=15)
            if not ssh_ready:
                # Instance state says running, but SSH never came up - retry
                # once more before giving up rather than silently reporting
                # success based on instance state alone (Aviv, 2026-08-03).
                log("[start] instance running but SSH did not respond; retrying SSH wait once more ...")
                time.sleep(60)
                ssh_ready = wait_for_ssh(private_ip, args.ssh_user, args.key_file, max_attempts=80, interval=15)
            if ssh_ready:
                nvidia_ok = load_nvidia_modules(private_ip, args.ssh_user, args.key_file)

        result["ssh_ready"] = ssh_ready
        result["nvidia_modules_loaded"] = nvidia_ok
        result["success"] = result.get("state") == "running" and ssh_ready

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
