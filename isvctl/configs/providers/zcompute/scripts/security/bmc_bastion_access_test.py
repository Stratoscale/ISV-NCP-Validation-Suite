#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
"""BMC bastion access test — not applicable to zCompute.

zCompute is a VM cloud with no BMC/IPMI/Redfish hardware management layer.
This check is excluded in the zCompute security config; this stub exists
only for completeness.

All tests return not_supported.

Usage:
    python3 bmc_bastion_access_test.py --region symphony
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser(description="BMC bastion access test (not applicable)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.parse_args()

    note = "zCompute is a VM cloud — no BMC/IPMI/Redfish layer exists"
    result: dict[str, Any] = {
        "success": True,
        "platform": "security",
        "test_name": "bmc_bastion_access",
        "not_supported": True,
        "tests": {
            "bmc_only_via_bastion": {"passed": True, "not_supported": True, "message": note},
            "bastion_mfa_enforced": {"passed": True, "not_supported": True, "message": note},
            "bastion_session_logged": {"passed": True, "not_supported": True, "message": note},
            "direct_bmc_access_blocked": {"passed": True, "not_supported": True, "message": note},
        },
    }

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
