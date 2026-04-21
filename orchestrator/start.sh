#!/bin/bash
# ==========================================================================
# Orchestrator boot script — single source of truth for runtime env vars.
#
# Layout-wise this also doubles as Docker-entrypoint-in-waiting: a Dockerfile
# copying the orchestrator dir + calling `start.sh start` in the foreground
# would pick up the same defaults + warnings. Nothing here assumes launchd,
# nohup is used for dev parity; a Docker variant would just replace nohup
# with exec.
#
# Usage: ./start.sh {start|stop|restart|worker|task-worker|status|logs}
# ==========================================================================

set -u  # reference to an unset var should be an error, not silent empty

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# --------------------------------------------------------------------------
# 1. Load .env if present
# --------------------------------------------------------------------------
# .env overrides in-process defaults. Each python process (main.py,
# hermes_worker.py, task_worker.py) already loads its own .env via
# os.environ.setdefault, BUT those only apply AFTER this shell has exported
# the defaults below. We source .env first so subsequent `:= default`
# assignments don't clobber user overrides.
if [ -f "$DIR/.env" ]; then
    set -a  # auto-export every var sourced
    # shellcheck disable=SC1091
    . "$DIR/.env"
    set +a
fi

# --------------------------------------------------------------------------
# 2. Declare every env var this stack reads. Defaults here, secrets (token,
#    keys) must come from .env or the ambient environment (Docker -e).
# --------------------------------------------------------------------------

# Orchestrator HTTP binding. task_worker also reads this to compute the
# localhost URL it posts scheduled-turn requests to.
: "${ORCHESTRATOR_PORT:=8090}"

# Per-request sandbox timeout (fallback — task_worker overrides with its own
# TASK_TIMEOUT_SECONDS for long-running tasks).
: "${SKILL_TIMEOUT:=60}"

# Skill roots — where Lynx finds skill manifests + run.py entrypoints.
: "${SKILL_ROOT:=/home/hqzn/grantllama-scrape-skill/.claude/skills}"

# Auth token every /chat caller must present. Must match what the zeon
# frontend + task_worker send. EMPTY default = unauthenticated (dev only).
: "${RUNNER_KEY:=}"

# Anthropic relay — the upstream Claude API proxy. Used by the main
# orchestrator (per-request) AND by hermes_worker (system-level default).
: "${RELAY_BASE_URL:=}"

# --- Phase 10 / Phase 11 plumbing ---

# task_worker bypasses Cloudflare on scheduled_turn POSTs by hitting the
# same-host orchestrator directly. Defaults to the port chosen above.
: "${LOCAL_ORCHESTRATOR_URL:=http://localhost:${ORCHESTRATOR_PORT}}"

# Durable task wall-clock ceiling. Individual skills still respect
# SKILL_TIMEOUT — this is the task-level fence.
: "${TASK_TIMEOUT_SECONDS:=3600}"
: "${TASK_HEARTBEAT_STALE_SECONDS:=300}"

# --- Hermes (memory extraction) ---

# Hermes worker needs a relay URL + token to call Haiku for extraction.
# Falls back to the orchestrator's relay values so operators only set
# them once for simple deployments.
: "${HERMES_RELAY_URL:=${RELAY_BASE_URL:-}}"
: "${HERMES_AUTH_TOKEN:=${RUNNER_KEY:-}}"
: "${HERMES_ENABLED:=true}"
: "${HERMES_POLL_INTERVAL:=5}"
: "${HERMES_BATCH_THRESHOLD:=5}"

# --- Misc ---
: "${DEFAULT_MODEL:=claude-sonnet-4-6}"
: "${PENDING_ACTION_TTL_MINUTES:=30}"

# Export everything the Python processes might read. Explicit list so
# operators can scan this file to see what knobs exist.
export \
    ORCHESTRATOR_PORT SKILL_TIMEOUT SKILL_ROOT \
    RUNNER_KEY RELAY_BASE_URL DEFAULT_MODEL PENDING_ACTION_TTL_MINUTES \
    LOCAL_ORCHESTRATOR_URL TASK_TIMEOUT_SECONDS TASK_HEARTBEAT_STALE_SECONDS \
    HERMES_RELAY_URL HERMES_AUTH_TOKEN HERMES_ENABLED HERMES_POLL_INTERVAL \
    HERMES_BATCH_THRESHOLD

# --------------------------------------------------------------------------
# 3. Sanity warnings — don't exit, just make the problem obvious in logs
# --------------------------------------------------------------------------
if [ -z "${RUNNER_KEY:-}" ]; then
    echo "WARN: RUNNER_KEY is empty — /chat requests will reject auth. Set in .env."
fi
if [ -z "${HERMES_RELAY_URL:-}" ] || [ -z "${HERMES_AUTH_TOKEN:-}" ]; then
    echo "WARN: HERMES_RELAY_URL/HERMES_AUTH_TOKEN not set — Hermes memory extraction disabled."
fi

# --------------------------------------------------------------------------
# 4. Process bookkeeping
# --------------------------------------------------------------------------
ORCHESTRATOR_PID_FILE="$DIR/orchestrator.pid"
WORKER_PID_FILE="$DIR/hermes_worker.pid"
TASK_WORKER_PID_FILE="$DIR/task_worker.pid"
LOG_DIR="$DIR/../logs"
mkdir -p "$LOG_DIR"

# Prefer orchestrator venv python; fall back to system python3
if [ -x "$DIR/venv/bin/python3" ]; then
    PYTHON="$DIR/venv/bin/python3"
else
    PYTHON="python3"
fi

