"""Matter sensors end to end: bridge status, discovery, registration and recording (bridge HTTP faked)."""
import sqlite3
import time
import urllib.error

import pytest
from fastapi.testclient import TestClient

from recordtemp import matter
from recordtemp.db import Database
from recordtemp.matter import MatterBridge, MatterError
from recordtemp.web.app import create_app


class FakeBridgeHttp:
    """Stands in for matter/bridge.mjs. Tests edit `devices` / `down` to change its answers."""

    def __init__(self):
        self.down = False
        self.urls = []
        self.devices = [{"id": "1/2/3", "node": "1", "name": "SwitchBot Hub Mini", "temperature": 24.4,
                         "humidity": 55.0, "updated": int(time.time()), "connected": True}]

    def __call__(self, url, timeout):
        self.urls.append(url)
        if self.down:
            raise urllib.error.URLError("connection refused")
        if url.endswith("/health"):
            return {"ok": True, "nodes": [{"node": "1", "vendor": "SwitchBot", "product": "SwitchBot Hub Mini",
                                           "connected": True}]}
        if url.endswith("/devices"):
            return {"devices": self.devices}
        raise urllib.error.HTTPError(url, 404, "not found", None, None)


@pytest.fixture
def fake(env):
    cfg, db, rec = env
    http = FakeBridgeHttp()
    rec.matter = MatterBridge(cfg, http_get=http)
    return http


@pytest.fixture
def client(env, fake):
    cfg, db, rec = env
    c = TestClient(create_app(rec))
    c.db, c.rec = db, rec
    return c


def test_bridge_read(env, fake):
    cfg, _, rec = env
    bridge = rec.matter
    assert fake.urls == [] and bridge.read("1/2/3") == (24.4, 55.0)
    assert fake.urls[-1] == f"http://127.0.0.1:{cfg['matter_port']}/devices"

    with pytest.raises(MatterError, match="見つかりません"):
        bridge.read("9/9/9")
    fake.devices[0]["connected"] = False
    with pytest.raises(MatterError, match="接続できていません"):
        bridge.read("1/2/3")
    fake.devices[0].update(connected=True, temperature=None, humidity=None)
    with pytest.raises(MatterError, match="まだ受け取っていません"):
        bridge.read("1/2/3")
    fake.devices[0].update(temperature=20.0, humidity=None)          # humidity-less sensor is fine
    assert bridge.read("1/2/3") == (20.0, None)
    with pytest.raises(MatterError, match="分間更新されていません"):
        bridge.read("1/2/3", now=time.time() + matter.STALE_SEC + 60)
    fake.down = True
    with pytest.raises(MatterError, match="接続できません"):
        bridge.read("1/2/3")
    assert bridge.status() == {"running": False, "error": bridge.status()["error"], "nodes": []}


def test_unavailable_reason(env, tmp_path, monkeypatch):
    cfg, _, _ = env
    bridge = MatterBridge(cfg)
    assert "ペアリング済みの機器がありません" in bridge.unavailable_reason()
    assert bridge.start() is False and "ペアリング" in bridge.error
    (cfg.data_dir / "matter" / "recordtemp-controller").mkdir(parents=True)
    monkeypatch.setattr(matter, "find_node", lambda: None)
    monkeypatch.setattr(matter, "BRIDGE", tmp_path / "bridge.mjs")
    assert "プログラムがありません" in bridge.unavailable_reason()
    monkeypatch.setattr(matter, "BRIDGE", matter.Path(__file__))
    monkeypatch.setattr(matter, "MATTER_DIR", tmp_path)
    (tmp_path / "node_modules").mkdir()
    assert "Node.js が見つかりません" in bridge.unavailable_reason()


def test_status_and_devices_api(client, fake):
    st = client.get("/api/matter").json()
    assert st["running"] is True and st["nodes"][0]["product"] == "SwitchBot Hub Mini"
    devs = client.get("/api/matter/devices").json()["devices"]
    assert [(d["id"], d["temperature"], d["registered"]) for d in devs] == [("1/2/3", 24.4, False)]

    fake.down = True
    st = client.get("/api/matter").json()
    assert st["running"] is False and st["error"]
    assert client.get("/api/matter/devices").status_code == 400

    client.rec.matter = None
    assert client.get("/api/matter").json() == {"running": False, "error": "Matter 連携が起動していません", "nodes": []}


def test_register_and_record_matter_device(client, fake):
    assert client.post("/api/cameras", json={"name": "寝室", "kind": "matter"}).status_code == 400   # no device id
    assert client.post("/api/cameras", json={"name": "x", "kind": "switchbot", "device_id": "1"}).status_code == 400
    cam = client.post("/api/cameras", json={"name": "寝室", "kind": "matter", "device_id": "1/2/3"}).json()
    assert (cam["kind"], cam["device_id"], cam["calibrated"]) == ("matter", "1/2/3", True)
    assert client.get("/api/matter/devices").json()["devices"][0]["registered"] is True

    def capture():
        return client.post("/api/capture-now", json={"camera_id": cam["id"]}).json()["results"][str(cam["id"])]

    res = capture()
    assert (res["status"], res["temperature"], res["humidity"]) == ("ok", 24.4, 55.0)
    st = client.get("/api/status").json()["cameras"][0]
    assert st["last_good"]["temperature"] == 24.4 and st["latest_image"] is None

    fake.devices[0]["temperature"] = 99.0                                  # outside the 70℃ limit
    res = capture()
    assert res["status"] == "invalid" and "範囲外" in res["messages"][0]

    fake.devices[0]["connected"] = False
    res = capture()
    assert res == {"status": "capture_error", "message": "Matter の機器に接続できていません"}

    client.rec.matter = None
    assert capture()["message"] == "Matter 連携が起動していません"

    now = int(time.time())
    rows = client.get(f"/api/table?status=&start={now - 60}&end={now + 60}").json()["rows"]
    assert sorted(r["status"] for r in rows) == ["capture_error", "capture_error", "invalid", "ok"]

    # renaming / disabling works like a camera and keeps the device id
    cam = client.put(f"/api/cameras/{cam['id']}", json={"name": "寝室2", "enabled": False}).json()
    assert (cam["name"], cam["enabled"], cam["device_id"]) == ("寝室2", 0, "1/2/3")


def test_cameras_page_has_matter_card(client):
    page = client.get("/cameras").text
    assert "Matter" in page and "switchbot" not in page.lower()


def test_migration_adds_camera_kind(tmp_path):
    p = tmp_path / "old.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE cameras (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, host TEXT NOT NULL"
                " DEFAULT '', port INTEGER NOT NULL DEFAULT 554, username TEXT NOT NULL DEFAULT '', stream TEXT NOT NULL"
                " DEFAULT 'stream1', url TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,"
                " created_at INTEGER NOT NULL)")
    con.execute("INSERT INTO cameras (name, host, created_at) VALUES ('リビング', '192.168.0.10', 0)")
    con.commit()
    con.close()
    db = Database(p)
    cam = db.list_cameras()[0]
    assert (cam["kind"], cam["device_id"]) == ("camera", "")
    db.close()
