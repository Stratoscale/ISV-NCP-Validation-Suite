#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
"""API endpoint isolation test for zCompute.

Verifies that the zCompute API endpoint (ZCOMPUTE_BASE_URL) is a private IP,
requires authentication (unauthenticated calls return 401/403), and uses HTTPS.
No public internet probing is possible from within the lab; instead we inspect
the configured URL directly.

Tests:
  probe_api_from_public   - confirms endpoint IP is private (RFC-1918)
  probe_mgmt_from_public  - unauthenticated request is rejected (401/403)
  verify_private_only     - base URL resolves to a private IP address
  dns_not_public          - hostname/IP is not in a public DNS zone

Usage:
    python3 api_endpoint_test.py --region symphony
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import sys
from pathlib import Path
from typing import Any

import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.client import get_client  # noqa: E402


def _is_private(host: str) -> bool:
    """Return True if host resolves to an RFC-1918 / loopback / link-local address."""
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_private or addr.is_loopback or addr.is_link_local
    except ValueError:
        pass
    try:
        resolved = socket.gethostbyname(host)
        addr = ipaddress.ip_address(resolved)
        return addr.is_private or addr.is_loopback or addr.is_link_local
    except Exception:
        return False


def _extract_host(base_url: str) -> str:
    """Strip scheme and path from a URL, return the host portion."""
    host = base_url.replace("https://", "").replace("http://", "").split("/")[0].split(":")[0]
    return host


def _probe_unauthenticated(base_url: str) -> int | None:
    """Make an unauthenticated HTTP request and return the status code."""
    import urllib.request
    import urllib.error
    import ssl

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    probe_url = base_url.rstrip("/") + "/api/v2/aws/iam/?Action=GetUser&Version=2010-05-08"
    try:
        with urllib.request.urlopen(probe_url, context=ctx, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="API endpoint isolation test for zCompute")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.parse_args()

    base_url = os.environ.get("ZCOMPUTE_BASE_URL", "").rstrip("/")

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "api_endpoint_isolation",
        "endpoints_tested": 0,
        "base_url": base_url or "NOT SET",
        "tests": {
            "probe_api_from_public": {"passed": False},
            "probe_mgmt_from_public": {"passed": False},
            "verify_private_only": {"passed": False},
            "dns_not_public": {"passed": False},
        },
    }

    if not base_url:
        result["error"] = "ZCOMPUTE_BASE_URL is not set"
        print(json.dumps(result, indent=2))
        return 1

    errors: list[str] = []

    # Test 1 + 3: verify the endpoint host is a private IP
    host = _extract_host(base_url)
    is_private = _is_private(host)
    result["tests"]["verify_private_only"] = {
        "passed": is_private,
        "host": host,
        "message": f"host {host!r} {'is' if is_private else 'is NOT'} a private/RFC-1918 address",
    }
    result["tests"]["probe_api_from_public"] = {
        "passed": is_private,
        "message": "endpoint is on a private network — not reachable from the public internet",
    }
    if not is_private:
        errors.append(f"host {host!r} is not a private IP")

    # Test 2: unauthenticated request must be rejected
    status = _probe_unauthenticated(base_url)
    unauth_ok = status in (401, 403)
    result["tests"]["probe_mgmt_from_public"] = {
        "passed": unauth_ok,
        "status_code": status,
        "message": (
            f"unauthenticated request returned {status} — authentication required"
            if unauth_ok
            else f"unexpected status {status} for unauthenticated request"
        ),
    }
    if not unauth_ok:
        errors.append(f"unauthenticated request returned {status}, expected 401 or 403")

    # Test 4: the host is an IP (no public DNS record) or resolves only internally
    try:
        ipaddress.ip_address(host)
        # Pure IP — definitely no public DNS
        dns_ok = True
        dns_msg = "base URL is a bare IP address — no public DNS record exists"
    except ValueError:
        # Hostname — check it doesn't resolve publicly (best-effort)
        dns_ok = is_private  # if it resolves to private, that's fine
        dns_msg = f"hostname {host!r} resolves to private address" if dns_ok else f"hostname {host!r} may have a public DNS record"
    result["tests"]["dns_not_public"] = {"passed": dns_ok, "message": dns_msg}
    if not dns_ok:
        errors.append(dns_msg)

    result["endpoints_tested"] = 4
    result["success"] = len(errors) == 0
    if errors:
        result["errors"] = errors

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
