"""Draws docs/social-preview.png, the repository's social preview card (1280x640).

    python examples/make_social_preview.py     # needs Pillow and the Inter font

Upload the result in Settings -> General -> Social preview. Everything that matters stays
inside the 40 pt safe border GitHub's template asks for.
"""

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "docs" / "social-preview.png"


def font(names, size):
    dirs = ["/usr/share/fonts/opentype/inter", "/usr/share/fonts/truetype/dejavu",
            os.path.expanduser("~/Library/Fonts"), "/Library/Fonts", "C:/Windows/Fonts", ""]
    for name in names:
        for d in dirs:
            try:
                return ImageFont.truetype(os.path.join(d, name) if d else name, size)
            except OSError:
                continue
    raise SystemExit(f"none of the fonts {names} found")


W, H = 1280, 640
BG, PANEL, BAR = "#0d1117", "#161b22", "#21262d"
FG, DIM, BLUE, GREEN, RED, YELLOW = "#e6edf3", "#8b949e", "#58a6ff", "#7ee787", "#ff7b72", "#d29922"
title_f = font(["InterDisplay-Bold.otf", "Inter-Bold.otf", "Inter-Bold.ttf"], 84)
tag_f = font(["Inter-Regular.otf", "Inter-Regular.ttf"], 30)
small_f = font(["Inter-Regular.otf", "Inter-Regular.ttf"], 23)
chip_f = font(["Inter-SemiBold.otf", "Inter-SemiBold.ttf"], 22)
mono = font(["DejaVuSansMono.ttf", "Menlo.ttc", "Consolas.ttf"], 18)

img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)

# Left: name, tagline, agents. Everything stays inside an 80 px safe margin.
x0 = 88
d.text((x0, 150), "run-tickets", font=title_f, fill=FG)
d.text((x0, 262), "Run your tickets overnight", font=tag_f, fill=FG)
d.text((x0, 302), "with a coding agent.", font=tag_f, fill=FG)
for i, line in enumerate(["One ticket at a time, a fresh agent per step,",
                          "every step checked by the runner."]):
    d.text((x0, 362 + i * 34), line, font=small_f, fill=DIM)
cx = x0
for name in ("Claude Code", "Codex", "omp", "pi"):
    w = int(d.textlength(name, font=chip_f)) + 32
    d.rounded_rectangle([cx, 462, cx + w, 504], radius=21, outline="#30363d", width=2, fill=PANEL)
    d.text((cx + 16, 483), name, font=chip_f, fill=FG, anchor="lm")
    cx += w + 12

# Right: a terminal with the moment the runner catches the agent.
tx0, ty0, tx1, ty1 = 700, 112, 1192, 528
d.rounded_rectangle([tx0, ty0, tx1, ty1], radius=14, fill=PANEL, outline="#30363d", width=2)
d.rounded_rectangle([tx0, ty0, tx1, ty0 + 38], radius=14, fill=BAR)
d.rectangle([tx0, ty0 + 24, tx1, ty0 + 38], fill=BAR)
for i, c in enumerate(("#ff5f57", "#febc2e", "#28c840")):
    d.ellipse([tx0 + 16 + i * 22, ty0 + 13, tx0 + 28 + i * 22, ty0 + 25], fill=c)
lines = [
    ("$ run-tickets run .scratch/export", GREEN),
    ("==> 02-export: review", BLUE),
    ("all done, every criterion is met", FG),
    ("==> 02-export: review failed", RED),
    ("    1 criterion is not ticked", RED),
    ("    retrying with a fresh agent", YELLOW),
    ("==> 02-export: review", BLUE),
    ("==> 02-export done: 1 commit", BLUE),
    ("==> final review: done", BLUE),
    ("export: 3/3 done", GREEN),
]
for i, (text, color) in enumerate(lines):
    d.text((tx0 + 22, ty0 + 58 + i * 34), text, font=mono, fill=color)

img.save(OUT, optimize=True)
print(f"wrote {OUT}")
