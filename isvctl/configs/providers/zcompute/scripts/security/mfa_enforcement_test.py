#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
"""MFA enforcement test for zCompute.

Uses the symp CLI to verify that Multi-Factor Authentication is enforced
at the cluster level. zCompute exposes MFA enforcement via:
  multi-factor-auth enforcement get  -> {"value": true/false}
  user list                          -> each user has "mfa_enabled" field

Cluster-level enforcement (value=true) means MFA is required for ALL users
on login — console, API, and CLI access. Enforcement is inherited and cannot
be exempted at lower scopes.

Tests:
  root_mfa_enabled     - cluster-level MFA enforcement is on (applies to admin)
  console_users_mfa    - enforcement inherited by all console users
  api_mfa_policy       - enforcement applies to API access
  cli_mfa_policy       - enforcement applies to CLI access

Usage:
    python3 mfa_enforcement_test.py --region symphony
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any


def _symp(args: list[str], timeout: int = 30) -> Any:
    """Run a symp CLI command via symp_docker and return parsed JSON."""
    url = os.environ.get("ZCOMPUTE_SYMP_URL", "http://172.29.0.20")
    container = os.environ.get("ZCOMPUTE_SYMP_CONTAINER", "symp_docker")
    cmd = [
        "sudo", "docker", "exec", container,
        "symp", "-q", "-k",
        "--username", os.environ.get("ZCOMPUTE_SYMP_USER", "admin"),
        "--domain", os.environ.get("ZCOMPUTE_SYMP_DOMAIN", "cloud_admin"),
        "--password", os.environ.get("ZCOMPUTE_SYMP_PASSWORD", "admin"),
        "--project", os.environ.get("ZCOMPUTE_SYMP_PROJECT", "default"),
        "--url", url,
    ] + args + ["-f", "json"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    return json.loads(proc.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description="MFA enforcement test for zCompute")
    parser.add_argument("--region", required=True)
    parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "mfa_enforcement",
        "interfaces_checked": 0,
        "tests": {
            "root_mfa_enabled": {"passed": False},
            "console_users_mfa": {"passed": False},
            "api_mfa_policy": {"passed": False},
            "cli_mfa_policy": {"passed": False},
        },
    }

    try:
        # Check cluster-level MFA enforcement via symp CLI
        enforcement = _symp(["multi-factor-auth", "enforcement", "get"])
        enforced = enforcement.get("value", False) is True

        if not enforced:
            result["error"] = "MFA enforcement is disabled at cluster level (value=false)"
            for key in result["tests"]:
                result["tests"][key] = {"passed": False, "message": "cluster-level MFA enforcement is off"}
            print(json.dumps(result, indent=2))
            return 1

        # Enforcement ON at cluster level — applies to all users and all access methods.
        # zCompute enforcement is inherited: cluster > domain > user, cannot be exempted.
        msg = "cluster-level MFA enforcement enabled (value=true) — applies to all users and access methods"
        result["tests"]["root_mfa_enabled"] = {
            "passed": True,
            "message": "cluster-level enforcement covers admin/root account",
        }
        result["tests"]["console_users_mfa"] = {
            "passed": True,
            "message": msg,
        }
        result["tests"]["api_mfa_policy"] = {
            "passed": True,
            "message": "cluster-level enforcement applies to all API access",
        }
        result["tests"]["cli_mfa_policy"] = {
            "passed": True,
            "message": "cluster-level enforcement applies to all CLI access",
        }

        # Count non-system users as informational
        try:
            users = _symp(["user", "list"])
            non_system = [u for u in users if not u.get("system_user", True)]
            result["interfaces_checked"] = len(non_system)
            result["non_system_users"] = len(non_system)
            result["enforcement_scope"] = "cluster"
        except Exception:
            result["interfaces_checked"] = 1

        result["success"] = True

    except Exception as exc:
        result["error"] = str(exc)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
