#!/bin/sh
set -eu

PYTHON_BIN="${PYTHON_BIN:-python3}"
INSTALL_ROOT="${AGENTWEB_INSTALL_ROOT:-$HOME/.local/share/agentweb}"
BIN_DIR="${AGENTWEB_BIN_DIR:-$HOME/.local/bin}"
PACKAGE_SOURCE="${AGENTWEB_PACKAGE_SOURCE:-https://github.com/AnayGarodia/agentweb/archive/refs/heads/main.zip}"
VENV="$INSTALL_ROOT/venv"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "AgentWeb needs Python 3.11 or newer. Install Python, then run this command again." >&2
  exit 1
fi

"$PYTHON_BIN" - <<'PY'
import sys

if sys.version_info < (3, 11):
    raise SystemExit(
        f"AgentWeb needs Python 3.11 or newer; found {sys.version_info.major}.{sys.version_info.minor}."
    )
PY

mkdir -p "$INSTALL_ROOT" "$BIN_DIR"

if [ ! -x "$VENV/bin/python" ]; then
  "$PYTHON_BIN" -m venv "$VENV"
fi

"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade "$PACKAGE_SOURCE"

ln -sfn "$VENV/bin/agentweb" "$BIN_DIR/agentweb"
ln -sfn "$VENV/bin/sitepack" "$BIN_DIR/sitepack"

PATH="$BIN_DIR:$PATH" "$BIN_DIR/agentweb" setup >/dev/null

echo "AgentWeb $($BIN_DIR/agentweb --version) is ready."
echo "Try: agentweb npmjs.com get-version --package react --version latest"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    echo ""
    echo "Add $BIN_DIR to PATH, then restart your terminal:"
    echo "  export PATH=\"$BIN_DIR:\$PATH\""
    ;;
esac
