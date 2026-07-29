#!/usr/bin/env bash
# bootstrap_run_station.sh
# Sets up a fresh Ubuntu VM as the run/hop station for the zcompute Bare
# Metal (BMaaS) suite: clones the repo, installs uv + Python 3.12, builds
# the workspace venv, and drops an env-var template to fill in with
# zcompute/GB200 cluster credentials.
#
# This station only runs isvctl and SSHes out to the launched BM instance —
# it does NOT need GPU drivers, CUDA, or Docker (that's the target instance's
# problem, handled by the suite's own scripts over SSH).
#
# Run as the ubuntu user (sudo available). Safe to re-run — idempotent.
#
# Usage:
#   bash bootstrap_run_station.sh [repo_url] [clone_dir]
#
#   repo_url   default: git@github.com:Stratoscale/ISV-NCP-Validation-Suite.git
#              (requires this VM's SSH key to already be registered as a
#              GitHub deploy key / your account key — this script does not
#              generate or register one)
#   clone_dir  default: $HOME/ISV-NCP-Validation-Suite

set -euo pipefail

REPO_URL="${1:-git@github.com:Stratoscale/ISV-NCP-Validation-Suite.git}"
CLONE_DIR="${2:-$HOME/ISV-NCP-Validation-Suite}"
ENV_TEMPLATE="$HOME/zcompute-bm.env"

log() { echo "[bootstrap] $*"; }
die() { echo "[bootstrap] FATAL: $*" >&2; exit 1; }

[[ $(id -u) -ne 0 ]] || die "Run as ubuntu (not root) — the script uses sudo internally"

# ── 1. Base packages ─────────────────────────────────────────────────────────
log "Installing base packages ..."
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
    git curl ca-certificates build-essential openssh-client unzip

# ── 2. uv (manages Python + the workspace venv) ──────────────────────────────
if command -v uv > /dev/null 2>&1; then
    log "uv already installed: $(uv --version)"
else
    log "Installing uv ..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
grep -q '.local/bin' "$HOME/.bashrc" 2>/dev/null || \
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"

log "Installing Python 3.12 via uv ..."
uv python install 3.12

# ── 3. Clone (or update) the repo ────────────────────────────────────────────
if [[ -d "$CLONE_DIR/.git" ]]; then
    log "Repo already cloned at $CLONE_DIR — pulling latest ..."
    git -C "$CLONE_DIR" pull --ff-only
else
    log "Cloning $REPO_URL -> $CLONE_DIR ..."
    git clone "$REPO_URL" "$CLONE_DIR"
fi

# ── 4. Build the workspace venv ──────────────────────────────────────────────
log "Running uv sync ..."
(cd "$CLONE_DIR" && uv sync)

# ── 5. Env-var template ──────────────────────────────────────────────────────
if [[ -f "$ENV_TEMPLATE" ]]; then
    log "Env template already exists at $ENV_TEMPLATE — leaving it untouched"
else
    log "Writing env template to $ENV_TEMPLATE ..."
    cat > "$ENV_TEMPLATE" <<EOF
# Fill in and 'source $ENV_TEMPLATE' before running the BM suite.
# See isvctl/configs/providers/zcompute/config/bare_metal.yaml header for
# the full option list (reuse-existing-instance vars, etc).

export ZCOMPUTE_BASE_URL=https://<zcompute-cluster-ip>
export AWS_ACCESS_KEY_ID=<key-id>
export AWS_SECRET_ACCESS_KEY=<secret>
export AWS_REGION=symphony
export ZCOMPUTE_BM_AMI_ID=<ami-id>

# Required on Mac in this repo's docs, but harmless/needed here too since
# this VM won't have the packages editable-installed onto system Python:
export PYTHONPATH=isvctl/src:isvtest/src:isvreporter/src

# Uncomment to reuse an already-launched instance instead of paying launch cost:
# export ZCOMPUTE_BM_INSTANCE_ID=i-xxx
# export ZCOMPUTE_BM_KEY_FILE=/path/to/key.pem
EOF
    chmod 600 "$ENV_TEMPLATE"
fi

# ── 6. Verification ──────────────────────────────────────────────────────────
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

check "git"                 git --version
check "uv"                  uv --version
check "ssh client"          ssh -V
check "repo cloned"         test -d "$CLONE_DIR/.git"
check "workspace venv"      bash -c "cd '$CLONE_DIR' && uv run python --version"

echo ""
echo "  Results: $PASS passed, $FAIL failed"
echo ""
if [[ $FAIL -eq 0 ]]; then
    echo "  Run station ready. Next steps:"
    echo "    1. Edit $ENV_TEMPLATE with real credentials"
    echo "    2. source $ENV_TEMPLATE"
    echo "    3. cd $CLONE_DIR"
    echo "    4. uv run isvctl test run -f isvctl/configs/providers/zcompute/config/bare_metal.yaml -v"
else
    echo "  Fix the failures above before running the suite."
fi
