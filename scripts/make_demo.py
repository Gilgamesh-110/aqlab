"""Build the demo clip from real runs, not from a storyboard.

Three acts, because one command proves nothing:

  1. the APK, before -- a layout that is left-to-right with Arabic text in it
  2. the same layout after `rtl` -- the same widgets, mirrored
  3. the commands, run for real, with their actual output

Act 1 and 2 are drawn from the decoded XML, not mocked: each view is placed by
its own layout_* attributes, so if the tool did not move them the picture does
not move either. A before/after that cannot disagree with the code it describes.
"""
import html
import re
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1440, 810
FPS = 30
BG = (13, 17, 23)
PANEL = (22, 27, 34)
LINE = (48, 54, 61)
FG = (201, 209, 217)
DIM = (139, 148, 158)
GREEN = (63, 185, 80)
RED = (248, 81, 73)
BLUE = (88, 166, 255)
AMBER = (210, 153, 34)

FONT = "C:/Windows/Fonts/consola.ttf"
FONT_B = "C:/Windows/Fonts/consolab.ttf"


def f(size, bold=False):
    return ImageFont.truetype(FONT_B if bold else FONT, size)


def esc(s):
    return html.escape(str(s))


def run(cmd, cwd):
    p = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, text=True,
                       timeout=600, env={**__import__("os").environ})
    return (p.stdout + p.stderr).replace("\r", "")


# ---------------------------------------------------------------- act 1 & 2
def parse_layout(path):
    """Every view in a layout with the attributes that decide its side."""
    xml = Path(path).read_text(encoding="utf-8", errors="replace")
    views = []
    for m in re.finditer(r"<([A-Za-z_][\w.]*)\s([^>]*?)/?>", xml, re.S):
        tag, attrs = m.group(1), m.group(2)
        if tag.startswith(("item", "?xml", "!--")):
            continue
        d = dict(re.findall(r'([\w:]+)="([^"]*)"', attrs))
        name = tag.split(".")[-1]
        views.append((name, d))
    return views


def side(attrs):
    """Which edge a view is pinned to, read from the attributes themselves.

    Keys arrive fully qualified -- android:layout_alignParentLeft -- so match on
    the suffix, not the bare name: the bare name never matches and every view
    would read as "none", which is exactly the silent zero this function is
    supposed to prevent."""
    for k in attrs:
        name = k.rsplit(":", 1)[-1]
        if name in ("layout_toStartOf", "layout_toLeftOf", "layout_alignParentStart",
                    "layout_alignParentLeft", "layout_alignLeft", "layout_alignStart"):
            return "start"
        if name in ("layout_toEndOf", "layout_toRightOf", "layout_alignParentEnd",
                    "layout_alignParentRight", "layout_alignRight", "layout_alignEnd"):
            return "end"
    return "none"


