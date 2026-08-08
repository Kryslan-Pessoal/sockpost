#!/usr/bin/env bash
#
# install.sh - install sockpost from this checkout.
#
# Prefers pipx (isolated environment, command on PATH) and falls back to a
# user level pip install. Pass --dev for an editable install with the test
# dependencies.
#
#   ./install.sh
#   ./install.sh --dev

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${PYTHON:-python3}"
DEV=0

usage() {
  cat <<'TXT'
install.sh - install sockpost from this checkout.

Prefers pipx (isolated environment, command on PATH) and falls back to a user
level pip install. Pass --dev for an editable install with the test
dependencies.

  ./install.sh
  ./install.sh --dev
TXT
}

for arg in "$@"; do
  case "$arg" in
    --dev) DEV=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "error: unknown option $arg" >&2; exit 2 ;;
  esac
done

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "error: $PYTHON not found; install Python 3.9 or newer" >&2
  exit 1
fi

"$PYTHON" - <<'PY' || { echo "error: Python 3.9 or newer is required" >&2; exit 1; }
import sys
raise SystemExit(0 if sys.version_info >= (3, 9) else 1)
PY

case "$(uname -s)" in
  Linux|Darwin|FreeBSD|OpenBSD|NetBSD) ;;
  *) echo "error: sockpost needs a POSIX host with Unix domain sockets" >&2; exit 1 ;;
esac

cd "$REPO_ROOT"

if [ "$DEV" -eq 1 ]; then
  echo "installing in editable mode with test dependencies"
  "$PYTHON" -m pip install -e ".[dev]"
elif command -v pipx >/dev/null 2>&1; then
  echo "installing with pipx"
  pipx install --force .
else
  echo "pipx not found, installing with pip --user"
  "$PYTHON" -m pip install --user .
fi

echo
if command -v sockpost >/dev/null 2>&1; then
  echo "installed: $(command -v sockpost)"
  sockpost --version
else
  echo "installed, but 'sockpost' is not on PATH yet."
  echo "add the user script directory to PATH, for example:"
  echo "  export PATH=\"\$($PYTHON -m site --user-base)/bin:\$PATH\""
fi

echo
echo "next steps:"
echo "  sockpost listen --id worker &"
echo "  sockpost send --from planner --to worker --text hello"
