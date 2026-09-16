#!/usr/bin/env bash
# oa_control.sh - OpenAlgo control script for fno-server
# Usage: bash oa_control.sh {start|stop|restart|status}
# - start: launches app.py detached (script pseudo-TTY, required by Flask-SocketIO)
# - polls the port instead of a fixed sleep, so it reports accurately
# - pkill patterns are bracketed and never match this script itself
set -u
APP_DIR="/home/ubuntu/openalgo"
PORT=5000
WS_PORT=8765
LOG="$APP_DIR/log/server.log"
PATTERN='[a]pp\.py'

port_up()   { ss -tln 2>/dev/null | grep -q ":$PORT "; }
running()   { pgrep -f "$PATTERN" >/dev/null 2>&1; }

do_status() {
    echo "  processes:"
    ps aux | grep "$PATTERN" | grep -v grep | awk '{printf "    %s %s %s\n", $2, $11, $12}'
    if port_up; then echo "  web port   : $PORT  [LISTENING]"; else echo "  web port   : $PORT  [down]"; fi
    if ss -tln 2>/dev/null | grep -q ":$WS_PORT "; then echo "  ws  port   : $WS_PORT [LISTENING]"; else echo "  ws  port   : $WS_PORT [down]"; fi
    if running; then echo "  state      : RUNNING"; else echo "  state      : STOPPED"; fi
}

do_stop() {
    if ! running && ! port_up; then
        echo "  OpenAlgo was not running."
        return 0
    fi
    pkill -f "$PATTERN" 2>/dev/null
    for i in $(seq 1 10); do
        if ! running && ! port_up; then break; fi
        sleep 1
        if [ "$i" = "5" ]; then pkill -9 -f "$PATTERN" 2>/dev/null; fi
    done
    if running || port_up; then
        echo "  [WARN] OpenAlgo did not stop cleanly - check logs."
        return 1
    fi
    echo "  [OK] OpenAlgo stopped."
}

do_start() {
    if port_up || running; then
        echo "  OpenAlgo is already running."
        do_status
        return 0
    fi
    echo "  starting app.py (detached, pseudo-TTY), waiting for port $PORT ..."
    cd "$APP_DIR" || { echo "  [ERROR] cannot cd to $APP_DIR"; return 1; }
    mkdir -p log
    setsid nohup script -qec '.venv/bin/python app.py' /dev/null >> "$LOG" 2>&1 < /dev/null &
    # poll up to 60s: app runs migrations first, so first boot can be slow
    for i in $(seq 1 60); do
        if port_up; then
            echo "  [OK] OpenAlgo started (port $PORT listening after ~${i}s)."
            return 0
        fi
        sleep 1
    done
    echo "  [WARN] Not listening after 60s - check the log (tail -40 log/server.log)."
    return 1
}

do_restart() {
    do_stop || true
    do_start
}

case "${1:-}" in
    start)   do_start ;;
    stop)    do_stop ;;
    restart) do_restart ;;
    status)  do_status ;;
    *) echo "usage: bash oa_control.sh {start|stop|restart|status}"; exit 2 ;;
esac
