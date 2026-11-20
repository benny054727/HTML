#!/data/data/com.termux/files/usr/bin/bash
# מריץ את הבוט ומפעיל אותו מחדש אוטומטית אם הוא נופל
cd "$(dirname "$0")"
termux-wake-lock 2>/dev/null
while true; do
    python bot.py
    echo "הבוט נעצר, מפעיל מחדש בעוד 5 שניות... (Ctrl+C פעמיים ליציאה)"
    sleep 5
done
