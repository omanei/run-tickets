"""Records docs/demo.gif from a real run of examples/demo.py.

    python examples/record_demo.py            # needs Pillow (pip install pillow)

The text is the runner's actual output, timed as it was printed. Two edits for
readability: the temporary directory is shown as ~, and the "(log: ...)" path after each
step is left out.
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "docs" / "demo.gif"

COLS, ROWS = 92, 26
FONT_SIZE, LINE_H, PAD, BAR = 15, 20, 16, 30
BG, FG, DIM = "#0d1117", "#c9d1d9", "#8b949e"
BLUE, GREEN, RED, YELLOW = "#58a6ff", "#7ee787", "#ff7b72", "#d29922"


def font() -> ImageFont.FreeTypeFont:
    for name in ("DejaVuSansMono.ttf", "Menlo.ttc", "Consolas.ttf", "LiberationMono-Regular.ttf"):
        for base in ("/usr/share/fonts/truetype/dejavu", "/System/Library/Fonts",
                     "C:/Windows/Fonts", "/usr/share/fonts/truetype/liberation", ""):
            try:
                return ImageFont.truetype(os.path.join(base, name) if base else name, FONT_SIZE)
            except OSError:
                continue
    raise SystemExit("no monospace font found")


def capture() -> list:
    """(seconds since start, line) for every line the demo prints."""
    env = dict(os.environ, DEMO_DELAY="0.12", PYTHONUNBUFFERED="1")
    proc = subprocess.Popen([sys.executable, str(HERE / "demo.py")], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env, text=True, encoding="utf-8")
    start, lines = time.monotonic(), []
    for line in proc.stdout:
        line = re.sub(r"\S*run-tickets-demo-[^/\\\s]+[/\\]", "~/", line.rstrip("\n"))
        line = re.sub(r" \(log: [^)]*\)", "", line)
        lines.append((time.monotonic() - start, line))
    proc.wait()
    return lines


def color(line: str) -> str:
    if line.startswith("$ "):
        return GREEN
    if "failed" in line or "STOPPED" in line:
        return RED
    if line.startswith("==>"):
        return YELLOW if "retrying" in line else BLUE
    if line.startswith(("## ", "Done in this run", "  [x]")) or "every ticket is done" in line:
        return GREEN
    if line.startswith(("  [ ]", "next:", "done:")):
        return FG
    return DIM if line.startswith(("summary:", "report:", "  git ", "The work is")) else FG


def render(screen: list, f: ImageFont.FreeTypeFont) -> Image.Image:
    w = PAD * 2 + int(f.getlength("M") * COLS)
    h = BAR + PAD * 2 + LINE_H * ROWS
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w, BAR], fill="#161b22")
    for i, c in enumerate(("#ff5f57", "#febc2e", "#28c840")):
        d.ellipse([14 + i * 20, 10, 26 + i * 20, 22], fill=c)
    d.text((w // 2, BAR // 2), "run-tickets", fill=DIM, font=f, anchor="mm")
    for row, line in enumerate(screen[-ROWS:]):
        text = line if len(line) <= COLS else line[:COLS - 1] + "…"
        d.text((PAD, BAR + PAD + row * LINE_H), text, fill=color(line), font=f)
    return img


def main() -> None:
    f = font()
    frames, durations, screen = [], [], [""]
    lines = capture()
    last = 0.0

    def frame(ms: int) -> None:
        frames.append(render(screen, f))
        durations.append(max(40, ms))

    for t, line in lines:
        if line.startswith("$ "):
            frame(900 if screen != [""] else 300)
            screen.append("$ ")
            for i in range(2, len(line) + 1, 3):  # typing
                screen[-1] = line[:i]
                frame(45)
            screen[-1] = line
            frame(400)
        else:
            frame(int(min(t - last, 1.0) * 1000))
            screen.append(line)
            if "failed" in line:  # the moment the runner catches the agent: hold it
                frame(2500)
        last = t
    frame(5000)
    OUT.parent.mkdir(exist_ok=True)
    frames = [fr.convert("P", palette=Image.ADAPTIVE, colors=32) for fr in frames]
    frames[0].save(OUT, save_all=True, append_images=frames[1:], duration=durations, loop=0,
                   optimize=True, disposal=1)
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB, {len(frames)} frames)")


if __name__ == "__main__":
    main()
