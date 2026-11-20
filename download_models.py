"""מוריד את מודלי החלפת הפנים לתיקייה models/ (בערך 1GB להורדה).

הרצה:  python download_models.py
"""

import hashlib
import sys
import urllib.request
import zipfile
from pathlib import Path

MODELS_DIR = Path(__file__).parent / "models"

BUFFALO_URL = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"
ASSETS = "https://github.com/facefusion/facefusion-assets/releases/download/models-3.0.0/"
SWAPPER_URL = "https://github.com/facefusion/facefusion-assets/releases/download/models-3.3.0/hyperswap_1a_256.onnx"
GFPGAN_URL = ASSETS + "gfpgan_1.4.onnx"

# שם קובץ -> sha256 (לוודא שההורדה תקינה ושזה בדיוק הקובץ שנבדק)
HASHES = {
    "det_10g.onnx": "5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91",
    "w600k_r50.onnx": "4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43",
    "hyperswap_1a_256.onnx": "c0e98a8a03a238f461ed3d2570e426b49f46745ee400854a60dceeb70c246add",
    "gfpgan_1.4.onnx": "accc4757b26bdb89b32b4d3500d4f79c9dff97c1dd7c7104bf9dcb95e3311385",
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ok(name: str) -> bool:
    path = MODELS_DIR / name
    return path.exists() and sha256(path) == HASHES[name]


def download(url: str, dest: Path) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url) as r, tmp.open("wb") as f:
        total = int(r.headers.get("Content-Length", 0))
        done = 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  {done >> 20}/{total >> 20} MB ({done * 100 // total}%)", end="", flush=True)
    print()
    tmp.rename(dest)


def main() -> None:
    MODELS_DIR.mkdir(exist_ok=True)

    if not (ok("det_10g.onnx") and ok("w600k_r50.onnx")):
        print("⬇️  מוריד מודלים לזיהוי פנים (~290MB)...")
        zip_path = MODELS_DIR / "buffalo_l.zip"
        download(BUFFALO_URL, zip_path)
        with zipfile.ZipFile(zip_path) as z:
            for name in ("det_10g.onnx", "w600k_r50.onnx"):
                (MODELS_DIR / name).write_bytes(z.read(name))
        zip_path.unlink()
    else:
        print("✅ מודלי זיהוי פנים כבר קיימים")

    if not ok("hyperswap_1a_256.onnx"):
        print("⬇️  מוריד את מודל החלפת הפנים HyperSwap (~400MB)...")
        download(SWAPPER_URL, MODELS_DIR / "hyperswap_1a_256.onnx")
    else:
        print("✅ מודל החלפת הפנים כבר קיים")

    if not ok("gfpgan_1.4.onnx"):
        print("⬇️  מוריד את מודל שחזור הפנים GFPGAN (~340MB)...")
        download(GFPGAN_URL, MODELS_DIR / "gfpgan_1.4.onnx")
    else:
        print("✅ מודל שחזור הפנים כבר קיים")

    print("🔎 בודק שהקבצים תקינים...")
    bad = [n for n in HASHES if not ok(n)]
    if bad:
        sys.exit(f"❌ קבצים פגומים: {', '.join(bad)}. מחק אותם והרץ שוב.")
    print("✅ כל המודלים מוכנים!")

    old = MODELS_DIR / "inswapper_128.onnx"
    if old.exists():
        print("💡 המודל הישן inswapper_128.onnx כבר לא בשימוש. למחיקה (חוסך ~555MB):")
        print("   rm models/inswapper_128.onnx")


if __name__ == "__main__":
    main()
