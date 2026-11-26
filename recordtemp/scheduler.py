"""Periodic capture + recognition + storage."""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import camera
from .matter import MatterError
from .config import Config
from .db import Database
from .sevenseg import pipeline as P

log = logging.getLogger(__name__)


@dataclass
class CameraState:
    last_attempt: int = 0
    last_status: str = ""
    last_error: str = ""
    align_cc: float | None = None
    messages: list[str] = field(default_factory=list)
    low_align_count: int = 0


class Recorder:
    """Captures one frame per camera, reads it and stores the result."""

    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self.state: dict[int, CameraState] = {}
        self.matter = None              # MatterBridge, set by __main__ (reads Matter sensors)
        self._ref_cache: dict[int, tuple[int, object]] = {}
        self._cam_locks: dict[int, threading.Lock] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ helpers
    def rel(self, p: Path) -> str:
        return str(p.relative_to(self.cfg.data_dir)).replace("\\", "/")

    def abs(self, rel: str) -> Path:
        return self.cfg.data_dir / rel

    def latest_path(self, cam_id: int) -> Path:
        return self.cfg.images_dir / f"cam{cam_id}" / "latest.jpg"

    def _ref_small(self, cam_id: int, calib: dict):
        cached = self._ref_cache.get(cam_id)
        if cached and cached[0] == calib["id"]:
            return cached[1]
        img = camera.load_image(self.abs(calib["reference_image"]))
        ref = P.small_gray(img)[0] if img is not None else None
        self._ref_cache[cam_id] = (calib["id"], ref)
        return ref

    def _cam_lock(self, cam_id: int) -> threading.Lock:
        with self._lock:
            return self._cam_locks.setdefault(cam_id, threading.Lock())

    def grab(self, cam: dict):
        """Grab a frame with retries (network may still be coming up after resume)."""
        retries = max(1, int(self.cfg["capture_retries"]))
        delays = [2, 5, 10, 15]
        err = None
        for i in range(retries):
            try:
                frame = camera.grab_frame(cam, self.cfg["capture_timeout_sec"])
                camera.save_jpeg(self.latest_path(cam["id"]), frame, 85)
                return frame
            except Exception as e:  # noqa: BLE001
                err = e
                log.warning("camera %s capture failed (%d/%d): %s", cam["id"], i + 1, retries, e)
                if i + 1 < retries:
                    time.sleep(delays[min(i, len(delays) - 1)])
        raise camera.CaptureError(str(err))

    # -------------------------------------------------------------- main
    def capture_camera(self, cam: dict, ts: int | None = None) -> dict:
        lock = self._cam_lock(cam["id"])
        if not lock.acquire(blocking=False):
            return {"status": "busy"}
        try:
            return self._capture_camera(cam, ts or int(time.time()))
        finally:
            lock.release()

    def _capture_camera(self, cam: dict, ts: int) -> dict:
        st = self.state.setdefault(cam["id"], CameraState())
        st.last_attempt = ts
        if cam.get("kind") == "matter":
            return self._capture_matter(cam, ts, st)
        try:
            frame = self.grab(cam)
        except Exception as e:  # noqa: BLE001
            st.last_status, st.last_error, st.messages = "capture_error", str(e), [str(e)]
            self.db.add_reading(cam["id"], ts, "capture_error", message=str(e))
            return {"status": "capture_error", "message": str(e)}
        st.last_error = ""

        calib = self.db.active_calibration(cam["id"])
        if not calib:
            st.last_status, st.messages = "uncalibrated", ["未校正です。校正画面で設定してください。"]
            self.db.add_reading(cam["id"], ts, "uncalibrated", message=st.messages[0])
            return {"status": "uncalibrated"}

        prev = self.db.last_good_reading(cam["id"], ts - int(self.cfg["jump_window_sec"]))
        try:
            res = P.read_frame(frame, calib["params"], self._ref_small(cam["id"], calib),
                               prev=prev, limits=self.cfg.limits())
        except Exception as e:  # noqa: BLE001
            log.exception("recognition failed for camera %s", cam["id"])
            res = None
            err = f"認識処理エラー: {e}"

        status = res.status if res else "invalid"
        image_rel = None
        if status not in ("ok",) or self.cfg["save_ok_images"]:
            d = datetime.fromtimestamp(ts)
            p = self.cfg.images_dir / f"cam{cam['id']}" / d.strftime("%Y%m%d") / f"{d:%H%M%S}_{status}.jpg"
            camera.save_jpeg(p, frame, 90)
            image_rel = self.rel(p)

        if res:
            st.align_cc = res.align_cc
            st.low_align_count = st.low_align_count + 1 if res.align_cc < self.cfg["min_align_cc"] else 0
            msgs = res.messages
            rid = self.db.add_reading(cam["id"], ts, status, res.temperature, res.humidity,
                                      round(res.confidence, 3), image_rel, res.raw_text, "; ".join(msgs) or None)
        else:
            msgs = [err]
            rid = self.db.add_reading(cam["id"], ts, status, image_path=image_rel, message=err)
        st.last_status, st.messages = status, msgs
        log.info("camera %s: %s T=%s H=%s %s", cam["id"], status,
                 res.temperature if res else None, res.humidity if res else None, "; ".join(msgs))
        return {"status": status, "id": rid,
                "temperature": res.temperature if res else None,
                "humidity": res.humidity if res else None, "messages": msgs}

    def _capture_matter(self, cam: dict, ts: int, st: CameraState) -> dict:
        """Readings from a Matter sensor (via the bridge): no image, no calibration; only the range check."""
        try:
            if self.matter is None:
                raise MatterError("Matter 連携が起動していません")
            t, h = self.matter.read(cam["device_id"], now=ts)
        except MatterError as e:
            st.last_status, st.last_error, st.messages = "capture_error", str(e), [str(e)]
            self.db.add_reading(cam["id"], ts, "capture_error", message=str(e))
            log.warning("matter %s: %s", cam["id"], e)
            return {"status": "capture_error", "message": str(e)}
        st.last_error = ""
        (tlo, thi), (hlo, hhi) = self.cfg["temp_range"], self.cfg["hum_range"]
        msgs = []
        if t is not None and not tlo <= t <= thi:
            msgs.append(f"温度が範囲外 ({t})")
        if h is not None and not hlo <= h <= hhi:
            msgs.append(f"湿度が範囲外 ({h})")
        status = "invalid" if msgs else "ok"
        rid = self.db.add_reading(cam["id"], ts, status, t, h, 1.0, raw_text=f"Matter {t} / {h}",
                                  message="; ".join(msgs) or None)
        st.last_status, st.messages = status, msgs
        log.info("matter %s: %s T=%s H=%s", cam["id"], status, t, h)
        return {"status": status, "id": rid, "temperature": t, "humidity": h, "messages": msgs}

    def capture_all(self, ts: int | None = None) -> None:
        cams = self.db.list_cameras(enabled_only=True)
        if not cams:
            return
        ts = ts or int(time.time())
        with ThreadPoolExecutor(max_workers=min(8, len(cams))) as ex:
            list(ex.map(lambda c: self._safe_capture(c, ts), cams))

    def _safe_capture(self, cam, ts):
        try:
            self.capture_camera(cam, ts)
        except Exception:  # noqa: BLE001
            log.exception("capture failed for camera %s", cam.get("id"))

    # ------------------------------------------------------------ cleanup
    def cleanup_images(self, now: float | None = None) -> int:
        now = now or time.time()
        removed = 0
        for good, days in ((True, self.cfg["keep_ok_images_days"]), (False, self.cfg["keep_bad_images_days"])):
            for r in self.db.readings_with_images_before(int(now - days * 86400), good):
                if self.db.image_in_use(r["image_path"]):
                    continue
                try:
                    self.abs(r["image_path"]).unlink(missing_ok=True)
                except OSError:
                    continue
                self.db.clear_image(r["id"])
                removed += 1
        # remove empty day folders
        for d in self.cfg.images_dir.glob("cam*/*"):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        return removed


