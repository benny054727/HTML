"""בוט טלגרם להחלפת פנים בתמונות ובסרטונים - מותאם להרצה ב-Termux."""

import asyncio
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest, InvalidToken
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

CONSENT = "⚠️ השתמש רק בתמונות וסרטונים של אנשים שהסכימו לכך."
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # בוט יכול להוריד מטלגרם קבצים עד 20MB
TG_UPLOAD_LIMIT = 50 * 1024 * 1024    # ולשלוח עד 50MB

HOME_TEXT = "🏠 תפריט ראשי"  # הכפתור הקבוע בתחתית המסך

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


# ---------- הגדרות ----------

DEFAULT_ENHANCE = "gpen"
ENHANCE_INFO = {
    "gpen": "GPEN - חידוד חזק, הכי חד ומפורט",
    "restore": "RestoreFormer++ - חידוד טבעי",
    "gfpgan": "GFPGAN - חידוד עדין",
    "off": "בלי חידוד - הכי מהיר",
}
ENHANCE_SHORT = {"gpen": "GPEN", "restore": "RestoreFormer", "gfpgan": "GFPGAN", "off": "כבוי"}

DEFAULT_COLOR = 60  # אחוז העברת צבע העור כברירת מחדל
COLOR_CHOICES = (0, 30, 60, 100)

DEFAULT_VIDEO_MODE = "fast"
VIDEO_MODE_INFO = {
    "fast": "מהיר - 15 פריימים לשנייה, עד 720p, בלי חידוד",
    "quality": "איכותי - עד 30 פריימים לשנייה, עד 1080p, עם החידוד שנבחר (איטי מאוד!)",
}
VIDEO_MODE_SHORT = {"fast": "מהיר", "quality": "איכותי"}


def _enhance(context) -> str:
    return context.chat_data.get("enhance", DEFAULT_ENHANCE)


def _color(context) -> int:
    return context.chat_data.get("color", DEFAULT_COLOR)


def _video_mode(context) -> str:
    return context.chat_data.get("video_mode", DEFAULT_VIDEO_MODE)


# ---------- מקלדות (כפתורים) ----------

def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


HOME_KEYBOARD = ReplyKeyboardMarkup([[KeyboardButton(HOME_TEXT)]], resize_keyboard=True, is_persistent=True)
CANCEL_KB = InlineKeyboardMarkup([[btn("❌ ביטול", "cancel")]])
STOP_KB = InlineKeyboardMarkup([[btn("⛔ עצור עיבוד", "stop")]])
BACK_HOME_KB = InlineKeyboardMarkup([[btn("🏠 תפריט ראשי", "home")]])


def home_keyboard(context) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [btn("📸 החלפה בתמונה", "go:photo"), btn("🎬 החלפה בסרטון", "go:video")],
        [btn(f"✨ חידוד: {ENHANCE_SHORT[_enhance(context)]}", "menu:enhance"),
         btn(f"🎨 צבע עור: {_color(context)}%", "menu:color")],
        [btn(f"⚙️ סרטון: {VIDEO_MODE_SHORT[_video_mode(context)]}", "menu:vmode"), btn("❓ עזרה", "help")],
    ])


def after_result_kb(kind: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[btn("🔁 עוד אחד", f"go:{kind}"), btn("🏠 תפריט ראשי", "home")]])


def _mark(selected: bool, text: str) -> str:
    return f"✅ {text}" if selected else text


def settings_menu(context, which: str) -> tuple[str, InlineKeyboardMarkup]:
    back = [btn("⬅️ חזרה", "home")]
    if which == "enhance":
        cur = _enhance(context)
        rows = [[btn(_mark(k == cur, v), f"set:enhance:{k}")] for k, v in ENHANCE_INFO.items()]
        text = "✨ בחר חידוד פנים (אחרי ההחלפה):"
    elif which == "color":
        cur = _color(context)
        rows = [[btn(_mark(v == cur, f"{v}%"), f"set:color:{v}") for v in COLOR_CHOICES]]
        text = ("🎨 כמה מצבע העור של התמונה המקורית להעביר?\n"
                "0% = הצבע של תמונת היעד, 100% = הצבע של התמונה המקורית.\n"
                "(ערך אחר: /color 45)")
    else:
        cur = _video_mode(context)
        rows = [[btn(_mark(k == cur, v), f"set:vmode:{k}")] for k, v in VIDEO_MODE_INFO.items()]
        text = "⚙️ בחר מצב לסרטונים:"
    return text, InlineKeyboardMarkup(rows + [back])


