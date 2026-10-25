#!/usr/bin/env sh
# One-command launcher for macOS / Linux: starts ORCA and opens the dashboard.
cd "$(dirname "$0")" || exit 1
PY=$(command -v python3 || command -v python) || { echo "Python 3.10+ is required"; exit 1; }
( sleep 2; (command -v xdg-open >/dev/null && xdg-open http://127.0.0.1:8000) || (command -v open >/dev/null && open http://127.0.0.1:8000) ) >/dev/null 2>&1 &
exec "$PY" run.py "$@"
