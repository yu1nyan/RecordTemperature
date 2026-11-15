"""Synthetic 7-segment display images for tests."""
from __future__ import annotations

import cv2
import numpy as np

SEG_ON = {
    "0": "abcdef", "1": "bc", "2": "abdeg", "3": "abcdg", "4": "bcfg", "5": "acdfg",
    "6": "acdefg", "7": "abc", "8": "abcdefg", "9": "abcdfg", "-": "g", " ": "",
}


def _seg_polys(x, y, w, h, t):
    """Hexagon-ish polygons for segments of a digit cell at (x, y)."""
    hm = h / 2
    g = t * 0.25  # gap between segments

    def hseg(cy):
        return [(x + t / 2 + g, cy), (x + t + g, cy - t / 2), (x + w - t - g, cy - t / 2),
                (x + w - t / 2 - g, cy), (x + w - t - g, cy + t / 2), (x + t + g, cy + t / 2)]

    def vseg(cx, y0, y1):
        return [(cx, y0 + g), (cx + t / 2, y0 + t / 2 + g), (cx + t / 2, y1 - t / 2 - g),
                (cx, y1 - g), (cx - t / 2, y1 - t / 2 - g), (cx - t / 2, y0 + t / 2 + g)]

    return {
        "a": hseg(y + t / 2), "g": hseg(y + hm), "d": hseg(y + h - t / 2),
        "f": vseg(x + t / 2, y + t / 2, y + hm), "b": vseg(x + w - t / 2, y + t / 2, y + hm),
        "e": vseg(x + t / 2, y + hm, y + h - t / 2), "c": vseg(x + w - t / 2, y + hm, y + h - t / 2),
    }


def render_display(text: str, decimals: int, polarity: str = "dark", italic: float = 0.18,
                   cell_h: int = 100, ghost: float = 0.0, rng=None) -> np.ndarray:
    """Render text (one char per cell) as a display image (grayscale)."""
    rng = rng or np.random.default_rng(0)
    n = len(text)
    cw, t, pitch = int(cell_h * 0.52), int(cell_h * 0.12), int(cell_h * 0.72)
    margin = int(cell_h * 0.25)
    W = margin * 2 + pitch * (n - 1) + cw + int(italic * cell_h)
    H = cell_h + margin * 2
    bg, fg = (200, 40) if polarity == "dark" else (25, 230)
    img = np.full((H, W), bg, np.uint8)
    ghost_col = int(bg + (fg - bg) * ghost)
    for i, ch in enumerate(text):
        x, y = margin + i * pitch, margin
        polys = _seg_polys(x, y, cw, cell_h, t)
        for s, p in polys.items():
            pts = np.array(p, np.float32)
            # italic: shift by (centre_y - y) * italic
            pts[:, 0] += (y + cell_h / 2 - pts[:, 1]) * italic + italic * cell_h / 2
            col = fg if s in SEG_ON[ch] else (ghost_col if ghost else None)
            if col is not None:
                cv2.fillPoly(img, [np.round(pts * 8).astype(np.int32)], int(col), cv2.LINE_AA, shift=3)
        if decimals and i == n - decimals - 1:
            cx = x + cw + (pitch - cw) // 2 + int(italic * cell_h / 2) - int(italic * cell_h / 2)
            cv2.circle(img, (cx, y + cell_h - t // 2), t // 2, fg, -1, cv2.LINE_AA)
    return img


def make_frame(temp_text: str, hum_text: str, polarity="dark", italic=0.18, noise=6.0,
               gradient=40.0, tilt=8.0, ghost=0.0, seed=0, frame_size=(1280, 720)):
    """Return (BGR frame, temp_corners, hum_corners)."""
    rng = np.random.default_rng(seed)
    t_img = render_display(temp_text, 1, polarity, italic, ghost=ghost, rng=rng)
    h_img = render_display(hum_text, 0, polarity, italic, cell_h=70, ghost=ghost, rng=rng)
    fw, fh = frame_size
    frame = np.full((fh, fw), 90, np.uint8)
    corners = []
    for img, (ox, oy) in ((t_img, (400, 180)), (h_img, (450, 400))):
        h, w = img.shape
        src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
        d = tilt
        dst = np.float32([[ox, oy + d], [ox + w, oy], [ox + w + d, oy + h], [ox - d, oy + h + d]])
        m = cv2.getPerspectiveTransform(src, dst)
        warped = cv2.warpPerspective(img, m, (fw, fh), flags=cv2.INTER_LINEAR)
        mask = cv2.warpPerspective(np.full_like(img, 255), m, (fw, fh))
        frame[mask > 127] = warped[mask > 127]
        corners.append(dst.tolist())
    # illumination gradient + noise
    gx = np.linspace(-gradient / 2, gradient / 2, fw)[None, :]
    f = frame.astype(np.float32) + gx + rng.normal(0, noise, frame.shape)
    frame = np.clip(f, 0, 255).astype(np.uint8)
    frame = cv2.GaussianBlur(frame, (3, 3), 0)
    return cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR), corners[0], corners[1]
