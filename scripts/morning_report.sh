#!/bin/sh
# Build today's report and have Claude email it, using the morning-report
# skill in .claude/skills/. Run by scripts/launchd/com.ffopt.morning-report.plist
# every morning, or by hand. Needs FFOPT_REPORT_EMAIL in .env, and the
# Claude CLI signed in to an account with the Gmail connector.

cd "$(dirname "$0")/.." || exit 1
mkdir -p data/logs

# launchd starts with a bare environment.
export PATH="$HOME/.ink/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

# Read one KEY=value line from .env.
env_value() {
    sed -n "s/^$1=//p" .env | tail -1 | sed 's/^["'\'']//; s/["'\'']$//'
}

EMAIL="$(env_value FFOPT_REPORT_EMAIL)"
CONFIG_DIR="$(env_value CLAUDE_CONFIG_DIR)"
[ -n "$CONFIG_DIR" ] && export CLAUDE_CONFIG_DIR="$CONFIG_DIR"
export FFOPT_READ_ONLY=true

{
    echo "=== $(date '+%Y-%m-%d %H:%M') ==="
    if [ -z "$EMAIL" ]; then
        echo "FFOPT_REPORT_EMAIL is not set in .env; nothing sent."
        exit 1
    fi
    claude -p "/morning-report $EMAIL" \
        --allowedTools "Bash(.venv/bin/python -m ffopt report*)" "mcp__claude_ai_Gmail__send_message"
    echo
} >> data/logs/morning_report.log 2>&1
