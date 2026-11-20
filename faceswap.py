"""החלפת פנים עם מודלי ONNX - בלי OpenCV, רק numpy / Pillow / scipy.

שלושה מודלים בתיקייה models/:
  det_10g.onnx       - זיהוי פנים (SCRFD)
  w600k_r50.onnx     - "טביעת אצבע" של פנים (ArcFace)
  inswapper_128.onnx    - החלפה ראשונה: מוחקת משקפיים ודברים שמכסים את הפנים
  hyperswap_1a_256.onnx - החלפה שנייה: איכות ודמיון (FaceFusion HyperSwap, 256x256)
  bisenet_resnet_18.onnx - זיהוי משקפיים (רשות: בלעדיו תמיד מוחקים משקפיים)
  gpen_bfr_512.onnx / restoreformer_plus_plus.onnx / gfpgan_1.4.onnx
                     - חידוד פנים אחרי ההחלפה (רשות, בוחרים אחד)
"""

from io import BytesIO
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image, ImageDraw, ImageOps
from scipy import ndimage

MODELS_DIR = Path(__file__).parent / "models"
MAX_SIDE = 2048  # מקטין תמונות ענקיות כדי שהטלפון לא ייתקע

# מיקום 5 נקודות הפנים (עיניים, אף, פינות פה) בתמונת פנים מיושרת של 112x112
ARCFACE_DST = np.array(
    [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
     [41.5493, 92.3655], [70.7299, 92.2041]],
    dtype=np.float32,
)


# מיקום 5 נקודות הפנים בתבנית של HyperSwap, כשבר מגודל התמונה
ARCFACE_128 = np.array(
    [[0.36167656, 0.40387734], [0.63696719, 0.40235469], [0.50019687, 0.56044219],
     [0.38710391, 0.72160547], [0.61507734, 0.72034453]],
    dtype=np.float64,
)

# מיקום 5 נקודות הפנים בתבנית FFHQ (בשביל GFPGAN), כשבר מגודל התמונה
FFHQ_DST = np.array(
    [[0.37691676, 0.46864664], [0.62285697, 0.46912813], [0.50123859, 0.61331904],
     [0.39308822, 0.72541100], [0.61150205, 0.72490465]],
    dtype=np.float64,
)

# מודלי החידוד: שם קצר -> קובץ. נטענים רק כשבוחרים בהם (חוסך זיכרון)
ENHANCERS = {
    "gpen": "gpen_bfr_512.onnx",
    "restore": "restoreformer_plus_plus.onnx",
    "gfpgan": "gfpgan_1.4.onnx",
}

GLASSES_CLASS = 6          # במודל BiSeNet: 6 = משקפיים
GLASSES_MIN_RATIO = 0.01   # מעל 1% מהפנים מסומן כמשקפיים -> יש משקפיים (בבדיקות: עם ~7.5%, בלי 0%)
GLASSES_MIN_FACE = 48      # בפנים קטנות מזה הזיהוי לא אמין -> מוחקים תמיד, ליתר ביטחון
_IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
_IMAGENET_STD = np.array([0.229, 0.224, 0.225], np.float32)

ENHANCE_BLEND = 0.8  # כמה מהשחזור לקחת (1.0 = הכל). קצת פחות נראה טבעי יותר


class NoFaceError(Exception):
    """לא נמצאו פנים בתמונה."""


# ---------- עזרי גיאומטריה ----------