HELP_TEXT = (
    "❓ איך משתמשים:\n\n"
    "📸 החלפה בתמונה:\n"
    "1. לוחצים 'החלפה בתמונה'\n"
    "2. שולחים תמונה מקורית - ממנה נלקחות הפנים\n"
    "3. שולחים תמונת יעד - אליה מועברות הפנים\n\n"
    "🎬 החלפה בסרטון:\n"
    "1. לוחצים 'החלפה בסרטון'\n"
    "2. שולחים תמונה מקורית\n"
    "3. שולחים סרטון (עד 20MB, נחתך ל-15 שניות)\n"
    "אפשר לעצור עיבוד באמצע עם '⛔ עצור עיבוד'.\n\n"
    "⚙️ ההגדרות (חידוד, צבע עור, מצב סרטון) נשמרות עד שמשנים אותן.\n\n"
    "פקודות: /swap /swapvideo /enhance /color /videomode /cancel\n\n"
    f"{CONSENT}"
)


# ---------- דף הבית ----------

async def show_home(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False) -> None:
    text = "🏠 מה תרצה לעשות?"
    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=home_keyboard(context))
            return
        except BadRequest:
            pass  # הודעה עם תמונה/סרטון אי אפשר לערוך לטקסט - שולחים חדשה
    await update.effective_chat.send_message(text, reply_markup=home_keyboard(context))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = update.effective_user.first_name
    await update.message.reply_text(
        f"שלום {name}! 👋\nאני בוט להחלפת פנים בתמונות ובסרטונים.\n{CONSENT}",
        reply_markup=HOME_KEYBOARD,
    )
    await show_home(update, context)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, reply_markup=BACK_HOME_KB)


async def time_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    await update.message.reply_text(f"🕒 {now}")


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"ה-ID שלך: {update.effective_user.id}\nה-ID של הצ'אט: {update.effective_chat.id}"
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """כל טקסט (כולל הכפתור הקבוע '🏠 תפריט ראשי') - מציג את דף הבית."""
    if update.message.text.strip() != HOME_TEXT and context.user_data.get("step"):
        await update.message.reply_text("📎 מחכה לקובץ. לביטול:", reply_markup=CANCEL_KB)
        return
    await show_home(update, context)


# ---------- התחלת החלפה ----------

async def begin_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str) -> None:
    context.user_data.clear()
    if kind == "photo":
        context.user_data["step"] = "source"
        text = ("📸 החלפת פנים בתמונה\n\n"
                "1️⃣ שלח עכשיו את התמונה המקורית - ממנה אקח את הפנים.\n\n"
                f"{CONSENT}")
    else:
        context.user_data["step"] = "vsource"
        text = ("🎬 החלפת פנים בסרטון\n\n"
                "1️⃣ שלח עכשיו את התמונה המקורית - ממנה אקח את הפנים.\n\n"
                f"מצב: {VIDEO_MODE_INFO[_video_mode(context)]}\n"
                f"{CONSENT}")
    await update.effective_chat.send_message(text, reply_markup=CANCEL_KB)


async def swap_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await begin_flow(update, context, "photo")


async def swapvideo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await begin_flow(update, context, "video")


def _stop_running_video(context) -> bool:
    event = context.chat_data.get("video_cancel")
    if event is not None and not event.is_set():
        event.set()
        return True
    return False


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    stopped = _stop_running_video(context)
    text = "⛔ עוצר את עיבוד הסרטון..." if stopped else "❌ בוטל."
    await update.message.reply_text(text, reply_markup=BACK_HOME_KB)


# ---------- פקודות הגדרות (עובדות גם בלי הכפתורים) ----------

async def enhance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    choice = context.args[0].lower() if context.args else None
    if choice in ENHANCE_INFO:
        context.chat_data["enhance"] = choice
        await update.message.reply_text(f"✨ חידוד פנים: {ENHANCE_INFO[choice]}", reply_markup=BACK_HOME_KB)
        return
    text, kb = settings_menu(context, "enhance")
    await update.message.reply_text(("❌ אפשרות לא מוכרת.\n\n" if choice else "") + text, reply_markup=kb)