def next_boundary(now: float, interval: int) -> float:
    """Next wall-clock multiple of `interval` strictly after `now`."""
    return (int(now) // interval + 1) * interval


class Scheduler(threading.Thread):
    """Wall-clock based loop.

    Waits in short slices and compares against time.time(), so after the PC
    resumes from sleep/hibernation the overdue capture runs immediately instead
    of waiting for a monotonic timer that was frozen during sleep.
    """

    def __init__(self, recorder: Recorder, interval: int = 60, clock=time.time, capture_on_start=True):
        super().__init__(name="scheduler", daemon=True)
        self.recorder = recorder
        self.interval = max(10, int(interval))
        self.clock = clock
        self.stop_event = threading.Event()
        self.capture_on_start = capture_on_start
        self.last_cleanup = 0.0
        self.runs = 0

    def stop(self):
        self.stop_event.set()

    def tick(self, now: float, due: float) -> float:
        """Run one capture if due; return the next due time."""
        if now < due:
            return due
        if now - due > self.interval * 1.5:
            log.info("resumed after %.0f s gap (sleep / hibernate?)", now - due)
        self.recorder.capture_all(int(now))
        self.runs += 1
        if now - self.last_cleanup > 86400:
            self.last_cleanup = now
            try:
                n = self.recorder.cleanup_images(now)
                if n:
                    log.info("removed %d old images", n)
            except Exception:  # noqa: BLE001
                log.exception("cleanup failed")
        return next_boundary(self.clock(), self.interval)

    def run(self):
        log.info("scheduler started (interval %d s)", self.interval)
        due = self.clock() if self.capture_on_start else next_boundary(self.clock(), self.interval)
        while not self.stop_event.is_set():
            try:
                due = self.tick(self.clock(), due)
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick failed")
                due = next_boundary(self.clock(), self.interval)
            self.stop_event.wait(min(1.0, max(0.05, due - self.clock())))
        log.info("scheduler stopped")
