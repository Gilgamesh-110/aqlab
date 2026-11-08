#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regenerate the CLDR-derived tables in `aqlab.py`.

The table used to be hand-grouped, and both times a row was typed it was wrong
in a way no test caught (`he` missing its `many`, `vi` handed a CLDR `one` its
rules do not produce, `tg` absent entirely). So the input is checked in —
`tests/fixtures/cldr/plurals.json`, a copy of CLDR core `supplemental/plurals.json`
with only the cardinal rows kept — and `tests/test_aqlab.py` rebuilds the table
from it on every run. A hand edit to `PLURAL_CATEGORIES_BY_KIND` now fails a
check instead of shipping.

Also `RTL_LOCALES`, from `likelySubtags.xml` + `scriptMetadata.txt` — see
`rtl_table()`. Same deal: checked-in fixture, rebuilt by the test suite.

    python tools/cldr_tables.py --update   # rewrite the blocks in aqlab.py
    python tools/cldr_tables.py            # print the blocks, change nothing
"""
import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "cldr" / "plurals.json"
RTL_FIXTURE = ROOT / "tests" / "fixtures" / "cldr" / "rtl.json"
TARGET = ROOT / "aqlab.py"
BEGIN = "PLURAL_CATEGORIES_BY_KIND = {"
END = "\n}\n"
RTL_BEGIN = "RTL_LOCALES = frozenset(("
RTL_END = "))"
WIDTH = 92


def table(fixture=FIXTURE):
    """`{category tuple: [locales]}` from the checked-in CLDR cardinal rules."""
    data = json.loads(pathlib.Path(fixture).read_text(encoding="utf-8"))
    groups = {}
    for tag in sorted(data["cardinal"]):
        # `und` is CLDR's "undetermined"; Android never writes it as a qualifier
        # and treating it as a locale would lint a folder nobody can create.
        if tag == "und":
            continue
        groups.setdefault(tuple(data["cardinal"][tag]), []).append(tag)
    # Tuple order follows CLDR's own category order, and CLDR order is stable
    # across the file: the first locale met fixes its group's order.
    first = {}
    for tag in sorted(data["cardinal"]):
        cats = tuple(data["cardinal"][tag])
        first.setdefault(cats, tag)
    return dict(sorted(groups.items(), key=lambda kv: (-len(kv[0]), first[kv[0]])))


def render(groups):
    def key(cats):
        # A one-element tuple renders as a bare string (`('other'):` is `'other':`),
        # so Python reads back a str key and every lookup by tuple misses.
        if len(cats) == 1:
            return "(" + repr(cats[0]) + ",)"
        return "(" + ", ".join(repr(c) for c in cats) + ")"
    lines = ["PLURAL_CATEGORIES_BY_KIND = {"]
    for cats, locales in groups.items():
        head = "    " + key(cats) + ": ("
        body, line = [], head
        for i, loc in enumerate(locales):
            piece = repr(loc) + (", " if i < len(locales) - 1 else ",),")
            if len(line) + len(piece) > WIDTH and body:
                body.append(line.rstrip())
                line = " " * len(head)
            line += piece
        body.append(line)
        lines.extend(body)
    lines.append("}")
    return "\n".join(lines)


def rtl_table(fixture=RTL_FIXTURE):
    """`frozenset` of right-to-left language tags from the checked-in CLDR data."""
    data = json.loads(pathlib.Path(fixture).read_text(encoding="utf-8"))
    return frozenset(data["rtl_langs"])


def render_rtl(locales):
    lines, head = [RTL_BEGIN], "    "
    body, line = [], head
    for i, loc in enumerate(sorted(locales)):
        piece = '"' + loc + '"' + (", " if i < len(locales) - 1 else ",")
        if len(line) + len(piece) > WIDTH and body:
            body.append(line.rstrip())
            line = " " * len(head)
        line += piece
    body.append(line)
    lines.extend(body)
    lines.append("))")
    return "\n".join(lines)


def rtl_table(fixture=RTL_FIXTURE):
    """Right-to-left language tags from the checked-in CLDR data.

    The rule is two CLDR files: a language's *likely* script (`ar` -> `Arab`
    from `likelySubtags.xml`) and that script's field 6 in `scriptMetadata.txt`
    (`YES` for the 37 scripts whose letters are Bidi_Class RTL). It covers
    Kurdish, N'Ko and Syriac languages nobody types from memory, and it cannot
    drift from ICU because the fixture is CLDR's own rows.
    """
    data = json.loads(pathlib.Path(fixture).read_text(encoding="utf-8"))
    return frozenset(data["rtl_langs"])


def render_rtl(locales):
    lines, head = [RTL_BEGIN], "    "
    body, line = [], head
    for i, loc in enumerate(sorted(locales)):
        piece = '"' + loc + '"' + (", " if i < len(locales) - 1 else ",")
        if len(line) + len(piece) > WIDTH and body:
            body.append(line.rstrip())
            line = " " * len(head)
        line += piece
    body.append(line)
    lines.extend(body)
    lines.append("))")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--update", action="store_true",
                    help="rewrite the block in aqlab.py instead of printing it")
    ap.add_argument("--fixture", default=str(FIXTURE))
    a = ap.parse_args(argv)
    block = render(table(a.fixture))
    if not a.update:
        print(block)
        return 0
    src = TARGET.read_text(encoding="utf-8")
    i = src.index(BEGIN)
    j = src.index(END, i) + len(END)
    TARGET.write_text(src[:i] + block + "\n" + src[j:], encoding="utf-8", newline="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
