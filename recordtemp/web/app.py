"""FastAPI web UI and REST API."""
from __future__ import annotations

import base64
import csv
import io
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import amedas, camera
from ..config import DEFAULTS
from ..scheduler import Recorder
from ..sevenseg import pipeline as P
from ..sevenseg import calibrate as C

HERE = Path(__file__).parent
BAD_STATUSES = ["low_conf", "invalid"]


def _b64(img, ext=".png") -> str:
    ok, buf = cv2.imencode(ext, img)
    return ("data:image/png;base64," if ext == ".png" else "data:image/jpeg;base64,") + \
        base64.b64encode(buf.tobytes()).decode()


def _num(v):
    if v is None or v == "":
        return None
    return float(v)


def create_app(recorder: Recorder, scheduler=None, amedas_syncer=None) -> FastAPI:
    cfg, db = recorder.cfg, recorder.db
    app = FastAPI(title="RecordTemperature")
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")

    def page(name):
        def handler(request: Request):
            return templates.TemplateResponse(request, f"{name}.html", {"page": name})
        return handler

    for name, path in (("dashboard", "/"), ("table", "/table"), ("cameras", "/cameras"),
                       ("review", "/review"), ("settings", "/settings")):
        app.add_api_route(path, page(name), response_class=HTMLResponse, include_in_schema=False)

    @app.get("/calibrate/{cam_id}", response_class=HTMLResponse, include_in_schema=False)
    def calibrate_page(request: Request, cam_id: int):
        cam = db.get_camera(cam_id)
        if not cam:
            raise HTTPException(404)
        return templates.TemplateResponse(request, "calibrate.html", {"page": "cameras", "cam": cam})

    # ---------------------------------------------------------------- files
    @app.get("/files/{rel:path}", include_in_schema=False)
    def files(rel: str):
        p = (cfg.data_dir / rel).resolve()
        if cfg.images_dir.resolve() not in p.parents or not p.is_file():
            raise HTTPException(404)
        return FileResponse(p, headers={"Cache-Control": "no-cache"})

    def file_url(rel: str | None) -> str | None:
        return f"/files/{rel}" if rel else None

    # -------------------------------------------------------------- cameras
    def cam_out(c: dict) -> dict:
        out = dict(c)
        out["has_password"] = bool(c.get("url")) or bool(camera.get_password(c["id"]))
        out["calibrated"] = db.active_calibration(c["id"]) is not None
        return out

    @app.get("/api/cameras")
    def list_cameras():
        return [cam_out(c) for c in db.list_cameras()]

    def _cam_fields(body: dict) -> dict:
        f = {k: body[k] for k in ("name", "host", "port", "username", "stream", "url", "enabled") if k in body}
        if "port" in f:
            f["port"] = int(f["port"] or 554)
        if "enabled" in f:
            f["enabled"] = 1 if f["enabled"] else 0
        for k in ("name", "host", "username", "stream", "url"):
            if k in f:
                f[k] = (f[k] or "").strip()
        return f

    @app.post("/api/cameras")
    def add_camera(body: dict = Body(...)):
        f = _cam_fields(body)
        if not f.get("name"):
            raise HTTPException(400, "名前は必須です")
        if not f.get("host") and not f.get("url"):
            raise HTTPException(400, "IPアドレスまたはURLを入力してください")
        cid = db.add_camera(**f)
        if body.get("password"):
            camera.set_password(cid, body["password"])
        return cam_out(db.get_camera(cid))

    @app.put("/api/cameras/{cam_id}")
    def update_camera(cam_id: int, body: dict = Body(...)):
        if not db.get_camera(cam_id):
            raise HTTPException(404)
        db.update_camera(cam_id, **_cam_fields(body))
        if body.get("password"):
            camera.set_password(cam_id, body["password"])
        return cam_out(db.get_camera(cam_id))

    @app.delete("/api/cameras/{cam_id}")
    def delete_camera(cam_id: int):
        db.delete_camera(cam_id)
        camera.delete_password(cam_id)
        return {"ok": True}

    @app.post("/api/cameras/test")
    def test_camera(body: dict = Body(...)):
        """Try a capture with (possibly unsaved) settings and return a thumbnail."""
        cam = _cam_fields(body)
        cam.setdefault("port", 554)
        cid = body.get("id")
        tmp_id = None
        if body.get("password"):
            tmp_id = -int(time.time() * 1000) % 10**9
            camera.set_password(tmp_id, body["password"])
            cam["id"] = tmp_id
        elif cid:
            cam["id"] = int(cid)
        try:
            t0 = time.time()
            frame = camera.grab_frame(cam, cfg["capture_timeout_sec"])
            h, w = frame.shape[:2]
            thumb = cv2.resize(frame, (640, int(640 * h / w)))
            return {"ok": True, "width": w, "height": h, "elapsed": round(time.time() - t0, 1),
                    "image": _b64(thumb, ".jpg")}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        finally:
            if tmp_id is not None:
                camera.delete_password(tmp_id)

    # --------------------------------------------------------------- status
    @app.get("/api/status")
    def status():
        out = []
        for c in db.list_cameras():
            st = recorder.state.get(c["id"])
            last = db.last_reading(c["id"])
            good = db.last_good_reading(c["id"])
            out.append({
                "camera": cam_out(c),
                "last": last, "last_good": good,
                "messages": st.messages if st else [],
                "align_cc": st.align_cc if st else None,
                "needs_recalibration": bool(st and st.low_align_count >= 5),
                "recent": db.recent_statuses(c["id"], 30),
                "latest_image": "/files/" + recorder.rel(recorder.latest_path(c["id"]))
                if recorder.latest_path(c["id"]).exists() else None,
            })
        return {"cameras": out, "now": int(time.time()),
                "interval": cfg["interval_sec"], "scheduler_alive": bool(scheduler and scheduler.is_alive())}

    @app.post("/api/capture-now")
    def capture_now(body: dict = Body(default={})):
        cid = body.get("camera_id")
        cams = [db.get_camera(int(cid))] if cid else db.list_cameras(enabled_only=True)
        return {"results": {c["id"]: recorder.capture_camera(c) for c in cams if c}}

    # ------------------------------------------------------------- readings
    def _range(start, end, hours=24):
        end = int(end) if end else int(time.time())
        start = int(start) if start else end - hours * 3600
        return start, end

    def _ids(camera_ids: str | None) -> list[int]:
        if camera_ids:
            return [int(x) for x in str(camera_ids).split(",") if x.strip()]
        return [c["id"] for c in db.list_cameras()]

    @app.get("/api/series")
    def series(camera_ids: str | None = None, start: int | None = None, end: int | None = None):
        start, end = _range(start, end)
        span = end - start
        bucket = 0 if span <= 2 * 86400 else max(120, (span // 1500) // 60 * 60)
        rows = db.series(_ids(camera_ids), start, end, bucket)
        out = {"bucket": bucket, "start": start, "end": end, "rows": rows, "outdoor": None}
        if cfg["amedas_enabled"]:
            st = str(cfg["amedas_station"]).strip()
            out["outdoor"] = {"station": st, "name": amedas.station_name(st),
                              "rows": db.outdoor_series(st, start, end, bucket),
                              "error": amedas_syncer.last_error if amedas_syncer else None}
        return out

    @app.get("/api/table")
    def table(camera_ids: str | None = None, start: int | None = None, end: int | None = None,
              status: str | None = None, page: int = 1, per_page: int = 100):
        start, end = _range(start, end)
        statuses = status.split(",") if status else None
        per_page = max(10, min(1000, per_page))
        rows, total = db.table(_ids(camera_ids) if camera_ids else None, start, end, statuses,
                               per_page, (max(1, page) - 1) * per_page)
        for r in rows:
            r["image_url"] = file_url(r["image_path"])
        return {"rows": rows, "total": total, "page": page, "per_page": per_page}

    @app.get("/api/readings.csv")
    def readings_csv(camera_ids: str | None = None, start: int | None = None, end: int | None = None,
                     all_status: int = 0):
        start, end = _range(start, end)
        rows, _ = db.table(_ids(camera_ids) if camera_ids else None, start, end,
                           None if all_status else ["ok", "manual"], 10**7, 0)
        names = {c["id"]: c["name"] for c in db.list_cameras()}
        buf = io.StringIO()
        buf.write("﻿")  # BOM so Excel opens UTF-8 correctly
        w = csv.writer(buf)
        w.writerow(["日時", "カメラ", "温度(℃)", "湿度(%)", "状態", "信頼度", "修正前温度(℃)", "修正前湿度(%)"])
        for r in reversed(rows):
            edited = r.get("edited_at") is not None
            w.writerow([datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M:%S"),
                        names.get(r["camera_id"], r["camera_id"]), r["temperature"], r["humidity"],
                        r["status"], r["confidence"],
                        r["orig_temperature"] if edited else "", r["orig_humidity"] if edited else ""])
        fname = f"readings_{datetime.fromtimestamp(start):%Y%m%d%H%M}-{datetime.fromtimestamp(end):%Y%m%d%H%M}.csv"
        return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                                 headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    # --------------------------------------------------------------- review
    @app.get("/api/review")
    def review(camera_ids: str | None = None, limit: int = 50, days: int = 30):
        end = int(time.time())
        rows, total = db.table(_ids(camera_ids) if camera_ids else None, end - days * 86400, end,
                               BAD_STATUSES, limit, 0)
        rows = [dict(r, image_url=file_url(r["image_path"])) for r in rows if r["image_path"]]
        return {"rows": rows, "total": total}

    @app.post("/api/readings/{rid}/correct")
    def correct(rid: int, body: dict = Body(...)):
        r = db.get_reading(rid)
        if not r:
            raise HTTPException(404)
        try:
            t, h = _num(body.get("temperature")), _num(body.get("humidity"))
        except (TypeError, ValueError):
            raise HTTPException(400, "数値を入力してください")
        if t is None and h is None:
            raise HTTPException(400, "温度か湿度の少なくとも一方を入力してください")
        db.correct_reading(rid, t, h)
        if body.get("add_sample", True) and r["image_path"]:
            db.upsert_sample(r["camera_id"], r["image_path"], t, h)
        return {"ok": True, "reading": db.get_reading(rid)}

    @app.post("/api/readings/{rid}/revert")
    def revert(rid: int):
        r = db.get_reading(rid)
        if not r:
            raise HTTPException(404)
        if not db.revert_reading(rid):
            raise HTTPException(400, "この記録は修正されていません")
        if r["image_path"]:
            db.delete_samples_for_image(r["camera_id"], r["image_path"])
        return {"ok": True, "reading": db.get_reading(rid)}

    @app.post("/api/readings/{rid}/dismiss")
    def dismiss(rid: int):
        r = db.get_reading(rid)
        if not r:
            raise HTTPException(404)
        db.set_status(rid, "dismissed")
        return {"ok": True}

    # ---------------------------------------------------------- calibration
    def _load(rel: str):
        img = camera.load_image(recorder.abs(rel))
        if img is None:
            raise HTTPException(400, f"画像を読み込めません: {rel}")
        return img

    @app.get("/api/cameras/{cam_id}/calib")
    def get_calib(cam_id: int):
        cal = db.active_calibration(cam_id)
        samples = db.list_samples(cam_id)
        return {"calibration": cal and {"params": cal["params"], "reference_image": cal["reference_image"],
                                        "reference_url": file_url(cal["reference_image"]),
                                        "created_at": cal["created_at"]},
                "samples": [dict(s, image_url=file_url(s["image_path"])) for s in samples]}

    @app.post("/api/cameras/{cam_id}/calib/snapshot")
    def calib_snapshot(cam_id: int, body: dict = Body(default={})):
        cam = db.get_camera(cam_id)
        if not cam:
            raise HTTPException(404)
        src = body.get("from")
        if src:   # reuse an existing image (e.g. reference or review image)
            frame = _load(src)
        else:
            try:
                frame = recorder.grab(cam)
            except Exception as e:  # noqa: BLE001
                raise HTTPException(502, f"撮影に失敗しました: {e}")
        p = cfg.images_dir / f"cam{cam_id}" / "calib" / f"snap_{datetime.now():%Y%m%d_%H%M%S}.jpg"
        camera.save_jpeg(p, frame, 95)
        rel = recorder.rel(p)
        return {"snapshot": rel, "url": file_url(rel), "width": frame.shape[1], "height": frame.shape[0]}

    def _display_out(gray, res) -> dict:
        return {"params": res.params, "text": res.text, "value": res.value,
                "confidence": round(res.confidence, 3),
                "warped": _b64(cv2.resize(gray, (gray.shape[1] * 2, gray.shape[0] * 2))),
                "debug": _b64(P.render_debug(res, 2))}

    @app.post("/api/cameras/{cam_id}/calib/preview")
    def calib_preview(cam_id: int, body: dict = Body(...)):
        frame = _load(body["snapshot"])
        out = {}
        for key, dp in (body.get("displays") or {}).items():
            if key not in P.DISPLAY_KEYS or not dp or not dp.get("corners") or len(dp["corners"]) != 4:
                continue
            gray = P.warp_gray(frame, dp["corners"])
            dp = C.propose(gray, dp) if body.get("auto") else dp
            out[key] = _display_out(gray, P.read_gray(gray, dp))
        return {"displays": out}

    def _sample_grays(cam_id: int, ref_frame, key: str, corners, exclude: str | None = None):
        """Warped images of stored samples for one display, aligned to ref_frame."""
        ref_small = P.small_gray(ref_frame)[0]
        grays, truths = [], []
        for s in db.list_samples(cam_id):
            truth = s["true_temp"] if key == "temperature" else s["true_hum"]
            if truth is None or s["image_path"] == exclude:
                continue
            img = camera.load_image(recorder.abs(s["image_path"]))
            if img is None or img.shape != ref_frame.shape:
                continue
            m, _ = P.align(ref_small, img)
            grays.append(P.warp_gray(img, P.transform_corners(corners, m)))
            truths.append(truth)
        return grays, truths

    @app.post("/api/cameras/{cam_id}/calib/fit")
    def calib_fit(cam_id: int, body: dict = Body(...)):
        frame = _load(body["snapshot"])
        truth = body.get("truth") or {}
        out = {}
        for key, dp in (body.get("displays") or {}).items():
            if key not in P.DISPLAY_KEYS or not dp or not dp.get("corners"):
                continue
            gray = P.warp_gray(frame, dp["corners"])
            grays, truths = [], []
            if _num(truth.get(key)) is not None:
                grays.append(gray)
                truths.append(_num(truth[key]))
            if body.get("use_samples", True):
                g2, t2 = _sample_grays(cam_id, frame, key, dp["corners"], exclude=body["snapshot"])
                grays += g2
                truths += t2
            if not grays:
                continue
            best, stats = C.fit(grays, truths, dp)
            res = P.read_gray(gray, best)
            out[key] = dict(_display_out(gray, res), stats=stats)
        return {"displays": out}

    @app.post("/api/cameras/{cam_id}/calib/save")
    def calib_save(cam_id: int, body: dict = Body(...)):
        frame = _load(body["snapshot"])
        displays = {}
        for key, dp in (body.get("displays") or {}).items():
            if key in P.DISPLAY_KEYS and dp and dp.get("corners"):
                gray = P.warp_gray(frame, dp["corners"])
                displays[key] = P.resolve(gray, dp)   # freeze automatic values
        if not displays:
            raise HTTPException(400, "表示領域が設定されていません")
        db.save_calibration(cam_id, {"displays": displays}, body["snapshot"])
        truth = body.get("truth") or {}
        if _num(truth.get("temperature")) is not None or _num(truth.get("humidity")) is not None:
            if not any(s["image_path"] == body["snapshot"] for s in db.list_samples(cam_id)):
                db.add_sample(cam_id, body["snapshot"], _num(truth.get("temperature")),
                              _num(truth.get("humidity")))
        recorder.state.pop(cam_id, None)
        return {"ok": True}

    @app.post("/api/cameras/{cam_id}/calib/refit")
    def calib_refit(cam_id: int):
        cal = db.active_calibration(cam_id)
        if not cal:
            raise HTTPException(400, "校正データがありません")
        ref = _load(cal["reference_image"])
        displays, stats = {}, {}
        for key, dp in cal["params"]["displays"].items():
            grays, truths = _sample_grays(cam_id, ref, key, dp["corners"])
            if not grays:
                displays[key] = dp
                continue
            displays[key], stats[key] = C.fit(grays, truths, dp)
        db.save_calibration(cam_id, {"displays": displays}, cal["reference_image"])
        return {"ok": True, "stats": stats}

    @app.delete("/api/samples/{sid}")
    def delete_sample(sid: int):
        db.delete_sample(sid)
        return {"ok": True}

    # ------------------------------------------------------------- settings
    @app.get("/api/settings")
    def get_settings():
        return cfg.as_dict()

    @app.put("/api/settings")
    def put_settings(body: dict = Body(...)):
        clean = {}
        for k, v in body.items():
            if k not in DEFAULTS:
                continue
            d = DEFAULTS[k]
            try:
                if isinstance(d, bool):
                    v = bool(v)
                elif isinstance(d, int):
                    v = int(v)
                elif isinstance(d, float):
                    v = float(v)
                elif isinstance(d, list):
                    v = [float(x) for x in v]
                elif isinstance(d, str):
                    v = str(v).strip()
            except (TypeError, ValueError):
                raise HTTPException(400, f"不正な値: {k}")
            clean[k] = v
        if "amedas_station" in clean and not clean["amedas_station"].isdigit():
            raise HTTPException(400, "アメダス観測所番号は数字で入力してください（例: 練馬 44071）")
        changed = any(clean.get(k, cfg[k]) != cfg[k] for k in ("amedas_enabled", "amedas_station"))
        cfg.update(clean)
        if changed and cfg["amedas_enabled"] and amedas_syncer:
            threading.Thread(target=amedas_syncer.run_once, daemon=True).start()
        return cfg.as_dict()

    return app
