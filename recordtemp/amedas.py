"""Outdoor temperature / humidity from JMA AMeDAS (気象庁アメダス).

Uses the public JSON published for the www.jma.go.jp/bosai/amedas/ pages:
  data/point/{station}/{YYYYMMDD}_{HH}.json  -- 10-minute observations, one file per 3-hour block (JST)
  const/amedastable.json                      -- station list
Only about the last 10 days are available, so the syncer backfills at most that far.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

BASE = "https://www.jma.go.jp/bosai/amedas"
JST = timezone(timedelta(hours=9))
BLOCK_H = 3
MAX_BACKFILL_DAYS = 9
SYNC_EVERY_SEC = 600
USER_AGENT = "RecordTemperature (local thermo-hygrometer logger)"

_station_cache: dict | None = None


def _get_json(url: str, timeout: float = 20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def stations() -> dict:
    global _station_cache
    if _station_cache is None:
        _station_cache = _get_json(f"{BASE}/const/amedastable.json")
    return _station_cache


def station_name(code: str) -> str:
    """Kanji name from the cached station table (never blocks on the network)."""
    return ((_station_cache or {}).get(str(code)) or {}).get("kjName") or str(code)


def block_start(dt: datetime) -> datetime:
    dt = dt.astimezone(JST)
    return dt.replace(hour=dt.hour - dt.hour % BLOCK_H, minute=0, second=0, microsecond=0)


def _value(obs: dict, key: str):
    v = obs.get(key)
    if isinstance(v, list) and v and v[0] is not None:
        return float(v[0])
    return None


def parse_block(data: dict) -> list[tuple[int, float | None, float | None]]:
    rows = []
    for key, obs in data.items():
        dt = datetime.strptime(key, "%Y%m%d%H%M%S").replace(tzinfo=JST)
        t, h = _value(obs, "temp"), _value(obs, "humidity")
        if t is not None or h is not None:
            rows.append((int(dt.timestamp()), t, h))
    return sorted(rows)


def fetch_block(station: str, start: datetime, getter=_get_json) -> list[tuple[int, float | None, float | None]]:
    url = f"{BASE}/data/point/{station}/{start:%Y%m%d}_{start:%H}.json"
    try:
        return parse_block(getter(url))
    except urllib.error.HTTPError as e:
        if e.code == 404:   # not published yet / too old
            return []
        raise


def sync(db, station: str, now: float | None = None, getter=_get_json) -> int:
    """Fetch every 3-hour block from the latest stored observation up to now. Returns rows stored."""
    now_dt = datetime.fromtimestamp(now if now is not None else time.time(), JST)
    oldest = now_dt - timedelta(days=MAX_BACKFILL_DAYS)
    last = db.last_outdoor_ts(station)
    start = block_start(datetime.fromtimestamp(last, JST) if last else oldest)
    start = max(start, block_start(oldest))
    n = 0
    while start <= now_dt:
        rows = fetch_block(station, start, getter)
        n += db.add_outdoor(station, rows)
        start += timedelta(hours=BLOCK_H)
    return n


class AmedasSyncer(threading.Thread):
    """Syncs every 10 minutes while enabled. Wall-clock based, so it catches up right after sleep."""

    def __init__(self, cfg, db):
        super().__init__(name="amedas", daemon=True)
        self.cfg, self.db = cfg, db
        self.stop_event = threading.Event()
        self.last_ok = 0.0
        self.last_error: str | None = None

    def stop(self):
        self.stop_event.set()

    def run_once(self):
        station = str(self.cfg["amedas_station"]).strip()
        try:
            if _station_cache is None:
                stations()
            n = sync(self.db, station)
            self.last_ok, self.last_error = time.time(), None
            if n:
                log.debug("amedas %s: %d rows", station, n)
        except Exception as e:  # noqa: BLE001  network errors etc.: retry next round
            self.last_error = str(e)
            log.warning("amedas sync failed: %s", e)

    def run(self):
        last_try = 0.0
        while not self.stop_event.is_set():
            now = time.time()
            if self.cfg["amedas_enabled"] and now - last_try >= SYNC_EVERY_SEC:
                last_try = now
                self.run_once()
            self.stop_event.wait(15)
