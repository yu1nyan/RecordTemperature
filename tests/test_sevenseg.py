import itertools

import cv2
import numpy as np
import pytest

from recordtemp.sevenseg import pipeline as P
from recordtemp.sevenseg import calibrate as C
from recordtemp.sevenseg.decode import compose_value
from synth import make_frame


def calib_for(tc, hc, **kw):
    return {"displays": {
        "temperature": dict(P.DEFAULT_DISPLAY, corners=tc, n_digits=3, decimals=1, **kw),
        "humidity": dict(P.DEFAULT_DISPLAY, corners=hc, n_digits=2, decimals=0, **kw),
    }}


def expected(t, h):
    return float(t.replace(" ", "")) / 10, float(h)


ALL_DIGIT_CASES = [("012", "34"), ("567", "89"), ("098", "12"), ("345", "67"), ("-12", "50"), (" 74", "06")]


@pytest.mark.parametrize("polarity", ["dark", "light"])
@pytest.mark.parametrize("t,h", ALL_DIGIT_CASES)
def test_reads_all_digits(polarity, t, h):
    frame, tc, hc = make_frame(t, h, polarity=polarity, seed=sum(map(ord, t + h)))
    r = P.read_frame(frame, calib_for(tc, hc))
    assert (r.temperature, r.humidity) == expected(t, h), r.raw_text
    assert r.status == "ok"


@pytest.mark.parametrize("italic,ghost,noise,gradient", [
    (0.0, 0.0, 6, 40),      # upright font
    (0.25, 0.0, 6, 40),     # strongly slanted
    (0.18, 0.25, 6, 40),    # LCD ghost segments visible
    (0.18, 0.0, 14, 90),    # noisy, uneven light
])
def test_robust_conditions(italic, ghost, noise, gradient):
    ok = 0
    cases = [("235", "56"), ("187", "99"), ("304", "12"), ("260", "83")]
    for i, (t, h) in enumerate(cases):
        frame, tc, hc = make_frame(t, h, italic=italic, ghost=ghost, noise=noise, gradient=gradient, seed=i)
        r = P.read_frame(frame, calib_for(tc, hc))
        ok += (r.temperature, r.humidity) == expected(t, h)
    assert ok == len(cases)


def test_compose_value():
    assert compose_value(list("235"), 1) == 23.5
    assert compose_value(list("-52"), 1) == -5.2
    assert compose_value(list(" 98"), 1) == 9.8
    assert compose_value(list("05"), 0) == 5
    assert compose_value(list("2 5"), 1) is None
    assert compose_value(list("2?5"), 1) is None
    assert compose_value(list("   "), 1) is None
    assert compose_value(list("5-2"), 1) is None


def test_range_and_jump_validation():
    frame, tc, hc = make_frame("235", "56")
    calib = calib_for(tc, hc)
    r = P.read_frame(frame, calib, prev={"temperature": 15.0, "humidity": 55})
    assert r.status == "invalid"
    r = P.read_frame(frame, calib, prev={"temperature": 23.0, "humidity": 55})
    assert r.status == "ok"
    r = P.read_frame(frame, calib, limits={"temp_range": (-10, 20)})
    assert r.status == "invalid" and r.temperature is None


def test_alignment_follows_camera_shift():
    ref, tc, hc = make_frame("235", "56", seed=1)
    ref_small, _ = P.small_gray(ref)
    m = np.float32([[1, 0, 18], [0, 1, -11]])
    cur, _, _ = make_frame("248", "61", seed=2)
    cur = cv2.warpAffine(cur, m, (cur.shape[1], cur.shape[0]), borderMode=cv2.BORDER_REPLICATE)
    calib = calib_for(tc, hc)
    r = P.read_frame(cur, calib, ref_small=ref_small)
    assert (r.temperature, r.humidity) == (24.8, 61.0), r.raw_text
    assert r.align_cc > 0.8


def test_fit_recovers_from_bad_params():
    frames = [make_frame(t, h, seed=i) for i, (t, h) in enumerate([("235", "56"), ("187", "40")])]
    grays = [P.warp_gray(f, tc) for f, tc, _ in frames]
    bad = dict(P.DEFAULT_DISPLAY, corners=frames[0][1], n_digits=3, decimals=1,
               polarity="light", thresh_offset=90, seg_width=0.4, fill_thresh=0.8)
    assert P.read_gray(grays[0], bad).value != 23.5
    best, stats = C.fit(grays, [23.5, 18.7], bad)
    assert stats["matches"] == 2
    assert best["polarity"] == "dark"
    assert P.read_gray(grays[0], best).value == 23.5
