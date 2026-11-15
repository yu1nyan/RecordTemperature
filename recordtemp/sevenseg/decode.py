"""Decode digit cells by sampling the seven segment regions."""
from __future__ import annotations

import numpy as np

SEGS = "abcdefg"
#   aaa
#  f   b
#   ggg
#  e   c
#   ddd
_PATTERNS = {
    "0": "abcdef", "1": "bc", "2": "abdeg", "3": "abcdg", "4": "bcfg",
    "5": "acdfg", "6": "acdefg", "7": "abc", "8": "abcdefg", "9": "abcdfg",
    "-": "g", " ": "",
}
# common font variants
_VARIANTS = {"7": ["abcf"], "9": ["abcfg"], "6": ["cdefg"], "1": ["ef"]}


def _vec(segs: str) -> tuple[int, ...]:
    return tuple(1 if s in segs else 0 for s in SEGS)


LOOKUP: dict[tuple[int, ...], str] = {}
for ch, segs in _PATTERNS.items():
    LOOKUP[_vec(segs)] = ch
for ch, vs in _VARIANTS.items():
    for segs in vs:
        LOOKUP.setdefault(_vec(segs), ch)


def segment_regions(box, seg_width: float) -> dict[str, tuple[int, int, int, int]]:
    """Sampling rectangles (x0, y0, x1, y1) for each segment inside a cell box."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    t = max(1.0, seg_width * w)
    m = 0.18  # shrink along segment length to avoid corner overlap
    hm = h / 2.0
    lx0, lx1 = t + m * (w - 2 * t), w - t - m * (w - 2 * t)
    up0, up1 = t + m * (hm - 1.5 * t), hm - t / 2 - m * (hm - 1.5 * t)
    lo0, lo1 = hm + t / 2 + m * (hm - 1.5 * t), h - t - m * (hm - 1.5 * t)
    r = {
        "a": (lx0, 0, lx1, t),
        "b": (w - t, up0, w, up1),
        "c": (w - t, lo0, w, lo1),
        "d": (lx0, h - t, lx1, h),
        "e": (0, lo0, t, lo1),
        "f": (0, up0, t, up1),
        "g": (lx0, hm - t / 2, lx1, hm + t / 2),
    }
    out = {}
    for k, (a, b, c, d) in r.items():
        ax, bx = int(round(x0 + a)), int(round(x0 + c))
        ay, by = int(round(y0 + b)), int(round(y0 + d))
        out[k] = (ax, ay, max(ax + 1, bx), max(ay + 1, by))
    return out


def decode_cell(binary: np.ndarray, box, seg_width: float = 0.2,
                fill_thresh: float = 0.4) -> tuple[str, float, list[float]]:
    """Return (char, confidence 0..1, fill ratios a..g). char '?' if unknown."""
    regions = segment_regions(box, seg_width)
    fills = []
    for s in SEGS:
        x0, y0, x1, y1 = regions[s]
        patch = binary[max(0, y0):max(0, y1), max(0, x0):max(0, x1)]
        fills.append(float(patch.mean()) if patch.size else 0.0)
    on = tuple(1 if f > fill_thresh else 0 for f in fills)
    margins = [((f - fill_thresh) / (1 - fill_thresh)) if o else ((fill_thresh - f) / fill_thresh)
               for f, o in zip(fills, on)]
    conf = float(max(0.0, min(margins)))
    ch = LOOKUP.get(on)
    if ch is None:
        # nearest known pattern by fill distance, zero confidence
        ch = "?"
        conf = 0.0
    return ch, conf, fills


def compose_value(chars: list[str], decimals: int) -> float | None:
    """Combine decoded cell characters into a number, honouring the implied decimal point."""
    s = "".join(chars)
    if "?" in s:
        return None
    s = s.strip()
    if not s:
        return None
    neg = s.startswith("-")
    body = s[1:].strip() if neg else s
    if not body or not body.isdigit():
        return None
    # cells right of the decimal point must all be present
    if decimals > 0 and len(body) < decimals:
        body = body.rjust(decimals, "0")
    v = int(body) / (10 ** decimals)
    return -v if neg else v
