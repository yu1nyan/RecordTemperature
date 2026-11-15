"""Automatic proposal of digit cell boxes in a binarised, de-skewed display image."""
from __future__ import annotations

import numpy as np


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Contiguous True runs as (start, end_exclusive)."""
    runs, start = [], None
    for i, v in enumerate(mask.tolist()):
        if v and start is None:
            start = i
        elif not v and start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(mask)))
    return runs


def _merge(runs: list[tuple[int, int]], gap: int) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for a, b in runs:
        if out and a - out[-1][1] <= gap:
            out[-1][1] = b
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def digit_rows(binary: np.ndarray) -> tuple[int, int]:
    """Vertical extent of the digits (largest band of rows containing foreground)."""
    h = binary.shape[0]
    rows = binary.sum(axis=1)
    if rows.max() == 0:
        return 0, h
    mask = rows > max(1, 0.02 * binary.shape[1])
    runs = _merge(_runs(mask), max(2, int(h * 0.08)))
    a, b = max(runs, key=lambda r: r[1] - r[0])
    return a, b


def propose_boxes(binary: np.ndarray, n_digits: int) -> list[list[int]]:
    """Propose n_digits boxes [x0, y0, x1, y1] (right-aligned cells).

    The rightmost digit of a reading is always lit, and digit '1' lights the right
    column of its cell, so cells are anchored on the right edges of ink runs.
    Missing leading cells (blank / minus sign) are extrapolated with the pitch.
    """
    h, w = binary.shape
    y0, y1 = digit_rows(binary)
    hd = max(1, y1 - y0)
    band = binary[y0:y1]
    # project only the upper 78% so decimal points / underlines can't bridge digits
    upper = band[: max(1, int(hd * 0.78))]
    col = upper.sum(axis=0)
    mask = col > max(1, int(0.03 * hd))
    runs = _merge(_runs(mask), max(2, int(hd * 0.04)))

    kept = []
    for a, b in runs:
        if b - a < max(2, 0.07 * hd):      # thin noise lines
            continue
        rows = np.nonzero(band[:, a:b].sum(axis=1))[0]
        if rows.size and (rows[-1] - rows[0] + 1) >= 0.45 * hd:
            kept.append((a, b))

    if not kept:
        wd = int(hd * 0.55)
        pitch = int(wd * 1.3)
        right = w - 2
        return [[max(0, right - (n_digits - 1 - i) * pitch - wd), y0,
                 max(1, right - (n_digits - 1 - i) * pitch), y1] for i in range(n_digits)]

    widths = [b - a for a, b in kept]
    wmax = max(widths)
    wd = int(np.median([x for x in widths if x >= 0.6 * wmax]))
    if wd < 0.3 * hd:          # e.g. display shows only '1's
        wd = int(0.5 * hd)
    wd = min(wd, int(0.9 * hd))

    assigned = kept[-n_digits:]
    rights = [b for _, b in assigned]
    diffs = np.diff(rights)
    if len(diffs):
        pitch = float(np.median(diffs))
        if pitch < 0.8 * wd:
            pitch = wd * 1.3
    else:
        pitch = wd * 1.3

    boxes = [[b - wd, y0, b, y1] for b in rights]
    while len(boxes) < n_digits:
        r = boxes[0][2] - pitch
        boxes.insert(0, [r - wd, y0, r, y1])
    out = []
    for x0, a, x1, b in boxes:
        x0 = int(round(max(0, x0)))
        x1 = int(round(min(w, max(x0 + 2, x1))))
        out.append([x0, int(a), x1, int(b)])
    return out