async def color_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        text, kb = settings_menu(context, "color")
        await update.message.reply_text(f"🎨 כרגע: {_color(context)}%\n\n{text}", reply_markup=kb)
        return
    try:
        value = int(context.args[0].strip("%"))
        if not 0 <= value <= 100:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ צריך מספר בין 0 ל-100. למשל: /color 60")
        return
    context.chat_data["color"] = value
    await update.message.reply_text(f"🎨 העברת צבע עור: {value}%", reply_markup=BACK_HOME_KB)


async def videomode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    choice = context.args[0].lower() if context.args else None
    if choice in VIDEO_MODE_INFO:
        context.chat_data["video_mode"] = choice
        await update.message.reply_text(f"⚙️ מצב סרטונים: {VIDEO_MODE_INFO[choice]}", reply_markup=BACK_HOME_KB)
        return
    text, kb = settings_menu(context, "vmode")
    await update.message.reply_text(("❌ אפשרות לא מוכרת.\n\n" if choice else "") + text, reply_markup=kb)


# ---------- לחיצות על כפתורים ----------

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""

    if data == "stop":
        stopped = _stop_running_video(context)
        await query.answer("⛔ עוצר..." if stopped else "אין עיבוד פעיל")
        return

    await query.answer()

    if data == "home":
        await show_home(update, context, edit=True)
    elif data.startswith("go:"):
        await begin_flow(update, context, data[3:])
    elif data.startswith("menu:"):
        text, kb = settings_menu(context, data[5:])
        await query.edit_message_text(text, reply_markup=kb)
    elif data.startswith("set:"):
        _, key, value = data.split(":", 2)
        if key == "enhance" and value in ENHANCE_INFO:
            context.chat_data["enhance"] = value
        elif key == "color" and value.isdigit():
            context.chat_data["color"] = int(value)
        elif key == "vmode" and value in VIDEO_MODE_INFO:
            context.chat_data["video_mode"] = value
        # מציגים שוב את אותו תפריט, עם ✅ על הבחירה החדשה
        text, kb = settings_menu(context, {"enhance": "enhance", "color": "color", "vmode": "vmode"}[key])
        try:
            await query.edit_message_text(text, reply_markup=kb)
        except BadRequest:
            pass  # לחצו על מה שכבר נבחר - אין מה לעדכן
    elif data == "help":
        await query.edit_message_text(HELP_TEXT, reply_markup=BACK_HOME_KB)
    elif data == "cancel":
        context.user_data.clear()
        await query.edit_message_text("❌ בוטל.", reply_markup=BACK_HOME_KB)


# ---------- קבלת תמונות ----------

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
        await msg.reply_text("✅ קיבלתי.\n\n2️⃣ עכשיו שלח את תמונת היעד - אליה אעביר את הפנים.",
                             reply_markup=CANCEL_KB)
        return

    if step == "vsource":
        context.user_data["source"] = await _download(update)
        context.user_data["step"] = "video"
        await msg.reply_text("✅ קיבלתי.\n\n2️⃣ עכשיו שלח את הסרטון - אליו אעביר את הפנים (עד 20MB).",
                             reply_markup=CANCEL_KB)
        return

    if step == "video":
        await msg.reply_text("🎬 עכשיו צריך לשלוח סרטון, לא תמונה.", reply_markup=CANCEL_KB)
        return

    if step != "target":
        await msg.reply_text("כדי להחליף פנים, בחר קודם מה לעשות:", reply_markup=home_keyboard(context))
        return

    source = context.user_data["source"]
    target = await _download(update)
    context.user_data.clear()

    status = await msg.reply_text("⏳ מחליף פנים... (בפעם הראשונה זה לוקח קצת יותר, כי המודלים נטענים)")
    await msg.chat.send_action("upload_photo")
    try:
        async with _swap_lock:  # אחד בכל פעם, כדי לא להעמיס על הטלפון
            swapper = await asyncio.to_thread(_load_swapper)
            enhance = _enhance(context)
            enhance = None if enhance == "off" else enhance
            color = _color(context) / 100
            # אם מודל החידוד שנבחר לא הורד - מחליפים בלי חידוד ומודיעים
            missing = enhance if enhance and await asyncio.to_thread(swapper.enhancer, enhance) is None else None
            result, _ = await asyncio.to_thread(swapper.swap_faces, source, target, False, enhance, color)
    except FileNotFoundError as e:
        log.error("חסרים קבצי מודלים: %s", e)
        await status.edit_text(
            f"❌ חסרים קבצי המודלים: {e}\n\n"
            "ב-Termux עצור את הבוט (Ctrl+C) והרץ:\n"
            "python download_models.py",
            reply_markup=BACK_HOME_KB,
        )
        return
    except Exception as e:
        if type(e).__name__ == "NoFaceError":
            which = "בתמונה המקורית" if str(e) == "source" else "בתמונת היעד"
            await status.edit_text(f"😕 לא מצאתי פנים {which}. נסה תמונה אחרת.",
                                   reply_markup=after_result_kb("photo"))
        else:
            log.exception("החלפת פנים נכשלה")
            await status.edit_text("❌ משהו השתבש. נסה שוב.", reply_markup=after_result_kb("photo"))
        return

    await status.delete()
    caption = "✅ הנה!"
    if missing:
        caption += f"\n\n⚠️ מודל החידוד '{missing}' לא הורד, אז התמונה בלי חידוד. ב-Termux הרץ: python download_models.py"
    await msg.reply_photo(result, caption=caption, reply_markup=after_result_kb("photo"))


