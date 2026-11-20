"""בוט טלגרם לעריכת תמונות - מותאם להרצה ב-Termux."""

import asyncio
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import InvalidToken
from telegram.ext import (
    Application,
    CallbackQueryHandler,
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

try:
    import editor
except ImportError:
    raise SystemExit("❌ חסרה הספרייה Pillow. ב-Termux הרץ: pkg install python-pillow")

EDIT_CAPTION = "✏️ בחר עריכה. אפשר ללחוץ גם על כפתורים של תמונות קודמות כדי לחזור אחורה."


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
        f"שלום {name}! 👋\nאני בוט לעריכת תמונות.\n📷 שלח לי תמונה ואראה לך מה אפשר לעשות איתה.\nשלח /help לעזרה."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "הפקודות שלי:\n"
        "/start - הודעת פתיחה\n"
        "/help - העזרה הזו\n"
        "/time - השעה עכשיו\n"
        "/id - ה-ID שלך בטלגרם\n\n"
        "📷 שלח תמונה (או תמונה כקובץ) ותקבל כפתורי עריכה:\n"
        "שחור-לבן, ספיה, בהירות, ניגודיות, חידוד, טשטוש, סיבוב, מראה, חיתוך ועוד.\n"
        "כל עריכה נשלחת כתמונה חדשה, ואפשר להמשיך לערוך אותה.\n"
        "📥 'שמור באיכות מלאה' שולח את התמונה כקובץ בלי דחיסה."
    )


async def time_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    await update.message.reply_text(f"🕒 {now}")


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"ה-ID שלך: {update.effective_user.id}\nה-ID של הצ'אט: {update.effective_chat.id}"
    )


def edit_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(editor.ACTIONS[key][0], callback_data=f"edit:{key}") for key in row]
        for row in editor.LAYOUT
    ]
    rows.append([InlineKeyboardButton("📥 שמור באיכות מלאה", callback_data="save")])
    return InlineKeyboardMarkup(rows)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """קיבלנו תמונה - שולחים אותה בחזרה עם כפתורי עריכה."""
    msg = update.message
    if msg.photo:
        await msg.reply_photo(msg.photo[-1].file_id, caption=EDIT_CAPTION, reply_markup=edit_keyboard())
        return
    # תמונה שנשלחה כקובץ - מורידים ושולחים כתמונה
    await msg.chat.send_action("upload_photo")
    file = await msg.document.get_file()
    data = bytes(await file.download_as_bytearray())
    try:
        result = await asyncio.to_thread(editor.apply, "none", data)
    except Exception:
        await msg.reply_text("❌ לא הצלחתי לפתוח את הקובץ הזה כתמונה.")
        return
    await msg.reply_photo(result, caption=EDIT_CAPTION, reply_markup=edit_keyboard())


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """לחיצה על כפתור עריכה - עורכים את התמונה שהכפתור מחובר אליה."""
    query = update.callback_query
    if not query.message or not query.message.photo:
        await query.answer("התמונה כבר לא זמינה", show_alert=True)
        return

    await query.answer("⏳ עובד על זה...")
    chat = query.message.chat
    await chat.send_action("upload_photo")
    file = await query.message.photo[-1].get_file()
    data = bytes(await file.download_as_bytearray())

    if query.data == "save":
        await chat.send_document(data, filename="edited.jpg", caption="📥 הנה התמונה באיכות מלאה")
        return

    action = query.data.removeprefix("edit:")
    if action not in editor.ACTIONS:
        return
    try:
        result = await asyncio.to_thread(editor.apply, action, data)
    except Exception:
        log.exception("עריכה נכשלה")
        await chat.send_message("❌ משהו השתבש בעריכה. נסה שוב.")
        return
    label = editor.ACTIONS[action][0]
    await chat.send_photo(result, caption=f"{label}\n\n{EDIT_CAPTION}", reply_markup=edit_keyboard())


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
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo))
    app.add_handler(CallbackQueryHandler(on_button))
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
