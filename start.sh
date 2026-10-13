#!/usr/bin/env bash
# One-command start for Mac/Linux: ./start.sh
set -e
cd "$(dirname "$0")"

PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "Python 3.11 or newer is needed. Install it from https://www.python.org/downloads/ and run ./start.sh again."
  exit 1
fi

[ -d .venv ] || "$PY" -m venv .venv
. .venv/bin/activate
pip install -q -e .

if [ -z "$TYPESAFE_API_KEY" ]; then
  printf "Paste your TypeSafe API key (typing is hidden), then press Enter: "
  read -r -s TYPESAFE_API_KEY; echo
  export TYPESAFE_API_KEY
fi

exec jevbot web $JEVBOT_ARGS
