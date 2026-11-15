"""Notification-area (system tray) icon."""
from __future__ import annotations

import webbrowser

from PIL import Image, ImageDraw


def make_icon(size: int = 64) -> Image.Image:
    """Simple thermometer glyph."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64
    d.rounded_rectangle([4 * s, 4 * s, 60 * s, 60 * s], radius=12 * s, fill=(47, 111, 219, 255))
    d.rounded_rectangle([27 * s, 12 * s, 37 * s, 42 * s], radius=5 * s, fill=(255, 255, 255, 255))
    d.ellipse([22 * s, 36 * s, 42 * s, 56 * s], fill=(255, 255, 255, 255))
    d.ellipse([26 * s, 40 * s, 38 * s, 52 * s], fill=(228, 87, 46, 255))
    d.rectangle([30 * s, 24 * s, 34 * s, 44 * s], fill=(228, 87, 46, 255))
    return img


def run_tray(url: str, on_capture, on_quit) -> None:
    """Blocks until the user chooses 終了."""
    import pystray

    def open_ui(icon=None, item=None):
        webbrowser.open(url)

    def capture(icon, item):
        on_capture()

    def quit_app(icon, item):
        on_quit()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("ブラウザで開く", open_ui, default=True),
        pystray.MenuItem("今すぐ撮影", capture),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("終了", quit_app),
    )
    pystray.Icon("RecordTemperature", make_icon(), "温湿度記録", menu).run()
