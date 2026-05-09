"""regen-app-icons.py — Two sources, copied verbatim. No centering,
no padding, no background stripping.

  - icon-192 / icon-512 (PWA home-screen tile)
        ← 02 - Blue on White/Icon.jpg  (blue F + black E)
  - fe-logo.png (everywhere else: login screen, in-app top bars)
        ← 02 - Blue on White/Fusion Electric_Blue and White Logo.png
"""
import shutil
import sys
from pathlib import Path
from PIL import Image

ROOT = Path("c:/Users/AlexToler/Documents/Fusion Software")

ICON_SRC = Path(
    "C:/Users/AlexToler/FUSION ELECTRIC Dropbox/Fusion Electric Folder/"
    "10- COMPANY FILES/06- LOGOS & DOCUMENT TEMPLATES/"
    "00 - FUSION LOGOS/00 - FE ICON/02 - Blue on White/Icon.jpg"
)
LOGO_SRC = Path(
    "C:/Users/AlexToler/FUSION ELECTRIC Dropbox/Fusion Electric Folder/"
    "10- COMPANY FILES/06- LOGOS & DOCUMENT TEMPLATES/"
    "00 - FUSION LOGOS/00 - FE ICON/02 - Blue on White/"
    "Fusion Electric_Blue and White Logo.png"
)
for s in (ICON_SRC, LOGO_SRC):
    if not s.exists():
        sys.exit(f"Missing source: {s}")

# Convert Icon.jpg → PNG once (destinations expect .png). No size or
# composition changes — pixels stay as-authored in Dropbox.
icon_png_bytes = ROOT / "_icon_jpg_as_png.tmp.png"
Image.open(ICON_SRC).convert("RGBA").save(icon_png_bytes, "PNG")

TARGETS = [
    ROOT / "fusion-bid-list" / "site" / "field-panel" / "assets",
    ROOT / "fusion-bid-list" / "site" / "pm-panel"    / "assets",
    ROOT / "fusion-bid-list" / "site" / "bay-bid-list"/ "assets",
    ROOT / "fusion-bid-list" / "site" / "assets",
    ROOT / "fusion-bid-list" / "site",
    ROOT / "fusion-field panel"  / "src" / "assets",
    ROOT / "fusion-bay-bid-list" / "src" / "assets",
    ROOT / "fusion-pm panel"     / "src" / "assets",
    ROOT / "fusion-main-panel"   / "src" / "assets",
]

print(f"Icon source : {ICON_SRC.name} (verbatim)")
print(f"Logo source : {LOGO_SRC.name} (verbatim)")
for tdir in TARGETS:
    if not tdir.exists():
        print(f"  skip (no dir): {tdir}")
        continue
    plan = (
        ("icon-192.png", icon_png_bytes),
        ("icon-512.png", icon_png_bytes),
        ("fe-logo.png",  LOGO_SRC),
    )
    for name, src in plan:
        dest = tdir / name
        if not dest.exists():
            continue
        shutil.copyfile(src, dest)
        print(f"  wrote: {dest.relative_to(ROOT)} ({dest.stat().st_size:,} bytes)")

icon_png_bytes.unlink(missing_ok=True)
print("\nDone. Re-deploy fusion-bid-list/site to push to Netlify.")
