#!/bin/bash
# Start the orchestrator and Hermes worker together.
# Usage: ./start.sh [--worker-only]

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

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

    echo "Starting orchestrator..."
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
    local health=$(curl -s localhost:${ORCHESTRATOR_PORT:-8090}/health 2>/dev/null)
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
    *)
        echo "Usage: $0 {start|stop|restart|worker|task-worker|status|logs}"
        exit 1
        ;;
esac
