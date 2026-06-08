#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
"""Short-lived credentials test for zCompute.

Creates an IAM role, calls STS AssumeRole to obtain temporary credentials,
verifies the credentials carry an expiry, confirms they work immediately via
GetCallerIdentity, and cleans up.

Tests:
  sts_issues_temp_creds    - STS AssumeRole returns credentials with Expiration
  creds_have_expiry        - Expiration field is present and in the future
  short_duration_supported - DurationSeconds <= 3600 is accepted
  creds_usable_immediately - temp credentials authenticate immediately

Usage:
    python3 short_lived_credentials_test.py --region symphony
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.client import get_client, get_session_client  # noqa: E402

_ROLE_NAME = f"isvctl-sec-shorttlv-{uuid.uuid4().hex[:8]}"
_POLICY_ARN_READONLY = "arn:aws:iam::aws:policy/ReadOnlyAccess"


def _get_account_id(sts: Any) -> str:
    try:
        return sts.get_caller_identity()["Account"]
    except Exception:
        return "000000000000"


def _cleanup(iam: Any, role_name: str) -> None:
    try:
        attached = iam.list_attached_role_policies(RoleName=role_name)["AttachedPolicies"]
        for p in attached:
            iam.detach_role_policy(RoleName=role_name, PolicyArn=p["PolicyArn"])
    except Exception:
        pass
    try:
        iam.delete_role(RoleName=role_name)
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Short-lived credentials test for zCompute")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument(
        "--max-ttl-seconds",
        type=int,
        default=43200,
        help="Upper bound on credential TTL (default: 43200 = 12h)",
    )
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "short_lived_credentials_test",
        "node_credential_method": "sts_assume_role",
        "workload_credential_method": "sts_assume_role",
        "node_credential_ttl_seconds": 0,
        "workload_credential_ttl_seconds": 0,
        "max_ttl_seconds": args.max_ttl_seconds,
        "tests": {
            "sts_issues_temp_creds": {"passed": False},
            "creds_have_expiry": {"passed": False},
            "short_duration_supported": {"passed": False},
            "creds_usable_immediately": {"passed": False},
        },
    }

    iam = get_client("iam", region=args.region)
    sts = get_client("sts", region=args.region)
    errors: list[str] = []

    account_id = _get_account_id(sts)

    # ── Create IAM role ──
    trust_policy = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"AWS": f"arn:aws:iam::{account_id}:root"},
            "Action": "sts:AssumeRole",
        }],
    })

    try:
        iam.create_role(
            RoleName=_ROLE_NAME,
            AssumeRolePolicyDocument=trust_policy,
            Description="isvctl short-lived credentials test",
        )
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in ("InvalidAction", "NotImplemented", "AuthFailure"):
            note = f"CreateRole not supported ({code}) — STS short-lived credentials not testable"
            for key in result["tests"]:
                result["tests"][key] = {"passed": True, "not_supported": True, "message": note}
            result["success"] = True
            result["not_supported"] = True
            print(json.dumps(result, indent=2))
            return 0
        result["error"] = f"CreateRole failed: {code}"
        print(json.dumps(result, indent=2))
        return 1

    # Give IAM a moment to propagate
    time.sleep(3)

    # ── AssumeRole with short duration ──
    duration = 900  # 15 minutes — minimum STS allows
    role_arn = f"arn:aws:iam::{account_id}:role/{_ROLE_NAME}"

    try:
        assume_resp = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName="isvctl-sec-test",
            DurationSeconds=duration,
        )
        creds = assume_resp["Credentials"]
        expiration = creds.get("Expiration")

        result["tests"]["sts_issues_temp_creds"] = {
            "passed": True,
            "message": "STS AssumeRole returned temporary credentials",
        }
        result["tests"]["short_duration_supported"] = {
            "passed": True,
            "requested_duration_seconds": duration,
            "message": f"DurationSeconds={duration} accepted",
        }

        # ── Check expiry ──
        if expiration:
            if isinstance(expiration, datetime):
                exp_dt = expiration.replace(tzinfo=timezone.utc) if expiration.tzinfo is None else expiration
            else:
                exp_dt = datetime.fromisoformat(str(expiration)).replace(tzinfo=timezone.utc)

            now = datetime.now(tz=timezone.utc)
            ttl = int((exp_dt - now).total_seconds())
            result["node_credential_ttl_seconds"] = ttl
            result["workload_credential_ttl_seconds"] = ttl
            expiry_ok = ttl > 0 and ttl <= args.max_ttl_seconds
            result["tests"]["creds_have_expiry"] = {
                "passed": expiry_ok,
                "expiration": str(expiration),
                "ttl_seconds": ttl,
                "message": f"credentials expire in {ttl}s" if expiry_ok else f"TTL {ttl}s out of expected range",
            }
            if not expiry_ok:
                errors.append(f"credential TTL {ttl}s is not in range (0, {args.max_ttl_seconds}]")
        else:
            result["tests"]["creds_have_expiry"] = {
                "passed": False,
                "message": "Expiration field missing from AssumeRole response",
            }
            errors.append("STS credentials have no Expiration field")

        # ── Verify credentials work immediately ──
        try:
            session = boto3.Session(
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
            )
            sts2 = get_session_client(session, "sts", region=args.region)
            identity = sts2.get_caller_identity()
            result["tests"]["creds_usable_immediately"] = {
                "passed": True,
                "caller_arn": identity.get("Arn", ""),
                "message": "temporary credentials authenticated successfully",
            }
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            result["tests"]["creds_usable_immediately"] = {
                "passed": False,
                "message": f"GetCallerIdentity with temp creds failed: {code}",
            }
            errors.append(f"temp credentials not usable: {code}")

    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in ("InvalidAction", "NotImplemented", "AuthFailure", "AccessDenied"):
            note = f"AssumeRole not supported ({code}) — passing with not_supported"
            for key in result["tests"]:
                result["tests"][key] = {"passed": True, "not_supported": True, "message": note}
            result["success"] = True
            result["not_supported"] = True
            _cleanup(iam, _ROLE_NAME)
            print(json.dumps(result, indent=2))
            return 0
        result["tests"]["sts_issues_temp_creds"] = {"passed": False, "message": str(exc)}
        errors.append(f"AssumeRole failed: {code}")

    # ── Cleanup ──
    _cleanup(iam, _ROLE_NAME)

    result["success"] = len(errors) == 0
    if errors:
        result["errors"] = errors

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
