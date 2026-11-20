"""בוט טלגרם להחלפת פנים בתמונות ובסרטונים - מותאם להרצה ב-Termux."""

import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
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

CONSENT = "⚠️ השתמש רק בתמונות וסרטונים של אנשים שהסכימו לכך."
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # בוט יכול להוריד מטלגרם קבצים עד 20MB
TG_UPLOAD_LIMIT = 50 * 1024 * 1024    # ולשלוח עד 50MB

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
        f"שלום {name}! 👋\nאני בוט להחלפת פנים בתמונות ובסרטונים.\n"
        "🔄 /swap - החלפה בתמונה\n"
        "🎬 /swapvideo - החלפה בסרטון\n"
        f"{CONSENT}"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "הפקודות שלי:\n"
        "/start - הודעת פתיחה\n"
        "/help - העזרה הזו\n"
        "/time - השעה עכשיו\n"
        "/id - ה-ID שלך בטלגרם\n"
        "/swap - החלפת פנים בתמונה\n"
        "/swapvideo - החלפת פנים בסרטון (עד 15 שניות)\n"
        "/videomode - מהירות/איכות לסרטונים (fast / quality)\n"
        "/enhance - בחירת מודל חידוד הפנים (gpen / restore / gfpgan / off)\n"
        "/color - כמה מצבע העור של התמונה המקורית להעביר (0-100)\n"
        "/cancel - ביטול החלפה באמצע\n\n"
        "🔄 איך מחליפים פנים:\n"
        "1. שולחים /swap\n"
        "2. שולחים תמונה מקורית - ממנה נלקחות הפנים\n"
        "3. שולחים תמונת יעד - אליה מועברות הפנים\n"
        "מקבלים את תמונת היעד עם הפנים מהתמונה המקורית.\n\n"
        "🎬 בסרטון: /swapvideo, תמונה מקורית, ואז סרטון (עד 20MB, נחתך ל-15 שניות).\n\n"
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


DEFAULT_ENHANCE = "gpen"
ENHANCE_INFO = {
    "gpen": "GPEN - חידוד חזק, הכי חד ומפורט",
    "restore": "RestoreFormer++ - חידוד טבעי",
    "gfpgan": "GFPGAN - חידוד עדין",
    "off": "בלי חידוד - הכי מהיר",
}


async def enhance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # chat_data נשמר גם כש-user_data מתאפס בתחילת כל /swap
    current = context.chat_data.get("enhance", DEFAULT_ENHANCE)
    choice = context.args[0].lower() if context.args else None
    if choice not in ENHANCE_INFO:
        options = "\n".join(f"/enhance {k} - {v}" for k, v in ENHANCE_INFO.items())
        prefix = "❌ אפשרות לא מוכרת.\n\n" if choice else ""
        await update.message.reply_text(
            f"{prefix}✨ חידוד פנים כרגע: {ENHANCE_INFO[current]}\n\nלשינוי:\n{options}"
        )
        return
    context.chat_data["enhance"] = choice
    await update.message.reply_text(f"✨ חידוד פנים: {ENHANCE_INFO[choice]}")


DEFAULT_COLOR = 60  # אחוז העברת צבע העור כברירת מחדל


async def color_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    current = context.chat_data.get("color", DEFAULT_COLOR)
    if not context.args:
        await update.message.reply_text(
            f"🎨 העברת צבע עור: {current}%\n\n"
            "כמה מצבע העור של התמונה המקורית להעביר לתוצאה.\n"
            "0 = הצבע של תמונת היעד, 100 = הצבע של התמונה המקורית.\n\n"
            "לשינוי, שלח למשל:\n/color 60"
        )
        return
    try:
        value = int(context.args[0].strip("%"))
        if not 0 <= value <= 100:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ צריך מספר בין 0 ל-100. למשל: /color 60")
        return
    context.chat_data["color"] = value
    await update.message.reply_text(f"🎨 העברת צבע עור: {value}%")


DEFAULT_VIDEO_MODE = "fast"
VIDEO_MODE_INFO = {
    "fast": "מהיר - 15 פריימים לשנייה, עד 720p, בלי חידוד",
    "quality": "איכותי - עד 30 פריימים לשנייה, עד 1080p, עם החידוד מ-/enhance (איטי מאוד!)",
}


