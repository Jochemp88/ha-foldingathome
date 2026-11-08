"""Draw the brand icons (a double helix) for the foldingathome_v8 integration.

Own artwork, not the Folding@home logo, so it can be published with the
integration. Writes icon.png (256), icon@2x.png (512) and dark variants into
custom_components/foldingathome_v8/brand/.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "custom_components" / "foldingathome_v8" / "brand"
SCALE = 4  # supersampling for smooth edges

THEMES = {
    "": {"a": (31, 95, 168), "b": (243, 146, 50), "rung": (150, 160, 175)},
    "dark_": {"a": (96, 165, 250), "b": (251, 170, 80), "rung": (120, 130, 145)},
}


def _point(t: float, size: int, phase: float) -> tuple[float, float]:
    """Point on a strand; t runs 0..1 from top to bottom."""
    margin = size * 0.08
    y = margin + t * (size - 2 * margin)
    amplitude = size * 0.30
    x = size / 2 + amplitude * math.sin(2 * math.pi * (1.25 * t) + phase)
    return x, y


def draw(size: int, theme: dict[str, tuple[int, int, int]]) -> Image.Image:
    big = size * SCALE
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    width = int(big * 0.055)
    steps = 3000

    # Rungs between the strands.
    for i in range(1, 11):
        t = i / 11
        x1, y1 = _point(t, big, 0)
        x2, y2 = _point(t, big, math.pi)
        d.line([(x1, y1), (x2, y2)], fill=theme["rung"] + (255,), width=int(width * 0.45))

    # Strands as continuous runs; the back halves first, so the front covers them.
    runs: dict[bool, list[tuple[list[tuple[float, float]], tuple[int, int, int]]]] = {
        False: [],
        True: [],
    }
    for phase, color in ((0, theme["a"]), (math.pi, theme["b"])):
        current: list[tuple[float, float]] = []
        current_front: bool | None = None
        for k in range(steps + 1):
            t = k / steps
            front = math.cos(2 * math.pi * 1.25 * t + phase) > 0
            if current_front is not None and front != current_front:
                current.append(_point(t, big, phase))
                runs[current_front].append((current, color))
                current = []
            current.append(_point(t, big, phase))
            current_front = front
        runs[current_front].append((current, color))
    # A dense row of discs gives a smooth round-capped stroke (wide PIL lines
    # made of many short segments get jagged joints).
    r = width / 2
    for front in (False, True):
        for points, color in runs[front]:
            for x, y in points:
                d.ellipse([x - r, y - r, x + r, y + r], fill=color + (255,))

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for prefix, theme in THEMES.items():
        for size, name in ((256, "icon.png"), (512, "icon@2x.png")):
            # A 48-colour palette keeps the files at 3-7 KB without visible loss.
            image = draw(size, theme).quantize(colors=48, method=Image.Quantize.FASTOCTREE)
            image.save(OUT / f"{prefix}{name}", optimize=True)
    print("written:", sorted(p.name for p in OUT.iterdir()))


if __name__ == "__main__":
    main()
