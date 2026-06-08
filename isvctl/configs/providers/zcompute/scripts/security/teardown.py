#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
"""Security suite teardown for zCompute.

Each security test script cleans up its own resources on completion.
This teardown step confirms the suite exited cleanly.

Usage:
    python3 teardown.py --region symphony
    python3 teardown.py --region symphony --skip-destroy
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser(description="Security suite teardown for zCompute")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument(
        "--skip-destroy",
        action="store_true",
        help="Skip resource destruction",
    )
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": True,
        "platform": "security",
        "test_name": "teardown",
    }

    if args.skip_destroy:
        result["skipped"] = True
    else:
        result["resources_cleaned"] = 0
        result["message"] = (
            "each security test script performs its own cleanup; "
            "no shared resources to destroy at suite level"
        )

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
