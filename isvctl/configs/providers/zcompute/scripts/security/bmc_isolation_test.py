#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
"""BMC tenant isolation test — not applicable to zCompute.

zCompute is a VM cloud with no BMC/IPMI/Redfish hardware management layer.
This check is excluded in the zCompute security config; this stub exists
only for completeness.

All tests return not_supported.

Usage:
    python3 bmc_isolation_test.py --region symphony
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser(description="BMC tenant isolation test (not applicable)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.parse_args()

    note = "zCompute is a VM cloud — no BMC/IPMI/Redfish layer exists"
    result: dict[str, Any] = {
        "success": True,
        "platform": "security",
        "test_name": "bmc_tenant_isolation",
        "not_supported": True,
        "tests": {
            "bmc_tenant_network_isolated": {"passed": True, "not_supported": True, "message": note},
            "bmc_credentials_not_shared": {"passed": True, "not_supported": True, "message": note},
            "bmc_firmware_isolated": {"passed": True, "not_supported": True, "message": note},
            "bmc_console_isolated": {"passed": True, "not_supported": True, "message": note},
        },
    }

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
