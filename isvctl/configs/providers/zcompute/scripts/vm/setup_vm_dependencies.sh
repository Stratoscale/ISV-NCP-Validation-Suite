#!/usr/bin/env bash
# setup_vm_dependencies.sh
# Installs all dependencies required for the NVIDIA ISV NCP Validation Suite (VM suite)
# on a fresh Ubuntu 24.04 VM with NVIDIA GPU passthrough.
#
# Run as the ubuntu user (sudo available).
# Safe to re-run — all steps are idempotent.
#
# Usage:
#   bash setup_vm_dependencies.sh
#
# What this installs:
#   - NVIDIA kernel module (linux-modules-nvidia-535-server-<kernel>)
#   - nvidia-utils-535-server (nvidia-smi, nvidia-persistenced)
#   - Docker CE (docker.io)
#   - NVIDIA Container Toolkit (nvidia-ctk, nvidia-container-runtime)
#   - CUDA 12.6 toolkit (nvcc + core libraries, NOT the full 3GB suite)

set -euo pipefail

DRIVER_MAJOR=535
CUDA_VERSION=12-6

log() { echo "[setup] $*"; }
die() { echo "[setup] FATAL: $*" >&2; exit 1; }

# ── 0. Sanity checks ─────────────────────────────────────────────────────────
[[ $(id -u) -ne 0 ]] || die "Run as ubuntu (not root) — the script uses sudo internally"
KERNEL=$(uname -r)
log "Kernel: $KERNEL"

# ── 1. NVIDIA kernel module ──────────────────────────────────────────────────
log "Installing NVIDIA kernel module for $KERNEL ..."
sudo apt-get update -qq
sudo apt-get install -y \
    linux-modules-nvidia-${DRIVER_MAJOR}-server-${KERNEL} \
    nvidia-utils-${DRIVER_MAJOR}-server \
    libnvidia-compute-${DRIVER_MAJOR}-server

# ── 2. Load modules ──────────────────────────────────────────────────────────
log "Loading NVIDIA kernel modules ..."
sudo modprobe nvidia nvidia-uvm nvidia-modeset || true

# ── 3. Verify version consistency ────────────────────────────────────────────
KMOD_VER=$(cat /sys/module/nvidia/version 2>/dev/null || echo "")
if [[ -z "$KMOD_VER" ]]; then
    die "nvidia kernel module did not load — check 'dmesg | grep -i nvidia'"
fi
log "Kernel module version: $KMOD_VER"

# Ensure libnvidia-ml matches the loaded kernel module.
KMOD_MAJOR="${KMOD_VER%%.*}"
log "Pinning nvidia-utils to major version $KMOD_MAJOR ..."
sudo apt-get install -y --allow-downgrades \
    nvidia-utils-${KMOD_MAJOR}-server 2>/dev/null || \
sudo apt-get install -y --allow-downgrades \
    nvidia-utils-${KMOD_MAJOR} 2>/dev/null || \
    log "WARNING: could not find nvidia-utils-${KMOD_MAJOR} — mismatch may persist"
sudo ldconfig

# Verify nvidia-smi works
if nvidia-smi --query-gpu=driver_version --format=csv,noheader > /dev/null 2>&1; then
    log "nvidia-smi OK: $(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)"
else
    log "WARNING: nvidia-smi failed after pin — $(nvidia-smi 2>&1 | head -1)"
fi

# ── 4. Persist modules across reboots ────────────────────────────────────────
log "Persisting NVIDIA modules in /etc/modules ..."
sudo sh -c 'grep -q "^nvidia$" /etc/modules || printf "nvidia\nnvidia-uvm\nnvidia-modeset\n" >> /etc/modules'

# Ensure nvidia-smi is in PATH for non-interactive SSH sessions.
NVSMI=$(find /usr /opt -name nvidia-smi -type f 2>/dev/null | head -1)
if [[ -n "$NVSMI" ]]; then
    sudo ln -sf "$NVSMI" /usr/local/bin/nvidia-smi
    log "nvidia-smi symlinked: $NVSMI -> /usr/local/bin/nvidia-smi"
fi

# Ensure nvidia-persistenced is in PATH.
NVPD=$(find /usr /opt -name nvidia-persistenced -type f 2>/dev/null | head -1)
if [[ -n "$NVPD" ]]; then
    sudo ln -sf "$NVPD" /usr/local/bin/nvidia-persistenced
    log "nvidia-persistenced symlinked: $NVPD -> /usr/local/bin/nvidia-persistenced"
