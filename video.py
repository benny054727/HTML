"""החלפת פנים בסרטון - מפרקים לפריימים עם ffmpeg, מחליפים בכל פריים, ומרכיבים בחזרה עם הסאונד."""

import json
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image

import faceswap as fs

MAX_SECONDS = 15  # סרטון ארוך יותר נחתך

# fast: מהיר (10 פריימים לשנייה, עד 720p, בלי חידוד)
# quality: איכותי (עד 30 פריימים לשנייה, עד 1080p, עם מודל החידוד שנבחר ב-/enhance)
MODES = {
    "fast": {"fps": 10, "short_side": 720, "enhance": False},
    "quality": {"fps": 30, "short_side": 1080, "enhance": True},
}

KPS_SMOOTH = 0.6   # כמה לסמוך על המיקום החדש של הפנים (פחות = פחות רעידות, אבל יותר "גרירה")
STATS_SMOOTH = 0.2  # כמה מהר צבע העור מתעדכן בין פריימים (נמוך = בלי הבהובים)
HOLD_FRAMES = 3    # כמה פריימים להמשיך עם המיקום האחרון אם הפנים לא זוהו לרגע
GLASSES_CHECKS = 5  # בכמה פריימים לאורך הסרטון לבדוק אם יש משקפיים


class FfmpegMissing(Exception):
    """ffmpeg לא מותקן."""


class NoFaceInVideo(Exception):
    """לא נמצאו פנים באף פריים."""


class Cancelled(Exception):
    """המשתמש עצר את העיבוד."""


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    except FileNotFoundError:
        raise FfmpegMissing() from None


def probe(path: Path) -> dict:
    """מידע על הסרטון: רוחב, גובה (אחרי סיבוב), fps, אורך, האם יש סאונד."""
    info = json.loads(_run(["ffprobe", "-v", "error", "-print_format", "json",
                            "-show_streams", "-show_format", str(path)]))
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if video is None:
        raise ValueError("no video stream")
    w, h = int(video["width"]), int(video["height"])
    # סרטונים מהטלפון שמורים לפעמים "על הצד" עם הוראת סיבוב
    rotation = 0
    for sd in video.get("side_data_list", []):
        rotation = int(sd.get("rotation", rotation))
    rotation = int(video.get("tags", {}).get("rotate", rotation))
    if abs(rotation) % 180 == 90:
        w, h = h, w
    num, den = (video.get("avg_frame_rate") or video.get("r_frame_rate") or "30/1").split("/")
    fps = float(num) / float(den) if float(den) else 30.0
    duration = float(info.get("format", {}).get("duration") or video.get("duration") or 0)
    has_audio = any(s["codec_type"] == "audio" for s in info["streams"])
    return {"width": w, "height": h, "fps": fps or 30.0, "duration": duration, "has_audio": has_audio}


