"""Frame capture from Tapo cameras (RTSP) or image files (testing)."""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from urllib.parse import quote

import cv2
import numpy as np

log = logging.getLogger(__name__)

# TCP is far more reliable than UDP for single-frame grabs over Wi-Fi.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

KEYRING_SERVICE = "RecordTemperature"
_mem_secrets: dict[str, str] = {}
_use_keyring = os.environ.get("RECORDTEMP_NO_KEYRING") != "1"


def _key(cam_id: int) -> str:
    return f"camera:{cam_id}"


def set_password(cam_id: int, password: str) -> None:
    """Store a camera password in Windows Credential Manager."""
    if _use_keyring:
        import keyring
        keyring.set_password(KEYRING_SERVICE, _key(cam_id), password)
    else:
        _mem_secrets[_key(cam_id)] = password


def get_password(cam_id: int) -> str:
    if _use_keyring:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, _key(cam_id)) or ""
    return _mem_secrets.get(_key(cam_id), "")


def delete_password(cam_id: int) -> None:
    try:
        if _use_keyring:
            import keyring
            keyring.delete_password(KEYRING_SERVICE, _key(cam_id))
        else:
            _mem_secrets.pop(_key(cam_id), None)
    except Exception:  # noqa: BLE001 - missing entry is fine
        pass


def source_for(cam: dict) -> str:
    """Capture source: explicit url/path override or the Tapo RTSP URL."""
    if cam.get("url"):
        return cam["url"]
    user = quote(cam.get("username") or "", safe="")
    pw = quote(get_password(cam["id"]) if cam.get("id") else "", safe="")
    auth = f"{user}:{pw}@" if user else ""
    return f"rtsp://{auth}{cam['host']}:{int(cam.get('port') or 554)}/{cam.get('stream') or 'stream1'}"


def redact(url: str) -> str:
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        return f"{scheme}://***@{rest.split('@', 1)[1]}"
    return url


class CaptureError(RuntimeError):
    pass


def _is_file_source(src: str) -> bool:
    return src.startswith("file:") or (not src.lower().startswith(("rtsp:", "http:", "https:"))
                                       and Path(src).exists())


_dir_index: dict[str, int] = {}
_dir_lock = threading.Lock()


def _grab_file(src: str) -> np.ndarray:
    if src.startswith("file:///"):
        src = src[8:]
    elif src.startswith("file:"):
        src = src[5:]
    path = Path(src)
    if path.is_dir():  # cycle through images in a folder (for testing / demo)
        files = sorted(p for p in path.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".bmp"))
        if not files:
            raise CaptureError(f"画像がありません: {path}")
        with _dir_lock:
            i = _dir_index.get(str(path), 0)
            _dir_index[str(path)] = i + 1
        path = files[i % len(files)]
    img = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise CaptureError(f"画像を読み込めません: {path}")
    return img


def _grab_rtsp(src: str, timeout_sec: float) -> np.ndarray:
    ms = int(timeout_sec * 1000)
    cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG,
                           [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, ms, cv2.CAP_PROP_READ_TIMEOUT_MSEC, ms])
    try:
        if not cap.isOpened():
            raise CaptureError(f"RTSP 接続に失敗: {redact(src)}")
        # The first frames after connecting can be grey/smeared until a keyframe
        # arrives; read for a short while and keep the last good frame.
        frame, good = None, 0
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline and good < 8:
            ok, f = cap.read()
            if ok and f is not None and f.size:
                frame, good = f, good + 1
            elif frame is None:
                time.sleep(0.05)
            else:
                break
        if frame is None:
            raise CaptureError(f"フレームを取得できません: {redact(src)}")
        return frame
    finally:
        cap.release()


def grab_frame(cam: dict, timeout_sec: float = 15) -> np.ndarray:
    src = source_for(cam)
    if _is_file_source(src):
        return _grab_file(src)
    return _grab_rtsp(src, timeout_sec)


def save_jpeg(path: Path, img: np.ndarray, quality: int = 90) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise IOError(f"JPEG エンコード失敗: {path}")
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(buf.tobytes())
    os.replace(tmp, path)


def load_image(path: Path | str) -> np.ndarray | None:
    try:
        return cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    except (OSError, ValueError):
        return None
