#!/usr/bin/env bash
# Cron entry point. Posts up to the daily cap with no manual approval.
# Schedule (America/New_York): 0 8,13,18 * * *
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p "$ROOT/data"

export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

if [[ -x "$ROOT/.venv/bin/python" ]] && "$ROOT/.venv/bin/python" -c "import x_automation" >/dev/null 2>&1; then
  PYTHON="$ROOT/.venv/bin/python"
else
  PYTHON="$(command -v python3)"
  export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
  if [[ -d "$ROOT/.pydeps" ]]; then
    export PYTHONPATH="$ROOT/.pydeps:$PYTHONPATH"
  fi
fi

LOCK="$ROOT/data/autopost.lock"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "===== $(date -u +%Y-%m-%dT%H:%M:%SZ) autopost skipped; previous run still active =====" \
    >> "$ROOT/data/autopost.log"
  exit 0
fi

{
  echo "===== $(date -u +%Y-%m-%dT%H:%M:%SZ) autopost ====="
  "$PYTHON" -m x_automation.cli drafts autopost
} >> "$ROOT/data/autopost.log" 2>&1
