"""פעולות עריכת תמונה (Pillow)."""

from io import BytesIO

from PIL import Image, ImageFilter, ImageEnhance, ImageOps

MAX_SIDE = 4096  # מקטין תמונות ענקיות כדי שהטלפון לא ייתקע


def _sepia(img: Image.Image) -> Image.Image:
    gray = ImageOps.grayscale(img)
    return ImageOps.colorize(gray, black="#2e1f0f", white="#fff3d6", mid="#a87c4f")


def _square(img: Image.Image) -> Image.Image:
    side = min(img.size)
    return ImageOps.fit(img, (side, side))


def _half(img: Image.Image) -> Image.Image:
    w, h = img.size
    return img.resize((max(1, w // 2), max(1, h // 2)), Image.LANCZOS)


# מפתח -> (תווית לכפתור, פונקציה)
ACTIONS = {
    "none": ("ללא שינוי", lambda i: i),
    "bw": ("⚫ שחור-לבן", lambda i: ImageOps.grayscale(i).convert("RGB")),
    "sepia": ("🟤 ספיה", _sepia),
    "invert": ("🔄 היפוך צבעים", ImageOps.invert),
    "bright": ("☀️ בהיר יותר", lambda i: ImageEnhance.Brightness(i).enhance(1.3)),
    "dark": ("🌙 כהה יותר", lambda i: ImageEnhance.Brightness(i).enhance(0.75)),
    "contrast": ("🌓 ניגודיות", lambda i: ImageEnhance.Contrast(i).enhance(1.4)),
    "color": ("🌈 צבעים חזקים", lambda i: ImageEnhance.Color(i).enhance(1.5)),
    "sharp": ("🔍 חידוד", lambda i: i.filter(ImageFilter.SHARPEN)),
    "blur": ("💧 טשטוש", lambda i: i.filter(ImageFilter.GaussianBlur(4))),
    "rot_l": ("↩️ סיבוב שמאלה", lambda i: i.rotate(90, expand=True)),
    "rot_r": ("↪️ סיבוב ימינה", lambda i: i.rotate(-90, expand=True)),
    "mirror": ("🪞 מראה", ImageOps.mirror),
    "square": ("⬛ חיתוך לריבוע", _square),
    "half": ("📉 הקטנה ב-50%", _half),
}

# סדר הכפתורים: כל רשימה פנימית היא שורה
LAYOUT = [
    ["bw", "sepia", "invert"],
    ["bright", "dark", "contrast"],
    ["color", "sharp", "blur"],
    ["rot_l", "rot_r", "mirror"],
    ["square", "half"],
]


def apply(action: str, data: bytes) -> bytes:
    """מקבל תמונה כבייטים, מפעיל עליה עריכה ומחזיר JPEG כבייטים."""
    img = Image.open(BytesIO(data))
    img = ImageOps.exif_transpose(img)  # מסובב לפי כיוון הצילום
    img = img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    img = ACTIONS[action][1](img)
    out = BytesIO()
    img.save(out, "JPEG", quality=92)
    return out.getvalue()
