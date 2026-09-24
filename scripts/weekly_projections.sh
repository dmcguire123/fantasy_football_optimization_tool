#!/bin/sh
# Pull every projection source for the current week, archive it, and write
# the value report. Run by the LaunchAgent in scripts/launchd/, or by hand.
# Output is appended to data/logs/projections.log.

cd "$(dirname "$0")/.." || exit 1
mkdir -p data/logs
{
    echo "=== $(date '+%Y-%m-%d %H:%M') ==="
    .venv/bin/python -m ffopt projections --limit 10
    .venv/bin/python -m ffopt value --limit 10
    echo
} >> data/logs/projections.log 2>&1