# ---------- קבלת סרטונים ----------

async def on_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if context.user_data.get("step") != "video":
        await msg.reply_text("כדי להחליף פנים בסרטון, בחר קודם מה לעשות:", reply_markup=home_keyboard(context))
        return
    media = msg.video or msg.video_note or msg.animation or msg.document
    if media.file_size and media.file_size > TG_DOWNLOAD_LIMIT:
        await msg.reply_text(
            f"😕 הסרטון גדול מדי ({media.file_size / 1024 / 1024:.0f}MB). "
            "טלגרם מאפשר לבוט לקבל עד 20MB. שלח סרטון קצר יותר.",
            reply_markup=CANCEL_KB,
        )
        return

    source = context.user_data["source"]
    context.user_data.clear()
    mode = _video_mode(context)
    enhance = _enhance(context)
    enhance = None if enhance == "off" else enhance
    color = _color(context) / 100
    cancel = threading.Event()
    context.chat_data["video_cancel"] = cancel

    workdir = Path(tempfile.mkdtemp(prefix="swapvideo_"))
    in_path, out_path = workdir / "in.mp4", workdir / "out.mp4"
    status = await msg.reply_text("📥 מוריד את הסרטון...", reply_markup=STOP_KB)
    try:
        await (await media.get_file()).download_to_drive(in_path)
        if _swap_lock.locked():
            await status.edit_text("⏳ ממתין שעבודה קודמת תסתיים...", reply_markup=STOP_KB)
        async with _swap_lock:  # אחד בכל פעם, כדי לא להעמיס על הטלפון
            if cancel.is_set():
                raise _Stopped()
            await status.edit_text("⏳ טוען מודלים...", reply_markup=STOP_KB)
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
                if cancel.is_set() or now - last[0] < 5 or pct == last[1]:
                    return
                last[0], last[1] = now, pct
                left = (now - started) / done * max(0, total - done)
                text = f"🎬 מחליף פנים בסרטון... {pct}%\n({done}/{total} פריימים, נשארו בערך {left / 60:.0f} דק')"
                pending.append(asyncio.run_coroutine_threadsafe(
                    status.edit_text(text, reply_markup=STOP_KB), loop))

            try:
                info = await asyncio.to_thread(video.swap_video, swapper, src, in_path, out_path,
                                               mode, enhance, color, progress, cancel)
            finally:
                # מחכים שעדכוני ההתקדמות יסתיימו, כדי שלא ידרסו את ההודעה הבאה
                await asyncio.gather(*(asyncio.wrap_future(f) for f in pending), return_exceptions=True)
        took = (time.monotonic() - started) / 60
        if out_path.stat().st_size > TG_UPLOAD_LIMIT:
            await status.edit_text("😕 הסרטון שיצא גדול מ-50MB, וטלגרם לא מאפשר לבוט לשלוח אותו. נסה סרטון קצר יותר.",
                                   reply_markup=after_result_kb("video"))
            return
        await status.edit_text("📤 שולח...")
        await msg.chat.send_action("upload_video")
        caption = f"✅ הנה! ({info['frames']} פריימים, {took:.1f} דק')"
        if info["trimmed"]:
            caption += f"\n✂️ הסרטון נחתך ל-{video.MAX_SECONDS} השניות הראשונות."
        if info["swapped"] < info["frames"]:
            caption += f"\nℹ️ ב-{info['frames'] - info['swapped']} פריימים לא זוהו פנים, והם נשארו כמו שהם."
        with out_path.open("rb") as f:
            await msg.reply_video(f, caption=caption, supports_streaming=True,
                                  read_timeout=300, write_timeout=300, reply_markup=after_result_kb("video"))
        await status.delete()
    except FileNotFoundError as e:
        log.error("חסרים קבצי מודלים: %s", e)
        await status.edit_text(f"❌ חסרים קבצי המודלים: {e}\n\nב-Termux הרץ: python download_models.py",
                               reply_markup=BACK_HOME_KB)
    except Exception as e:
        name = type(e).__name__
        if name in ("Cancelled", "_Stopped"):
            await status.edit_text("⛔ העיבוד נעצר.", reply_markup=after_result_kb("video"))
        elif name == "FfmpegMissing":
            await status.edit_text("❌ חסר ffmpeg. ב-Termux עצור את הבוט (Ctrl+C) והרץ:\npkg install ffmpeg",
                                   reply_markup=BACK_HOME_KB)
        elif name == "NoFaceError":
            await status.edit_text("😕 לא מצאתי פנים בתמונה המקורית. נסה תמונה אחרת.",
                                   reply_markup=after_result_kb("video"))
        elif name == "NoFaceInVideo":
            await status.edit_text("😕 לא מצאתי פנים בסרטון. נסה סרטון אחר.",
                                   reply_markup=after_result_kb("video"))
        else:
            log.exception("החלפת פנים בסרטון נכשלה")
            await status.edit_text("❌ משהו השתבש בעיבוד הסרטון. נסה שוב.",
                                   reply_markup=after_result_kb("video"))
    finally:
        if context.chat_data.get("video_cancel") is cancel:
            context.chat_data.pop("video_cancel", None)
        shutil.rmtree(workdir, ignore_errors=True)


