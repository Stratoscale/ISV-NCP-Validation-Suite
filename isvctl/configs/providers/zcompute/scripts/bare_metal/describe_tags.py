#!/usr/bin/env python3
"""Describe tags on a zcompute bare-metal instance.

Same as scripts/vm/describe_tags.py, with platform="bm". Uses the
DescribeTags API directly (confirmed working by the VM suite's
InstanceTagCheck), not the DescribeInstances-tags fallback AWS's own
bare-metal script uses.

Output JSON:
{
    "success": true,
    "platform": "bm",
    "instance_id": "i-xxx",
    "tags": {
        "Name": "isv-bm-test-gpu",
        "CreatedBy": "isvtest"
    },
    "tag_count": 2
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Describe tags on a zcompute bare-metal instance")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "symphony"))
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": args.instance_id,
        "tags": {},
        "tag_count": 0,
    }

    ec2 = get_client("ec2", region=args.region)

    try:
        resp = ec2.describe_tags(
            Filters=[
                {"Name": "resource-id", "Values": [args.instance_id]},
                {"Name": "resource-type", "Values": ["instance"]},
            ]
        )

        tags: dict[str, str] = {}
        for tag in resp.get("Tags", []):
            tags[tag["Key"]] = tag["Value"]

        result["tags"] = tags
        result["tag_count"] = len(tags)
        result["success"] = True

    except ClientError as e:
        result["error"] = str(e)
        result["error_code"] = e.response.get("Error", {}).get("Code", "")
    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
