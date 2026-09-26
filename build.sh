#!/usr/bin/env bash
# build.sh - Pathogene macOS/Linux Build
set -euo pipefail

echo "============================================================"
echo " Pathogene - macOS/Linux Build"
echo "============================================================"
echo

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "[1/6] Project path: $PROJECT_DIR"
echo

if command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
elif command -v python >/dev/null 2>&1; then
    PYTHON=python
else
    echo "[ERROR] Python not found."
    echo "        Install via: brew install python  or  https://www.python.org"
    exit 1
fi

PY_VER="$($PYTHON --version 2>&1)"
echo "[INFO] System Python: $PY_VER"
echo

VENV_DIR="$PROJECT_DIR/.venv-build"
if [ -f "$VENV_DIR/bin/activate" ]; then
    echo "[2/6] Reusing existing venv: $VENV_DIR"
else
    echo "[2/6] Creating venv: $VENV_DIR"
    "$PYTHON" -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
echo "[INFO] venv activated"
echo "[INFO] Build venv Python: $(python -c 'import sys; print(sys.executable)')"
echo

echo "[3/6] Upgrading pip..."
python -m pip install --upgrade pip --quiet
echo

echo "[4/6] Installing dependencies..."
python -m pip install -r "$PROJECT_DIR/requirements.txt"
python -m pip show pyinstaller >/dev/null 2>&1 || python -m pip install pyinstaller
echo

echo "[CHECK] Verifying pyvips in build venv..."
python -m pip show pyvips >/dev/null
python -c "import pyvips; print('pyvips OK before PyInstaller:', pyvips.__version__)"
echo

echo "[INFO] Skipping manual Tcl/Tk detection."
echo "       PyInstaller will handle bundled Tcl/Tk files automatically."
echo

echo "[5/6] Cleaning previous build..."
rm -rf "$PROJECT_DIR/dist/Pathogene"
rm -rf "$PROJECT_DIR/dist/Pathogene.app"
rm -rf "$PROJECT_DIR/build/Pathogene"
echo

# In bundled mode app.py expects launcher.py to set PATHOGENE_PYTHON at runtime.
# This fallback is useful for subprocess checks during local test runs.
export PATHOGENE_PYTHON="$VENV_DIR/bin/python"

echo "[6/6] Building with PyInstaller..."
echo
cd "$PROJECT_DIR"
python -m PyInstaller pathogene.spec --noconfirm --clean

for sub in slides tiles cosmx cosmx_tiles annotations qc_results uploads; do
    mkdir -p "$PROJECT_DIR/dist/Pathogene/data/$sub"
done

if [ -f "$PROJECT_DIR/dist/Pathogene/Pathogene" ]; then
    chmod +x "$PROJECT_DIR/dist/Pathogene/Pathogene"
fi

echo
echo "[CHECK] Dist pyvips/libvips hints:"
find "$PROJECT_DIR/dist/Pathogene" \( -iname '*pyvips*' -o -iname '*vips*' \) | head -30 || true
echo

echo "============================================================"
echo " Build complete!"
echo "============================================================"
echo
echo " Executable : $PROJECT_DIR/dist/Pathogene/Pathogene"
echo " Dist folder: $PROJECT_DIR/dist/Pathogene/"
echo
echo " Distribute the entire dist/Pathogene/ folder."
echo
echo " Test run:"
echo "   ./dist/Pathogene/Pathogene"
echo
if [ "$(uname)" = "Darwin" ]; then
    echo " NOTE: If macOS Gatekeeper blocks the app:"
    echo "   xattr -cr dist/Pathogene/Pathogene"
    echo
fi
echo "============================================================"
