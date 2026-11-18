#!/usr/bin/env python3
"""Draw the plugin's store icon.

The icon is generated rather than hand-drawn so it can be tweaked and rebuilt
instead of being an unexplained binary in the repo:

    python3 tools/make_icon.py

Outputs icon.png (512), icon-256.png and icon-128.png beside the script's
parent directory. Everything is drawn at 4x and downsampled with Lanczos, which
is where the smooth edges come from — there is no SVG rasteriser in the build
image, and a second hand-maintained SVG would drift from this.

Design: a layered isometric cube with a LAN signal mark — the plugin's whole
point is printing to a Creator 5 over the local network, with no cloud account
in the middle. Deliberately bold: the hub shows this small.
"""

import os

from PIL import Image, ImageDraw

SS = 4                     # supersample factor
OUT = 512                  # final edge length
S = OUT * SS

BG_TOP = (27, 44, 78)
BG_BOTTOM = (10, 15, 27)
TEAL = (58, 214, 200)
TEAL_DIM = (40, 150, 146)
ORANGE = (255, 138, 61)
WHITE = (244, 248, 255)

# Pillow renamed the resampling constants; keep working on both sides of 9.1.
try:
    RESAMPLE = Image.Resampling.LANCZOS
    BICUBIC = Image.Resampling.BICUBIC
except AttributeError:                                   # Pillow < 9.1
    RESAMPLE = Image.LANCZOS
    BICUBIC = Image.BICUBIC

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def rounded_mask(size, radius):
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size - 1, size - 1],
                                        radius=radius, fill=255)
    return m


def background():
    """A vertical gradient, clipped to a rounded square."""
    strip = Image.new("RGB", (1, S))
    px = strip.load()
    for y in range(S):
        t = y / (S - 1)
        # ease the ramp so the top stays dark and the mid picks up depth
        t = t ** 0.85
        px[0, y] = tuple(round(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM))
    grad = strip.resize((S, S), BICUBIC)

    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    img.paste(grad, (0, 0), rounded_mask(S, int(S * 0.225)))
    return img


def draw_mark(img):
    d = ImageDraw.Draw(img)
    cx = S / 2

    # -- an isometric cube with printed layer lines ------------------------- #
    # A nozzle glyph was tried first and read as a funnel or a cocktail glass:
    # a taper with something below it is inherently ambiguous at icon scale. A
    # layered cube says "3D printing" with no such risk and survives the hub
    # shrinking it.
    w = S * 0.292        # half-width of the top face
    h = S * 0.352        # height of the top face
    dep = S * 0.204      # how far the side faces drop
    y0 = S * 0.222

    top = (cx, y0)
    right = (cx + w, y0 + h / 2)
    bottom = (cx, y0 + h)
    left = (cx - w, y0 + h / 2)

    d.polygon([right, bottom, (cx, y0 + h + dep), (cx + w, y0 + h / 2 + dep)],
              fill=TEAL_DIM)
    d.polygon([left, bottom, (cx, y0 + h + dep), (cx - w, y0 + h / 2 + dep)],
              fill=TEAL)
    d.polygon([top, right, bottom, left], fill=WHITE)

    # Two gaps split each face into three printed layers; they meet on the
    # front edge and read as a chevron.
    line_w = max(1, int(S * 0.015))
    for frac in (1 / 3, 2 / 3):
        yl = frac * dep
        d.line([(cx - w, y0 + h / 2 + yl), (cx, y0 + h + yl)],
               fill=(18, 28, 48, 255), width=line_w)
        d.line([(cx + w, y0 + h / 2 + yl), (cx, y0 + h + yl)],
               fill=(18, 28, 48, 255), width=line_w)

    # -- signal mark, top right -------------------------------------------- #
    # The one thing the cube cannot say: this prints over the LAN, with no
    # cloud account in the middle.
    sx, sy = S * 0.833, S * 0.158
    for r, wd in ((S * 0.079, int(S * 0.024)), (S * 0.044, int(S * 0.024))):
        box = [sx - r, sy - r, sx + r, sy + r]
        d.arc(box, start=205, end=335, fill=ORANGE, width=wd)
    dr = S * 0.021
    d.ellipse([sx - dr, sy - dr, sx + dr, sy + dr], fill=ORANGE)


def main():
    img = background()
    draw_mark(img)
    final = img.resize((OUT, OUT), RESAMPLE)

    main_path = os.path.join(ROOT, "icon.png")
    final.save(main_path)
    print("wrote", main_path, final.size)

    for size in (256, 128):
        p = os.path.join(ROOT, f"icon-{size}.png")
        final.resize((size, size), RESAMPLE).save(p)
        print("wrote", p)


if __name__ == "__main__":
    main()
