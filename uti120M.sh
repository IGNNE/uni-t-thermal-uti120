#!/usr/bin/env bash
set -euo pipefail

# Resolve script directory so it works when called from anywhere
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Check for python3
if ! command -v python3 &>/dev/null; then
    echo "Error: python3 is not installed or not in PATH" >&2
    exit 1
fi

# Create venv if it doesn't exist
if [ ! -d .venv ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

# Install/update dependencies when pyproject.toml is newer than stamp file
STAMP=".venv/.deps_installed"
if [ ! -f "$STAMP" ] || [ pyproject.toml -nt "$STAMP" ]; then
    echo "Installing dependencies..."
    .venv/bin/pip install .
    rm -rf build/ uti120.egg-info/
    touch "$STAMP"
fi

exec .venv/bin/python3 -m uti120 "$@"
