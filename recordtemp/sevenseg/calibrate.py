"""Semi-automatic calibration.

A human supplies the four corners of each display and the values actually shown
on a few frames.  Everything else (polarity, shear, threshold offset, segment
thickness, fill threshold and optionally the digit boxes) is found by a grid
search that maximises the number of frames decoded exactly right, then the
mean confidence.
"""
from __future__ import annotations

import itertools

import numpy as np

from . import preprocess as pp
from .decode import decode_cell, compose_value
from .pipeline import display_params, resolve
from .segment import propose_boxes

OFFSETS = (-40, -25, -12, 0, 12, 25, 40)
SEG_WIDTHS = (0.14, 0.18, 0.22, 0.26)
FILL_THRESHES = (0.3, 0.4, 0.5)
SHEAR_DELTAS = (-0.06, -0.03, 0.0, 0.03, 0.06)


def propose(gray: np.ndarray, dp: dict) -> dict:
    """Automatic first guess for one display (no ground truth needed)."""
    dp = display_params(dp)
    dp["shear"] = None if not dp.get("boxes_manual") else dp["shear"]
    if not dp.get("boxes_manual"):
        dp["boxes"] = None
    return resolve(gray, dp)


def _matches(value: float | None, truth: float, decimals: int) -> bool:
    return value is not None and round(value, decimals) == round(float(truth), decimals)


def fit(grays: list[np.ndarray], truths: list[float], dp: dict) -> tuple[dict, dict]:
    """Grid-search decoding parameters so that grays decode to truths.

    Returns (best display params, stats {matches, total, confidence}).
    """
    assert grays and len(grays) == len(truths)
    base = display_params(dp)
    n, dec = base["n_digits"], base["decimals"]
    manual = bool(base.get("boxes_manual") and base.get("boxes") and len(base["boxes"]) == n)
    best_key, best_dp = None, None

    for pol in ("dark", "light"):
        fgs = [pp.foreground(g, pol) for g in grays]
        s0 = base["shear"] if base["shear"] is not None else pp.auto_shear(fgs[0])
        for ds in SHEAR_DELTAS:
            s = float(np.clip(s0 + ds, -pp.MAX_SHEAR, pp.MAX_SHEAR))
            shs = [pp.shear(f, s) for f in fgs]
            levels = [pp.otsu_level(x) for x in shs]
            for off in OFFSETS:
                bins = [pp.binarize(x, off, lv) for x, lv in zip(shs, levels)]
                boxes = base["boxes"] if manual else propose_boxes(bins[0], n)
                for sw, ft in itertools.product(SEG_WIDTHS, FILL_THRESHES):
                    matches, confs = 0, []
                    for b, truth in zip(bins, truths):
                        chars, cs = [], []
                        for box in boxes:
                            ch, c, _ = decode_cell(b, box, sw, ft)
                            chars.append(ch)
                            cs.append(c)
                        v = compose_value(chars, dec)
                        if _matches(v, truth, dec):
                            matches += 1
                            confs.append(min(cs))
                        else:
                            confs.append(0.0)
                    key = (matches, float(np.mean(confs)))
                    if best_key is None or key > best_key:
                        best_key = key
                        best_dp = dict(base, polarity=pol, shear=round(s, 3), thresh_offset=off,
                                       seg_width=sw, fill_thresh=ft, boxes=[list(map(int, bx)) for bx in boxes],
                                       boxes_manual=manual)
    stats = {"matches": best_key[0], "total": len(grays), "confidence": round(best_key[1], 3)}
    return best_dp, stats
