"""Render a terminal session to video frames, from real command output.

Not a recording of a mock: each frame is produced after actually running the
command, so a number that changes in the tool changes in the video. ffmpeg then
stitches the PNG frames into an mp4.

Palette and metrics match site/index.html so the clip drops into the page
without looking pasted in.
"""
import os, re, shlex, subprocess, sys, textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1440, 810
PAD = 56
LINE_H = 34
FONT_SIZE = 21
BAR_H = 54

BG = (11, 15, 20)
BAR = (22, 27, 34)
FG = (201, 209, 217)
DIM = (97, 112, 126)
GREEN = (63, 185, 80)
BLUE = (88, 166, 255)
YELLOW = (210, 153, 34)
RED = (248, 81, 73)
WHITE = (230, 237, 243)
PROMPT = (63, 185, 80)

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\consola.ttf",
    r"C:\Windows\Fonts\lucon.ttf",
    r"C:\Windows\Fonts\cour.ttf",
]


def load_font(size=FONT_SIZE):
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def colorize(line):
    """One colour rule per token class -- [DONE] green, [FINDING] blue, numbers."""
    if line.startswith("[DONE]"):
        return [(line, GREEN)]
    if line.startswith("[PATCH]"):
        rest = line[len("[PATCH]"):]
        return [("[PATCH]", BLUE), (rest, FG)]
    if line.startswith("[FINDING]"):
        rest = line[len("[FINDING]"):]
        return [("[FINDING]", BLUE), (rest, FG)]
    if line.startswith("[WARN]") or "[WARN]" in line:
        return [(line, YELLOW)]
    if line.startswith("[FAIL]") or "[ERROR]" in line:
        return [(line, RED)]
    if line.startswith("$"):
        return [(line, PROMPT)]
    m = re.match(r"^(\s*)([a-zA-Z ]+?)(\s*:\s*)(.*)$", line)
    if m:
        return [(m.group(1) + m.group(2), DIM), (m.group(3), DIM), (m.group(4), WHITE)]
    return [(line, FG)]


class Frame:
    def __init__(self):
        self.lines = []          # list of [(text, colour)]

    def add(self, text, colour=None):
        for physical in (text.split("\n") if "\n" in text else [text]):
            if colour:
                self.lines.append([(physical, colour)])
            else:
                self.lines.append(colorize(physical))

    def add_block(self, text):
        for ln in text.rstrip("\n").split("\n"):
            self.add(ln)


def render(frame, typed=None, cursor=False):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    font = load_font()

    # title bar
    d.rectangle([0, 0, W, BAR_H], fill=BAR)
    for i, c in enumerate([RED, YELLOW, GREEN]):
        cx = 26 + i * 24
        d.ellipse([cx - 7, BAR_H // 2 - 7, cx + 7, BAR_H // 2 + 7], fill=c)
    d.text((W // 2 - 40, BAR_H // 2 - 9), "bash", font=load_font(17), fill=DIM)

    y = BAR_H + PAD - 10
    visible = frame.lines[-22:] if len(frame.lines) > 22 else frame.lines
    for segs in visible:
        x = PAD
        for text, colour in segs:
            d.text((x, y), text, font=font, fill=colour)
            x += d.textlength(text, font=font)
        y += LINE_H

    if typed is not None:
        d.text((PAD, y), "$ ", font=font, fill=PROMPT)
        tx = PAD + d.textlength("$ ", font=font)
        d.text((tx, y), typed, font=font, fill=WHITE)
        if cursor:
            cw = d.textlength(typed, font=font)
            d.rectangle([tx + cw + 2, y + 2, tx + cw + 13, y + FONT_SIZE + 6], fill=GREEN)

    return img


def run(cmd, cwd):
    """Actually execute -- the frame shows what the tool printed, not a script."""
    # `aqlab` is not on PATH in this environment; the venv copy is the installed
    # console script, which is the real entry point a user gets from pip.
    script = os.environ.get("AQLAB_BIN") or str(Path(__file__).resolve().parent.parent / "aqlab.py")
    if script.endswith(".py"):
        cmd = cmd.replace("aqlab ", f'"{sys.executable}" "{script}" ', 1)
    else:
        cmd = cmd.replace("aqlab ", f'"{script}" ', 1)

    p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return (p.stdout or "") + (p.stderr or "")


def main():
    cwd = sys.argv[1]
    outdir = Path(sys.argv[2])
    outdir.mkdir(parents=True, exist_ok=True)

    steps = []
    frame = Frame()

    frame.add("aqlab — ChessAssistant 1.1.10", DIM)
    frame.add("")
    steps.append((frame, None, False, 22))

    commands = [
        ("aqlab scan ChessAssistant.apk",
         "aqlab scan ChessAssistant_1.1.10_TRAPLORD_PRO_FINAL.apk",
         lambda o: [l for l in o.split("\n") if l.strip()][:9]),
        ("aqlab rtl app/",
         "aqlab rtl app",
         lambda o: [l for l in o.split("\n") if l.strip()][-6:]),
        ("aqlab scaffold app/ --locale he",
         "aqlab scaffold app --locale he",
         lambda o: [l for l in o.split("\n") if l.strip()][:6]),
    ]

    for label, actual, keep in commands:
        # typing animation: 3 frames per chunk
        typed = ""
        for i in range(0, len(label) + 1, 4):
            typed = label[:i]
            steps.append((frame, typed, True, 2))
        frame.add("$ " + label, PROMPT)
        steps.append((frame, None, False, 6))

        out = run(actual, cwd)
        for ln in keep(out):
            frame.add(ln)
            steps.append((frame, None, False, 5))
        frame.add("")
        steps.append((frame, None, False, 10))

    # hold the finished run
    for _ in range(30):
        steps.append((frame, None, False, 5))

    n = 0
    for fr, typed, cur, reps in steps:
        img = render(fr, typed, cur)
        for _ in range(reps):
            img.save(outdir / f"f{n:05d}.png")
            n += 1

    print(f"{n} frames -> {outdir}")


if __name__ == "__main__":
    main()
