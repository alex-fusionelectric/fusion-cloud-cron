"""regen-splash.py — Generate iOS PWA splash screens for the field
panel. Renders the official F-mark on a dark navy canvas at the
common iPhone resolutions. iOS uses these via apple-touch-startup-image
meta tags; Android draws its splash from the manifest's background_color.

Source: …/02 - Blue on White/Fusion Electric_Blue and White Logo.png
Output: fusion-bid-list/site/field-panel/splash/<W>x<H>.png

Run after any logo refresh, then redeploy.
"""
import sys
from pathlib import Path
from PIL import Image

ROOT = Path("c:/Users/AlexToler/Documents/Fusion Software")
MASTER = Path(
    "C:/Users/AlexToler/FUSION ELECTRIC Dropbox/Fusion Electric Folder/"
    "10- COMPANY FILES/06- LOGOS & DOCUMENT TEMPLATES/"
    "00 - FUSION LOGOS/00 - FE ICON/02 - Blue on White/"
    "Fusion Electric_Blue and White Logo.png"
)
if not MASTER.exists():
    sys.exit(f"Missing source logo: {MASTER}")

OUT_DIR = ROOT / "fusion-bid-list" / "site" / "field-panel" / "splash"
OUT_DIR.mkdir(parents=True, exist_ok=True)
# Also drop into the field panel src so future builds keep them.
SRC_OUT = ROOT / "fusion-field panel" / "src" / "splash"
SRC_OUT.mkdir(parents=True, exist_ok=True)

# Bluish-dark — slate-900 / #0f172a. Matches the consent modal in
# the sign-in flow and pairs nicely with the brand-blue F-only mark
# used here (the splash uses the F-only logo so the black E from the
# home-screen icon doesn't get involved).
BG = (15, 23, 42, 255)


def strip_white(img: Image.Image, threshold: int = 245) -> Image.Image:
    """JPG source has no alpha. Convert near-white pixels to
    transparent so we can find the artwork's bbox and composite it
    onto our own canvas."""
    img = img.convert("RGBA")
    px = img.load()
    w, h = img.size
    for y in range(h):
        for x in range(w):
            r, g, b, _a = px[x, y]
            if r >= threshold and g >= threshold and b >= threshold:
                px[x, y] = (255, 255, 255, 0)
    return img


def trim_alpha(img: Image.Image) -> Image.Image:
    img = img.convert("RGBA")
    bbox = img.split()[3].getbbox()
    return img.crop(bbox) if bbox else img


def splash(art: Image.Image, w: int, h: int, art_pct: float = 0.36) -> Image.Image:
    """Center the F-mark on a w×h dark canvas. art_pct is the target
    fraction of the SHORTER side that the artwork occupies — keeps the
    logo proportionate to the smaller dimension regardless of orientation."""
    canvas = Image.new("RGBA", (w, h), BG)
    target = int(min(w, h) * art_pct)
    sw, sh = art.size
    scale = min(target / sw, target / sh)
    new_w, new_h = max(1, int(sw * scale)), max(1, int(sh * scale))
    resized = art.resize((new_w, new_h), Image.LANCZOS)
    canvas.paste(resized, ((w - new_w) // 2, (h - new_h) // 2), resized)
    return canvas


# Common iPhone splash sizes (portrait). Apple wants the exact
# device pixel resolution. We cover the popular models — older
# devices fall back to the closest match.
SIZES_PORTRAIT = [
    (1290, 2796),  # iPhone 14/15 Pro Max, 16 Plus
    (1179, 2556),  # iPhone 14/15 Pro, 16
    (1284, 2778),  # iPhone 12/13/14 Plus
    (1170, 2532),  # iPhone 12/13/14, 15
    (1125, 2436),  # iPhone X/Xs/11 Pro
    (1080, 2340),  # iPhone 13 mini, 12 mini
    ( 828, 1792),  # iPhone XR/11
    ( 750, 1334),  # iPhone 6/7/8/SE 2
    (1024, 1024),  # universal fallback (square)
]

art = trim_alpha(strip_white(Image.open(MASTER)))
print(f"Source: {MASTER.name}")
for (w, h) in SIZES_PORTRAIT:
    img = splash(art, w, h)
    for d in (OUT_DIR, SRC_OUT):
        dest = d / f"{w}x{h}.png"
        img.save(dest, "PNG", optimize=True)
        print(f"  wrote: {dest.relative_to(ROOT)} ({dest.stat().st_size:,} bytes)")

print("\nDone. Next: add the <link rel='apple-touch-startup-image'> tags")
print("to fusion-field panel/src/index.html and redeploy.")
