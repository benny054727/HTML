#!/data/data/com.termux/files/usr/bin/bash
# Stops a bot that was started with ./run.sh (in the background or in another window).
cd "$(dirname "$0")" || exit 1
PIDFILE=bot.pid
if ! [ -f "$PIDFILE" ] || ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    rm -f "$PIDFILE"
    echo "The bot is not running (from run.sh)."
    exit 0
fi
pid=$(cat "$PIDFILE")
kill -TERM "$pid"
for _ in $(seq 1 30); do
    kill -0 "$pid" 2>/dev/null || { echo "Bot stopped."; exit 0; }
    sleep 1
done
echo "The bot did not stop within 30s - forcing it."
pkill -KILL -P "$pid" 2>/dev/null
kill -KILL "$pid" 2>/dev/null
rm -f "$PIDFILE"
echo "Bot stopped."
