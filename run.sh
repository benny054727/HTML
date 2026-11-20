#!/data/data/com.termux/files/usr/bin/bash
# Runs the bot and restarts it automatically if it crashes.
#   ./run.sh               - run in this window (Ctrl+C stops everything)
#   ./run.sh --background  - run in the background (log: bot.log, stop: ./stop.sh)
# (English messages: Termux shows Hebrew reversed)

cd "$(dirname "$0")" || exit 1
PIDFILE=bot.pid
BOT_CMD="${BOT_CMD:-python bot.py}"
RESTART_DELAY="${RESTART_DELAY:-10}"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "The bot is already running (pid $(cat "$PIDFILE"))."
    echo "To stop it: ./stop.sh"
    exit 1
fi

if [ "$1" = "--background" ]; then
    # keep the log small: over 5MB -> move to bot.log.1
    if [ -f bot.log ] && [ "$(wc -c < bot.log)" -gt 5000000 ]; then
        mv -f bot.log bot.log.1
    fi
    nohup bash "$0" --loop >> bot.log 2>&1 &
    sleep 1
    echo "Bot started in the background."
    echo "  See the log:  tail -f bot.log   (Ctrl+C only stops viewing)"
    echo "  Stop the bot: ./stop.sh"
    exit 0
fi

echo $$ > "$PIDFILE"
child=""
cleanup() {
    trap - INT TERM
    if [ -n "$child" ] && kill -0 "$child" 2>/dev/null; then
        kill -TERM "$child" 2>/dev/null   # the bot shuts down cleanly on TERM
        wait "$child" 2>/dev/null
    fi
    rm -f "$PIDFILE"
    echo "$(date '+%F %T') Bot stopped."
    exit 0
}
trap cleanup INT TERM

command -v termux-wake-lock >/dev/null && termux-wake-lock   # don't let Android sleep Termux

while true; do
    echo "$(date '+%F %T') Starting bot..."
    $BOT_CMD &
    child=$!
    wait "$child"
    code=$?
    child=""
    echo "$(date '+%F %T') Bot exited (code $code). Restarting in ${RESTART_DELAY}s... (Ctrl+C to stop)"
    sleep "$RESTART_DELAY" &
    wait $!
done
