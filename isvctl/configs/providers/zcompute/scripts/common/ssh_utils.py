#!/usr/bin/env python3
"""SSH utilities for zcompute VM validation.

zcompute-specific notes:
  - SSH must run from within the cluster network (manager VM).
  - Public IPs are internal (172.28.x.x range).
  - Use BatchMode=yes so SSH never prompts for input.

Password-auth override:
  If ISVTEST_SSH_PASSWORD is set, all connections in this module use password
  auth via sshpass instead of the supplied key file - for instances where the
  key file doesn't match what's actually registered (e.g. manually-created
  instances outside the normal launch_instance.py key-pair flow). Never hardcode
  the password anywhere; export it locally in your own shell only. Requires the
  `sshpass` binary to be installed on the run station.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

_SSH_PASSWORD_ENV_VAR = "ISVTEST_SSH_PASSWORD"

# Common SSH options used for all connections.
_SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",  # prevent host key conflicts between VMs sharing IPs
    "-o", "ConnectTimeout=10",
    "-o", "BatchMode=yes",
]

# Same as _SSH_OPTS but BatchMode=no - password auth needs sshpass to be able
# to feed the prompt, which BatchMode=yes suppresses entirely.
_SSH_OPTS_PASSWORD = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "ConnectTimeout=10",
    "-o", "BatchMode=no",
    "-o", "PreferredAuthentications=password",
    "-o", "PubkeyAuthentication=no",
]


def _ssh_prefix(user: str, host: str, key_file: str) -> list[str]:
    """Build the `ssh`/`sshpass ssh` argument prefix up to (but not including) the remote command.

    Uses password auth via sshpass when ISVTEST_SSH_PASSWORD is set, ignoring
    key_file entirely in that case; otherwise uses the key file as normal.
    """
    password = os.environ.get(_SSH_PASSWORD_ENV_VAR)
    if password:
        if not shutil.which("sshpass"):
            raise RuntimeError(
                f"{_SSH_PASSWORD_ENV_VAR} is set but 'sshpass' is not installed. "
                "Install it (e.g. `sudo apt-get install -y sshpass`) or unset "
                f"{_SSH_PASSWORD_ENV_VAR} to use key-based auth instead."
            )
        return ["sshpass", "-p", password, "ssh", *_SSH_OPTS_PASSWORD, f"{user}@{host}"]
    return ["ssh", *_SSH_OPTS, "-i", key_file, f"{user}@{host}"]


def wait_for_ssh(
    host: str,
    user: str,
    key_file: str,
    max_attempts: int = 40,
    interval: int = 15,
) -> bool:
    """Try SSH connection repeatedly until it succeeds.

    Uses a no-op command ('true') to test connectivity without side effects.

    Args:
        host:         IP or hostname of the instance.
        user:         SSH username (e.g. 'ubuntu').
        key_file:     Path to the private key PEM file.
        max_attempts: Maximum number of connection attempts.
        interval:     Seconds to wait between attempts.

    Returns:
        True if SSH connection succeeded, False if all attempts were exhausted.
    """
    print(
        f"[ssh] waiting for SSH on {host} (max {max_attempts} attempts, "
        f"{interval}s interval) ...",
        file=sys.stderr,
    )
    for attempt in range(1, max_attempts + 1):
        cmd = [*_ssh_prefix(user, host, key_file), "true"]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            print(f"[ssh] SSH ready on {host} (attempt {attempt})", file=sys.stderr)
            return True
        print(
            f"[ssh] attempt {attempt}/{max_attempts} failed: "
            f"{result.stderr.strip()!r}",
            file=sys.stderr,
        )
        if attempt < max_attempts:
            time.sleep(interval)

    print(f"[ssh] SSH did not become available on {host}", file=sys.stderr)
    return False


def run_ssh_command(
    host: str,
    user: str,
    key_file: str,
    command: str,
    timeout: int = 60,
) -> tuple[int, str, str]:
    """Run a command on a remote host via SSH.

    Args:
        host:     IP or hostname of the instance.
        user:     SSH username.
        key_file: Path to the private key PEM file.
        command:  Shell command to execute remotely.
        timeout:  Command execution timeout in seconds.

    Returns:
        Tuple of (returncode, stdout, stderr).
    """
    cmd = [*_ssh_prefix(user, host, key_file), command]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.returncode, result.stdout, result.stderr
