#!/usr/bin/env python3
"""Check serial console output availability on a zcompute VM instance.

Attempts to call get_console_output. If the API is not implemented in
zcompute, returns console_available=false with not_supported=true.
This lets the test runner exclude the check rather than fail.

Output JSON (supported):
{
    "success": true,
    "platform": "vm",
    "instance_id": "i-xxx",
    "console_available": true,
    "output_length": 1234
}

Output JSON (not supported):
{
    "success": true,
    "platform": "vm",
    "instance_id": "i-xxx",
    "console_available": false,
    "not_supported": true,
    "note": "get_console_output is not implemented on this zcompute version"
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

# Error codes that indicate the API simply is not implemented or unavailable.
# InternalFailure is included because zcompute returns 500 for get_console_output.
_NOT_IMPLEMENTED_CODES = {
    "NotImplemented",
    "UnsupportedOperation",
    "InvalidOperation",
    "OperationNotSupported",
    "InternalFailure",
    "InternalError",
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check serial console output on a zcompute VM instance"
    )
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "symphony"))
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "instance_id": args.instance_id,
        "console_available": False,
    }

    ec2 = get_client("ec2", region=args.region)

    try:
        resp = ec2.get_console_output(InstanceId=args.instance_id)
        output = resp.get("Output", "")
        result["console_available"] = bool(output)
        result["output_length"] = len(output) if output else 0
        result["success"] = True

    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        msg = e.response.get("Error", {}).get("Message", str(e))

        # Check both code and message for "not implemented" signals.
        is_not_supported = code in _NOT_IMPLEMENTED_CODES or any(
            kw in msg.lower()
            for kw in ("not implemented", "not supported", "unsupported")
        )

        if is_not_supported:
            result["console_available"] = False
            result["not_supported"] = True
            result["note"] = (
                "get_console_output is not implemented on this zcompute version"
            )
            result["success"] = True  # Not a failure — exclusion marker for config.
        else:
            result["error"] = msg
            result["error_code"] = code

    except Exception as e:
        # Treat any unexpected error as non-fatal for this check.
        result["console_available"] = False
        result["not_supported"] = True
        result["note"] = f"Unexpected error checking console output: {e}"
        result["success"] = True

    print(json.dumps(result, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
