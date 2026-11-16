import urllib.error
from datetime import datetime

from recordtemp import amedas
from recordtemp.db import Database


def fake_getter(calls, missing=()):
    def get(url):
        calls.append(url)
        name = url.rsplit("/", 1)[1]            # YYYYMMDD_HH.json
        if name in missing:
            raise urllib.error.HTTPError(url, 404, "not found", None, None)
        day, hh = name[:8], int(name[9:11])
        return {f"{day}{h:02d}{m:02d}00": {"temp": [10.0 + h + m / 100, 0], "humidity": [50, 0]}
                for h in range(hh, hh + 3) for m in (0, 10, 20, 30, 40, 50)}
    return get


def jst(*a):
    return datetime(*a, tzinfo=amedas.JST).timestamp()


def test_parse_block_skips_missing_values():
    rows = amedas.parse_block({
        "20261116120000": {"temp": [18.4, 0], "humidity": [37, 0]},
        "20261116121000": {"temp": [None, 5], "humidity": [36, 0]},
        "20261116122000": {"temp": [None, 5]},
    })
    assert rows == [(int(jst(2026, 11, 16, 12)), 18.4, 37.0), (int(jst(2026, 11, 16, 12, 10)), None, 36.0)]


def test_sync_backfills_then_continues_incrementally(tmp_path):
    db = Database(tmp_path / "t.db")
    calls = []
    now = jst(2026, 11, 16, 14, 55)
    n = amedas.sync(db, "44071", now=now, getter=fake_getter(calls, missing={"20261116_15.json"}))
    # 9 days back, aligned to 3-hour blocks, up to the block containing `now`
    assert calls[0].endswith("/44071/20261107_12.json") and calls[-1].endswith("/44071/20261116_12.json")
    assert n == len(calls) * 18
    assert db.last_outdoor_ts("44071") == int(jst(2026, 11, 16, 14, 50))

    calls.clear()
    amedas.sync(db, "44071", now=jst(2026, 11, 16, 15, 20), getter=fake_getter(calls, missing={"20261116_15.json"}))
    # resumes from the block of the last stored row; a not-yet-published block (404) is not an error
    assert [c.rsplit("/", 1)[1] for c in calls] == ["20261116_12.json", "20261116_15.json"]

    rows = db.outdoor_series("44071", int(jst(2026, 11, 16, 12)), int(jst(2026, 11, 16, 13)))
    assert len(rows) == 7 and rows[0]["temperature"] == 22.0 and rows[0]["humidity"] == 50
    avg = db.outdoor_series("44071", int(jst(2026, 11, 16, 0)), int(jst(2026, 11, 17)), bucket=3600)
    assert len(avg) == 15
    db.close()


def test_station_name_uses_cache_only(monkeypatch):
    monkeypatch.setattr(amedas, "_station_cache", None)
    assert amedas.station_name("44071") == "44071"
    monkeypatch.setattr(amedas, "_station_cache", {"44071": {"kjName": "練馬"}})
    assert amedas.station_name("44071") == "練馬"
