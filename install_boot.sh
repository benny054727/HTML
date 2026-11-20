#!/data/data/com.termux/files/usr/bin/bash
# Makes the bot start by itself when the phone turns on.
# Needs the Termux:Boot app (from F-Droid), opened once after installing.
cd "$(dirname "$0")" || exit 1
BOT_DIR="$(pwd)"
mkdir -p ~/.termux/boot
cat > ~/.termux/boot/start-bot.sh <<BOOT
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
"$BOT_DIR/run.sh" --background
BOOT
chmod +x ~/.termux/boot/start-bot.sh "$BOT_DIR/run.sh" "$BOT_DIR/stop.sh"
echo "Done: the bot will start automatically when the phone turns on."
echo "(Make sure the Termux:Boot app is installed and was opened once.)"
echo "To undo: rm ~/.termux/boot/start-bot.sh"
