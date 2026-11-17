"""Application configuration stored in data/config.json."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("RECORDTEMP_DATA", PROJECT_ROOT / "data"))

DEFAULTS = {
    "host": "127.0.0.1",          # "0.0.0.0" to allow access from other LAN devices
    "port": 8765,
    "interval_sec": 60,
    "save_ok_images": False,      # full frames of successful readings (large!)
    "keep_ok_images_days": 7,
    "keep_bad_images_days": 30,
    "temp_range": [-30, 70],
    "hum_range": [0, 100],
    "max_temp_jump": 5.0,         # per reading, compared with last valid reading
    "max_hum_jump": 15.0,
    "jump_window_sec": 600,       # only compare with readings newer than this
    "min_confidence": 0.15,
    "min_align_cc": 0.6,
    "capture_timeout_sec": 15,
    "capture_retries": 3,
    "amedas_enabled": True,       # plot outdoor temperature/humidity from JMA AMeDAS
    "amedas_station": "44071",    # 練馬 (nearest station to Nakano-ku that also measures humidity)
    "overlay_enabled": True,      # small always-on-top window with the latest reading
    "overlay_camera_id": 0,       # 0 = first enabled camera
    "overlay_alpha": 0.9,
    "overlay_x": -1,              # -1 = bottom-right of the primary screen
    "overlay_y": -1,
}


class Config:
    def __init__(self, data_dir: Path | None = None):
        self.data_dir = Path(data_dir or DATA_DIR)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "config.json"
        self._lock = threading.Lock()
        self._values = dict(DEFAULTS)
        if self.path.exists():
            try:
                self._values.update(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
        else:
            self.save()

    def __getitem__(self, key):
        return self._values.get(key, DEFAULTS.get(key))

    def get(self, key, default=None):
        return self._values.get(key, default)

    def as_dict(self) -> dict:
        return dict(self._values)

    def update(self, values: dict) -> None:
        with self._lock:
            for k, v in values.items():
                if k in DEFAULTS:
                    self._values[k] = v
            self.save()

    def save(self) -> None:
        self.path.write_text(json.dumps(self._values, indent=2, ensure_ascii=False), encoding="utf-8")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "recordtemp.db"

    @property
    def images_dir(self) -> Path:
        p = self.data_dir / "images"
        p.mkdir(exist_ok=True)
        return p

    @property
    def logs_dir(self) -> Path:
        p = self.data_dir / "logs"
        p.mkdir(exist_ok=True)
        return p

    def limits(self) -> dict:
        return {k: self[k] for k in ("temp_range", "hum_range", "max_temp_jump", "max_hum_jump",
                                     "min_confidence", "min_align_cc")}
