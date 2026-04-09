#!/usr/bin/env bash
# Make avicenna commands available in the current shell session.
# Source this file — do not execute it directly.
# Usage:   source /path/to/vic_env_setup.sh
# To have it run automatically, add that line to your ~/.bashrc or ~/.zshrc.

_avn_conda=""
for _avn_cmd in mamba conda micromamba; do
    if command -v "$_avn_cmd" &>/dev/null; then
        _avn_conda="$_avn_cmd"
        break
    fi
done

if [[ -z "$_avn_conda" && -x "$HOME/miniconda3/bin/conda" ]]; then
    export PATH="$HOME/miniconda3/bin:$PATH"
    _avn_conda="conda"
fi

if [[ -z "$_avn_conda" ]]; then
    echo "avicenna: conda not found — cannot activate environment." >&2
    unset _avn_conda _avn_cmd
    return 1
fi

_avn_base="$($_avn_conda info --base)"
# shellcheck source=/dev/null
source "$_avn_base/etc/profile.d/conda.sh"
conda activate avicenna 2>/dev/null

unset _avn_conda _avn_cmd _avn_base
