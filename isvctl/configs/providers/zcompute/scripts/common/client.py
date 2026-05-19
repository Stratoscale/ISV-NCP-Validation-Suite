"""Boto3 client factory for zcompute.

zcompute exposes AWS-compatible APIs at per-service URLs:
    https://<ip>/api/v2/aws/ec2/
    https://<ip>/api/v2/aws/iam/
    https://<ip>/api/v2/aws/sts/
    ... etc.

SSL certificates are self-signed, so verification is disabled by default.

Configuration:
    ZCOMPUTE_BASE_URL   - base URL, e.g. https://172.16.10.110
                          (do NOT include /api/v2/aws/<service>/ — that is
                          appended automatically per service)
    AWS_ACCESS_KEY_ID   - zcompute access key
    AWS_SECRET_ACCESS_KEY - zcompute secret key
    AWS_REGION          - zcompute region (e.g. symphony)

Example:
    export ZCOMPUTE_BASE_URL=https://172.16.10.110
    export AWS_ACCESS_KEY_ID=...
    export AWS_SECRET_ACCESS_KEY=...
    export AWS_REGION=symphony
"""

from __future__ import annotations

import os
import sys
import urllib3
from typing import Any

import boto3

# Suppress SSL warnings for self-signed certs.
# zcompute uses self-signed certificates; verification is intentionally disabled.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _endpoint_url(service: str) -> str:
    """Build the per-service endpoint URL for zcompute.

    Pattern: {ZCOMPUTE_BASE_URL}/api/v2/aws/{service}/

    Args:
        service: AWS service name (e.g. 'ec2', 'iam', 'sts').

    Returns:
        Full endpoint URL string.

    Raises:
        RuntimeError: If ZCOMPUTE_BASE_URL is not set.
    """
    base = os.environ.get("ZCOMPUTE_BASE_URL", "").rstrip("/")
    if not base:
        raise RuntimeError(
            "ZCOMPUTE_BASE_URL is not set. "
            "Example: export ZCOMPUTE_BASE_URL=https://172.16.10.110"
        )
    return f"{base}/api/v2/aws/{service}/"


def get_client(service: str, region: str | None = None, **kwargs: Any) -> Any:
    """Create a boto3 client pointed at the zcompute endpoint for this service.

    Args:
        service:  AWS service name (e.g. 'ec2', 'iam', 'sts').
        region:   Region name. Falls back to AWS_REGION env var, then 'symphony'.
        **kwargs: Extra kwargs forwarded to boto3.client().

    Returns:
        Configured boto3 client with SSL verification disabled.
    """
    region = region or os.environ.get("AWS_REGION", "symphony")
    return boto3.client(
        service,
        region_name=region,
        endpoint_url=_endpoint_url(service),
        verify=False,  # zcompute uses self-signed certificates
        **kwargs,
    )


def get_session_client(
    session: Any,
    service: str,
    region: str | None = None,
    **kwargs: Any,
) -> Any:
    """Create a boto3 client from an explicit Session.

    Use this when you need a client authenticated with different credentials
    than the environment defaults (e.g. a newly created test access key).

    Args:
        session:  boto3.Session already configured with the target credentials.
        service:  AWS service name.
        region:   Region name.
        **kwargs: Extra kwargs forwarded to session.client().

    Returns:
        Configured boto3 client with SSL verification disabled.
    """
    region = region or os.environ.get("AWS_REGION", "symphony")
    return session.client(
        service,
        region_name=region,
        endpoint_url=_endpoint_url(service),
        verify=False,  # zcompute uses self-signed certificates
        **kwargs,
    )
