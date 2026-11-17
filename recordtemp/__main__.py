"""Entry point: python -m recordtemp  (use pythonw.exe for a window-less resident process)."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import Config
from .db import Database
from .scheduler import Recorder, Scheduler

log = logging.getLogger("recordtemp")
MUTEX_NAME = "Local\\RecordTemperatureApp"
_mutex = None


def acquire_single_instance() -> bool:
    """Named mutex so the logon and resume triggers never start a second copy."""
    global _mutex
    if os.name != "nt":
        return True
    import ctypes
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def write_endpoint(port: int) -> None:
    r"""Tell the Windows widget (widget/) where the web API is: %LOCALAPPDATA%\RecordTemperature\endpoint.json"""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return
    path = Path(base) / "RecordTemperature" / "endpoint.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"url": f"http://127.0.0.1:{port}"}), encoding="utf-8")
    except OSError as e:
        log.warning("could not write %s: %s", path, e)


def setup_logging(cfg: Config, console: bool) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(cfg.logs_dir / "recordtemp.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if console:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="recordtemp")
    ap.add_argument("--no-tray", action="store_true", help="タスクトレイアイコンを表示しない")
    ap.add_argument("--open", action="store_true", help="起動時にブラウザを開く")
    ap.add_argument("--port", type=int, help="Web 画面のポート（設定より優先）")
    args = ap.parse_args(argv)

    # pythonw.exe has no console: give libraries somewhere harmless to write
    console = sys.stdout is not None and sys.stderr is not None
    if not console:
        sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")

    cfg = Config()
    setup_logging(cfg, console)

    if not acquire_single_instance():
        log.info("already running; exiting")
        if args.open:
            webbrowser.open(f"http://127.0.0.1:{args.port or cfg['port']}/")
        return 0

    db = Database(cfg.db_path)
    recorder = Recorder(cfg, db)
    scheduler = Scheduler(recorder, cfg["interval_sec"])
    scheduler.start()
    from .amedas import AmedasSyncer
    amedas_syncer = AmedasSyncer(cfg, db)
    amedas_syncer.start()

    import uvicorn
    from .web.app import create_app
    port = args.port or cfg["port"]
    server = uvicorn.Server(uvicorn.Config(create_app(recorder, scheduler, amedas_syncer), host=cfg["host"], port=port,
                                           log_config=None, access_log=False))
    url = f"http://127.0.0.1:{port}/"
    web = threading.Thread(target=server.run, name="web", daemon=True)
    web.start()
    log.info("started: %s (data: %s)", url, cfg.data_dir)
    write_endpoint(port)
    from .overlay import Overlay
    overlay = Overlay(cfg, db, url)
    overlay.start()
    if args.open:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()

    def shutdown():
        log.info("shutting down")
        scheduler.stop()
        amedas_syncer.stop()
        overlay.stop()
        server.should_exit = True

    try:
        if args.no_tray:
            while web.is_alive():
                time.sleep(1)
        else:
            from .tray import run_tray
            run_tray(url, on_capture=lambda: threading.Thread(target=recorder.capture_all, daemon=True).start(),
                     on_quit=shutdown, overlay=overlay)
    except KeyboardInterrupt:
        pass
    finally:
        shutdown()
        web.join(timeout=5)
        scheduler.join(timeout=5)
        overlay.join(timeout=3)
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
