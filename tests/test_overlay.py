import time

import pytest

from recordtemp.climate import absolute_humidity, flu_level
from recordtemp.overlay import pick_camera, summarize, tooltip_text


def test_absolute_humidity_and_flu_level():
    assert absolute_humidity(21.7, 55) == pytest.approx(10.49, abs=0.01)
    assert absolute_humidity(20, 100) == pytest.approx(17.3, abs=0.1)
    assert flu_level(6.9) == "warning"
    assert flu_level(7.0) == "caution"
    assert flu_level(10.99) == "caution"
    assert flu_level(11.0) == "low"


def test_pick_camera():
    cams = [{"id": 1, "enabled": 0}, {"id": 2, "enabled": 1}, {"id": 3, "enabled": 1}]
    assert pick_camera(cams, 3)["id"] == 3
    assert pick_camera(cams, 0)["id"] == 2        # auto: first enabled
    assert pick_camera(cams, 99)["id"] == 2       # deleted camera -> auto
    assert pick_camera([{"id": 1, "enabled": 0}], 0)["id"] == 1
    assert pick_camera([], 0) is None


def test_summarize(env):
    cfg, db, _ = env
    assert summarize(db, cfg).message == "カメラ未登録"

    a = db.add_camera(name="リビング", url="x")
    b = db.add_camera(name="寝室", url="y")
    assert summarize(db, cfg).message == "記録なし"

    now = int(time.time())
    db.add_reading(a, now - 60, "ok", 21.7, 55, 0.8)
    db.add_reading(a, now, "invalid")                       # failed reading is ignored
    db.add_reading(b, now - 3600, "ok", 18.0, 40, 0.8)

    s = summarize(db, cfg, now=now)
    assert (s.name, s.temperature, s.humidity, s.level, s.stale) == ("リビング", 21.7, 55, "caution", False)
    assert "絶対湿度 10.5 g/m³（インフルエンザ 注意）" in tooltip_text(s)

    cfg.update({"overlay_camera_id": b})
    s = summarize(db, cfg, now=now)
    assert (s.name, s.level, s.stale) == ("寝室", "warning", True)   # 18℃ 40% = 6.1 g/m³; 1 hour old
    assert "更新が止まっています" in tooltip_text(s)
