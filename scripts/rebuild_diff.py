"""Rebuild the before/after XML block on the landing page from real files.

The two samples come from ChessAssistant 1.1.10 decoded twice -- once untouched,
once after `aqlab rtl`. Nothing here is hand-written except the attribute list
this section is about: a full layout line is 300+ chars of irrelevant attributes
and pushed the block to 2000px of horizontal scroll, so each sample keeps only
the attributes the migration touches.
"""
import html
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from build_diff import PAIRS  # single source for the old->new attribute table

BEFORE = pathlib.Path("C:/tmp/before.xml")
AFTER = pathlib.Path("C:/tmp/after.xml")
PAGE = pathlib.Path("C:/Users/speed/arabize/site/index.html")

KEEP_BEFORE = ("layout_marginLeft", "layout_marginRight", "alignParentLeft", "alignParentRight")
KEEP_AFTER = ("layout_marginStart", "layout_marginEnd", "alignParentStart", "alignParentEnd")


def mark(line, old):
    esc = html.escape(line)
    names = [a for a, _ in PAIRS] if old else [b for _, b in PAIRS]
    names = sorted(set(names + (["Left", "Right"] if old else ["Start", "End"])),
                   key=len, reverse=True)
    for n in names:
        esc = re.sub(rf"(?<![A-Za-z]){n}(?![A-Za-z])",
                     f'<span class="hl {"old" if old else "new"}">{n}</span>', esc)
    return esc


def sample(text, keep, limit=2):
    out = []
    for line in text.split("\n"):
        if not any(k in line for k in keep):
            continue
        m = re.match(r'\s*(<[A-Za-z.]+)', line)
        if not m:
            continue
        # Attribute names contain underscores (layout_marginLeft) -- `[a-zA-Z]+`
        # silently matched only duplicateParentState and the sample came out empty.
        attrs = re.findall(r'android:[a-zA-Z_]+="[^"]*"', line)
        picked = [a for a in attrs if any(k in a for k in keep)]
        if not picked:
            continue
        out.append(m.group(1) + "\n    " + "\n    ".join(picked) + " />")
        if len(out) >= limit:
            break
    return out


def main():
    before = BEFORE.read_text(encoding="utf-8")
    after = AFTER.read_text(encoding="utf-8")

    b = sample(before, KEEP_BEFORE)
    a = sample(after, KEEP_AFTER)

    print(f"before: {len(b)} element(s), after: {len(a)}")
    if not b or not a:
        sys.exit("no matching attributes -- refusing to emit an empty sample")
    if len(b) != len(a):
        sys.exit("before and after have different counts -- sample is misaligned")

    # the element types must line up, and the attribute count per element too
    for x, y in zip(b, a):
        if x.split()[0] != y.split()[0]:
            sys.exit("element mismatch -- sample is misaligned")
        if x.count("\n") != y.count("\n"):
            sys.exit("attribute count differs -- migration dropped something")

    bh = "\n".join(mark(l, True) for l in b)
    ah = "\n".join(mark(l, False) for l in a)

    page = PAGE.read_text(encoding="utf-8")
    n_before = len(re.findall(r'<pre class="dpre old">', page))
    n_after = len(re.findall(r'<pre class="dpre new">', page))
    if n_before != 1 or n_after != 1:
        sys.exit(f"expected exactly one pre block each, found {n_before}/{n_after}")

    page = re.sub(r'<pre class="dpre old">.*?</pre>',
                  lambda m: f'<pre class="dpre old">{bh}</pre>', page, count=1, flags=re.S)
    page = re.sub(r'<pre class="dpre new">.*?</pre>',
                  lambda m: f'<pre class="dpre new">{ah}</pre>', page, count=1, flags=re.S)
    PAGE.write_text(page, encoding="utf-8")

    width = max(len(s) for s in b + a)
    print(f"longest line: {width} chars")
    for line in b + a:
        assert "<span" not in line, "span leaked into raw sample"
    print("written")


if __name__ == "__main__":
    main()
