#!/bin/bash
# launchd-fix.sh — diagnose and reset the orchestrator's launchd service so
# `git pull` deploys actually take effect.
#
# Symptom: PID listening on :8090 doesn't match orchestrator.pid (managed by
# launchd, not start.sh). start.sh restart spawns a child that fails to bind,
# and the running binary keeps serving stale code from before the launchd
# load. All recent code changes appear unshipped.
#
# Run on the VPS:
#   bash orchestrator/launchd-fix.sh diagnose       # safe, read-only
#   bash orchestrator/launchd-fix.sh restart        # bootout + bootstrap
#
# After running `restart`, the service comes up against the current repo HEAD.

set -e

ORCH_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${ORCHESTRATOR_PORT:-8090}"

cmd="${1:-diagnose}"

find_listener_pid() {
    lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1
}

find_label() {
    # Look for any user-context launchd job whose stdout/stderr point into our
    # orchestrator dir (catches both *-launchd.log and the std logs).
    launchctl list 2>/dev/null \
        | awk 'NR>1 && $1 != "-" {print $3}' \
        | while read -r label; do
            plist=$(launchctl print "gui/$(id -u)/$label" 2>/dev/null \
                | awk -F'= ' '/path =/ {print $2}' | tr -d '"' | head -1)
            [ -z "$plist" ] && continue
            if grep -q "$ORCH_DIR" "$plist" 2>/dev/null; then
                echo "$label|$plist"
            fi
          done
}

case "$cmd" in
    diagnose)
        echo "=== Orchestrator launchd diagnosis ==="
        echo "ORCH_DIR=$ORCH_DIR"
        echo "PORT=$PORT"
        listener=$(find_listener_pid)
        if [ -z "$listener" ]; then
            echo "No process listening on :$PORT"
        else
            echo "PID listening on :$PORT: $listener"
            ps -p "$listener" -o pid,etime,user,command 2>/dev/null | tail -n +1
        fi
        echo
        echo "Matching launchd jobs (by plist path):"
        find_label || echo "(none found)"
        echo
        echo "Repo HEAD:"
        git -C "$ORCH_DIR/.." log -1 --oneline 2>/dev/null
        ;;
    restart)
        match=$(find_label | head -1)
        if [ -z "$match" ]; then
            echo "No launchd-managed orchestrator job found in $ORCH_DIR."
            echo "You may be running via start.sh directly. Try ./start.sh restart"
            exit 1
        fi
        label="${match%|*}"
        plist="${match##*|}"
        echo "Found launchd job: $label"
        echo "Plist: $plist"
        echo
        echo "Bootstrapping (bootout + bootstrap)..."
        launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || echo "(not loaded)"
        # Bootout is async — wait for the port to actually free before
        # bootstrap, otherwise bootstrap fails with EIO/exit 5.
        for i in 1 2 3 4 5 6 7 8 9 10; do
            if [ -z "$(find_listener_pid)" ]; then break; fi
            sleep 1
        done
        if [ -n "$(find_listener_pid)" ]; then
            echo "WARN: port :$PORT still held after 10s — bootstrap may fail"
        fi
        launchctl bootstrap "gui/$(id -u)" "$plist"
        sleep 2
        new_pid=$(find_listener_pid)
        echo
        echo "✓ Restarted. New PID on :$PORT: ${new_pid:-(none yet — check log)}"
        ps -p "${new_pid:-0}" -o pid,etime,command 2>/dev/null | tail -n +1
        ;;
    *)
        echo "Usage: $0 {diagnose|restart}"
        exit 1
        ;;
esac
