"""Make the app icons (.icns for macOS, .ico for Windows) from one image.

    python packaging/icon/make_icons.py                  # icon.png -> packaging/build/icon/
    python packaging/icon/make_icons.py --out DIR
    python packaging/icon/make_icons.py --compose LOGO   # redraw icon.png from a logo
    python packaging/icon/make_icons.py --placeholder    # redraw icon.png as the old placeholder

The source is packaging/icon/icon.png: a square PNG, 1024 x 1024, with a
transparent margin. The build (packaging/build_installer.py) converts it
every time, so the .icns and .ico are never committed and cannot drift
from it.

--compose makes that source from a logo of any shape: it trims the logo's
empty margins and centers it on a white rounded square, the shape of a
macOS app icon. The plate matters for a line drawing on a transparent
background, which would all but vanish on the dark Dock of dark mode.
--plate sets the square's color; pixels of (nearly) that color in the
logo count as background too, so a drawing on a solid card of that color
keeps only the drawing. The current icon.png was made from the app's logo
with
    --compose app/static/logo/WikiWrapped_Logo_v2.png --plate "#286bb1"
(the white drawing on its own blue). Run it again after changing the
logo, or replace icon.png with a finished design directly.

--placeholder redraws the original placeholder (a gift box drawn from
shapes), in case it is ever wanted again.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageColor, ImageDraw

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "icon.png"
DEFAULT_OUT = HERE.parent / "build" / "icon"
NAME = "wikiwrapped"     # the menu shortcut refers to this name

SIZE = 1024
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]

# The rounded square every icon sits in: about 10% transparent margin,
# as macOS app icons have, and the corner radius of the same shape.
PLATE = (100, 100, 924, 924)
PLATE_RADIUS = 185
# The logo fills at most this much of the plate, centered: a margin inside
# the plate keeps it clear of the rounded corners.
LOGO_BOX = 600


def draw_placeholder() -> Image.Image:
    """A gift box on a dark-blue rounded square, drawn at 1024 px."""
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(PLATE, radius=PLATE_RADIUS, fill="#24476b")
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


def compose(logo_path: Path, plate: str = "#ffffff") -> Image.Image:
    """A logo, trimmed and centered on a rounded square of color `plate`.

    What counts as the logo's empty margin, to trim: transparency, and any
    pixel close to the plate's color. So a logo drawn on a solid background
    of that color (a white drawing on a blue card, say) loses its card and
    keeps only the drawing, which then sits on a plate of the same color.
    """
    logo = Image.open(logo_path).convert("RGBA")
    background = ImageColor.getrgb(plate)

    # Ink: visible (alpha above 16) and unlike the plate (some channel
    # more than 24 away from it).
    diff = ImageChops.difference(logo.convert("RGB"), Image.new("RGB", logo.size, background))
    r, g, b = diff.split()
    unlike = ImageChops.lighter(ImageChops.lighter(r, g), b).point(lambda v: 255 if v > 24 else 0)
    visible = logo.getchannel("A").point(lambda v: 255 if v > 16 else 0)
    logo = logo.crop(ImageChops.multiply(unlike, visible).getbbox())
    scale = LOGO_BOX / max(logo.size)
    logo = logo.resize((round(logo.width * scale), round(logo.height * scale)),
                       Image.LANCZOS)

    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle(
        PLATE, radius=PLATE_RADIUS, fill=plate,
        outline="#d9d9d9" if background == (255, 255, 255) else None,
        width=6)
    corner = ((SIZE - logo.width) // 2, (SIZE - logo.height) // 2)
    img.alpha_composite(logo, corner)
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
    parser.add_argument("--compose", type=Path, metavar="LOGO",
                        help=f"make {SOURCE.name} from LOGO on a white rounded square, and stop")
    parser.add_argument("--plate", default="#ffffff",
                        help="--compose: the rounded square's color, also trimmed "
                             "from the logo as background (default white)")
    parser.add_argument("--placeholder", action="store_true",
                        help=f"redraw the placeholder into {SOURCE.name} and stop")
    args = parser.parse_args(argv)

    if args.compose:
        compose(args.compose, args.plate).save(args.source, optimize=True)
        print(f"Made {args.source} from {args.compose}")
        return 0
    if args.placeholder:
        draw_placeholder().save(args.source, optimize=True)
        print(f"Drew the placeholder icon into {args.source}")
        return 0
    for path in make_icons(args.source, args.out):
        print(f"Wrote {path} ({path.stat().st_size / 1e3:.0f} kB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
