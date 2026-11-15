"""Image preprocessing for 7-segment displays.

Pipeline for one display region:
    frame --(4 corners)--> perspective-warped display (fixed height)
          --> "foreground" image where segments are bright (polarity aware)
          --> white top-hat (removes uneven illumination) + contrast stretch
          --> horizontal shear (de-italicise slanted digits)
          --> binary mask (Otsu + user/auto offset)
"""
from __future__ import annotations

import cv2
import numpy as np

WARP_H = 120          # height in px of the normalised display image
MAX_SHEAR = 0.4       # |shear| search range; also defines fixed padding
PAD = int(np.ceil(MAX_SHEAR * WARP_H / 2)) + 2


def order_corners(corners) -> np.ndarray:
    """Return corners ordered top-left, top-right, bottom-right, bottom-left."""
    pts = np.asarray(corners, dtype=np.float32).reshape(4, 2)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()  # y - x
    tl = pts[np.argmin(s)]
    br = pts[np.argmax(s)]
    tr = pts[np.argmin(d)]
    bl = pts[np.argmax(d)]
    out = np.array([tl, tr, br, bl], dtype=np.float32)
    if len({tuple(p) for p in out.tolist()}) < 4:  # degenerate -> keep given order
        return pts
    return out


def warp_size(corners, height: int = WARP_H) -> tuple[int, int]:
    tl, tr, br, bl = order_corners(corners)
    w = (np.linalg.norm(tr - tl) + np.linalg.norm(br - bl)) / 2
    h = (np.linalg.norm(bl - tl) + np.linalg.norm(br - tr)) / 2
    aspect = w / max(h, 1.0)
    return max(16, int(round(height * aspect))), height


def warp_display(frame: np.ndarray, corners, height: int = WARP_H) -> np.ndarray:
    """Perspective-correct the display region to a fronto-parallel image."""
    src = order_corners(corners)
    w, h = warp_size(corners, height)
    dst = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], dtype=np.float32)
    m = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(frame, m, (w, h), flags=cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_REPLICATE)


def to_gray(img: np.ndarray) -> np.ndarray:
    if img.ndim == 3:
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return img


def guess_polarity(gray: np.ndarray) -> str:
    """'dark' = dark segments on light background (LCD), 'light' = lit segments (LED).

    Segments normally cover well under half of a tightly cropped display, so the
    minority class after Otsu thresholding is the foreground.
    """
    g = cv2.GaussianBlur(gray, (3, 3), 0)
    t, _ = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    dark_frac = float((g < t).mean())
    return "dark" if dark_frac < 0.5 else "light"


def foreground(gray: np.ndarray, polarity: str) -> np.ndarray:
    """Illumination-normalised image with segments bright (0..255 uint8)."""
    f = 255 - gray if polarity == "dark" else gray.copy()
    h = f.shape[0]
    k = max(15, int(h * 0.45)) | 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    th = cv2.morphologyEx(f, cv2.MORPH_TOPHAT, kernel)
    th = cv2.GaussianBlur(th, (3, 3), 0)
    lo, hi = np.percentile(th, [2, 99.5])
    if hi - lo < 1:
        return np.zeros_like(th)
    out = np.clip((th.astype(np.float32) - lo) * (255.0 / (hi - lo)), 0, 255).astype(np.uint8)
    b = 2  # warp/top-hat border artefacts
    out[:b, :] = 0
    out[-b:, :] = 0
    out[:, :b] = 0
    out[:, -b:] = 0
    return out


def shear(img: np.ndarray, s: float) -> np.ndarray:
    """De-italicise: rows above the centre move left by s*dy (s>0 for right-leaning digits).

    Output has a fixed padding of PAD px on both sides so box coordinates stay
    comparable across shear values.
    """
    h, w = img.shape[:2]
    m = np.array([[1.0, s, PAD - s * h / 2.0], [0.0, 1.0, 0.0]], dtype=np.float32)
    return cv2.warpAffine(img, m, (w + 2 * PAD, h), flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=0)


def otsu_level(fg: np.ndarray) -> float:
    t, _ = cv2.threshold(fg, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return float(t)


def binarize(fg: np.ndarray, offset: float = 0.0, level: float | None = None) -> np.ndarray:
    """Binary mask (0/1 uint8). offset>0 makes the threshold stricter."""
    if level is None:
        level = otsu_level(fg)
    t = float(np.clip(level + offset, 1, 254))
    return (fg > t).astype(np.uint8)


def auto_shear(fg: np.ndarray, steps: int = 33) -> float:
    """Shear that makes vertical strokes most vertical (sharpest column projection)."""
    # use horizontal gradient so that only vertical stroke edges vote
    gx = np.abs(cv2.Sobel(fg.astype(np.float32), cv2.CV_32F, 1, 0, ksize=3))
    best_s, best_score = 0.0, -1.0
    for s in np.linspace(-MAX_SHEAR, MAX_SHEAR, steps):
        proj = shear(gx, float(s)).sum(axis=0).astype(np.float64)
        score = float((proj ** 2).sum())
        if score > best_score:
            best_s, best_score = float(s), score
    return round(best_s, 3)


def prepare(frame: np.ndarray, corners, polarity: str, shear_s: float):
    """frame -> (sheared foreground image, otsu level)."""
    gray = to_gray(warp_display(frame, corners))
    fg = foreground(gray, polarity)
    sh = shear(fg, shear_s)
    return sh, otsu_level(sh)
