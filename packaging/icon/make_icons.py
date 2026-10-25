"""Make the app icons (.icns for macOS, .ico for Windows) from one image.

    python packaging/icon/make_icons.py                  # icon.png -> packaging/build/icon/
    python packaging/icon/make_icons.py --out DIR
    python packaging/icon/make_icons.py --placeholder    # redraw icon.png itself

The source is packaging/icon/icon.png: a square PNG, ideally 1024 x 1024
with a transparent background. To use your own icon, replace that file
and rebuild; nothing else needs to change. The build
(packaging/build_installer.py) runs this every time, so the converted
files are never committed and cannot drift from the source.

macOS wants the artwork inside a rounded square with some transparent
margin (as every app in the Dock has); Windows shows the image as is.

--placeholder redraws icon.png from shapes only (a wrapped gift box, for
"Wrapped"), so it needs no font and comes out the same everywhere.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "icon.png"
DEFAULT_OUT = HERE.parent / "build" / "icon"
NAME = "wikipedia-wrapped"     # the menu shortcut refers to this name

SIZE = 1024
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def draw_placeholder() -> Image.Image:
    """A gift box on a dark-blue rounded square, drawn at 1024 px."""
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Rounded square with the margin macOS icons use (about 10%).
    d.rounded_rectangle((100, 100, 924, 924), radius=185, fill="#24476b")
    # Box body and lid.
    d.rectangle((262, 470, 762, 800), fill="#f4efe4")
    d.rectangle((232, 390, 792, 480), fill="#e8e0cf")
    # Ribbon: a vertical band over lid and body, a horizontal one on the body.
    d.rectangle((482, 390, 542, 800), fill="#c0392b")
    d.rectangle((262, 600, 762, 650), fill="#c0392b")
    # Bow: two loops and a knot.
    d.ellipse((372, 270, 512, 400), outline="#c0392b", width=34)
    d.ellipse((512, 270, 652, 400), outline="#c0392b", width=34)
    d.ellipse((482, 355, 542, 415), fill="#a93226")
    return img


def load_source(path: Path) -> Image.Image:
    img = Image.open(path).convert("RGBA")
    if img.width != img.height:
        raise SystemExit(f"{path} must be square (it is {img.width} x {img.height})")
    if img.width < 512:
        print(f"warning: {path} is {img.width} px; 1024 px looks sharper on Retina screens",
              file=sys.stderr)
    return img.resize((SIZE, SIZE), Image.LANCZOS) if img.width != SIZE else img


def make_icons(source: Path, out: Path) -> list[Path]:
    img = load_source(source)
    out.mkdir(parents=True, exist_ok=True)
    icns = out / f"{NAME}.icns"
    ico = out / f"{NAME}.ico"
    img.save(icns, format="ICNS")
    img.save(ico, format="ICO", sizes=ICO_SIZES)
    return [icns, ico]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--placeholder", action="store_true",
                        help=f"redraw the placeholder into {SOURCE.name} and stop")
    args = parser.parse_args(argv)

    if args.placeholder:
        draw_placeholder().save(args.source, optimize=True)
        print(f"Drew the placeholder icon into {args.source}")
        return 0
    for path in make_icons(args.source, args.out):
        print(f"Wrote {path} ({path.stat().st_size / 1e3:.0f} kB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
