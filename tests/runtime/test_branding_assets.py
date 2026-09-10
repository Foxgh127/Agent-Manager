from pathlib import Path
from PIL import Image, ImageChops


def test_native_icon_sizes_have_real_transparent_corners_and_match_app_artwork():
    root = Path(__file__).resolve().parents[2]
    png = Image.open(root / "frontend/public/app-icon.png").convert("RGBA")
    assert png.size == (512, 512)
    ico = Image.open(root / "packaging/app-icon.ico")
    assert {(16, 16), (24, 24), (32, 32), (48, 48), (256, 256)} <= ico.ico.sizes()
    for size in ico.ico.sizes():
        frame = ico.ico.getimage(size).convert("RGBA")
        # Tiny ICO sizes can acquire a sub-1% Lanczos antialiasing fringe.
        assert frame.getpixel((0, 0))[3] <= 2
        assert frame.getpixel((size[0] // 2, size[1] // 2))[3] == 255
    reference = png.resize((256, 256), Image.Resampling.LANCZOS)
    assert ImageChops.difference(reference, ico.ico.getimage((256, 256)).convert("RGBA")).getbbox(alpha_only=False) is None
