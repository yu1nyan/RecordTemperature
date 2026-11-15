import cv2
import numpy as np

from recordtemp.scheduler import Scheduler, next_boundary
from recordtemp.sevenseg import pipeline as P
from synth import make_frame


class FakeRecorder:
    def __init__(self):
        self.calls = []

    def capture_all(self, ts=None):
        self.calls.append(ts)

    def cleanup_images(self, now=None):
        return 0


def test_next_boundary():
    assert next_boundary(120.0, 60) == 180
    assert next_boundary(119.9, 60) == 120
    assert next_boundary(3601, 60) == 3660


def test_tick_runs_when_due_and_after_sleep_gap():
    now = [1000.0]
    rec = FakeRecorder()
    s = Scheduler(rec, 60, clock=lambda: now[0])
    due = s.tick(now[0], 1000.0)          # due now -> capture
    assert rec.calls == [1000] and due == 1020
    assert s.tick(1010.0, due) == due     # not yet due
    assert len(rec.calls) == 1
    now[0] = 1000.0 + 3 * 3600 + 7        # PC slept for 3 hours
    due = s.tick(now[0], due)
    assert len(rec.calls) == 2            # captured immediately on resume
    assert due == next_boundary(now[0], 60)


def write(path, img):
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), img)


def test_recorder_end_to_end(env, tmp_path):
    cfg, db, rec = env
    frame, tc, hc = make_frame("235", "56")
    src = tmp_path / "cam.png"
    write(src, frame)
    cid = db.add_camera(name="test", url=str(src))
    cam = db.get_camera(cid)

    assert rec.capture_camera(cam, 1000)["status"] == "uncalibrated"

    ref_rel = "images/cam1/calib/ref.jpg"
    write(cfg.data_dir / ref_rel, frame)
    displays = {
        "temperature": P.resolve(P.warp_gray(frame, tc), dict(corners=tc, n_digits=3, decimals=1)),
        "humidity": P.resolve(P.warp_gray(frame, hc), dict(corners=hc, n_digits=2, decimals=0)),
    }
    db.save_calibration(cid, {"displays": displays}, ref_rel)

    r = rec.capture_camera(cam, 1060)
    assert r["status"] == "ok" and r["temperature"] == 23.5 and r["humidity"] == 56

    # a jump of 8 degrees within the window is rejected and its image kept for review
    frame2, _, _ = make_frame("315", "56")
    write(src, frame2)
    r = rec.capture_camera(cam, 1120)
    assert r["status"] == "invalid"
    bad = db.get_reading(r["id"])
    assert bad["image_path"] and (cfg.data_dir / bad["image_path"]).exists()

    # capture error is recorded, not raised
    db.update_camera(cid, url=str(tmp_path / "missing.png"))
    assert rec.capture_camera(db.get_camera(cid), 1180)["status"] == "capture_error"

    # old failure images are cleaned up, samples are kept
    removed = rec.cleanup_images(now=1120 + 31 * 86400)
    assert removed == 1 and db.get_reading(r["id"])["image_path"] is None
