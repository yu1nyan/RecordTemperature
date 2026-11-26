"""SQLite storage."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS cameras (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    host TEXT NOT NULL DEFAULT '',
    port INTEGER NOT NULL DEFAULT 554,
    username TEXT NOT NULL DEFAULT '',
    stream TEXT NOT NULL DEFAULT 'stream1',
    url TEXT NOT NULL DEFAULT '',          -- optional override (any RTSP URL or image file path)
    kind TEXT NOT NULL DEFAULT 'camera',   -- camera | matter
    device_id TEXT NOT NULL DEFAULT '',    -- Matter device id "node/tempEndpoint/humEndpoint" (kind = matter)
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS calibrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id INTEGER NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    active INTEGER NOT NULL DEFAULT 1,
    params_json TEXT NOT NULL,
    reference_image TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id INTEGER NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    ts INTEGER NOT NULL,
    temperature REAL,
    humidity REAL,
    status TEXT NOT NULL,          -- ok | low_conf | invalid | capture_error | uncalibrated | manual | dismissed
    confidence REAL,
    image_path TEXT,
    raw_text TEXT,
    message TEXT
);
CREATE INDEX IF NOT EXISTS idx_readings_cam_ts ON readings(camera_id, ts);
CREATE INDEX IF NOT EXISTS idx_readings_status ON readings(status, ts);
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id INTEGER NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    image_path TEXT NOT NULL,
    true_temp REAL,
    true_hum REAL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS outdoor (     -- AMeDAS observations (10-minute)
    station TEXT NOT NULL,
    ts INTEGER NOT NULL,
    temperature REAL,
    humidity REAL,
    PRIMARY KEY (station, ts)
);
"""

