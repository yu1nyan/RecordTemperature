"""Matter temperature/humidity sensors (e.g. SwitchBot Hub Mini with its sensor cable), read over the LAN.

The Matter controller runs in a small Node.js program (matter/bridge.mjs, matter.js). This module starts it
as a child process, restarts it when it exits, and reads the latest values from its local HTTP API:

    GET http://127.0.0.1:<matter_port>/health   -> {"ok": true, "nodes": [...]}
    GET http://127.0.0.1:<matter_port>/devices  -> {"devices": [{"id": "1/2/3", "temperature": 24.4, ...}]}

Pairing is done once with `node matter/rtmatter.mjs pair <code>` while the app is stopped.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parent.parent
MATTER_DIR = REPO / "matter"
BRIDGE = MATTER_DIR / "bridge.mjs"
STALE_SEC = 15 * 60          # values older than this are not recorded
CREATE_NO_WINDOW = 0x08000000


class MatterError(Exception):
    """User-presentable (Japanese) error message."""


def find_node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    for p in (Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs" / "node.exe",):
        if p.exists():
            return str(p)
    return None


def _urllib_get(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class MatterBridge:
    def __init__(self, cfg, http_get=None):
        self.cfg = cfg
        self.port = int(cfg["matter_port"])
        self.storage = cfg.data_dir / "matter"
        self.log_path = cfg.logs_dir / "matter-bridge.log"
        self.error: str | None = None     # why the bridge is not running (shown on the cameras page)
        self.proc: subprocess.Popen | None = None
        self._http_get = http_get or _urllib_get
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ process
    def unavailable_reason(self) -> str | None:
        if not self.storage.exists() or not any(self.storage.iterdir()):
            return r"ペアリング済みの機器がありません（node matter\rtmatter.mjs pair で追加してください）"
        if not BRIDGE.exists() or not (MATTER_DIR / "node_modules").exists():
            return r"Matter 連携のプログラムがありません（matter フォルダで npm install を実行してください）"
        if not find_node():
            return "Node.js が見つかりません（Node.js をインストールしてください）"
        return None

    def start(self) -> bool:
        reason = self.unavailable_reason()
        if reason:
            self.error = reason
            log.info("matter bridge not started: %s", reason)
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="matter", daemon=True)
        self._thread.start()
        return True

    def _spawn(self) -> subprocess.Popen:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if self.log_path.exists() and self.log_path.stat().st_size > 1_000_000:
            self.log_path.replace(self.log_path.with_suffix(".log.1"))
        env = dict(os.environ, RECORDTEMP_DATA=str(self.cfg.data_dir), RTMATTER_PORT=str(self.port),
                   RTMATTER_PARENT_PID=str(os.getpid()))
        out = open(self.log_path, "ab")
        try:
            return subprocess.Popen([find_node(), str(BRIDGE)], cwd=str(REPO), env=env, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=subprocess.STDOUT,
                                    creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
        finally:
            out.close()      # the child keeps its own handle

    def _run(self) -> None:
        delay = 5
        while not self._stop.is_set():
            started = time.time()
            try:
                self.proc = self._spawn()
                self.error = None
                log.info("matter bridge started (pid %s, port %s)", self.proc.pid, self.port)
                code = self.proc.wait()
            except OSError as e:
                code = None
                self.error = f"Matter 連携を起動できません: {e}"
            if self._stop.is_set():
                break
            if code is not None:
                self.error = f"Matter 連携が停止しました（終了コード {code}）。再起動します"
            log.warning("%s", self.error)
            delay = 5 if time.time() - started > 300 else min(delay * 2, 300)   # back off when it keeps crashing
            self._stop.wait(delay)

    def stop(self) -> None:
        self._stop.set()
        p = self.proc
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    # ------------------------------------------------------------ HTTP API of the bridge
    def _get(self, path: str) -> dict:
        try:
            return self._http_get(f"http://127.0.0.1:{self.port}{path}", 5)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise MatterError(self.error or "Matter 連携に接続できません（起動直後は数十秒かかります）") from e

    def status(self) -> dict:
        """For the cameras page: whether the bridge runs and which nodes are paired."""
        try:
            health = self._get("/health")
            return {"running": True, "error": None, "nodes": health.get("nodes") or []}
        except MatterError as e:
            return {"running": False, "error": str(e), "nodes": []}

    def devices(self) -> list[dict]:
        return list(self._get("/devices").get("devices") or [])

    def read(self, device_id: str, now: float | None = None) -> tuple[float | None, float | None]:
        """Latest (temperature, humidity) of one device; raises MatterError when there is nothing to record."""
        dev = next((d for d in self.devices() if d.get("id") == device_id), None)
        if dev is None:
            raise MatterError(f"Matter の機器 {device_id} が見つかりません（ペアリングを確認してください）")
        if not dev.get("connected"):
            raise MatterError("Matter の機器に接続できていません")
        t, h = dev.get("temperature"), dev.get("humidity")
        if t is None and h is None:
            raise MatterError("Matter の機器から値をまだ受け取っていません")
        updated = dev.get("updated")
        now = time.time() if now is None else now
        if updated and now - updated > STALE_SEC:
            raise MatterError(f"Matter の機器の値が {int((now - updated) // 60)} 分間更新されていません")
        return (None if t is None else float(t)), (None if h is None else float(h))
