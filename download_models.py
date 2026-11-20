"""מוריד את מודלי החלפת הפנים לתיקייה models/ (בערך 750MB להורדה).

הרצה:  python download_models.py
"""

import hashlib
import sys
import urllib.request
import zipfile
from pathlib import Path

MODELS_DIR = Path(__file__).parent / "models"

BUFFALO_URL = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"
SWAPPER_URL = "https://github.com/facefusion/facefusion-assets/releases/download/models-3.0.0/inswapper_128.onnx"

# שם קובץ -> sha256 (לוודא שההורדה תקינה ושזה בדיוק הקובץ שנבדק)
HASHES = {
    "det_10g.onnx": "5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91",
    "w600k_r50.onnx": "4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43",
    "inswapper_128.onnx": "a290273ed497312095dac48cdef20feec9d5208298223dd01288ab202b54bea7",
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

    if not ok("inswapper_128.onnx"):
        print("⬇️  מוריד את מודל החלפת הפנים (~555MB)...")
        download(SWAPPER_URL, MODELS_DIR / "inswapper_128.onnx")
    else:
        print("✅ מודל החלפת הפנים כבר קיים")

    print("🔎 בודק שהקבצים תקינים...")
    bad = [n for n in HASHES if not ok(n)]
    if bad:
        sys.exit(f"❌ קבצים פגומים: {', '.join(bad)}. מחק אותם והרץ שוב.")
    print("✅ כל המודלים מוכנים!")


if __name__ == "__main__":
    main()
