"""Small always-on-top window that shows the latest reading of one camera (画面の小窓).

Runs tkinter in its own thread; other threads talk to it only through a command queue.
"""
from __future__ import annotations

import ctypes
import gc
import logging
import queue
import threading
import time
import webbrowser
from ctypes import wintypes
from dataclasses import dataclass

from .climate import FLU_LABEL, absolute_humidity, flu_level

log = logging.getLogger(__name__)

REFRESH_MS = 15_000
VISIBILITY_MS = 2_000
COMMAND_MS = 200

BG, FG, MUTED, STALE = "#1f1f1f", "#f2f2f2", "#a0a0a0", "#8a8a8a"
LEVEL_COLOR = {"low": "#3ccf7f", "caution": "#e6a43a", "warning": "#ef6b6b"}


# ------------------------------------------------------------------ data

@dataclass
class Summary:
    camera_id: int | None
    name: str
    temperature: float | None = None
    humidity: float | None = None
    abs_humidity: float | None = None
    level: str | None = None        # low / caution / warning
    ts: int | None = None
    stale: bool = False
    message: str = ""               # shown instead of values (e.g. no camera)


def pick_camera(cameras: list[dict], wanted: int) -> dict | None:
    """The configured camera, else the first enabled one, else the first one."""
    return (next((c for c in cameras if c["id"] == wanted), None)
            or next((c for c in cameras if c["enabled"]), None)
            or (cameras[0] if cameras else None))


def summarize(db, cfg, now: float | None = None) -> Summary:
    now = time.time() if now is None else now
    cam = pick_camera(db.list_cameras(), int(cfg["overlay_camera_id"] or 0))
    if cam is None:
        return Summary(None, "温湿度", message="カメラ未登録")
    s = Summary(cam["id"], cam["name"])
    good = db.last_good_reading(cam["id"])
    if not good:
        s.message = "記録なし"
        return s
    s.temperature, s.humidity, s.ts = good["temperature"], good["humidity"], good["ts"]
    s.stale = now - good["ts"] > cfg["interval_sec"] * 5
    if s.temperature is not None and s.humidity is not None:
        s.abs_humidity = absolute_humidity(s.temperature, s.humidity)
        s.level = flu_level(s.abs_humidity)
    return s


def tooltip_text(s: Summary) -> str:
    if s.message:
        return f"{s.name}: {s.message}"
    lines = [s.name]
    lines.append(f"温度 {_fmt(s.temperature, 1)}℃ / 湿度 {_fmt(s.humidity, 0)}%")
    if s.abs_humidity is not None:
        lines.append(f"絶対湿度 {s.abs_humidity:.1f} g/m³（インフルエンザ {FLU_LABEL[s.level]}）")
    if s.ts:
        lines.append(time.strftime("%m/%d %H:%M 時点", time.localtime(s.ts)) + ("（更新が止まっています）" if s.stale else ""))
    return "\n".join(lines)


def _fmt(v: float | None, digits: int) -> str:
    return "--" if v is None else f"{v:.{digits}f}"


# ------------------------------------------------------------------ Win32 helpers

class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", _RECT), ("rcWork", _RECT), ("dwFlags", wintypes.DWORD)]


_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


def set_dpi_aware() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per-monitor
    except (AttributeError, OSError):
        pass


def work_area() -> tuple[int, int, int, int]:
    """Primary monitor area excluding the taskbar: (left, top, right, bottom)."""
    r = _RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)   # SPI_GETWORKAREA
    return r.left, r.top, r.right, r.bottom


def virtual_screen() -> tuple[int, int, int, int]:
    """Bounding box of all monitors: (left, top, right, bottom)."""
    m = ctypes.windll.user32.GetSystemMetrics
    x, y = m(76), m(77)
    return x, y, x + m(78), y + m(79)


def foreground_is_fullscreen(exclude: set[int]) -> bool:
    """True while a full-screen app (video, game, presentation) is in front."""
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd or hwnd in exclude:
        return False
    buf = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, buf, 64)
    if buf.value in _SHELL_CLASSES:
        return False
    rect = _RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    mi = _MONITORINFO(cbSize=ctypes.sizeof(_MONITORINFO))
    if not user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(mi)):
        return False
    m = mi.rcMonitor
    return rect.left <= m.left and rect.top <= m.top and rect.right >= m.right and rect.bottom >= m.bottom


def round_corners(hwnd: int) -> None:
    """Windows 11 rounded corners (ignored on older Windows)."""
    try:
        pref = ctypes.c_int(2)   # DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
    except (AttributeError, OSError):
        pass