def _similarity(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """מחשב טרנספורמציית דמיון (Umeyama) שממפה src -> dst. מחזיר מטריצה 2x3."""
    n = src.shape[0]
    mu_s, mu_d = src.mean(0), dst.mean(0)
    s_c, d_c = src - mu_s, dst - mu_d
    cov = d_c.T @ s_c / n
    u, sig, vt = np.linalg.svd(cov)
    d = np.ones(2)
    if np.linalg.det(cov) < 0:
        d[1] = -1
    r = u @ np.diag(d) @ vt
    scale = (sig * d).sum() / (s_c ** 2).sum() * n
    t = mu_d - scale * r @ mu_s
    return np.hstack([scale * r, t[:, None]]).astype(np.float64)


def _invert(m: np.ndarray) -> np.ndarray:
    full = np.vstack([m, [0, 0, 1]])
    return np.linalg.inv(full)[:2]


def _warp(img: Image.Image, m: np.ndarray, size: tuple[int, int]) -> Image.Image:
    """כמו cv2.warpAffine: m ממפה מהתמונה המקורית לתמונת היעד."""
    inv = _invert(m)  # Pillow צריך מיפוי מהיעד למקור
    return img.transform(size, Image.AFFINE, data=inv.flatten().tolist(), resample=Image.BILINEAR)


def _align_matrix(kps: np.ndarray, size: int) -> np.ndarray:
    if size % 112 == 0:
        dst = ARCFACE_DST * (size / 112.0)
    else:
        dst = ARCFACE_DST * (size / 128.0)
        dst[:, 0] += 8.0 * (size / 128.0)
    return _similarity(kps.astype(np.float64), dst.astype(np.float64))


# ---------- קריאת מטריצת emap מתוך קובץ ה-ONNX (בלי הספרייה onnx) ----------

def _varint(buf: memoryview, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


def _fields(buf: memoryview, start: int, end: int):
    """עובר על שדות protobuf ומחזיר (מספר שדה, סוג, ערך/טווח)."""
    pos = start
    while pos < end:
        key, pos = _varint(buf, pos)
        field, wtype = key >> 3, key & 7
        if wtype == 0:
            val, pos = _varint(buf, pos)
            yield field, wtype, val
        elif wtype == 1:
            yield field, wtype, (pos, pos + 8)
            pos += 8
        elif wtype == 2:
            ln, pos = _varint(buf, pos)
            yield field, wtype, (pos, pos + ln)
            pos += ln
        elif wtype == 5:
            yield field, wtype, (pos, pos + 4)
            pos += 4
        else:
            raise ValueError("protobuf לא תקין")


def _load_emap(path: Path) -> np.ndarray:
    """ה-emap הוא ה-initializer האחרון בגרף של inswapper (מטריצה 512x512)."""
    data = path.read_bytes()
    buf = memoryview(data)
    graph = next(v for f, w, v in _fields(buf, 0, len(buf)) if f == 7 and w == 2)
    last = None
    for f, w, v in _fields(buf, *graph):
        if f == 5 and w == 2:  # initializer
            last = v
    dims, raw = [], None
    for f, w, v in _fields(buf, *last):
        if f == 1:
            if w == 0:
                dims.append(v)
            else:  # packed
                p = v[0]
                while p < v[1]:
                    d, p = _varint(buf, p)
                    dims.append(d)
        elif f == 9 and w == 2:
            raw = bytes(buf[v[0]:v[1]])
    if dims != [512, 512] or raw is None:
        raise ValueError(f"emap לא נמצא בקובץ המודל (dims={dims})")
    return np.frombuffer(raw, dtype=np.float32).reshape(512, 512).copy()


# ---------- המודלים ----------

class FaceSwapper:
    def __init__(self, models_dir: Path = MODELS_DIR):
        names = ("det_10g.onnx", "w600k_r50.onnx", "inswapper_128.onnx", "hyperswap_1a_256.onnx")
        missing = [n for n in names if not (models_dir / n).is_file()]
        if missing:
            raise FileNotFoundError(", ".join(missing))
        opts = ort.SessionOptions()
        opts.log_severity_level = 3  # בלי אזהרות מיותרות
        prov = ["CPUExecutionProvider"]
        self.det = ort.InferenceSession(str(models_dir / "det_10g.onnx"), opts, providers=prov)
        self.rec = ort.InferenceSession(str(models_dir / "w600k_r50.onnx"), opts, providers=prov)
        ins_path = models_dir / "inswapper_128.onnx"
        self.inswap = ort.InferenceSession(str(ins_path), opts, providers=prov)
        self.emap = _load_emap(ins_path)
        self.swap = ort.InferenceSession(str(models_dir / "hyperswap_1a_256.onnx"), opts, providers=prov)
        # מודלי החידוד הם רשות - נטענים רק כשמבקשים אותם
        self._models_dir, self._opts, self._prov = models_dir, opts, prov
        self._enhancers: dict = {}
        self._parser = None  # זיהוי משקפיים - נטען רק כשצריך
        self.det_in = self.det.get_inputs()[0].name
        self.rec_in = self.rec.get_inputs()[0].name

    # --- זיהוי פנים (SCRFD) ---
    def detect(self, img: np.ndarray, thresh: float = 0.5, size: int = 640):
        """מחזיר רשימת (bbox[4], kps[5x2], score), מהפנים הגדולות לקטנות."""
        h, w = img.shape[:2]
        scale = size / max(h, w)
        nh, nw = int(round(h * scale)), int(round(w * scale))
        resized = np.asarray(Image.fromarray(img).resize((nw, nh), Image.BILINEAR), dtype=np.float32)
        canvas = np.zeros((size, size, 3), dtype=np.float32)
        canvas[:nh, :nw] = resized
        blob = ((canvas - 127.5) / 128.0).transpose(2, 0, 1)[None]
        outs = self.det.run(None, {self.det_in: blob})

        boxes, kpss, scores = [], [], []
        for i, stride in enumerate((8, 16, 32)):
            sc = outs[i][:, 0]
            bb = outs[i + 3] * stride
            kp = outs[i + 6] * stride
            fh, fw = size // stride, size // stride
            ys, xs = np.mgrid[:fh, :fw]
            centers = np.stack([xs, ys], -1).reshape(-1, 2).astype(np.float32) * stride
            centers = np.repeat(centers, 2, axis=0)  # 2 anchors לכל נקודה
            keep = sc >= thresh
            c = centers[keep]
            b = bb[keep]
            boxes.append(np.stack([c[:, 0] - b[:, 0], c[:, 1] - b[:, 1],
                                   c[:, 0] + b[:, 2], c[:, 1] + b[:, 3]], -1))
            k = kp[keep].reshape(-1, 5, 2) + c[:, None, :]
            kpss.append(k)
            scores.append(sc[keep])

        boxes = np.concatenate(boxes) / scale
        kpss = np.concatenate(kpss) / scale
        scores = np.concatenate(scores)
        order = self._nms(boxes, scores)
        faces = [(boxes[i], kpss[i], float(scores[i])) for i in order]
        faces.sort(key=lambda f: (f[0][2] - f[0][0]) * (f[0][3] - f[0][1]), reverse=True)
        return faces

    @staticmethod
    def _nms(boxes: np.ndarray, scores: np.ndarray, iou: float = 0.4) -> list[int]:
        order = scores.argsort()[::-1]
        keep = []
        while order.size:
            i = order[0]
            keep.append(int(i))
            xx1 = np.maximum(boxes[i, 0], boxes[order[1:], 0])
            yy1 = np.maximum(boxes[i, 1], boxes[order[1:], 1])
            xx2 = np.minimum(boxes[i, 2], boxes[order[1:], 2])
            yy2 = np.minimum(boxes[i, 3], boxes[order[1:], 3])
            inter = np.clip(xx2 - xx1, 0, None) * np.clip(yy2 - yy1, 0, None)
            area = lambda b: (b[..., 2] - b[..., 0]) * (b[..., 3] - b[..., 1])
            ovr = inter / (area(boxes[i]) + area(boxes[order[1:]]) - inter)
            order = order[1:][ovr <= iou]
        return keep

    # --- טביעת אצבע של פנים (ArcFace) ---
    def embedding(self, img: Image.Image, kps: np.ndarray) -> np.ndarray:
        m = _align_matrix(kps, 112)
        face = np.asarray(_warp(img, m, (112, 112)), dtype=np.float32)
        blob = ((face - 127.5) / 127.5).transpose(2, 0, 1)[None]
        emb = self.rec.run(None, {self.rec_in: blob})[0][0]
        return emb / np.linalg.norm(emb)

    # --- הדבקת פנים מעובדות חזרה לתמונה ---
    @staticmethod
    def _paste(target: np.ndarray, face: Image.Image, m: np.ndarray, blend: float = 1.0) -> np.ndarray:
        """מדביק תמונת פנים מיושרת (face) חזרה ל-target. m ממפה מ-target לפנים המיושרות."""
        fs = face.size[0]
        inv = _invert(m)
        corners = np.array([[0, 0, 1], [fs, 0, 1], [0, fs, 1], [fs, fs, 1]], dtype=np.float64)
        pts = corners @ inv.T
        h, w = target.shape[:2]
        x0, y0 = np.floor(pts.min(0)).astype(int) - 2
        x1, y1 = np.ceil(pts.max(0)).astype(int) + 2
        x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, w), min(y1, h)
        if x1 <= x0 or y1 <= y0:
            return target
        # עובדים רק על האזור של הפנים (מהיר יותר)
        shift = np.array([[1, 0, -x0], [0, 1, -y0], [0, 0, 1]], dtype=np.float64)
        to_crop = (shift @ np.vstack([inv, [0, 0, 1]]))[:2]
        size = (x1 - x0, y1 - y0)
        face_c = np.asarray(_warp(face, to_crop, size), dtype=np.float32)
        mask = np.asarray(_warp(Image.new("L", (fs, fs), 255), to_crop, size), dtype=np.float32)

        # מסכה עם קצוות רכים כדי שהמעבר ייראה טבעי
        mask = np.where(mask > 20, 255.0, 0.0)
        ys, xs = np.nonzero(mask)
        if ys.size == 0:
            return target
        mask_size = int(np.sqrt((ys.max() - ys.min()) * (xs.max() - xs.min())))
        k = max(mask_size // 10, 10)
        mask = ndimage.grey_erosion(mask, size=(k, k))
        k = max(mask_size // 20, 5)
        ksize = 2 * k + 1
        sigma = 0.3 * ((ksize - 1) * 0.5 - 1) + 0.8
        mask = ndimage.gaussian_filter(mask, sigma, truncate=k / sigma)
        mask = (mask / 255.0 * blend)[..., None]

        region = target[y0:y1, x0:x1]
        target[y0:y1, x0:x1] = mask * face_c + (1 - mask) * region
        return target

    # --- החלפה ---
    def _inswap_one(self, img: Image.Image, target: np.ndarray, kps: np.ndarray, latent: np.ndarray) -> np.ndarray:
        """שלב 1 (inswapper): מצייר את כל אזור הפנים מחדש - כך נמחקים משקפיים."""
        m = _align_matrix(kps, 128)
        aligned = _warp(img, m, (128, 128))
        blob = (np.asarray(aligned, dtype=np.float32) / 255.0).transpose(2, 0, 1)[None]
        out = self.inswap.run(None, {"target": blob, "source": latent})[0][0]
        fake = Image.fromarray((out.transpose(1, 2, 0).clip(0, 1) * 255).astype(np.uint8))
        return self._paste(target, fake, m)

    def _swap_one(self, img: Image.Image, target: np.ndarray, kps: np.ndarray, emb: np.ndarray) -> np.ndarray:
        m = _similarity(kps.astype(np.float64), ARCFACE_128 * 256)
        crop = np.asarray(_warp(img, m, (256, 256)), dtype=np.float32)
        blob = ((crop / 255.0 - 0.5) / 0.5).transpose(2, 0, 1)[None]
        out = self.swap.run(None, {"source": emb, "target": blob})[0][0]
        face = ((out.transpose(1, 2, 0) * 0.5 + 0.5).clip(0, 1) * 255).astype(np.uint8)
        return self._paste(target, Image.fromarray(face), m)

    # --- חידוד פנים (GPEN / RestoreFormer++ / GFPGAN) ---
    def enhancer(self, name: str):
        """מחזיר את מודל החידוד, או None אם הקובץ שלו לא הורד."""
        if name not in self._enhancers:
            path = self._models_dir / ENHANCERS[name]
            if not path.is_file():
                return None
            self._enhancers.clear()  # רק מודל חידוד אחד בזיכרון בכל פעם
            self._enhancers[name] = ort.InferenceSession(str(path), self._opts, providers=self._prov)
        return self._enhancers[name]

    # --- זיהוי משקפיים (BiSeNet) ---
    def has_glasses(self, img: Image.Image, face) -> bool:
        """האם לפנים האלה יש משקפיים. בספק (בלי מודל / פנים קטנות) - True, כדי למחוק ליתר ביטחון."""
        bbox, kps, _ = face
        if bbox is not None and (bbox[2] - bbox[0]) < GLASSES_MIN_FACE:
            return True
        if self._parser is None:
            path = self._models_dir / "bisenet_resnet_18.onnx"
            if not path.is_file():
                return True
            self._parser = ort.InferenceSession(str(path), self._opts, providers=self._prov)
        m = _similarity(kps.astype(np.float64), ARCFACE_128 * 512)
        crop = np.asarray(_warp(img, m, (512, 512)), dtype=np.float32) / 255.0
        blob = ((crop - _IMAGENET_MEAN) / _IMAGENET_STD).transpose(2, 0, 1)[None]
        classes = self._parser.run(None, {"input": blob})[0][0].argmax(0)
        return float((classes == GLASSES_CLASS).mean()) > GLASSES_MIN_RATIO

    def _enhance_one(self, target: np.ndarray, kps: np.ndarray, model) -> np.ndarray:
        m = _similarity(kps.astype(np.float64), FFHQ_DST * 512)
        img = Image.fromarray(target.clip(0, 255).astype(np.uint8))
        crop = np.asarray(_warp(img, m, (512, 512)), dtype=np.float32)
        blob = ((crop / 255.0 - 0.5) / 0.5).transpose(2, 0, 1)[None]
        out = model.run(None, {model.get_inputs()[0].name: blob})[0][0]
        face = ((out.transpose(1, 2, 0).clip(-1, 1) + 1) / 2 * 255).astype(np.uint8)
        return self._paste(target, Image.fromarray(face), m, ENHANCE_BLEND)

    # --- התמונה המקורית: מכינים פעם אחת (גם בשביל סרטונים) ---
    def prepare_source(self, source: bytes) -> "Source":
        img = _open(source)
        faces = self.detect(np.asarray(img))
        if not faces:
            raise NoFaceError("source")
        kps = faces[0][1]
        emb = self.embedding(img, kps)[None].astype(np.float32)
        latent = emb @ self.emap
        latent = (latent / np.linalg.norm(latent)).astype(np.float32)
        return Source(emb, latent, _skin_stats(np.asarray(img), kps))

    def swap_image(self, src: "Source", tgt_img: Image.Image, faces: list, enhance: str | None,
                   color: float, redetect: bool = True, res_stats=None,
                   remove_glasses: bool | None = True) -> tuple[np.ndarray, list]:
        """מחליף פנים בתמונה אחת. faces = רשימת (bbox, kps, score) בתמונת היעד.

        remove_glasses: True = תמיד שלב מחיקת המשקפיים (inswapper, איטי),
                        False = מדלגים עליו (מהיר), None = אוטומטי לכל פנים לפי זיהוי משקפיים.
        redetect: לזהות שוב את הפנים אחרי השלב הראשון (מדויק יותר בתמונות;
                  בסרטון מדלגים כדי לחסוך זמן ולמנוע רעידות).
        מחזיר (מערך התמונה, הפנים שעליהן נעשה החידוד והצבע).
        """
        result = np.asarray(tgt_img, dtype=np.float32).copy()

        # שלב 1: inswapper - מוחק משקפיים ודברים שמכסים את הפנים (רק כשצריך - זה השלב הכי איטי)
        stage1 = False
        for face in faces:
            if remove_glasses or (remove_glasses is None and self.has_glasses(tgt_img, face)):
                result = self._inswap_one(tgt_img, result, face[1], src.latent)
                stage1 = True

        # שלב 2: HyperSwap על הפנים ה"נקיות" - איכות ודמיון טובים יותר
        mid_img = Image.fromarray(result.clip(0, 255).astype(np.uint8)) if stage1 else tgt_img
        mid_faces = faces
        if redetect and stage1:
            mid_faces = (self.detect(np.asarray(mid_img)) or faces)[:len(faces)]
        for _, kps, _ in mid_faces:
            result = self._swap_one(mid_img, result, kps, src.emb)

        # שלב 3 (רשות): חידוד הפנים
        model = self.enhancer(enhance) if enhance else None
        if model is not None:
            for _, kps, _ in mid_faces:
                result = self._enhance_one(result, kps, model)

        # שלב 4 (רשות): העברת צבע העור מהתמונה המקורית
        if color > 0:
            for _, kps, _ in mid_faces:
                result = _color_transfer(result, kps, src.stats, color, res_stats)
        return result, mid_faces

    def swap_faces(self, source: bytes, target: bytes, all_faces: bool = True,
                   enhance: str | None = "gpen", color: float = 0.6,
                   remove_glasses: bool | None = None) -> tuple[bytes, int]:
        """לוקח את הפנים הגדולות בתמונת המקור ושם אותן על הפנים בתמונת היעד.

        enhance: מודל החידוד - "gpen" / "restore" / "gfpgan", או None בלי חידוד.
        color: כמה מצבע העור של התמונה המקורית להעביר (0 = כלום, 1 = הכל).

        מחזיר (JPEG כבייטים, מספר הפנים שהוחלפו).
        """
        src = self.prepare_source(source)
        tgt_img = _open(target)
        tgt_faces = self.detect(np.asarray(tgt_img))
        if not tgt_faces:
            raise NoFaceError("target")
        if not all_faces:
            tgt_faces = tgt_faces[:1]
        result, _ = self.swap_image(src, tgt_img, tgt_faces, enhance, color, remove_glasses=remove_glasses)

        out = BytesIO()
        Image.fromarray(result.clip(0, 255).astype(np.uint8)).save(out, "JPEG", quality=95)
        return out.getvalue(), len(tgt_faces)


class Source:
    """הפנים מהתמונה המקורית, מוכנות להחלפה."""

    def __init__(self, emb: np.ndarray, latent: np.ndarray, stats):
        self.emb, self.latent, self.stats = emb, latent, stats


# ---------- העברת צבע עור מהתמונה המקורית ----------

def _rgb2lab(a: np.ndarray) -> np.ndarray:
    a = a / 255.0
    a = np.where(a > 0.04045, ((a + 0.055) / 1.055) ** 2.4, a / 12.92)
    xyz = a @ np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]]).T
    xyz /= np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1)


