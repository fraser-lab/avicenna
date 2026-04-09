#!/usr/bin/env bash
# Uninstall avicenna.
# Removes the conda environment named "avicenna" and cleans up any
# vic_env_setup.sh source line from the user's shell rc file.

set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

SHELL_NAME="$(basename "$SHELL")"
case "$SHELL_NAME" in
    zsh)  RC_FILE="$HOME/.zshrc" ;;
    *)    RC_FILE="$HOME/.bashrc" ;;
esac

# ── Remove avicenna source line from rc file ─────────────────────────────
remove_path_entry() {
    [[ -f "$RC_FILE" ]] || return
    if grep -qF 'vic_env_setup.sh' "$RC_FILE"; then
        sed -i '/vic_env_setup\.sh/d' "$RC_FILE"
        echo "Removed vic_env_setup.sh source line from $RC_FILE."
    else
        echo "No vic_env_setup.sh source line found in $RC_FILE."
    fi
}

# ── Find conda ────────────────────────────────────────────────────────────
CONDA=""
for cmd in mamba conda micromamba; do
    if command -v "$cmd" &>/dev/null; then
        CONDA="$cmd"
        break
    fi
done

if [ -z "$CONDA" ]; then
    echo "ERROR: No conda installation found."
    exit 1
fi

if ! $CONDA env list | grep -qE "^avicenna[[:space:]]"; then
    echo "Conda environment 'avicenna' not found — avicenna does not appear to be installed."
    exit 1
fi

echo "Removing conda environment 'avicenna'..."
CONDA_BASE="$($CONDA info --base)"
source "$CONDA_BASE/etc/profile.d/conda.sh"
$CONDA env remove -n avicenna -y
echo "Conda environment 'avicenna' removed."
remove_path_entry
echo "Uninstall complete."