async def videomode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    current = context.chat_data.get("video_mode", DEFAULT_VIDEO_MODE)
    choice = context.args[0].lower() if context.args else None
    if choice not in VIDEO_MODE_INFO:
        options = "\n".join(f"/videomode {k} - {v}" for k, v in VIDEO_MODE_INFO.items())
        prefix = "❌ אפשרות לא מוכרת.\n\n" if choice else ""
        await update.message.reply_text(f"{prefix}🎬 מצב סרטונים כרגע: {VIDEO_MODE_INFO[current]}\n\nלשינוי:\n{options}")
        return
    context.chat_data["video_mode"] = choice
    await update.message.reply_text(f"🎬 מצב סרטונים: {VIDEO_MODE_INFO[choice]}")


async def swapvideo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    context.user_data["step"] = "vsource"
    mode = context.chat_data.get("video_mode", DEFAULT_VIDEO_MODE)
    await update.message.reply_text(
        "🎬 החלפת פנים בסרטון\n\n"
        "1️⃣ שלח עכשיו את התמונה המקורית - ממנה אקח את הפנים.\n\n"
        f"מצב: {VIDEO_MODE_INFO[mode]} (לשינוי: /videomode)\n"
        f"{CONSENT}\n"
        "לביטול: /cancel"
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await update.message.reply_text("❌ בוטל. לחזרה: /swap או /swapvideo\n(סרטון שכבר בעיבוד ימשיך עד הסוף)")


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

    if step == "vsource":
        context.user_data["source"] = await _download(update)
        context.user_data["step"] = "video"
        await msg.reply_text("✅ קיבלתי.\n\n2️⃣ עכשיו שלח את הסרטון - אליו אעביר את הפנים (עד 20MB).")
        return

    if step == "video":
        await msg.reply_text("🎬 עכשיו צריך לשלוח סרטון, לא תמונה. לביטול: /cancel")
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
            enhance = context.chat_data.get("enhance", DEFAULT_ENHANCE)
            enhance = None if enhance == "off" else enhance
            color = context.chat_data.get("color", DEFAULT_COLOR) / 100
            # אם מודל החידוד שנבחר לא הורד - מחליפים בלי חידוד ומודיעים
            missing = enhance if enhance and await asyncio.to_thread(swapper.enhancer, enhance) is None else None
            result, _ = await asyncio.to_thread(swapper.swap_faces, source, target, False, enhance, color)
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
            which = "בתמונה המקורית" if str(e) == "source" else "בתמונת היעד"
            await status.edit_text(f"😕 לא מצאתי פנים {which}. נסה תמונה אחרת: /swap")
        else:
            log.exception("החלפת פנים נכשלה")
            await status.edit_text("❌ משהו השתבש. נסה שוב: /swap")
        return

    await status.delete()
    caption = "✅ הנה! להחלפה נוספת: /swap"
    if missing:
        caption += f"\n\n⚠️ מודל החידוד '{missing}' לא הורד, אז התמונה בלי חידוד. ב-Termux הרץ: python download_models.py"
    await msg.reply_photo(result, caption=caption)


async def on_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if context.user_data.get("step") != "video":
        await msg.reply_text("כדי להחליף פנים בסרטון, שלח קודם /swapvideo")
        return
    media = msg.video or msg.video_note or msg.animation or msg.document
    if media.file_size and media.file_size > TG_DOWNLOAD_LIMIT:
        await msg.reply_text(
            f"😕 הסרטון גדול מדי ({media.file_size / 1024 / 1024:.0f}MB). "
            "טלגרם מאפשר לבוט לקבל עד 20MB. נסה סרטון קצר יותר."
        )
        return

    source = context.user_data["source"]
    context.user_data.clear()
    mode = context.chat_data.get("video_mode", DEFAULT_VIDEO_MODE)
    enhance = context.chat_data.get("enhance", DEFAULT_ENHANCE)
    enhance = None if enhance == "off" else enhance
    color = context.chat_data.get("color", DEFAULT_COLOR) / 100

    workdir = Path(tempfile.mkdtemp(prefix="swapvideo_"))
    in_path, out_path = workdir / "in.mp4", workdir / "out.mp4"
    status = await msg.reply_text("📥 מוריד את הסרטון...")
    try:
        await (await media.get_file()).download_to_drive(in_path)
        if _swap_lock.locked():
            await status.edit_text("⏳ ממתין שעבודה קודמת תסתיים...")
        async with _swap_lock:  # אחד בכל פעם, כדי לא להעמיס על הטלפון
            await status.edit_text("⏳ טוען מודלים...")
            swapper = await asyncio.to_thread(_load_swapper)
            import video  # noqa: E402 - נטען רק כשצריך

            src = await asyncio.to_thread(swapper.prepare_source, source)
            loop = asyncio.get_running_loop()
            started, last = time.monotonic(), [0.0, -1]
            pending = []  # עדכוני התקדמות שעוד לא נשלחו

            def progress(done: int, total: int) -> None:
                # מעדכן את ההודעה לכל היותר כל 5 שניות
                now = time.monotonic()
                pct = min(99, done * 100 // total)
                if now - last[0] < 5 or pct == last[1]:
                    return
                last[0], last[1] = now, pct
                left = (now - started) / done * max(0, total - done)
                text = f"🎬 מחליף פנים בסרטון... {pct}%\n({done}/{total} פריימים, נשארו בערך {left / 60:.0f} דק')"
                pending.append(asyncio.run_coroutine_threadsafe(status.edit_text(text), loop))

            try:
                info = await asyncio.to_thread(video.swap_video, swapper, src, in_path, out_path,
                                               mode, enhance, color, progress)
            finally:
                # מחכים שעדכוני ההתקדמות יסתיימו, כדי שלא ידרסו את ההודעה הבאה
                await asyncio.gather(*(asyncio.wrap_future(f) for f in pending), return_exceptions=True)
        took = (time.monotonic() - started) / 60
        if out_path.stat().st_size > TG_UPLOAD_LIMIT:
            await status.edit_text("😕 הסרטון שיצא גדול מ-50MB, וטלגרם לא מאפשר לבוט לשלוח אותו. נסה סרטון קצר יותר.")
            return
        await status.edit_text("📤 שולח...")
        await msg.chat.send_action("upload_video")
        caption = f"✅ הנה! ({info['frames']} פריימים, {took:.1f} דק')"
        if info["trimmed"]:
            caption += f"\n✂️ הסרטון נחתך ל-{video.MAX_SECONDS} השניות הראשונות."
        if info["swapped"] < info["frames"]:
            caption += f"\nℹ️ ב-{info['frames'] - info['swapped']} פריימים לא זוהו פנים, והם נשארו כמו שהם."
        caption += "\n\nלסרטון נוסף: /swapvideo"
        with out_path.open("rb") as f:
            await msg.reply_video(f, caption=caption, supports_streaming=True,
                                  read_timeout=300, write_timeout=300)
        await status.delete()
    except FileNotFoundError as e:
        log.error("חסרים קבצי מודלים: %s", e)
        await status.edit_text(f"❌ חסרים קבצי המודלים: {e}\n\nב-Termux הרץ: python download_models.py")
    except Exception as e:
        name = type(e).__name__
        if name == "FfmpegMissing":
            await status.edit_text("❌ חסר ffmpeg. ב-Termux עצור את הבוט (Ctrl+C) והרץ:\npkg install ffmpeg")
        elif name == "NoFaceError":
            await status.edit_text("😕 לא מצאתי פנים בתמונה המקורית. נסה תמונה אחרת: /swapvideo")
        elif name == "NoFaceInVideo":
            await status.edit_text("😕 לא מצאתי פנים בסרטון. נסה סרטון אחר: /swapvideo")
        else:
            log.exception("החלפת פנים בסרטון נכשלה")
            await status.edit_text("❌ משהו השתבש בעיבוד הסרטון. נסה שוב: /swapvideo")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


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
    app.add_handler(CommandHandler("enhance", enhance_cmd))
    app.add_handler(CommandHandler("color", color_cmd))
    app.add_handler(CommandHandler("swapvideo", swapvideo_cmd))
    app.add_handler(CommandHandler("videomode", videomode_cmd))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo))
    app.add_handler(MessageHandler(
        filters.VIDEO | filters.VIDEO_NOTE | filters.ANIMATION | filters.Document.VIDEO, on_video))
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
