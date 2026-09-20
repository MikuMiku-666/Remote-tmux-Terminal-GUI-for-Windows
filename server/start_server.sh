#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
INSTALL_MARKER="$VENV_DIR/.requirements-installed"
LOCAL_REQUIREMENTS="$SCRIPT_DIR/requirements.txt"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Error: python3 is required but was not found." >&2
    exit 1
fi

if [ ! -x "$VENV_PYTHON" ]; then
    echo "[rterm] Creating isolated Python environment: $VENV_DIR"
    if ! python3 -m venv "$VENV_DIR"; then
        echo "Error: could not create the virtual environment." >&2
        echo "On Debian/Ubuntu, install it with: sudo apt install python3-venv" >&2
        exit 1
    fi
fi

if [ ! -f "$INSTALL_MARKER" ] \
    || [ "$LOCAL_REQUIREMENTS" -nt "$INSTALL_MARKER" ]; then
    echo "[rterm] Installing server dependencies into the isolated environment..."
    "$VENV_PYTHON" -m pip install --upgrade pip
    "$VENV_PYTHON" -m pip install -r "$LOCAL_REQUIREMENTS"
    touch "$INSTALL_MARKER"
else
    echo "[rterm] Reusing isolated Python environment: $VENV_DIR"
fi

echo "[rterm] Starting server..."
exec "$VENV_PYTHON" "$SCRIPT_DIR/server.py" "$@"
