import time

import cv2
import pytest
from fastapi.testclient import TestClient

from recordtemp.web.app import create_app
from synth import make_frame


@pytest.fixture
def client(env, tmp_path):
    cfg, db, rec = env
    frame, tc, hc = make_frame("235", "56")
    src = tmp_path / "cam.png"
    cv2.imwrite(str(src), frame)
    c = TestClient(create_app(rec))
    c.src, c.tc, c.hc, c.db = src, tc, hc, db
    return c


def test_camera_crud_and_test(client):
    r = client.post("/api/cameras", json={"name": "A", "host": "192.168.0.10", "username": "u", "password": "secret"})
    assert r.status_code == 200 and r.json()["has_password"]
    cid = r.json()["id"]
    assert "secret" not in r.text
    r = client.put(f"/api/cameras/{cid}", json={"name": "B", "url": str(client.src)})
    assert r.json()["name"] == "B"
    r = client.post("/api/cameras/test", json={"url": str(client.src)})
    assert r.json()["ok"] and r.json()["image"].startswith("data:image/jpeg")
    r = client.post("/api/cameras/test", json={"url": "Z:/nope/missing.png"})
    assert not r.json()["ok"]
    assert client.post("/api/cameras", json={"name": ""}).status_code == 400
    client.delete(f"/api/cameras/{cid}")
    assert client.get("/api/cameras").json() == []


def test_calibration_flow_and_readings(client):
    cid = client.post("/api/cameras", json={"name": "A", "url": str(client.src)}).json()["id"]
    snap = client.post(f"/api/cameras/{cid}/calib/snapshot", json={}).json()
    assert client.get(snap["url"]).status_code == 200

    displays = {"temperature": {"corners": client.tc, "n_digits": 3, "decimals": 1},
                "humidity": {"corners": client.hc, "n_digits": 2, "decimals": 0}}
    r = client.post(f"/api/cameras/{cid}/calib/preview", json={"snapshot": snap["snapshot"], "displays": displays,
                                                              "auto": True}).json()
    assert r["displays"]["temperature"]["value"] == 23.5
    assert r["displays"]["humidity"]["value"] == 56

    r = client.post(f"/api/cameras/{cid}/calib/fit", json={
        "snapshot": snap["snapshot"], "displays": displays, "truth": {"temperature": 23.5, "humidity": 56}}).json()
    assert r["displays"]["temperature"]["stats"]["matches"] == 1
    fitted = {k: v["params"] for k, v in r["displays"].items()}

    assert client.post(f"/api/cameras/{cid}/calib/save", json={
        "snapshot": snap["snapshot"], "displays": fitted, "truth": {"temperature": 23.5, "humidity": 56}}).json()["ok"]
    cal = client.get(f"/api/cameras/{cid}/calib").json()
    assert cal["calibration"] and len(cal["samples"]) == 1

    res = client.post("/api/capture-now", json={"camera_id": cid}).json()["results"]
    assert res[str(cid)]["status"] == "ok"

    st = client.get("/api/status").json()["cameras"][0]
    assert st["last_good"]["temperature"] == 23.5 and st["latest_image"]

    now = int(time.time())
    s = client.get(f"/api/series?camera_ids={cid}&start={now - 3600}&end={now + 60}").json()
    assert len(s["rows"]) == 1 and s["bucket"] == 0
    t = client.get(f"/api/table?start={now - 3600}&end={now + 60}").json()
    assert t["total"] == 1
    csv = client.get(f"/api/readings.csv?start={now - 3600}&end={now + 60}")
    assert "23.5" in csv.text and csv.headers["content-type"].startswith("text/csv")

    # failed reading -> review -> manual correction -> becomes a sample
    rid = client.db.add_reading(cid, now, "invalid", image_path=snap["snapshot"], raw_text="?")
    rv = client.get("/api/review").json()
    assert rv["rows"][0]["id"] == rid
    client.post(f"/api/readings/{rid}/correct", json={"temperature": "23.5", "humidity": "56"})
    assert client.db.get_reading(rid)["status"] == "manual"
    assert client.post(f"/api/cameras/{cid}/calib/refit").json()["stats"]["temperature"]["matches"] >= 1


def test_pages_render(client):
    cid = client.post("/api/cameras", json={"name": "A", "url": str(client.src)}).json()["id"]
    for p in ("/", "/table", "/cameras", "/review", "/settings", f"/calibrate/{cid}"):
        r = client.get(p)
        assert r.status_code == 200 and "温湿度記録" in r.text, p
    assert client.get("/files/../config.json").status_code == 404


def test_settings(client):
    r = client.put("/api/settings", json={"max_temp_jump": "3.5", "unknown": 1})
    assert r.json()["max_temp_jump"] == 3.5 and "unknown" not in r.json()
