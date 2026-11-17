import json

from recordtemp.__main__ import write_endpoint


def test_write_endpoint_for_widget(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    write_endpoint(8765)
    data = json.loads((tmp_path / "RecordTemperature" / "endpoint.json").read_text(encoding="utf-8"))
    assert data == {"url": "http://127.0.0.1:8765"}


def test_write_endpoint_without_localappdata(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    write_endpoint(8765)   # does nothing, no error