GOOD_STATUSES = ("ok", "manual")


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self):
        cam_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(cameras)")}
        for name, ddl in (("kind", "TEXT NOT NULL DEFAULT 'camera'"), ("device_id", "TEXT NOT NULL DEFAULT ''")):
            if name not in cam_cols:
                self._conn.execute(f"ALTER TABLE cameras ADD COLUMN {name} {ddl}")
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(readings)")}
        for name, typ in (("orig_temperature", "REAL"), ("orig_humidity", "REAL"),
                          ("orig_status", "TEXT"), ("edited_at", "INTEGER")):
            if name not in cols:
                self._conn.execute(f"ALTER TABLE readings ADD COLUMN {name} {typ}")

    def close(self):
        with self._lock:
            self._conn.close()

    def _q(self, sql, args=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def _x(self, sql, args=()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur.lastrowid

    # ------------------------------------------------------------- cameras
    CAMERA_FIELDS = ("name", "host", "port", "username", "stream", "url", "enabled", "kind", "device_id")

    def list_cameras(self, enabled_only=False) -> list[dict]:
        sql = "SELECT * FROM cameras" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY id"
        return self._q(sql)

    def get_camera(self, cam_id: int) -> dict | None:
        r = self._q("SELECT * FROM cameras WHERE id=?", (cam_id,))
        return r[0] if r else None

    def add_camera(self, **kw) -> int:
        data = {k: kw[k] for k in self.CAMERA_FIELDS if k in kw}
        data["created_at"] = int(time.time())
        cols = ",".join(data)
        return self._x(f"INSERT INTO cameras ({cols}) VALUES ({','.join('?' * len(data))})", tuple(data.values()))

    def update_camera(self, cam_id: int, **kw) -> None:
        data = {k: kw[k] for k in self.CAMERA_FIELDS if k in kw}
        if data:
            sets = ",".join(f"{k}=?" for k in data)
            self._x(f"UPDATE cameras SET {sets} WHERE id=?", (*data.values(), cam_id))

    def delete_camera(self, cam_id: int) -> None:
        self._x("DELETE FROM cameras WHERE id=?", (cam_id,))

    # -------------------------------------------------------- calibrations
    def active_calibration(self, cam_id: int) -> dict | None:
        r = self._q("SELECT * FROM calibrations WHERE camera_id=? AND active=1 ORDER BY id DESC LIMIT 1",
                    (cam_id,))
        if not r:
            return None
        r[0]["params"] = json.loads(r[0]["params_json"])
        return r[0]

    def save_calibration(self, cam_id: int, params: dict, reference_image: str) -> int:
        with self._lock:
            self._conn.execute("UPDATE calibrations SET active=0 WHERE camera_id=?", (cam_id,))
            cur = self._conn.execute(
                "INSERT INTO calibrations (camera_id, active, params_json, reference_image, created_at)"
                " VALUES (?,1,?,?,?)", (cam_id, json.dumps(params), reference_image, int(time.time())))
            self._conn.commit()
            return cur.lastrowid

    # ------------------------------------------------------------ readings
    def add_reading(self, camera_id: int, ts: int, status: str, temperature=None, humidity=None,
                    confidence=None, image_path=None, raw_text=None, message=None) -> int:
        return self._x(
            "INSERT INTO readings (camera_id, ts, temperature, humidity, status, confidence, image_path,"
            " raw_text, message) VALUES (?,?,?,?,?,?,?,?,?)",
            (camera_id, ts, temperature, humidity, status, confidence, image_path, raw_text, message))

    def get_reading(self, rid: int) -> dict | None:
        r = self._q("SELECT * FROM readings WHERE id=?", (rid,))
        return r[0] if r else None

    def correct_reading(self, rid: int, temperature, humidity) -> None:
        """Overwrite values by hand. The values/status before the first edit are kept for revert."""
        self._x("UPDATE readings SET"
                " orig_temperature = CASE WHEN edited_at IS NULL THEN temperature ELSE orig_temperature END,"
                " orig_humidity = CASE WHEN edited_at IS NULL THEN humidity ELSE orig_humidity END,"
                " orig_status = CASE WHEN edited_at IS NULL THEN status ELSE orig_status END,"
                " temperature=?, humidity=?, status='manual', edited_at=? WHERE id=?",
                (temperature, humidity, int(time.time()), rid))

    def revert_reading(self, rid: int) -> bool:
        """Restore the automatically recorded values of an edited reading."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE readings SET temperature=orig_temperature, humidity=orig_humidity, status=orig_status,"
                " orig_temperature=NULL, orig_humidity=NULL, orig_status=NULL, edited_at=NULL"
                " WHERE id=? AND edited_at IS NOT NULL", (rid,))
            self._conn.commit()
            return cur.rowcount > 0

    def set_status(self, rid: int, status: str) -> None:
        self._x("UPDATE readings SET status=? WHERE id=?", (status, rid))

    def last_reading(self, cam_id: int) -> dict | None:
        r = self._q("SELECT * FROM readings WHERE camera_id=? ORDER BY ts DESC, id DESC LIMIT 1", (cam_id,))
        return r[0] if r else None

    def last_good_reading(self, cam_id: int, since_ts: int = 0) -> dict | None:
        r = self._q("SELECT * FROM readings WHERE camera_id=? AND status IN ('ok','manual') AND ts>=?"
                    " ORDER BY ts DESC LIMIT 1", (cam_id, since_ts))
        return r[0] if r else None

    def recent_statuses(self, cam_id: int, n: int = 10) -> list[str]:
        return [r["status"] for r in self._q(
            "SELECT status FROM readings WHERE camera_id=? ORDER BY ts DESC LIMIT ?", (cam_id, n))]

    def series(self, cam_ids: list[int], start: int, end: int, bucket: int = 0) -> list[dict]:
        """Good readings for charts, optionally averaged into buckets of `bucket` seconds."""
        if not cam_ids:
            return []
        marks = ",".join("?" * len(cam_ids))
        base = (f"FROM readings WHERE camera_id IN ({marks}) AND ts BETWEEN ? AND ?"
                " AND status IN ('ok','manual')")
        args = (*cam_ids, start, end)
        if bucket and bucket > 60:
            return self._q(f"SELECT camera_id, (ts/{int(bucket)})*{int(bucket)} AS ts,"
                           f" AVG(temperature) AS temperature, AVG(humidity) AS humidity,"
                           f" MIN(temperature) AS tmin, MAX(temperature) AS tmax,"
                           f" MIN(humidity) AS hmin, MAX(humidity) AS hmax "
                           f"{base} GROUP BY camera_id, ts/{int(bucket)} ORDER BY ts", args)
        return self._q(f"SELECT camera_id, ts, temperature, humidity {base} ORDER BY ts", args)

    def table(self, cam_ids: list[int] | None, start: int, end: int, statuses: list[str] | None = None,
              limit: int = 100, offset: int = 0) -> tuple[list[dict], int]:
        where = ["ts BETWEEN ? AND ?"]
        args: list = [start, end]
        if cam_ids:
            where.append(f"camera_id IN ({','.join('?' * len(cam_ids))})")
            args += cam_ids
        if statuses:
            where.append(f"status IN ({','.join('?' * len(statuses))})")
            args += statuses
        w = " AND ".join(where)
        total = self._q(f"SELECT COUNT(*) AS n FROM readings WHERE {w}", args)[0]["n"]
        rows = self._q(f"SELECT * FROM readings WHERE {w} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                       (*args, limit, offset))
        return rows, total

    def clear_image(self, rid: int) -> None:
        self._x("UPDATE readings SET image_path=NULL WHERE id=?", (rid,))

    def readings_with_images_before(self, ts: int, good: bool) -> list[dict]:
        cond = "IN ('ok','manual')" if good else "NOT IN ('ok','manual')"
        return self._q(f"SELECT id, image_path FROM readings WHERE image_path IS NOT NULL AND ts<? AND status {cond}",
                       (ts,))

    def image_in_use(self, path: str) -> bool:
        return bool(self._q("SELECT 1 FROM samples WHERE image_path=? UNION SELECT 1 FROM calibrations"
                            " WHERE reference_image=? LIMIT 1", (path, path)))

    # ------------------------------------------------------------- samples
    def add_sample(self, cam_id: int, image_path: str, true_temp, true_hum) -> int:
        return self._x("INSERT INTO samples (camera_id, image_path, true_temp, true_hum, created_at)"
                       " VALUES (?,?,?,?,?)", (cam_id, image_path, true_temp, true_hum, int(time.time())))

    def upsert_sample(self, cam_id: int, image_path: str, true_temp, true_hum) -> None:
        """One sample per image: re-editing a reading updates its sample instead of duplicating it."""
        r = self._q("SELECT id FROM samples WHERE camera_id=? AND image_path=?", (cam_id, image_path))
        if r:
            self._x("UPDATE samples SET true_temp=?, true_hum=? WHERE id=?", (true_temp, true_hum, r[0]["id"]))
        else:
            self.add_sample(cam_id, image_path, true_temp, true_hum)

    def delete_samples_for_image(self, cam_id: int, image_path: str) -> None:
        self._x("DELETE FROM samples WHERE camera_id=? AND image_path=?", (cam_id, image_path))

    # ------------------------------------------------------------- outdoor (AMeDAS)
    def last_outdoor_ts(self, station: str) -> int | None:
        r = self._q("SELECT MAX(ts) AS ts FROM outdoor WHERE station=?", (str(station),))
        return r[0]["ts"] if r else None

    def add_outdoor(self, station: str, rows) -> int:
        rows = [(str(station), int(ts), t, h) for ts, t, h in rows]
        if rows:
            with self._lock:
                self._conn.executemany("INSERT OR REPLACE INTO outdoor (station, ts, temperature, humidity)"
                                       " VALUES (?,?,?,?)", rows)
                self._conn.commit()
        return len(rows)

    def outdoor_series(self, station: str, start: int, end: int, bucket: int = 0) -> list[dict]:
        args = (str(station), start, end)
        if bucket and bucket > 600:
            return self._q(f"SELECT (ts/{int(bucket)})*{int(bucket)} AS ts, AVG(temperature) AS temperature,"
                           f" AVG(humidity) AS humidity FROM outdoor WHERE station=? AND ts BETWEEN ? AND ?"
                           f" GROUP BY ts/{int(bucket)} ORDER BY ts", args)
        return self._q("SELECT ts, temperature, humidity FROM outdoor WHERE station=? AND ts BETWEEN ? AND ?"
                       " ORDER BY ts", args)

    def list_samples(self, cam_id: int) -> list[dict]:
        return self._q("SELECT * FROM samples WHERE camera_id=? ORDER BY id DESC", (cam_id,))

    def delete_sample(self, sid: int) -> None:
        self._x("DELETE FROM samples WHERE id=?", (sid,))
