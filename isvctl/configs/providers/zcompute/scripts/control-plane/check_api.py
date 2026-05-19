#!/usr/bin/env python3
"""Check zcompute API connectivity and health.

Tests that the zcompute EC2-compatible API endpoints are reachable and
responding correctly using the configured ZCOMPUTE_ENDPOINT.

Usage:
    python check_api.py --region us-east-1 --services ec2,iam,sts

Environment:
    ZCOMPUTE_ENDPOINT   - required, e.g. https://api.yourzone.zadarastorage.com
    AWS_ACCESS_KEY_ID   - zcompute access key
    AWS_SECRET_ACCESS_KEY - zcompute secret key
    AWS_REGION          - zcompute region

Output JSON:
{
    "success": true,
    "platform": "control_plane",
    "region": "us-east-1",
    "endpoint": "https://api.yourzone.zadarastorage.com",
    "account_id": "123456",
    "tests": {
        "sts_identity": {"passed": true, "latency_ms": 123},
        "ec2_api":      {"passed": true, "latency_ms": 89},
        "iam_api":      {"passed": true, "latency_ms": 56}
    },
    "summary": "3/3 services reachable"
}

Compatibility notes:
    - ec2:  describe_regions - supported in zcompute
    - iam:  list_users       - supported in zcompute
    - sts:  get_caller_identity - supported in zcompute
    - s3:   list_buckets     - zcompute S3 may use a separate endpoint;
                               test separately with ZCOMPUTE_S3_ENDPOINT
    - eks, lambda, rds, dynamodb: NOT supported in zcompute; do not include
"""

import argparse
import json
import os
import sys
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.client import get_client, get_session_client  # noqa: E402


# Services supported by zcompute's AWS-compatible API.
# If you add a service here, also add a probe call in _probe_service() below.
SUPPORTED_SERVICES = {"ec2", "iam", "sts"}


def _probe_service(session: Any, service: str, region: str) -> dict[str, Any]:
    """Run a lightweight read-only probe against a single service.

    Returns a dict with at least {"passed": bool}. On success, also
    includes "latency_ms". On failure, includes "error".
    """
    result: dict[str, Any] = {"passed": False}
    start = time.monotonic()

    try:
        client = get_session_client(session, service, region)

        if service == "ec2":
            # describe_regions is a fast, permission-light probe.
            # zcompute returns its own region list here.
            client.describe_regions(RegionNames=[region])
        elif service == "iam":
            client.list_users(MaxItems=1)
        elif service == "sts":
            client.get_caller_identity()
        else:
            result["error"] = f"No probe defined for service '{service}'"
            return result

        result["passed"] = True
        result["latency_ms"] = round((time.monotonic() - start) * 1000, 2)

    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        # Access denied means the endpoint IS reachable - auth error is fine.
        if code in ("AccessDenied", "AccessDeniedException", "UnauthorizedAccess"):
            result["passed"] = True
            result["latency_ms"] = round((time.monotonic() - start) * 1000, 2)
            result["note"] = f"API reachable (permission denied: {code})"
        else:
            result["error"] = f"{code}: {e.response['Error'].get('Message', str(e))}"

    except EndpointConnectionError as e:
        result["error"] = f"Cannot connect to endpoint: {e}"
    except NoCredentialsError:
        result["error"] = "No credentials configured"
    except Exception as e:
        result["error"] = str(e)

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Check zcompute API health")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument(
        "--services",
        default="ec2,iam,sts",
        help="Comma-separated list of services to probe (supported: ec2, iam, sts)",
    )
    args = parser.parse_args()

    services = [s.strip() for s in args.services.split(",") if s.strip()]
    endpoint = os.environ.get("ZCOMPUTE_ENDPOINT") or os.environ.get("AWS_ENDPOINT_URL", "")

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "region": args.region,
        "endpoint": endpoint or "(not set - will target real AWS)",
        "tests": {},
    }

    session = boto3.Session()

    # Always probe STS first - it gives us account identity and is required
    # for the suite to consider the control plane healthy.
    sts_result = _probe_service(session, "sts", args.region)
    result["tests"]["sts_identity"] = sts_result

    if sts_result["passed"]:
        try:
            sts = get_client("sts", region=args.region)
            identity = sts.get_caller_identity()
            result["account_id"] = identity.get("Account", "unknown")
            result["arn"] = identity.get("Arn", "unknown")
        except Exception as e:
            result["identity_error"] = str(e)

    # Probe the remaining requested services
    for service in services:
        if service == "sts":
            continue  # already done above
        if service not in SUPPORTED_SERVICES:
            result["tests"][f"{service}_api"] = {
                "passed": False,
                "error": f"Service '{service}' is not supported by zcompute's AWS-compatible API.",
            }
            continue
        result["tests"][f"{service}_api"] = _probe_service(session, service, args.region)

    passed = sum(1 for t in result["tests"].values() if t.get("passed"))
    total = len(result["tests"])
    result["summary"] = f"{passed}/{total} services reachable"

    # Success requires STS to pass (it gates everything else)
    result["success"] = sts_result.get("passed", False)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