def _lab2rgb(lab: np.ndarray) -> np.ndarray:
    fy = (lab[..., 0] + 16) / 116
    f = np.stack([fy + lab[..., 1] / 500, fy, fy - lab[..., 2] / 200], -1)
    xyz = np.where(f ** 3 > 0.008856, f ** 3, (f - 16 / 116) / 7.787) * np.array([0.95047, 1.0, 1.08883])
    rgb = xyz @ np.array([[3.2406, -1.5372, -0.4986], [-0.9689, 1.8758, 0.0415], [0.0557, -0.2040, 1.0570]]).T
    rgb = np.where(rgb > 0.0031308, 1.055 * np.clip(rgb, 0, None) ** (1 / 2.4) - 0.055, 12.92 * rgb)
    return (rgb * 255).clip(0, 255)


def _face_ellipse(size: tuple[int, int], k: np.ndarray, feather: bool) -> np.ndarray:
    """מסכה בצורת אליפסה על הפנים (מהעיניים עד הסנטר), לפי 5 נקודות הפנים."""
    w, h = size
    eye_c, mouth_c = (k[0] + k[1]) / 2, (k[3] + k[4]) / 2
    eye_d = np.linalg.norm(k[1] - k[0])
    center = (eye_c + mouth_c) / 2 + (mouth_c - eye_c) * 0.15
    ang = np.degrees(np.arctan2(k[1][1] - k[0][1], k[1][0] - k[0][0]))
    rx, ry = eye_d * 1.0, np.linalg.norm(mouth_c - eye_c) * 1.5
    ell = Image.new("L", (int(rx * 2) + 2, int(ry * 2) + 2), 0)
    ImageDraw.Draw(ell).ellipse([1, 1, rx * 2 + 1, ry * 2 + 1], fill=255)
    ell = ell.rotate(-ang, expand=True, resample=Image.BILINEAR)
    m = Image.new("L", (w, h), 0)
    m.paste(ell, (int(center[0] - ell.size[0] / 2), int(center[1] - ell.size[1] / 2)))
    m = np.asarray(m, dtype=np.float32) / 255
    return ndimage.gaussian_filter(m, eye_d * 0.15) if feather else m