class _Stopped(Exception):
    """עצרו את הסרטון עוד לפני שהעיבוד התחיל."""


# ---------- הפעלה ----------

COMMANDS = [
    BotCommand("start", "דף הבית"),
    BotCommand("swap", "החלפת פנים בתמונה"),
    BotCommand("swapvideo", "החלפת פנים בסרטון"),
    BotCommand("enhance", "בחירת חידוד פנים"),
    BotCommand("color", "העברת צבע עור (0-100)"),
    BotCommand("videomode", "מצב סרטונים: מהיר/איכותי"),
    BotCommand("cancel", "ביטול / עצירת עיבוד"),
    BotCommand("help", "עזרה"),
]


async def post_init(app: Application) -> None:
    # רשימת הפקודות שמופיעה כשמקלידים / בטלגרם
    try:
        await app.bot.set_my_commands(COMMANDS)
    except Exception:
        log.warning("לא הצלחתי לעדכן את רשימת הפקודות", exc_info=True)


def main() -> None:
    load_env()
    # מנקה תווים נסתרים שנכנסים לפעמים בהדבקה (למשל ב-Termux)
    match = re.search(r"\d+:[A-Za-z0-9_-]{30,}", os.environ.get("BOT_TOKEN", ""))
    token = match.group(0) if match else None
    if not token:
        raise SystemExit("❌ חסר BOT_TOKEN. צור קובץ .env (ראה .env.example) ושים בו את הטוקן.")

    # concurrent_updates: כדי שאפשר יהיה ללחוץ 'עצור' בזמן שסרטון בעיבוד
    app = Application.builder().token(token).concurrent_updates(True).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("time", time_cmd))
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(CommandHandler("swap", swap_cmd))
    app.add_handler(CommandHandler("swapvideo", swapvideo_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("enhance", enhance_cmd))
    app.add_handler(CommandHandler("color", color_cmd))
    app.add_handler(CommandHandler("videomode", videomode_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo))
    app.add_handler(MessageHandler(
        filters.VIDEO | filters.VIDEO_NOTE | filters.ANIMATION | filters.Document.VIDEO, on_video))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("הבוט רץ! לעצירה: Ctrl+C")
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except InvalidToken:
        raise SystemExit(
            "❌ טלגרם דחה את הטוקן. בדוק ב-.env שהדבקת את הטוקן העדכני מ-@BotFather, בלי רווחים או תווים מיותרים."
        ) from None


if __name__ == "__main__":
    main()
