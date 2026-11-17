"""ウィジェット用のアイコンとプレビュー画像を生成する（Pillow が必要）。

    .venv\\Scripts\\python widget\\make_assets.py
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).parent / "Assets"
FONT_DIR = Path("C:/Windows/Fonts")
ACCENT = (43, 123, 214)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for name in (("YuGothB.ttc", "meiryob.ttc") if bold else ("YuGothM.ttc", "meiryo.ttc")):
        if (FONT_DIR / name).exists():
            return ImageFont.truetype(str(FONT_DIR / name), size)
    return ImageFont.load_default(size)


def icon(size: int, background: bool = True) -> Image.Image:
    """温度計のアイコン。4倍で描いて縮小し、輪郭をなめらかにする。"""
    s = size * 4
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if background:
        d.rounded_rectangle((0, 0, s - 1, s - 1), radius=s // 5, fill=ACCENT)
        fg, liquid = (255, 255, 255, 255), (228, 87, 46, 255)
    else:
        fg, liquid = ACCENT + (255,), (228, 87, 46, 255)
    cx, w = s // 2, s // 7
    top, bulb_r = s // 6, s // 6
    bulb_cy = s - s // 4
    d.rounded_rectangle((cx - w, top, cx + w, bulb_cy), radius=w, fill=fg)
    d.ellipse((cx - bulb_r, bulb_cy - bulb_r, cx + bulb_r, bulb_cy + bulb_r), fill=fg)
    inner = max(1, w // 2)
    d.rounded_rectangle((cx - inner, s // 2 - s // 16, cx + inner, bulb_cy), radius=inner, fill=liquid)
    r2 = bulb_r - w // 2
    d.ellipse((cx - r2, bulb_cy - r2, cx + r2, bulb_cy + r2), fill=liquid)
    return im.resize((size, size), Image.LANCZOS)


def screenshot(path: Path, dark: bool) -> None:
    """ウィジェット選択画面に出すプレビュー（中サイズのウィジェットの見本）。"""
    w, h = 300, 304
    bg, fg, sub = ((32, 32, 32), (255, 255, 255), (170, 170, 170)) if dark else ((243, 243, 243), (26, 26, 26), (96, 96, 96))
    im = Image.new("RGB", (w, h), bg)
    d = ImageDraw.Draw(im)
    x = 20
    d.text((x, 20), "リビング", font=font(14), fill=sub)
    d.text((x, 44), "21.7℃", font=font(40, True), fill=fg)
    d.text((x + 150, 44), "55%", font=font(40, True), fill=fg)
    d.text((x, 98), "温度", font=font(13), fill=sub)
    d.text((x + 150, 98), "湿度", font=font(13), fill=sub)
    d.text((x, 136), "絶対湿度 10.5 g/m³", font=font(15), fill=fg)
    d.text((x, 164), "インフルエンザ 注意", font=font(17, True), fill=(202, 128, 16))
    d.text((x, 196), "01:52 時点", font=font(13), fill=sub)
    d.rounded_rectangle((x, 240, w - x, 276), radius=6, outline=sub, width=1)
    d.text((w // 2, 258), "ダッシュボードを開く", font=font(14), fill=fg, anchor="mm")
    im.save(path)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    icon(44).save(OUT / "Square44x44Logo.png")
    icon(150).save(OUT / "Square150x150Logo.png")
    icon(50).save(OUT / "StoreLogo.png")
    icon(64, background=False).save(OUT / "WidgetIcon.png")
    screenshot(OUT / "Screenshot_Light.png", dark=False)
    screenshot(OUT / "Screenshot_Dark.png", dark=True)
    print("generated:", ", ".join(sorted(p.name for p in OUT.glob("*.png"))))


if __name__ == "__main__":
    main()