def _skin_stats(img: np.ndarray, kps: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
    """ממוצע ופיזור של צבע הפנים (במרחב LAB)."""
    sel = _face_ellipse((img.shape[1], img.shape[0]), kps, False) > 0.5
    if sel.sum() < 20:
        return None
    lab = _rgb2lab(img[sel].astype(np.float64))
    return lab.mean(0), lab.std(0)


def _color_transfer(result: np.ndarray, kps: np.ndarray, src_stats, strength: float,
                    res_stats=None) -> np.ndarray:
    """מזיז את צבע הפנים בתוצאה לכיוון הצבע של הפנים בתמונה המקורית.

    res_stats: אפשר לתת צבע מוכן של התוצאה (בסרטון - ממוצע לאורך זמן, כדי שלא יהבהב).
    """
    res_stats = res_stats or _skin_stats(result, kps)
    if src_stats is None or res_stats is None:
        return result
    (mu_s, sd_s), (mu_r, sd_r) = src_stats, res_stats
    soft = _face_ellipse((result.shape[1], result.shape[0]), kps, True)
    ys, xs = np.nonzero(soft > 0.01)
    if ys.size == 0:
        return result
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1  # רק באזור הפנים
    region = result[y0:y1, x0:x1].astype(np.float64)
    lab = _rgb2lab(region)
    moved = (lab - mu_r) / (sd_r + 1e-6) * sd_s + mu_s
    rgb = _lab2rgb(lab + strength * (moved - lab))
    m = soft[y0:y1, x0:x1, None]
    result[y0:y1, x0:x1] = m * rgb + (1 - m) * region
    return result


def _open(data: bytes) -> Image.Image:
    img = ImageOps.exif_transpose(Image.open(BytesIO(data))).convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    return img
