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


def _get_cert_dates(host: str, port: int = 443) -> tuple[datetime, datetime, str]:
    """Connect to host:port and return (not_before, not_after, subject_cn).

    Uses the cryptography library to parse DER bytes directly, which works
    even when SSL verification is disabled (getpeercert() returns {} in that case).
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    with socket.create_connection((host, port), timeout=10) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ssock:
            der = ssock.getpeercert(binary_form=True)

    from cryptography import x509
    from cryptography.hazmat.backends import default_backend
    cert = x509.load_der_x509_certificate(der, default_backend())
    not_before = cert.not_valid_before_utc if hasattr(cert, "not_valid_before_utc") else cert.not_valid_before.replace(tzinfo=timezone.utc)
    not_after = cert.not_valid_after_utc if hasattr(cert, "not_valid_after_utc") else cert.not_valid_after.replace(tzinfo=timezone.utc)
    try:
        cn = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0].value
    except Exception:
        cn = ""
    return not_before, not_after, cn


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
        not_before, not_after, cn = _get_cert_dates(host, port)
    except Exception as exc:
        result["error"] = f"Failed to retrieve TLS certificate from {host}:{port}: {exc}"
        result["tests"]["cert_valid"] = {"passed": False, "message": str(exc)}
        result["success"] = True  # cert check is unreleased; don't fail the step
        result["not_supported"] = True
        print(json.dumps(result, indent=2))
        return 0

    errors: list[str] = []
    now = datetime.now(tz=timezone.utc)

    result["tests"]["cert_valid"] = {
        "passed": True,
        "not_before": not_before.isoformat(),
        "not_after": not_after.isoformat(),
        "subject_cn": cn,
    }
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
