"""בוט טלגרם להחלפת פנים בתמונות - מותאם להרצה ב-Termux."""

import asyncio
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

CONSENT = "⚠️ השתמש רק בתמונות של אנשים שהסכימו לכך."

# המודלים נטענים פעם אחת, בפעם הראשונה שמישהו מבקש החלפה
_swapper = None
_swap_lock = asyncio.Lock()


def _load_swapper():
    global _swapper
    if _swapper is None:
        import faceswap  # נטען רק כשצריך, כי זה כבד

        _swapper = faceswap.FaceSwapper()
    return _swapper


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
        f"שלום {name}! 👋\nאני בוט להחלפת פנים בתמונות.\n"
        "🔄 שלח /swap כדי להתחיל.\n"
        f"{CONSENT}"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "הפקודות שלי:\n"
        "/start - הודעת פתיחה\n"
        "/help - העזרה הזו\n"
        "/time - השעה עכשיו\n"
        "/id - ה-ID שלך בטלגרם\n"
        "/swap - החלפת פנים\n"
        "/cancel - ביטול החלפה באמצע\n\n"
        "🔄 איך מחליפים פנים:\n"
        "1. שולחים /swap\n"
        "2. שולחים תמונה מקורית - ממנה נלקחות הפנים\n"
        "3. שולחים תמונת יעד - אליה מועברות הפנים\n"
        "מקבלים את תמונת היעד עם הפנים מהתמונה המקורית.\n\n"
        f"{CONSENT}"
    )


async def time_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    await update.message.reply_text(f"🕒 {now}")


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"ה-ID שלך: {update.effective_user.id}\nה-ID של הצ'אט: {update.effective_chat.id}"
    )


async def swap_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    context.user_data["step"] = "source"
    await update.message.reply_text(
        "🔄 החלפת פנים\n\n"
        "1️⃣ שלח עכשיו את התמונה המקורית - ממנה אקח את הפנים.\n\n"
        f"{CONSENT}\n"
        "לביטול: /cancel"
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await update.message.reply_text("❌ בוטל. לחזרה: /swap")


async def _download(update: Update) -> bytes:
    msg = update.message
    file = await (msg.photo[-1] if msg.photo else msg.document).get_file()
    return bytes(await file.download_as_bytearray())


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    step = context.user_data.get("step")

    if step == "source":
        context.user_data["source"] = await _download(update)
        context.user_data["step"] = "target"
        await msg.reply_text("✅ קיבלתי.\n\n2️⃣ עכשיו שלח את תמונת היעד - אליה אעביר את הפנים.")
        return

    if step != "target":
        await msg.reply_text("כדי להחליף פנים, שלח קודם /swap")
        return

    source = context.user_data["source"]
    target = await _download(update)
    context.user_data.clear()

    status = await msg.reply_text("⏳ מחליף פנים... (בפעם הראשונה זה לוקח קצת יותר, כי המודלים נטענים)")
    await msg.chat.send_action("upload_photo")
    try:
        async with _swap_lock:  # אחד בכל פעם, כדי לא להעמיס על הטלפון
            swapper = await asyncio.to_thread(_load_swapper)
            result, _ = await asyncio.to_thread(swapper.swap_faces, source, target, False)
    except FileNotFoundError as e:
        log.error("חסרים קבצי מודלים: %s", e)
        await status.edit_text(
            f"❌ חסרים קבצי המודלים: {e}\n\n"
            "ב-Termux עצור את הבוט (Ctrl+C) והרץ:\n"
            "python download_models.py"
        )
        return
    except Exception as e:
        if type(e).__name__ == "NoFaceError":
            which = "המקורית" if str(e) == "source" else "היעד"
            await status.edit_text(f"😕 לא מצאתי פנים בתמונה {which}. נסה תמונה אחרת: /swap")
        else:
            log.exception("החלפת פנים נכשלה")
            await status.edit_text("❌ משהו השתבש. נסה שוב: /swap")
        return

    await status.delete()
    await msg.reply_photo(result, caption="✅ הנה! להחלפה נוספת: /swap")


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
    app.add_handler(CommandHandler("swap", swap_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo))
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
