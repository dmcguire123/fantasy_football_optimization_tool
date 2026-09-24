#!/bin/sh
# One breaking-news scan (ffopt/news.py). Run every few minutes by
# scripts/launchd/com.ffopt.news.plist. Suggests pickups by Mac notification
# and email; never makes a roster move. Logs to data/logs/news.log.

cd "$(dirname "$0")/.." || exit 1
mkdir -p data/logs

# launchd starts with a bare environment; the alert email goes through the
# Claude CLI, like the morning report.
export PATH="$HOME/.ink/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
CONFIG_DIR="$(sed -n 's/^CLAUDE_CONFIG_DIR=//p' .env | tail -1)"
[ -n "$CONFIG_DIR" ] && export CLAUDE_CONFIG_DIR="$CONFIG_DIR"
export FFOPT_READ_ONLY=true

{
    printf '%s ' "$(date '+%Y-%m-%d %H:%M')"
    .venv/bin/python -m ffopt news scan | head -1
} >> data/logs/news.log 2>&1
