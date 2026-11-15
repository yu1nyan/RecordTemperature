"""High level reading of a thermo-hygrometer frame using stored calibration."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

import cv2
import numpy as np

from . import preprocess as pp
from .decode import decode_cell, compose_value, segment_regions, SEGS
from .segment import propose_boxes

DISPLAY_KEYS = ("temperature", "humidity")

DEFAULT_DISPLAY = {
    "corners": None,        # 4 points [x, y] in reference-frame pixels
    "n_digits": 3,          # number of digit cells (include a cell for a leading minus)
    "decimals": 1,          # digits right of the (implied) decimal point
    "polarity": "auto",     # 'dark' (LCD) | 'light' (LED) | 'auto'
    "shear": None,          # None -> automatic
    "thresh_offset": 0.0,
    "seg_width": 0.2,
    "fill_thresh": 0.4,
    "boxes": None,          # [[x0,y0,x1,y1], ...] in sheared display coordinates
    "boxes_manual": False,
}


def display_params(dp: dict | None) -> dict:
    out = copy.deepcopy(DEFAULT_DISPLAY)
    if dp:
        out.update({k: v for k, v in dp.items() if k in DEFAULT_DISPLAY})
    return out


@dataclass
class DisplayResult:
    value: float | None
    text: str
    confidence: float
    chars: list[str]
    params: dict
    binary: np.ndarray | None = field(default=None, repr=False)
    cell_fills: list[list[float]] = field(default_factory=list)


def resolve(gray: np.ndarray, dp: dict) -> dict:
    """Fill in automatic parameters (polarity, shear, boxes) for one display image."""
    dp = display_params(dp)
    if dp["polarity"] not in ("dark", "light"):
        dp["polarity"] = pp.guess_polarity(gray)
    fg = pp.foreground(gray, dp["polarity"])
    if dp["shear"] is None:
        dp["shear"] = pp.auto_shear(fg)
    if not dp["boxes"] or len(dp["boxes"]) != dp["n_digits"]:
        sh = pp.shear(fg, dp["shear"])
        dp["boxes"] = propose_boxes(pp.binarize(sh, dp["thresh_offset"]), dp["n_digits"])
        dp["boxes_manual"] = False
    return dp


def read_gray(gray: np.ndarray, dp: dict) -> DisplayResult:
    """Decode a perspective-warped grayscale display image."""
    dp = resolve(gray, dp)
    fg = pp.foreground(gray, dp["polarity"])
    sh = pp.shear(fg, dp["shear"])
    binary = pp.binarize(sh, dp["thresh_offset"])
    chars, confs, fills = [], [], []
    for box in dp["boxes"]:
        ch, c, f = decode_cell(binary, box, dp["seg_width"], dp["fill_thresh"])
        chars.append(ch)
        confs.append(c)
        fills.append(f)
    value = compose_value(chars, dp["decimals"])
    text = "".join(chars)
    conf = float(min(confs)) if confs else 0.0
    if value is None:
        conf = 0.0
    return DisplayResult(value, text, conf, chars, dp, binary, fills)


def warp_gray(frame: np.ndarray, corners) -> np.ndarray:
    return pp.to_gray(pp.warp_display(frame, corners))


# ---------------------------------------------------------------- alignment
ALIGN_W = 640


def small_gray(frame: np.ndarray) -> tuple[np.ndarray, float]:
    g = pp.to_gray(frame)
    s = ALIGN_W / g.shape[1]
    small = cv2.resize(g, (ALIGN_W, max(1, int(round(g.shape[0] * s)))), interpolation=cv2.INTER_AREA)
    return cv2.GaussianBlur(small, (5, 5), 0), s


def align(ref_small: np.ndarray, frame: np.ndarray) -> tuple[np.ndarray, float]:
    """Estimate the camera shift since calibration.

    Returns (2x3 matrix mapping reference full-res coords -> current coords, correlation).
    Identity with cc=-1 if alignment failed.
    """
    cur, s = small_gray(frame)
    ident = np.eye(2, 3, dtype=np.float32)
    if cur.shape != ref_small.shape:
        return ident, -1.0
    warp = np.eye(2, 3, dtype=np.float32)
    try:
        cc, warp = cv2.findTransformECC(
            ref_small.astype(np.float32), cur.astype(np.float32), warp, cv2.MOTION_EUCLIDEAN,
            (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 60, 1e-4), None, 5)
    except cv2.error:
        return ident, -1.0
    full = warp.copy()
    full[:, 2] /= s
    # reject implausible jumps (> 15% of the frame width)
    if abs(full[0, 2]) > 0.15 * frame.shape[1] or abs(full[1, 2]) > 0.15 * frame.shape[0]:
        return ident, float(cc)
    return full, float(cc)


def transform_corners(corners, m: np.ndarray) -> list[list[float]]:
    pts = np.asarray(corners, dtype=np.float32).reshape(-1, 2)
    out = pts @ m[:, :2].T + m[:, 2]
    return out.tolist()


# ---------------------------------------------------------------- full frame
@dataclass
class FrameResult:
    temperature: float | None
    humidity: float | None
    status: str                  # ok | low_conf | invalid
    confidence: float
    raw_text: str
    align_cc: float
    messages: list[str]
    displays: dict[str, DisplayResult]


def read_frame(frame: np.ndarray, calib: dict, ref_small: np.ndarray | None = None,
               prev: dict | None = None, limits: dict | None = None) -> FrameResult:
    """Read both values from a frame.

    calib  : {"displays": {"temperature": dp, "humidity": dp}}
    prev   : {"temperature": v, "humidity": v} recent valid reading for jump check
    limits : temp_range, hum_range, max_temp_jump, max_hum_jump, min_confidence
    """
    limits = limits or {}
    m, cc = (np.eye(2, 3, dtype=np.float32), 1.0)
    if ref_small is not None:
        m, cc = align(ref_small, frame)
    results: dict[str, DisplayResult] = {}
    msgs: list[str] = []
    for key in DISPLAY_KEYS:
        dp = calib.get("displays", {}).get(key)
        if not dp or not dp.get("corners"):
            continue
        corners = transform_corners(dp["corners"], m)
        results[key] = read_gray(warp_gray(frame, corners), dp)

    t = results.get("temperature")
    h = results.get("humidity")
    tv = t.value if t else None
    hv = h.value if h else None
    confs = [r.confidence for r in results.values()]
    conf = float(min(confs)) if confs else 0.0
    status = "ok"

    rng_t = limits.get("temp_range", (-30, 70))
    rng_h = limits.get("hum_range", (0, 100))
    if t and tv is None:
        msgs.append(f"温度を読み取れません ('{t.text}')")
    if h and hv is None:
        msgs.append(f"湿度を読み取れません ('{h.text}')")
    if tv is not None and not (rng_t[0] <= tv <= rng_t[1]):
        msgs.append(f"温度が範囲外: {tv}")
        tv = None
    if hv is not None and not (rng_h[0] <= hv <= rng_h[1]):
        msgs.append(f"湿度が範囲外: {hv}")
        hv = None
    if prev:
        if tv is not None and prev.get("temperature") is not None and \
                abs(tv - prev["temperature"]) > limits.get("max_temp_jump", 5.0):
            msgs.append(f"温度の急変: {prev['temperature']} -> {tv}")
            status = "invalid"
        if hv is not None and prev.get("humidity") is not None and \
                abs(hv - prev["humidity"]) > limits.get("max_hum_jump", 15.0):
            msgs.append(f"湿度の急変: {prev['humidity']} -> {hv}")
            status = "invalid"
    if (t and tv is None) or (h and hv is None) or not results:
        status = "invalid"
    if status == "ok" and conf < limits.get("min_confidence", 0.15):
        status = "low_conf"
        msgs.append(f"信頼度が低い: {conf:.2f}")
    if ref_small is not None and cc < limits.get("min_align_cc", 0.6):
        msgs.append(f"カメラ位置ずれの可能性 (相関 {cc:.2f})")
    raw = " / ".join(f"{k}='{r.text}'" for k, r in results.items())
    return FrameResult(tv, hv, status, conf, raw, cc, msgs, results)


# ---------------------------------------------------------------- debug image
def render_debug(res: DisplayResult, scale: int = 3) -> np.ndarray:
    """Visualise binary image, cell boxes, segment sampling regions and decoded chars."""
    b = res.binary if res.binary is not None else np.zeros((pp.WARP_H, 100), np.uint8)
    img = cv2.cvtColor((b * 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)
    img = (img * 0.55).astype(np.uint8)
    img = cv2.resize(img, (img.shape[1] * scale, img.shape[0] * scale), interpolation=cv2.INTER_NEAREST)
    dp = res.params
    for i, box in enumerate(dp["boxes"] or []):
        x0, y0, x1, y1 = [v * scale for v in box]
        cv2.rectangle(img, (x0, y0), (x1, y1), (255, 200, 0), 1)
        fills = res.cell_fills[i] if i < len(res.cell_fills) else [0] * 7
        for s, (a, bb, c, d) in segment_regions(box, dp["seg_width"]).items():
            on = fills[SEGS.index(s)] > dp["fill_thresh"]
            cv2.rectangle(img, (a * scale, bb * scale), (c * scale, d * scale),
                          (0, 220, 0) if on else (0, 0, 200), 1)
        ch = res.chars[i] if i < len(res.chars) else "?"
        cv2.putText(img, ch, (x0 + 2, y0 + 22), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                    (0, 255, 255) if ch != "?" else (0, 0, 255), 2)
    return img
