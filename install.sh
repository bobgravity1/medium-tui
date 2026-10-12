#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON=${PYTHON:-python3}
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "Python 3.10 or newer is required. Install Python, then run ./install.sh again." >&2
  exit 1
fi
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else "Python 3.10+ is required.")'
if [ "$#" -eq 1 ] && [ "$1" = "--dev" ]; then
  PACKAGE="$ROOT[dev]"
elif [ "$#" -eq 0 ]; then
  PACKAGE="$ROOT"
else
  echo "Usage: ./install.sh [--dev]" >&2
  exit 2
fi
"$PYTHON" -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/python" -m pip install --disable-pip-version-check -e "$PACKAGE"
printf '\nInstalled. Start with:\n  "%s/medium-reader"\n\nOr activate the commands in this shell:\n  . "%s/.venv/bin/activate"\n' "$ROOT" "$ROOT"
