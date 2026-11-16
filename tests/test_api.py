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


def test_edit_confirmed_reading_and_revert(client):
    db = client.db
    cid = client.post("/api/cameras", json={"name": "A", "url": str(client.src)}).json()["id"]
    now = int(time.time())
    rid = db.add_reading(cid, now, "ok", 23.5, 56, 0.8, image_path="images/cam1/x.jpg")

    # first edit keeps the automatically recorded values
    r = client.post(f"/api/readings/{rid}/correct", json={"temperature": "24.1", "humidity": "57"}).json()["reading"]
    assert (r["temperature"], r["humidity"], r["status"]) == (24.1, 57, "manual")
    assert (r["orig_temperature"], r["orig_humidity"], r["orig_status"]) == (23.5, 56, "ok")
    # re-edit: original stays, sample is updated rather than duplicated
    r = client.post(f"/api/readings/{rid}/correct", json={"temperature": "24.3", "humidity": ""}).json()["reading"]
    assert (r["temperature"], r["humidity"], r["orig_temperature"]) == (24.3, None, 23.5)
    samples = db.list_samples(cid)
    assert len(samples) == 1 and samples[0]["true_temp"] == 24.3

    csv = client.get(f"/api/readings.csv?start={now - 60}&end={now + 60}").text
    assert "24.3" in csv and "23.5" in csv

    assert client.post(f"/api/readings/{rid}/correct", json={"temperature": "", "humidity": ""}).status_code == 400
    assert client.post(f"/api/readings/{rid}/correct", json={"temperature": "abc"}).status_code == 400

    r = client.post(f"/api/readings/{rid}/revert").json()["reading"]
    assert (r["temperature"], r["humidity"], r["status"], r["edited_at"]) == (23.5, 56, "ok", None)
    assert db.list_samples(cid) == []
    assert client.post(f"/api/readings/{rid}/revert").status_code == 400


def test_migration_adds_edit_columns(tmp_path):
    import sqlite3
    from recordtemp.db import Database
    p = tmp_path / "old.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE readings (id INTEGER PRIMARY KEY, camera_id INTEGER, ts INTEGER, temperature REAL,"
                " humidity REAL, status TEXT, confidence REAL, image_path TEXT, raw_text TEXT, message TEXT)")
    con.commit()
    con.close()
    db = Database(p)
    cols = {r["name"] for r in db._q("PRAGMA table_info(readings)")}
    assert {"orig_temperature", "orig_humidity", "orig_status", "edited_at"} <= cols
    db.close()