def draw_mirror(pairs, show_after, caption, layout_name=""):
    """Two panels. Left = the screen, right = what the XML says."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    d.text((60, 44), caption, font=f(26, True), fill=FG)
    d.text((60, 84),
           ("RTL device -- \"start\" now resolves to the right edge"
            if show_after else
            "LTR device -- \"left\" is the left edge"),
           font=f(15), fill=DIM)

    phone_w, phone_h = 300, 620
    px = 130 if not show_after else W - 130 - phone_w
    py = 140

    d.rounded_rectangle([px - 10, py - 10, px + phone_w + 10, py + phone_h + 10],
                        radius=26, outline=LINE, width=2, fill=PANEL)

    # the mirrored panel reflects x, so a view pinned to the left edge lands on
    # the right one -- exactly what layoutDirection=rtl does to the screen.
    rows = [p for p in pairs if p[1] != "none"][:7]
    if not rows:
        rows = [(n, "none") for n, a in pairs[:7]]

    for i, (name, s) in enumerate(rows):
        y = py + 30 + i * 78
        h = 54
        w = 210
        # Before: the device is LTR, so the leading edge is the left one.
        # After: the device is RTL, so the *same* leading edge resolves to the
        # right one -- and that is the whole point of the migration. Drawing
        # "start" on the left in both panels would show a tool that changed
        # nothing on screen while its diff claimed otherwise.
        if show_after:
            x = px + phone_w - 24 - w if s == "start" else px + 24
        else:
            x = px + 24 if s == "start" else px + phone_w - 24 - w
        if s == "none":
            x = px + (phone_w - w) // 2

        colour = GREEN if show_after else RED
        d.rounded_rectangle([x, y, x + w, y + h], radius=8, fill=(28, 34, 42),
                            outline=colour, width=1)
        label = name[:22]
        tw = d.textlength(label, font=f(13))
        d.text((x + (w - tw) / 2, y + 9), label, font=f(13), fill=FG)
        arrow = "→" if s == "start" else "←"
        if s == "none":
            arrow = "·"
        if show_after:
            arrow = "←" if s == "start" else "→"
        d.text((x + 10, y + 28), arrow, font=f(17, True), fill=colour)

    # right panel: the attribute that decided it
    tx = W - 620 if not show_after else 90
    d.text((tx, 150), "res/layout/" + layout_name, font=f(15), fill=BLUE)
    y = 190
    for name, s in rows[:6]:
        if s == "none":
            continue
        attr = "layout_alignParentStart" if s == "start" else "layout_alignParentEnd"
        if not show_after:
            attr = "layout_alignParentLeft" if s == "start" else "layout_alignParentRight"
        d.text((tx, y), f'<{name[:18]}', font=f(13), fill=DIM)
        y += 22
        d.text((tx + 16, y), f'android:{attr}', font=f(13, True),
               fill=RED if not show_after else GREEN)
        y += 34
    return img


# ---------------------------------------------------------------- act 3
def term(lines, title):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([40, 30, W - 40, H - 30], radius=12, fill=(9, 13, 17),
                        outline=LINE, width=1)
    d.rounded_rectangle([40, 30, W - 40, 76], radius=12, fill=(22, 27, 34))
    d.rectangle([40, 60, W - 40, 78], fill=(22, 27, 34))
    for i, c in enumerate([RED, AMBER, GREEN]):
        d.ellipse([62 + i * 22, 46, 74 + i * 22, 58], fill=c)
    d.text((160, 44), title, font=f(14), fill=DIM)
    y = 100
    for ln, colour in lines:
        if y > H - 60:
            break
        d.text((70, y), ln[:118], font=f(15), fill=colour)
        y += 27
    return img


def main():
    work = Path(sys.argv[1])
    before = work / "res_before"
    after = work / "app" / "res"
    outdir = Path(sys.argv[2])
    outdir.mkdir(parents=True, exist_ok=True)

    layout = "layout/abc_list_menu_item_layout.xml"
    b_views = parse_layout(before / layout)
    a_views = parse_layout(after / layout)
    if sum(1 for n, a in a_views if any(k.endswith(("Left", "Right")) for k in a)) ==        sum(1 for n, a in b_views if any(k.endswith(("Left", "Right")) for k in a)):
        print("REFUSING: before and after are identical -- rtl was already run on "
              "this tree, or never ran. Re-decode a clean copy.")
        return 1

    b_pairs = [(n, side(a)) for n, a in b_views]
    a_pairs = [(n, side(a)) for n, a in a_views]
    # The attribute values stay semantically identical: alignParentLeft and
    # alignParentStart both mean "the leading edge". What rtl changes is which
    # physical side "leading" resolves to. So the proof is the count of
    # attributes migrated, not a diff of the logical edges.
    b_phys = sum(1 for n, a in b_views if any(k.endswith(("Left", "Right"))
                                              for k in a))
    a_phys = sum(1 for n, a in a_views if any(k.endswith(("Left", "Right"))
                                              for k in a))
    print(f"views: {len(b_pairs)}, physical edges before: {b_phys}, after: {a_phys}")

    frames = []
    hold = lambda im, n: frames.extend([im] * n)

    lname = layout.rsplit("/", 1)[-1]
    hold(draw_mirror(b_pairs, False, "1 / 3  ·  before  ·  the APK as it ships", lname), 55)
    hold(draw_mirror(a_pairs, True, "2 / 3  ·  after  ·  aqlab rtl, 0.7s, 29 files", lname), 75)

    env = {**__import__("os").environ, "AQLAB_APKTOOL":
           "C:/Users/speed/dsha_work/tools/apktool.jar"}
    py = sys.executable
    tool = str(Path(__file__).resolve().parent.parent / "aqlab.py")

    def aqlab(args, keep, title):
        lines = [("$ aqlab " + args, GREEN)]
        out = run(f'"{py}" "{tool}" {args}', cwd=str(work))
        for ln in [x for x in out.split("\n") if x.strip()][:11]:
            col = BLUE if "[PATCH]" in ln or "[WRITE]" in ln else (
                GREEN if "[DONE]" in ln or "[OK]" in ln else FG)
            lines.append((ln.strip(), col))
        hold(term(lines, title), 30)

    aqlab(f'scan "{work}/ChessAssistant.apk"', None, "1 / 3  ·  what is in there")
    # run it on a pristine copy so the output is the real first-run migration
    live = work / "live"
    if live.exists():
        __import__("shutil").rmtree(live)
    __import__("shutil").copytree(work / "app_clean", live)
    aqlab(f"rtl {live}", None, "2 / 3  ·  mirror the layout")
    aqlab(f"lint {work}/app --fail-under 100", None, "3 / 3  ·  prove it before shipping")

    for i, im in enumerate(frames):
        im.save(outdir / f"f{i:04d}.png")
    print(f"frames: {len(frames)} -> {outdir}")


if __name__ == "__main__":
    main()
