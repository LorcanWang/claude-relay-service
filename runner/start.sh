#!/usr/bin/env bash
# Start the skill runner in the background
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$SCRIPT_DIR/runner.pid"
LOG_FILE="$SCRIPT_DIR/runner.log"

# Load .env from relay service root
ENV_FILE="$(dirname "$SCRIPT_DIR")/.env"
if [ -f "$ENV_FILE" ]; then
  set -o allexport
  source "$ENV_FILE"
  set +o allexport
fi

export SKILL_ROOT="${SKILL_ROOT:-/home/hqzn/grantllama-scrape-skill/.claude/skills}"
export PORT="${RUNNER_PORT:-8080}"

echo "Starting skill runner..."
echo "  SKILL_ROOT = $SKILL_ROOT"
echo "  PORT       = $PORT"

nohup python3 "$SCRIPT_DIR/main.py" >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
echo "  PID        = $(cat "$PID_FILE")"
echo "  Log        = $LOG_FILE"