else
    log "WARNING: nvidia-persistenced not found — nvidia_persistence subtest may fail"
fi

# ── 5. Docker CE ─────────────────────────────────────────────────────────────
log "Installing Docker ..."
sudo apt-get install -y --no-install-recommends docker.io curl wget gnupg2 ca-certificates
sudo systemctl enable --now docker
# Add ubuntu user to docker group (takes effect on next login / newgrp)
sudo usermod -aG docker ubuntu
log "Docker: $(docker --version)"

# ── 6. CUDA Toolkit (nvcc + libraries only — NOT the full 3GB package) ───────
if nvcc --version > /dev/null 2>&1; then
    log "nvcc already installed: $(nvcc --version | head -1)"
else
    log "Installing CUDA $CUDA_VERSION toolkit (nvcc + libraries) ..."
    wget -q https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb \
        -O /tmp/cuda-keyring.deb
    sudo dpkg -i /tmp/cuda-keyring.deb

    # Hold nvidia-utils before apt-get update so the CUDA repo cannot upgrade them.
    dpkg -l 'nvidia-utils-*' 2>/dev/null | awk '/^ii/{print $2}' \
        | xargs -r sudo apt-mark hold 2>/dev/null || true

    sudo apt-get update -qq
    sudo apt-get install -y --no-install-recommends \
        cuda-nvcc-${CUDA_VERSION} \
        cuda-libraries-${CUDA_VERSION} \
        libcufft-dev-${CUDA_VERSION} \
        libcurand-dev-${CUDA_VERSION}

    # Unhold now that CUDA packages are installed.
    dpkg -l 'nvidia-utils-*' 2>/dev/null | awk '/^ii/{print $2}' \
        | xargs -r sudo apt-mark unhold 2>/dev/null || true

    # Add CUDA bin to PATH for all session types.
    echo 'export PATH=/usr/local/cuda/bin:$PATH' | sudo tee /etc/profile.d/cuda.sh
    sudo ln -sf /usr/local/cuda/bin/nvcc /usr/local/bin/nvcc 2>/dev/null || true
    log "nvcc: $(nvcc --version | head -1)"
fi

# ── 7. NVIDIA Container Toolkit ──────────────────────────────────────────────
if dpkg -l nvidia-container-toolkit > /dev/null 2>&1; then
    log "NVIDIA Container Toolkit already installed"
else
    log "Installing NVIDIA Container Toolkit ..."
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
        | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -sL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
        | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
    sudo apt-get update -qq
    sudo apt-get install -y nvidia-container-toolkit
fi

sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
log "NVIDIA Container Toolkit: $(nvidia-ctk --version 2>/dev/null | head -1)"

# ── 8. Re-pin nvidia-utils after all apt operations ──────────────────────────
log "Final nvidia-utils pin to kernel module version $KMOD_MAJOR ..."
sudo apt-get install -y --allow-downgrades \
    nvidia-utils-${KMOD_MAJOR}-server 2>/dev/null || \
sudo apt-get install -y --allow-downgrades \
    nvidia-utils-${KMOD_MAJOR} 2>/dev/null || true
sudo ldconfig
sudo systemctl restart docker

# ── 9. Verification ──────────────────────────────────────────────────────────
echo ""
echo "============================================================"
echo " VERIFICATION"
echo "============================================================"
echo ""

PASS=0; FAIL=0
check() {
    local label="$1"; shift
    if "$@" > /dev/null 2>&1; then
        echo "  PASS  $label"
        ((PASS++)) || true
    else
        echo "  FAIL  $label"
        ((FAIL++)) || true
    fi
}

check "nvidia-smi"                  nvidia-smi
check "nvidia-persistenced in PATH" which nvidia-persistenced
check "nvcc"                        nvcc --version
check "docker daemon"               docker info
check "docker --gpus all"           docker run --rm --gpus all ubuntu nvidia-smi
check "lsmod nvidia"                sh -c 'lsmod | grep -q "^nvidia "'

echo ""
echo "  nvidia-smi output:"
nvidia-smi --query-gpu=index,name,driver_version,memory.total --format=csv 2>&1 | sed 's/^/    /'
echo ""
echo "  Kernel module: $(cat /sys/module/nvidia/version 2>/dev/null || echo N/A)"
echo "  nvidia-utils:  $(dpkg -l 'nvidia-utils-*' 2>/dev/null | awk '/^ii/{print $3}' | head -1)"
echo ""
echo "  Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]] && echo "  All checks passed — VM is ready for AMI snapshot." \
                  || echo "  Fix the failures above before creating the AMI."
