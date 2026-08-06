#!/usr/bin/env python3
"""Stop a running zcompute bare-metal instance.

Same as scripts/vm/stop_instance.py, but with a longer poll timeout —
bare-metal hardware power-down (BMC/IPMI) is slower than a VM stop.

Output JSON:
{
    "success": true,
    "platform": "bm",
    "instance_id": "i-xxx",
    "previous_state": "running",
    "state": "stopped",
    "time_to_stopped_seconds": 842.3
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
from common.ec2 import log, poll_instance_state  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Stop a zcompute bare-metal instance")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "symphony"))
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": args.instance_id,
        "stop_initiated": False,
        "time_to_stopped_seconds": None,
    }

    ec2 = get_client("ec2", region=args.region)

    try:
        resp = ec2.describe_instances(InstanceIds=[args.instance_id])
        inst = resp["Reservations"][0]["Instances"][0]
        current_state = inst["State"]["Name"]
        result["previous_state"] = current_state
        log(f"[stop] instance {args.instance_id} current state: {current_state}")

        if current_state == "stopped":
            result["state"] = "stopped"
            result["stop_initiated"] = True
            result["success"] = True
            result["time_to_stopped_seconds"] = 0
            result["note"] = "Instance was already stopped"
            print(json.dumps(result, indent=2))
            return 0

        if current_state not in ("running", "pending"):
            result["error"] = (
                f"Cannot stop instance in state '{current_state}'; expected 'running' or 'pending'."
            )
            print(json.dumps(result, indent=2))
            return 1

        stop_start = time.monotonic()
        log(f"[stop] stopping instance {args.instance_id} ...")
        ec2.stop_instances(InstanceIds=[args.instance_id])
        result["stop_initiated"] = True

        # Bare-metal hardware power-down (BMC/IPMI) can take far longer than
        # the ~30 min originally assumed here - bumped generously (Aviv,
        # 2026-08-06: "be very kind with your timeouts, bump it up").
        final_state = poll_instance_state(
            ec2, args.instance_id, ["stopped"], timeout=7200, interval=30
        )

        time_to_stopped = round(time.monotonic() - stop_start, 1)
        result["time_to_stopped_seconds"] = time_to_stopped
        log(f"[stop] instance reached '{final_state}' state {time_to_stopped}s after stop was issued")

        result["state"] = final_state
        result["success"] = final_state == "stopped"

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