stop_process() {
    local pid_file="$1"
    local name="$2"
    if [ -f "$pid_file" ]; then
        local pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            echo "Stopping $name (PID $pid)..."
            kill "$pid"
            sleep 1
            if kill -0 "$pid" 2>/dev/null; then
                kill -9 "$pid" 2>/dev/null
            fi
        fi
        rm -f "$pid_file"
    fi
}

start_orchestrator() {
    stop_process "$ORCHESTRATOR_PID_FILE" "orchestrator"

    echo "Starting orchestrator on port $ORCHESTRATOR_PORT..."
    nohup "$PYTHON" main.py > "$LOG_DIR/orchestrator.log" 2>&1 &
    local pid=$!
    echo "$pid" > "$ORCHESTRATOR_PID_FILE"
    echo "  Orchestrator started (PID $pid)"
    echo "  Log: $LOG_DIR/orchestrator.log"
}

start_worker() {
    stop_process "$WORKER_PID_FILE" "hermes_worker"

    echo "Starting Hermes worker..."
    nohup "$PYTHON" hermes_worker.py > "$LOG_DIR/hermes_worker.log" 2>&1 &
    local pid=$!
    echo "$pid" > "$WORKER_PID_FILE"
    echo "  Hermes worker started (PID $pid)"
    echo "  Log: $LOG_DIR/hermes_worker.log"
}

start_task_worker() {
    stop_process "$TASK_WORKER_PID_FILE" "task_worker"

    echo "Starting Task worker..."
    nohup "$PYTHON" task_worker.py > "$LOG_DIR/task_worker.log" 2>&1 &
    local pid=$!
    echo "$pid" > "$TASK_WORKER_PID_FILE"
    echo "  Task worker started (PID $pid)"
    echo "  Log: $LOG_DIR/task_worker.log"
}

status() {
    echo "=== Orchestrator ==="
    if [ -f "$ORCHESTRATOR_PID_FILE" ]; then
        local pid=$(cat "$ORCHESTRATOR_PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo "  Running (PID $pid)"
        else
            echo "  Dead (stale PID $pid)"
        fi
    else
        echo "  Not running"
    fi

    echo "=== Hermes Worker ==="
    if [ -f "$WORKER_PID_FILE" ]; then
        local pid=$(cat "$WORKER_PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo "  Running (PID $pid)"
        else
            echo "  Dead (stale PID $pid)"
        fi
    else
        echo "  Not running"
    fi

    echo "=== Task Worker ==="
    if [ -f "$TASK_WORKER_PID_FILE" ]; then
        local pid=$(cat "$TASK_WORKER_PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo "  Running (PID $pid)"
        else
            echo "  Dead (stale PID $pid)"
        fi
    else
        echo "  Not running"
    fi

    # Health check
    local health=$(curl -s "localhost:$ORCHESTRATOR_PORT/health" 2>/dev/null)
    if [ -n "$health" ]; then
        echo "=== Health ==="
        echo "  $health"
    fi
}

case "${1:-start}" in
    start)
        start_orchestrator
        start_worker
        start_task_worker
        echo ""
        echo "All services started. Use './start.sh status' to check."
        ;;
    stop)
        stop_process "$TASK_WORKER_PID_FILE" "task_worker"
        stop_process "$WORKER_PID_FILE" "hermes_worker"
        stop_process "$ORCHESTRATOR_PID_FILE" "orchestrator"
        echo "All services stopped."
        ;;
    restart)
        stop_process "$TASK_WORKER_PID_FILE" "task_worker"
        stop_process "$WORKER_PID_FILE" "hermes_worker"
        stop_process "$ORCHESTRATOR_PID_FILE" "orchestrator"
        sleep 1
        start_orchestrator
        start_worker
        start_task_worker
        echo ""
        echo "All services restarted."
        ;;
    --worker-only|worker)
        start_worker
        ;;
    --task-worker-only|task-worker)
        start_task_worker
        ;;
    status)
        status
        ;;
    logs)
        tail -f "$LOG_DIR/orchestrator.log" "$LOG_DIR/hermes_worker.log" "$LOG_DIR/task_worker.log"
        ;;
    env)
        # Debug helper: print every env var this stack cares about (redact secrets).
        echo "ORCHESTRATOR_PORT        = $ORCHESTRATOR_PORT"
        echo "SKILL_TIMEOUT            = $SKILL_TIMEOUT"
        echo "SKILL_ROOT               = $SKILL_ROOT"
        echo "RUNNER_KEY               = $([ -n "${RUNNER_KEY:-}" ] && echo '<set>' || echo '<EMPTY>')"
        echo "RELAY_BASE_URL           = ${RELAY_BASE_URL:-<empty>}"
        echo "LOCAL_ORCHESTRATOR_URL   = $LOCAL_ORCHESTRATOR_URL"
        echo "TASK_TIMEOUT_SECONDS     = $TASK_TIMEOUT_SECONDS"
        echo "HERMES_ENABLED           = $HERMES_ENABLED"
        echo "HERMES_POLL_INTERVAL     = $HERMES_POLL_INTERVAL"
        echo "HERMES_BATCH_THRESHOLD   = $HERMES_BATCH_THRESHOLD"
        echo "HERMES_RELAY_URL         = ${HERMES_RELAY_URL:-<empty>}"
        echo "HERMES_AUTH_TOKEN        = $([ -n "${HERMES_AUTH_TOKEN:-}" ] && echo '<set>' || echo '<EMPTY>')"
        echo "DEFAULT_MODEL            = $DEFAULT_MODEL"
        echo "PENDING_ACTION_TTL_MINUTES = $PENDING_ACTION_TTL_MINUTES"
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|worker|task-worker|status|logs|env}"
        exit 1
        ;;
esac
