#!/usr/bin/env bash
# Conda-based setup for avicenna.
# Installs Miniconda3 if no conda is found, creates a conda environment
# named "avicenna", installs pixi into it if needed, then installs
# avicenna in editable mode via pip. Entry points land in the conda
# environment's bin and are available after sourcing vic_env_setup.sh.

set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

ENV_NAME="avicenna"

# ── 1. Find or install conda ───────────────────────────────────────────────
CONDA=""
for cmd in mamba conda micromamba; do
    if command -v "$cmd" &>/dev/null; then
        CONDA="$cmd"
        break
    fi
done

if [ -z "$CONDA" ]; then
    if [[ -x "$HOME/miniconda3/bin/conda" ]]; then
        echo "Miniconda3 found at $HOME/miniconda3 but not on PATH — using it."
        export PATH="$HOME/miniconda3/bin:$PATH"
    else
        echo "No conda installation found — installing Miniconda3..."
        MINICONDA_INSTALLER="$HOME/miniconda3_installer.sh"
        curl -fsSL "https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh" \
            -o "$MINICONDA_INSTALLER"
        bash "$MINICONDA_INSTALLER" -b -p "$HOME/miniconda3"
        rm "$MINICONDA_INSTALLER"
        export PATH="$HOME/miniconda3/bin:$PATH"
        echo "Miniconda3 installed."
    fi
    CONDA="conda"
fi

echo "Using: $CONDA ($($CONDA --version))"

# ── 2. Accept Anaconda Terms of Service ───────────────────────────────────
$CONDA tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main
$CONDA tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r

# ── 3. Initialize conda shell integration ─────────────────────────────────
CONDA_BASE="$($CONDA info --base)"
source "$CONDA_BASE/etc/profile.d/conda.sh"

# ── 4. Create or update the conda environment ─────────────────────────────
if $CONDA env list | grep -qE "^${ENV_NAME}[[:space:]]"; then
    echo "Conda environment '$ENV_NAME' already exists — updating..."
    $CONDA install -n "$ENV_NAME" "python>=3.11" -y
else
    echo "Creating conda environment '$ENV_NAME'..."
    $CONDA create -n "$ENV_NAME" "python>=3.11" -y
fi

conda activate "$ENV_NAME"
echo "Activated: $ENV_NAME"

# ── 5. Install pixi into the conda environment if not present ─────────────
if ! command -v pixi &>/dev/null; then
    echo "pixi not found — installing into conda environment..."
    curl -fsSL https://pixi.sh/install.sh | PIXI_HOME="$CONDA_PREFIX" bash
    export PATH="$CONDA_PREFIX/bin:$PATH"
    source "$CONDA_BASE/etc/profile.d/conda.sh"
    conda activate "$ENV_NAME"
fi

echo "Using: pixi ($(pixi --version))"

# ── 6. Install avicenna ───────────────────────────────────────────────────
echo "Installing avicenna..."
pip install -e "$REPO_DIR"

echo "Setup complete."
echo ""
echo "To make avicenna commands available, source the environment script each"
echo "session, or add the following line to your ~/.bashrc or ~/.zshrc:"
echo ""
echo "    source $REPO_DIR/vic_env_setup.sh"