# ------------------------------------------------------------------ window

class Overlay(threading.Thread):
    def __init__(self, cfg, db, url: str):
        super().__init__(name="overlay", daemon=True)
        self.cfg, self.db, self.url = cfg, db, url
        self.commands: queue.Queue[str] = queue.Queue()
        self.user_hidden = False        # hidden from the menu until shown again from the tray
        self._shown = False

    # --- called from other threads
    @property
    def visible(self) -> bool:
        return bool(self.cfg["overlay_enabled"]) and not self.user_hidden

    def toggle(self) -> None:
        self.commands.put("toggle")

    def refresh_soon(self) -> None:
        self.commands.put("refresh")

    def stop(self) -> None:
        self.commands.put("quit")

    # --- overlay thread
    def run(self) -> None:
        try:
            self._run()
        except Exception:  # noqa: BLE001  never take the recorder down with the window
            log.exception("overlay stopped")

    def _run(self) -> None:
        import tkinter as tk
        from tkinter import font as tkfont

        set_dpi_aware()
        self.tk = tk
        root = self.root = tk.Tk()
        root.withdraw()
        root.title("温湿度")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", float(self.cfg["overlay_alpha"]))
        root.configure(bg=BG)

        base = tkfont.Font(family="Yu Gothic UI", size=11)
        bold = tkfont.Font(family="Yu Gothic UI", size=13, weight="bold")
        frame = tk.Frame(root, bg=BG, padx=10, pady=4)
        frame.pack()
        self.lbl_name = tk.Label(frame, bg=BG, fg=MUTED, font=base)
        self.lbl_temp = tk.Label(frame, bg=BG, fg=FG, font=bold)
        self.lbl_hum = tk.Label(frame, bg=BG, fg=FG, font=bold)
        self.lbl_level = tk.Label(frame, bg=BG, fg=MUTED, font=base)
        for i, w in enumerate((self.lbl_name, self.lbl_temp, self.lbl_hum, self.lbl_level)):
            w.grid(row=0, column=i, padx=(0 if i == 0 else 8, 0))

        self.tip = tk.Toplevel(root)
        self.tip.withdraw()
        self.tip.overrideredirect(True)
        self.tip.attributes("-topmost", True)
        self.tip_label = tk.Label(self.tip, bg="#2b2b2b", fg=FG, font=base, justify="left", padx=10, pady=6)
        self.tip_label.pack()

        self.camera_var = tk.IntVar(value=int(self.cfg["overlay_camera_id"] or 0))
        self.menu = tk.Menu(root, tearoff=0)

        for w in (root, frame, self.lbl_name, self.lbl_temp, self.lbl_hum, self.lbl_level):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<ButtonRelease-1>", self._drag_end)
            w.bind("<Double-Button-1>", lambda e: webbrowser.open(self.url))
            w.bind("<Button-3>", self._show_menu)
            w.bind("<Enter>", self._tip_show)
            w.bind("<Leave>", self._tip_hide)

        self.summary = Summary(None, "温湿度", message="読み込み中")
        self._render()
        root.update_idletasks()
        self.hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        round_corners(self.hwnd)
        self._place_initial()

        self._refresh()
        self._check_visibility()
        self._poll_commands()
        root.mainloop()

        # Release every Tk object in this thread. Left to the garbage collector they would be
        # deleted from the main thread at exit, which makes Tcl abort ("deleted by the wrong thread").
        del root, frame, base, bold
        for name in ("camera_var", "menu", "tip_label", "tip", "lbl_name", "lbl_temp", "lbl_hum", "lbl_level",
                     "root", "tk"):
            setattr(self, name, None)
        gc.collect()

    # --- periodic jobs
    def _poll_commands(self) -> None:
        try:
            while True:
                cmd = self.commands.get_nowait()
                if cmd == "quit":
                    self.root.destroy()
                    return
                if cmd == "toggle":
                    if self.visible:
                        self.user_hidden = True
                    else:
                        self.user_hidden = False
                        if not self.cfg["overlay_enabled"]:
                            self.cfg.update({"overlay_enabled": True})
                    self._check_visibility(reschedule=False)
                if cmd == "refresh":
                    self._refresh(reschedule=False)
        except queue.Empty:
            pass
        self.root.after(COMMAND_MS, self._poll_commands)

    def _refresh(self, reschedule: bool = True) -> None:
        try:
            self.summary = summarize(self.db, self.cfg)
            if (int(self.cfg["overlay_x"]), int(self.cfg["overlay_y"])) != self._applied_pos:
                self._place_initial()     # position changed from the settings page
            self.root.attributes("-alpha", min(1.0, max(0.3, float(self.cfg["overlay_alpha"]))))
            self._render()
        except Exception:  # noqa: BLE001
            log.exception("overlay refresh failed")
        if reschedule:
            self.root.after(REFRESH_MS, self._refresh)

    def _check_visibility(self, reschedule: bool = True) -> None:
        want = self.visible and not foreground_is_fullscreen({self.hwnd})
        if want and not self._shown:
            self.root.deiconify()
            self._clamp_to_screen()
        elif not want and self._shown:
            self.root.withdraw()
            self.tip.withdraw()
        self._shown = want
        if want:
            self.root.attributes("-topmost", True)   # stay above other topmost windows
        if reschedule:
            self.root.after(VISIBILITY_MS, self._check_visibility)

    # --- drawing
    def _render(self) -> None:
        s = self.summary
        self.lbl_name.config(text=s.name)
        if s.message:
            values = ("", "", s.message)
            colors = (FG, FG, MUTED)
        else:
            level_text = "更新停止" if s.stale else FLU_LABEL.get(s.level, "")
            level_color = STALE if s.stale else LEVEL_COLOR.get(s.level, MUTED)
            values = (f"{_fmt(s.temperature, 1)}℃", f"{_fmt(s.humidity, 0)}%", level_text)
            colors = (STALE if s.stale else FG, STALE if s.stale else FG, level_color)
        for lbl, text, color in zip((self.lbl_temp, self.lbl_hum, self.lbl_level), values, colors):
            lbl.config(text=text, fg=color)
            if text:
                lbl.grid()
            else:
                lbl.grid_remove()
        self.tip_label.config(text=tooltip_text(s))

    # --- position
    def _place_initial(self) -> None:
        x, y = int(self.cfg["overlay_x"]), int(self.cfg["overlay_y"])
        self._applied_pos = (x, y)
        if x < 0 or y < 0:
            left, top, right, bottom = work_area()
            x = right - self.root.winfo_reqwidth() - 16
            y = bottom - self.root.winfo_reqheight() - 16
        self.root.geometry(f"+{x}+{y}")

    def _clamp_to_screen(self) -> None:
        """Bring the window back if it ended up off-screen (e.g. a monitor was disconnected)."""
        self.root.update_idletasks()
        left, top, right, bottom = virtual_screen()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        x = min(max(self.root.winfo_x(), left), right - w)
        y = min(max(self.root.winfo_y(), top), bottom - h)
        if (x, y) != (self.root.winfo_x(), self.root.winfo_y()):
            self.root.geometry(f"+{x}+{y}")

    def _drag_start(self, e) -> None:
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y(), False)
        self._tip_hide()

    def _drag_move(self, e) -> None:
        dx, dy, _ = self._drag
        self.root.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")
        self._drag = (dx, dy, True)

    def _drag_end(self, e) -> None:
        if getattr(self, "_drag", (0, 0, False))[2]:
            self._applied_pos = (self.root.winfo_x(), self.root.winfo_y())
            self.cfg.update({"overlay_x": self._applied_pos[0], "overlay_y": self._applied_pos[1]})

    # --- tooltip and menu
    def _tip_show(self, e=None) -> None:
        self.tip.update_idletasks()
        th = self.tip.winfo_reqheight()
        x, y = self.root.winfo_x(), self.root.winfo_y() - th - 6
        if y < virtual_screen()[1]:
            y = self.root.winfo_y() + self.root.winfo_height() + 6
        self.tip.geometry(f"+{x}+{y}")
        self.tip.deiconify()
        self.tip.lift()

    def _tip_hide(self, e=None) -> None:
        self.tip.withdraw()

    def _show_menu(self, e) -> None:
        tk = self.tk
        self._tip_hide()
        self.menu.delete(0, "end")
        cams = tk.Menu(self.menu, tearoff=0)
        current = self.summary.camera_id or 0
        self.camera_var.set(current)
        for c in self.db.list_cameras():
            cams.add_radiobutton(label=c["name"], value=c["id"], variable=self.camera_var,
                                 command=lambda cid=c["id"]: self._select_camera(cid))
        self.menu.add_cascade(label="表示するカメラ", menu=cams)
        self.menu.add_command(label="ダッシュボードを開く", command=lambda: webbrowser.open(self.url))
        self.menu.add_separator()
        self.menu.add_command(label="小窓を隠す（トレイのメニューから再表示）", command=self.toggle)
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def _select_camera(self, cid: int) -> None:
        self.cfg.update({"overlay_camera_id": cid})
        self._refresh(reschedule=False)
