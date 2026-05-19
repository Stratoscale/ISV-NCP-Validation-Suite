#!/usr/bin/env python3
"""Stop a running zcompute VM instance.

zcompute-specific notes:
  - No boto3 waiters — uses custom polling.
  - Verifies instance is running before issuing stop.

Output JSON:
{
    "success": true,
    "platform": "vm",
    "instance_id": "i-xxx",
    "previous_state": "running",
    "state": "stopped"
}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.client import get_client  # noqa: E402
from common.ec2 import poll_instance_state  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Stop a zcompute VM instance")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "symphony"))
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "instance_id": args.instance_id,
        "stop_initiated": False,
    }

    ec2 = get_client("ec2", region=args.region)

    try:
        # Verify current state.
        resp = ec2.describe_instances(InstanceIds=[args.instance_id])
        inst = resp["Reservations"][0]["Instances"][0]
        current_state = inst["State"]["Name"]
        result["previous_state"] = current_state
        print(
            f"[stop] instance {args.instance_id} current state: {current_state}",
            file=sys.stderr,
        )

        if current_state == "stopped":
            result["state"] = "stopped"
            result["stop_initiated"] = True
            result["success"] = True
            result["note"] = "Instance was already stopped"
            print(json.dumps(result, indent=2))
            return 0

        if current_state not in ("running", "pending"):
            result["error"] = (
                f"Cannot stop instance in state '{current_state}'; "
                "expected 'running' or 'pending'."
            )
            print(json.dumps(result, indent=2))
            return 1

        # Issue the stop.
        print(f"[stop] stopping instance {args.instance_id} ...", file=sys.stderr)
        ec2.stop_instances(InstanceIds=[args.instance_id])
        result["stop_initiated"] = True

        # Poll until stopped.
        final_state = poll_instance_state(
            ec2, args.instance_id, ["stopped"], timeout=600, interval=15
        )

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
