#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
"""TLS certificate rotation test for zCompute.

Connects to ZCOMPUTE_BASE_URL, retrieves the TLS certificate, and inspects:
  - Validity (not before / not after fields parse correctly)
  - Expiry (not_after > now)
  - Rotation cycle (validity period <= 2 years — industry best practice)
  - Auto-renewal hint (validity window is short enough to imply automation)

Tests:
  cert_valid              - certificate parses and is structurally valid
  cert_not_expired        - certificate has not yet expired
  rotation_cycle_acceptable - validity period <= 730 days (2 years)
  cert_auto_renewed       - validity period <= 365 days (implies automated renewal)

Usage:
    python3 cert_rotation_test.py --region symphony
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _get_cert_info(host: str, port: int = 443) -> dict[str, Any]:
    """Connect to host:port and return DER certificate info dict."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    with socket.create_connection((host, port), timeout=10) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ssock:
            cert = ssock.getpeercert()
            der_cert = ssock.getpeercert(binary_form=True)
    return cert


def _parse_ssl_date(s: str) -> datetime:
    """Parse the date string from ssl.getpeercert() into a timezone-aware datetime."""
    # Format: 'Jan  1 00:00:00 2025 GMT'
    return datetime.strptime(s, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description="TLS certificate rotation test for zCompute")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.parse_args()

    base_url = os.environ.get("ZCOMPUTE_BASE_URL", "").rstrip("/")

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "cert_rotation_test",
        "tests": {
            "cert_valid": {"passed": False},
            "cert_not_expired": {"passed": False},
            "rotation_cycle_acceptable": {"passed": False},
            "cert_auto_renewed": {"passed": False},
        },
    }

    if not base_url:
        result["error"] = "ZCOMPUTE_BASE_URL is not set"
        print(json.dumps(result, indent=2))
        return 1

    host = base_url.replace("https://", "").replace("http://", "").split("/")[0].split(":")[0]
    port_str = base_url.replace("https://", "").split("/")[0]
    port = int(port_str.split(":")[1]) if ":" in port_str else 443

    try:
        cert = _get_cert_info(host, port)
    except Exception as exc:
        result["error"] = f"Failed to retrieve TLS certificate from {host}:{port}: {exc}"
        result["tests"]["cert_valid"] = {"passed": False, "message": str(exc)}
        print(json.dumps(result, indent=2))
        return 1

    errors: list[str] = []
    now = datetime.now(tz=timezone.utc)

    # cert_valid: cert dict has the expected fields
    not_before_str = cert.get("notBefore", "")
    not_after_str = cert.get("notAfter", "")
    subject = dict(x[0] for x in cert.get("subject", []))

    if not_before_str and not_after_str:
        result["tests"]["cert_valid"] = {
            "passed": True,
            "not_before": not_before_str,
            "not_after": not_after_str,
            "subject_cn": subject.get("commonName", ""),
        }
    else:
        result["tests"]["cert_valid"] = {"passed": False, "message": "certificate missing notBefore/notAfter fields"}
        errors.append("certificate is structurally invalid")
        print(json.dumps(result, indent=2))
        return 1

    not_before = _parse_ssl_date(not_before_str)
    not_after = _parse_ssl_date(not_after_str)
    validity_days = (not_after - not_before).days
    days_remaining = (not_after - now).days

    # cert_not_expired
    not_expired = not_after > now
    result["tests"]["cert_not_expired"] = {
        "passed": not_expired,
        "days_remaining": days_remaining,
        "not_after": not_after_str,
        "message": (
            f"certificate expires in {days_remaining} days"
            if not_expired
            else f"certificate EXPIRED {abs(days_remaining)} days ago"
        ),
    }
    if not not_expired:
        errors.append(f"certificate expired {abs(days_remaining)} days ago")

    # rotation_cycle_acceptable: <= 730 days (2 years)
    max_validity = 730
    cycle_ok = validity_days <= max_validity
    result["tests"]["rotation_cycle_acceptable"] = {
        "passed": cycle_ok,
        "validity_days": validity_days,
        "max_allowed_days": max_validity,
        "message": (
            f"validity period {validity_days}d is within {max_validity}d maximum"
            if cycle_ok
            else f"validity period {validity_days}d exceeds {max_validity}d maximum"
        ),
    }
    if not cycle_ok:
        errors.append(f"certificate validity period {validity_days}d exceeds {max_validity}d")

    # cert_auto_renewed: <= 365 days implies automated renewal tooling
    auto_renew_threshold = 365
    auto_ok = validity_days <= auto_renew_threshold
    result["tests"]["cert_auto_renewed"] = {
        "passed": auto_ok,
        "validity_days": validity_days,
        "threshold_days": auto_renew_threshold,
        "message": (
            f"validity period {validity_days}d suggests automated renewal"
            if auto_ok
            else f"validity period {validity_days}d exceeds {auto_renew_threshold}d — manual renewal likely"
        ),
    }
    # Auto-renewal is a best-practice check; don't block overall success on it alone
    # but do report it honestly.

    result["success"] = len(errors) == 0
    if errors:
        result["errors"] = errors

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
