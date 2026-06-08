#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
"""BMC management network test — not applicable to zCompute.

zCompute is a VM cloud with no BMC/IPMI/Redfish hardware management layer.
This check is excluded in the zCompute security config; this stub exists
only for completeness.

All tests return not_supported.

Usage:
    python3 bmc_management_network_test.py --region symphony
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser(description="BMC management network test (not applicable)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.parse_args()

    note = "zCompute is a VM cloud — no BMC/IPMI/Redfish layer exists"
    result: dict[str, Any] = {
        "success": True,
        "platform": "security",
        "test_name": "bmc_management_network",
        "not_supported": True,
        "management_networks_checked": 0,
        "tests": {
            "dedicated_management_network": {"passed": True, "not_supported": True, "message": note},
            "restricted_management_routes": {"passed": True, "not_supported": True, "message": note},
            "tenant_network_not_management": {"passed": True, "not_supported": True, "message": note},
            "management_acl_enforced": {"passed": True, "not_supported": True, "message": note},
        },
    }

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
