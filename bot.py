"""בוט טלגרם בסיסי - מותאם להרצה ב-Termux."""

import logging
import os
import re
from datetime import datetime
from pathlib import Path

from telegram import Update
from telegram.error import InvalidToken
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)


def load_env(path: str = ".env") -> None:
    """טוען משתנים מקובץ .env (בלי ספריות נוספות)."""
    env_file = Path(__file__).parent / path
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = update.effective_user.first_name
    await update.message.reply_text(
        f"שלום {name}! 👋\nאני בוט שרץ מהטלפון דרך Termux.\nשלח /help לרשימת הפקודות."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "הפקודות שלי:\n"
        "/start - הודעת פתיחה\n"
        "/help - העזרה הזו\n"
        "/time - השעה עכשיו\n"
        "/id - ה-ID שלך בטלגרם\n\n"
        "כל הודעה אחרת - אחזיר לך אותה."
    )


async def time_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    await update.message.reply_text(f"🕒 {now}")


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"ה-ID שלך: {update.effective_user.id}\nה-ID של הצ'אט: {update.effective_chat.id}"
    )


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(update.message.text)


def main() -> None:
    load_env()
    # מנקה תווים נסתרים שנכנסים לפעמים בהדבקה (למשל ב-Termux)
    match = re.search(r"\d+:[A-Za-z0-9_-]{30,}", os.environ.get("BOT_TOKEN", ""))
    token = match.group(0) if match else None
    if not token:
        raise SystemExit("❌ חסר BOT_TOKEN. צור קובץ .env (ראה .env.example) ושים בו את הטוקן.")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("time", time_cmd))
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    log.info("הבוט רץ! לעצירה: Ctrl+C")
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except InvalidToken:
        raise SystemExit(
            "❌ טלגרם דחה את הטוקן. בדוק ב-.env שהדבקת את הטוקן העדכני מ-@BotFather, בלי רווחים או תווים מיותרים."
        ) from None


if __name__ == "__main__":
    main()
