"""החלפת פנים עם מודלי ONNX (InsightFace) - בלי OpenCV, רק numpy / Pillow / scipy.

שלושה מודלים בתיקייה models/:
  det_10g.onnx       - זיהוי פנים (SCRFD)
  w600k_r50.onnx     - "טביעת אצבע" של פנים (ArcFace)
  inswapper_128.onnx - המודל שמחליף את הפנים
"""

from io import BytesIO
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image, ImageOps
from scipy import ndimage

MODELS_DIR = Path(__file__).parent / "models"
MAX_SIDE = 2048  # מקטין תמונות ענקיות כדי שהטלפון לא ייתקע

# מיקום 5 נקודות הפנים (עיניים, אף, פינות פה) בתמונת פנים מיושרת של 112x112
ARCFACE_DST = np.array(
    [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
     [41.5493, 92.3655], [70.7299, 92.2041]],
    dtype=np.float32,
)


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
        opts = ort.SessionOptions()
        opts.log_severity_level = 3  # בלי אזהרות מיותרות
        prov = ["CPUExecutionProvider"]
        self.det = ort.InferenceSession(str(models_dir / "det_10g.onnx"), opts, providers=prov)
        self.rec = ort.InferenceSession(str(models_dir / "w600k_r50.onnx"), opts, providers=prov)
        swap_path = models_dir / "inswapper_128.onnx"
        self.swap = ort.InferenceSession(str(swap_path), opts, providers=prov)
        self.emap = _load_emap(swap_path)
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

    # --- החלפה והדבקה חזרה ---
    def _swap_one(self, img: Image.Image, target: np.ndarray, kps: np.ndarray, latent: np.ndarray) -> np.ndarray:
        m = _align_matrix(kps, 128)
        aligned = _warp(img, m, (128, 128))
        blob = (np.asarray(aligned, dtype=np.float32) / 255.0).transpose(2, 0, 1)[None]
        out = self.swap.run(None, {"target": blob, "source": latent})[0][0]
        fake = Image.fromarray((out.transpose(1, 2, 0).clip(0, 1) * 255).astype(np.uint8))

        # מחזירים את הפנים החדשות למקום בתמונה, רק באזור הפנים (מהיר יותר)
        inv = _invert(m)
        corners = np.array([[0, 0, 1], [128, 0, 1], [0, 128, 1], [128, 128, 1]], dtype=np.float64)
        pts = corners @ inv.T
        h, w = target.shape[:2]
        x0, y0 = np.floor(pts.min(0)).astype(int) - 2
        x1, y1 = np.ceil(pts.max(0)).astype(int) + 2
        x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, w), min(y1, h)
        if x1 <= x0 or y1 <= y0:
            return target
        shift = np.array([[1, 0, -x0], [0, 1, -y0], [0, 0, 1]], dtype=np.float64)
        to_crop = (shift @ np.vstack([inv, [0, 0, 1]]))[:2]
        size = (x1 - x0, y1 - y0)
        fake_c = np.asarray(_warp(fake, to_crop, size), dtype=np.float32)
        mask = np.asarray(_warp(Image.new("L", (128, 128), 255), to_crop, size), dtype=np.float32)

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
        mask = (mask / 255.0)[..., None]

        region = target[y0:y1, x0:x1]
        target[y0:y1, x0:x1] = mask * fake_c + (1 - mask) * region
        return target

    def swap_faces(self, source: bytes, target: bytes, all_faces: bool = True) -> tuple[bytes, int]:
        """לוקח את הפנים הגדולות בתמונת המקור ושם אותן על הפנים בתמונת היעד.

        מחזיר (JPEG כבייטים, מספר הפנים שהוחלפו).
        """
        src_img = _open(source)
        src_faces = self.detect(np.asarray(src_img))
        if not src_faces:
            raise NoFaceError("source")
        emb = self.embedding(src_img, src_faces[0][1])
        latent = (emb[None] @ self.emap)
        latent = (latent / np.linalg.norm(latent)).astype(np.float32)

        tgt_img = _open(target)
        tgt_faces = self.detect(np.asarray(tgt_img))
        if not tgt_faces:
            raise NoFaceError("target")
        if not all_faces:
            tgt_faces = tgt_faces[:1]

        result = np.asarray(tgt_img, dtype=np.float32).copy()
        for _, kps, _ in tgt_faces:
            result = self._swap_one(tgt_img, result, kps, latent)

        out = BytesIO()
        Image.fromarray(result.clip(0, 255).astype(np.uint8)).save(out, "JPEG", quality=95)
        return out.getvalue(), len(tgt_faces)


def _open(data: bytes) -> Image.Image:
    img = ImageOps.exif_transpose(Image.open(BytesIO(data))).convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    return img
