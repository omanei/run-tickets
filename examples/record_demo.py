"""Records docs/demo.gif from a real run of examples/demo.py.

    pip install pillow pyte
    python examples/record_demo.py            # Linux or macOS: it needs a pty

The demo runs in a pseudo-terminal, so the runner shows its live view, and pyte plays the
terminal: every frame is the screen exactly as a terminal would show it at that moment.
"""

import os
import pty
import select
import struct
import sys
import time
from pathlib import Path

import pyte
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "docs" / "demo.gif"

COLS, ROWS = 100, 34
FONT_SIZE, LINE_H, PAD, BAR = 15, 20, 14, 30
FRAME_EVERY = 0.15  # seconds between frames while the screen changes
BG = "#0d1117"
COLORS = {
    "default": "#c9d1d9", "black": "#484f58", "red": "#ff7b72", "green": "#7ee787",
    "brown": "#d29922", "blue": "#58a6ff", "magenta": "#d2a8ff", "cyan": "#56d4dd",
    "white": "#e6edf3", "brightblack": "#8b949e",
}


def load_font(names: tuple) -> ImageFont.FreeTypeFont:
    for name in names:
        for base in ("/usr/share/fonts/truetype/dejavu", "/Library/Fonts", "/System/Library/Fonts", ""):
            try:
                return ImageFont.truetype(os.path.join(base, name) if base else name, FONT_SIZE)
            except OSError:
                continue
    raise SystemExit(f"none of these fonts found: {', '.join(names)}")


REGULAR = load_font(("DejaVuSansMono.ttf", "Menlo.ttc"))
BOLD = load_font(("DejaVuSansMono-Bold.ttf", "Menlo.ttc"))
ITALIC = load_font(("DejaVuSansMono-Oblique.ttf", "Menlo.ttc"))
SYMBOLS = load_font(("DejaVuSans.ttf", "Apple Symbols.ttf", "Menlo.ttc"))  # the spinner's braille
CELL = int(REGULAR.getlength("M"))


def capture() -> list:
    """(seconds since start, screen copy) whenever the screen changed."""
    screen = pyte.Screen(COLS, ROWS)
    stream = pyte.ByteStream(screen)
    env = dict(os.environ, DEMO_DELAY="0.35", COLUMNS=str(COLS), LINES=str(ROWS),
               TERM="xterm-256color", PYTHONUNBUFFERED="1")
    pid, fd = pty.fork()
    if pid == 0:
        os.execve(sys.executable, [sys.executable, str(HERE / "demo.py")], env)
    import fcntl
    import termios
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
    start, last, frames, dirty = time.monotonic(), 0.0, [], False
    while True:
        ready, _, _ = select.select([fd], [], [], 0.02)
        if ready:
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            stream.feed(data)
            dirty = True
        t = time.monotonic() - start
        if dirty and t - last >= FRAME_EVERY:
            frames.append((t, [[screen.buffer[y][x] for x in range(COLS)] for y in range(ROWS)]))
            last, dirty = t, False
    frames.append((time.monotonic() - start, [[screen.buffer[y][x] for x in range(COLS)] for y in range(ROWS)]))
    os.waitpid(pid, 0)
    return frames


def render(cells: list) -> Image.Image:
    w, h = PAD * 2 + CELL * COLS, BAR + PAD * 2 + LINE_H * ROWS
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w, BAR], fill="#161b22")
    for i, c in enumerate(("#ff5f57", "#febc2e", "#28c840")):
        d.ellipse([14 + i * 20, 10, 26 + i * 20, 22], fill=c)
    d.text((w // 2, BAR // 2), "run-tickets", fill=COLORS["brightblack"], font=REGULAR, anchor="mm")
    for y, row in enumerate(cells):
        for x, ch in enumerate(row):
            fg = COLORS.get(ch.fg, COLORS["default"])
            bg = BG if ch.bg == "default" else COLORS.get(ch.bg, BG)
            if ch.reverse:
                fg, bg = BG, fg
            px, py = PAD + x * CELL, BAR + PAD + y * LINE_H
            if bg != BG:
                d.rectangle([px, py, px + CELL - 1, py + LINE_H - 1], fill=bg)
            if not ch.data.strip():
                continue
            font = (SYMBOLS if "⠀" <= ch.data <= "⣿" else
                    BOLD if ch.bold else ITALIC if ch.italics else REGULAR)
            d.text((px, py + 2), ch.data, fill=fg, font=font)
            if ch.underscore:
                d.line([px, py + LINE_H - 3, px + CELL, py + LINE_H - 3], fill=fg)
    return img


def main() -> None:
    frames = capture()
    rendered = [render(cells) for _, cells in frames]
    # One palette for every frame, taken from a sample of them: per-frame palettes flicker.
    sample = rendered[:: max(1, len(rendered) // 8)] + rendered[-1:]
    sheet = Image.new("RGB", (sample[0].width, sample[0].height * len(sample)))
    for i, img in enumerate(sample):
        sheet.paste(img, (0, i * img.height))
    palette = sheet.quantize(colors=128, method=Image.Quantize.MEDIANCUT)
    images, durations = [], []
    for i, (t, _) in enumerate(frames):
        nxt = frames[i + 1][0] if i + 1 < len(frames) else t + 6
        images.append(rendered[i].quantize(palette=palette, dither=Image.Dither.NONE))
        durations.append(int(min(max(nxt - t, 0.04), 2.5) * 1000))
    durations[-1] = 6000
    OUT.parent.mkdir(exist_ok=True)
    images[0].save(OUT, save_all=True, append_images=images[1:], duration=durations, loop=0,
                   optimize=True, disposal=1)
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB, {len(images)} frames)")


if __name__ == "__main__":
    main()
