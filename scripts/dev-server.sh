#!/bin/bash
# Start the OpenAlgo dev server detached from the calling shell.
# Usage: bash scripts/dev-server.sh start | stop | status
set -u
cd "$(dirname "$0")/.."

PIDFILE="log/server_launcher.pid"
LOGFILE="log/server.log"

is_running() {
  [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null
}

case "${1:-start}" in
  start)
    if is_running; then
      echo "already running (launcher PID $(cat "$PIDFILE"))"
      exit 0
    fi
    # A pseudo-TTY so Flask-SocketIO's interactive-terminal check passes when
    # Flask runs with debug off; nohup alone detaches stdin and trips it.
    setsid sh -c "tail -f /dev/null | script -qec 'uv run app.py' /dev/null >> $LOGFILE 2>&1" &
    echo $! > "$PIDFILE"
    disown
    echo "launcher started (PID $(cat "$PIDFILE")), log: $LOGFILE"
    ;;
  stop)
    if ! is_running; then
      echo "not running"
      rm -f "$PIDFILE"
      exit 0
    fi
    kill -TERM -- -"$(cat "$PIDFILE")" 2>/dev/null || kill -TERM "$(cat "$PIDFILE")"
    sleep 3
    pkill -KILL -f "uv run app.py" 2>/dev/null
    pkill -KILL -f "tail -f /dev/null" 2>/dev/null
    rm -f "$PIDFILE"
    echo "stopped"
    ;;
  status)
    if is_running; then
      echo "running (launcher PID $(cat "$PIDFILE"))"
    else
      echo "not running"
      exit 1
    fi
    ;;
  *)
    echo "usage: $0 {start|stop|status}"
    exit 2
    ;;
esac