def _out_size(w: int, h: int, short_side: int) -> tuple[int, int]:
    scale = min(1.0, short_side / min(w, h))
    return max(2, int(w * scale) // 2 * 2), max(2, int(h * scale) // 2 * 2)


def preview_frame(swapper: fs.FaceSwapper, src: fs.Source, in_path: Path, mode: str = "fast",
                  enhance: str | None = None, color: float = 0.6,
                  glasses: str = "auto") -> tuple[bytes, float, int, bool]:
    """מחליף פנים בפריים אחד (הראשון שיש בו פנים) - לתצוגה מקדימה לפני כל הסרטון.

    glasses: "auto" = בודקים אם יש משקפיים לאורך הסרטון; "always" = תמיד מוחקים.
    מחזיר (JPEG, כמה שניות לקח הפריים, כמה פריימים יהיו בסרטון, האם למחוק משקפיים).
    """
    import time
    from io import BytesIO

    cfg = MODES[mode]
    info = probe(in_path)
    fps = min(cfg["fps"], info["fps"])
    w, h = _out_size(info["width"], info["height"], cfg["short_side"])
    seconds = min(info["duration"] or MAX_SECONDS, MAX_SECONDS)
    total = max(1, int(round(seconds * fps)))
    enhance = enhance if cfg["enhance"] else None

    # עוברים על הסרטון בפריים אחד לשנייה: הפריים הראשון עם פנים -> לתצוגה המקדימה,
    # ובודקים משקפיים בכמה פריימים לאורך הסרטון (אם באחד מהם יש - מוחקים בכל הסרטון)
    decoder = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-t", str(MAX_SECONDS), "-i", str(in_path),
         "-vf", f"fps=1,scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    first = None
    remove_glasses = glasses == "always"
    checked = 0
    try:
        while True:
            raw = decoder.stdout.read(w * h * 3)
            if len(raw) < w * h * 3:
                break
            img = Image.frombytes("RGB", (w, h), raw)
            faces = swapper.detect(np.asarray(img))
            if not faces:
                continue
            if first is None:
                first = (img, faces[:1])
            if remove_glasses or checked >= GLASSES_CHECKS:
                break
            checked += 1
            remove_glasses = swapper.has_glasses(img, faces[0])
    finally:
        if decoder.poll() is None:
            decoder.kill()
            decoder.wait()
    if first is None:
        raise NoFaceInVideo()

    img, faces = first
    started = time.monotonic()
    result, _ = swapper.swap_image(src, img, faces, enhance, color, redetect=False,
                                   remove_glasses=remove_glasses)
    took = time.monotonic() - started
    out = BytesIO()
    Image.fromarray(result.clip(0, 255).astype(np.uint8)).save(out, "JPEG", quality=92)
    return out.getvalue(), took, total, remove_glasses


def swap_video(swapper: fs.FaceSwapper, src: fs.Source, in_path: Path, out_path: Path,
               mode: str = "fast", enhance: str | None = None, color: float = 0.6,
               progress=None, cancel=None, remove_glasses: bool = True) -> dict:
    """מחליף את הפנים הגדולות בכל פריים. progress(done, total) נקרא מדי פעם.

    cancel: threading.Event - אם הוא מופעל, העיבוד נעצר (Cancelled) והקובץ החלקי נמחק.
    """
    cfg = MODES[mode]
    info = probe(in_path)
    fps = min(cfg["fps"], info["fps"])
    w, h = _out_size(info["width"], info["height"], cfg["short_side"])
    seconds = min(info["duration"] or MAX_SECONDS, MAX_SECONDS)
    total = max(1, int(round(seconds * fps)))
    enhance = enhance if cfg["enhance"] else None

    decoder = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-t", str(MAX_SECONDS), "-i", str(in_path),
         "-vf", f"fps={fps},scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    encoder = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
         "-t", str(MAX_SECONDS), "-i", str(in_path),
         "-map", "0:v:0", "-map", "1:a:0?",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", str(out_path)],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    frame_bytes = w * h * 3
    done = swapped = 0
    kps_s = None      # מיקום הפנים אחרי החלקה
    stats_s = None    # צבע העור בתוצאה אחרי החלקה
    missing = 0
    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            raw = decoder.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                break
            img = Image.frombytes("RGB", (w, h), raw)
            faces = swapper.detect(np.asarray(img))
            if faces:
                bbox, kps, score = faces[0]
                # אם הפנים קפצו רחוק (תנועה מהירה / סצנה חדשה) - לא מחליקים
                if kps_s is not None:
                    eye_d = np.linalg.norm(kps[1] - kps[0])
                    if np.abs(kps - kps_s).max() < eye_d * 0.5:
                        kps = KPS_SMOOTH * kps + (1 - KPS_SMOOTH) * kps_s
                kps_s, missing = kps, 0
            elif kps_s is not None and missing < HOLD_FRAMES:
                missing += 1
            else:
                kps_s, stats_s = None, None

            if kps_s is not None:
                face = [(None, kps_s, 1.0)]
                result, _ = swapper.swap_image(src, img, face, enhance, 0, redetect=False,
                                               remove_glasses=remove_glasses)
                if color > 0:
                    stats = fs._skin_stats(result, kps_s)
                    if stats is not None:
                        stats_s = stats if stats_s is None else tuple(
                            STATS_SMOOTH * a + (1 - STATS_SMOOTH) * b for a, b in zip(stats, stats_s))
                        result = fs._color_transfer(result, kps_s, src.stats, color, stats_s)
                frame = result.clip(0, 255).astype(np.uint8).tobytes()
                swapped += 1
            else:
                frame = raw
            encoder.stdin.write(frame)
            done += 1
            if progress:
                progress(done, total)
        encoder.stdin.close()
        err = encoder.stderr.read().decode(errors="replace")
        if encoder.wait() != 0:
            raise RuntimeError(f"ffmpeg encode failed: {err[-500:]}")
    except BaseException:
        out_path.unlink(missing_ok=True)  # לא משאירים קובץ חלקי
        raise
    finally:
        for proc in (decoder, encoder):
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    if done == 0:
        raise ValueError("no frames decoded")
    if swapped == 0:
        out_path.unlink(missing_ok=True)
        raise NoFaceInVideo()
    return {"frames": done, "swapped": swapped, "fps": fps, "size": (w, h),
            "trimmed": info["duration"] > MAX_SECONDS + 0.5}
