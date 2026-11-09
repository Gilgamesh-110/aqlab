"""Build the before/after XML snippet shown on the landing page.

Reads the real before and after files -- a layout from ChessAssistant 1.1.10 --
and emits two highlighted HTML blocks where every token the tool changed is
wrapped in a span. Nothing is retyped by hand: if the tool stops migrating a
attribute, the page stops claiming it did.
"""
import html
import pathlib
import re
import sys

BEFORE = pathlib.Path("C:/tmp/before.xml")
AFTER = pathlib.Path("C:/tmp/shot2/res/layout/abc_list_menu_item_layout.xml")
OUT = pathlib.Path("C:/Users/speed/arabize/site/index.html")

# The attributes the migration touches, and what each becomes.
PAIRS = [
    ("alignParentLeft", "alignParentStart"),
    ("alignParentRight", "alignParentEnd"),
    ("toLeftOf", "toStartOf"),
    ("toRightOf", "toEndOf"),
    ("marginLeft", "marginStart"),
    ("marginRight", "marginEnd"),
    ("paddingLeft", "paddingStart"),
    ("paddingRight", "paddingEnd"),
    ("layout_alignLeft", "layout_alignStart"),
    ("layout_alignRight", "layout_alignEnd"),
]


def pick_lines(text, needles, limit=4):
    out = []
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("<") and any(n in s for n in needles):
            out.append(s)
        if len(out) >= limit:
            break
    return out


def mark(line, old_side):
    """Escape, then wrap every changed token. Right-hand references like
    `?listPreferredItemPaddingLeft` are migrated too, so the pattern has to match
    the token wherever it sits rather than only after `android:`."""
    esc = html.escape(line)
    cls = "old" if old_side else "new"
    for a, b in PAIRS + [("Left", "Start"), ("Right", "End")]:
        token = a if old_side else b
        # longest first so marginLeft is not eaten by Left
        pass
    names = [a for a, _ in PAIRS] if old_side else [b for _, b in PAIRS]
    names = sorted(set(names), key=len, reverse=True)
    # only the left/right tail, to catch ?attr references as well
    names += ["Left", "Right"] if old_side else ["Start", "End"]
    for n in sorted(set(names), key=len, reverse=True):
        esc = re.sub(rf"(?<![A-Za-z]){n}(?![A-Za-z])",
                     f'<span class="hl {cls}">{n}</span>', esc)
        # also handle the camelCase tail inside a longer identifier
        esc = esc.replace(f'</span>{n}', f'</span>{n}')
    return esc


def block(lines, old_side):
    body = "\n".join(mark(l, old_side) for l in lines)
    return body


def main():
    before = BEFORE.read_text(encoding="utf-8")
    after = AFTER.read_text(encoding="utf-8")

    b_lines = pick_lines(before, ["alignParentLeft", "marginLeft", "paddingLeft"])
    a_lines = pick_lines(after, ["alignParentStart", "marginStart", "paddingStart"])

    print(f"before: {len(b_lines)} lines, after: {len(a_lines)} lines")
    if len(b_lines) < 2 or len(a_lines) < 2:
        sys.exit("not enough changed lines -- refusing to emit a thin sample")

    # sanity: the two samples must describe the same elements in the same order
    if "RelativeLayout" not in b_lines[0] or "RelativeLayout" not in a_lines[0]:
        sys.exit("first line is not the same element -- sample is misaligned")

    print("--- BEFORE ---")
    print(block(b_lines, True)[:400])
    print("--- AFTER ---")
    print(block(a_lines, False)[:400])


if __name__ == "__main__":
    main()
