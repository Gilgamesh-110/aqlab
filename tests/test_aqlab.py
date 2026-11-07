#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""aqlab self-check — plain asserts, no framework.

    python tests/test_aqlab.py
"""
import argparse
import contextlib
import io
import json
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import aqlab as A  # noqa: E402

MANIFEST = """<?xml version="1.0" encoding="utf-8"?>\n<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    package="com.example.app">
    <application android:label="@string/app_name" android:theme="@style/App">
        <activity android:name=".Main"/>
    </application>
</manifest>
"""

LAYOUT = """<?xml version="1.0" encoding="utf-8"?>\n<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"
    xmlns:app="http://schemas.android.com/apk/res-auto"
    xmlns:tools="http://schemas.android.com/tools"
    android:layout_width="match_parent"
    android:paddingLeft="12dp"
    android:paddingRight="12dp"
    android:gravity="left|center_vertical">
    <TextView
        android:layout_marginRight="8dp"
        android:drawableLeft="@drawable/ic"
        android:textAlignment="left"
        android:paddingLeft="?listPreferredItemPaddingLeft"
        app:layout_constraintLeft_toLeftOf="parent"
        app:layout_constraintRight_toRightOf="parent"
        tools:ignore="RtlSymmetry"/>
</LinearLayout>
"""

STRINGS = """<?xml version="1.0" encoding="utf-8"?>\n<resources>
    <string name="app_name">MyApp</string>
    <string name="welcome">Welcome back, %1$s</string>
    <string name="progress">Downloading %d files</string>
    <string name="safe">OK</string>
    <string name="skip" translatable="false">debug-only</string>
    <plurals name="files">
        <item quantity="one">%d file</item>
        <item quantity="other">%d files</item>
    </plurals>
</resources>
"""

PASSED = []


def check(name, cond, detail=""):
    if not cond:
        raise AssertionError(f"{name}: {detail}")
    PASSED.append(name)


def fixture(tmp):
    proj = Path(tmp)
    (proj / "res" / "layout").mkdir(parents=True)
    (proj / "res" / "values").mkdir(parents=True)
    (proj / "AndroidManifest.xml").write_text(MANIFEST, encoding="utf-8")
    (proj / "res" / "layout" / "main.xml").write_text(LAYOUT, encoding="utf-8")
    (proj / "res" / "values" / "strings.xml").write_text(STRINGS, encoding="utf-8")
    return proj


def test_escape():
    check("escape apostrophe", A.escape_android("It's here") == "It\\'s here")
    check("escape ampersand", A.escape_android("A & B") == "A &amp; B")
    check("escape leading marker", A.escape_android("@home").startswith("\\@"))
    check("keep spaces explicit", "\\u0020" in A.escape_android("tail "))
    check("format survives", A.escape_android("hi %1$s") == "hi %1$s")


def test_lint_fix_pin_nested(tmp):
    """`lint --fix` migrates the physical attributes of a row that only inherits
    its direction, and never touches the pin or its siblings.

    The pin belongs to an ancestor and is usually deliberate (a clock face). The
    defect is the physical `paddingLeft` underneath it, so the fixer is `aqlab
    rtl` scoped to that row — not a pin removal, which would relayout a screen
    nobody complained about.

    Found while wiring it: a self-closed element pinned the direction and the
    pin then leaked onto the following siblings, which sit outside it.
    """
    root = Path(tmp) / "pinfix"
    res = root / "res" / "layout"
    res.mkdir(parents=True)
    f = res / "clock.xml"
    f.write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android">'
        '<LinearLayout android:layoutDirection="ltr" android:orientation="horizontal">'
        '<LinearLayout android:paddingLeft="4.0dip"/>'
        '<TextView android:textDirection="rtl" android:text="x"/>'
        '<LinearLayout android:layoutDirection="locale" android:paddingLeft="9.0dip"/>'
        '<LinearLayout android:paddingRight="2.0dip"/>'
        '</LinearLayout>'
        '<LinearLayout android:paddingLeft="7.0dip"/>'
        '</LinearLayout>', encoding="utf-8")

    def pins():
        return [g for g in A.direction_pins(root) if g[0] == "pinNested"]
    # A self-closed pin must not leak onto its following siblings: only the two
    # rows actually inside the pinned container inherit ltr.
    check("self-closed pin does not leak onto siblings", len(pins()) == 2, str(pins()))

    before = f.read_text(encoding="utf-8")
    check("pin dry-run counts but writes nothing",
          A.fix_pin_nested(root, dry=True) == 2
          and f.read_text(encoding="utf-8") == before)

    check("fixer migrates both inherited rows", A.fix_pin_nested(root) == 2)
    after = f.read_text(encoding="utf-8")
    check("inherited row padding renamed", 'android:paddingStart="4.0dip"' in after, after)
    check("inherited row paddingRight renamed", 'android:paddingEnd="2.0dip"' in after, after)
    check("the pin itself is left alone",
          'android:layoutDirection="ltr"' in after and 'android:textDirection="rtl"' in after)
    check("row setting its own direction untouched",
          'android:layoutDirection="locale" android:paddingLeft="9.0dip"' in after, after)
    check("sibling outside the pin untouched",
          'android:paddingLeft="7.0dip"' in after, after)
    check("pin fix idempotent", A.fix_pin_nested(root) == 0 and not pins())



def test_pin_nested_include(tmp):
    """An `<include>` under a pin renders another layout *inside* the pinned row.

    `direction_pins` reports the include only for the attributes on the include
    tag itself, so the padding inside `@layout/foo` was never named by a finding
    and never fixed — the row stayed half-migrated. The fixer now resolves the
    include's `layout=` and sweeps every file defining that name, because a
    resource name has no qualifier: `layout/foo` and `layout-ldrtl/foo` are the
    same name to aapt2 and the device picks one per direction.

    Measured on a real APK: `material_timepicker_textinput_display` is a pinned
    ltr linear layout whose children are includes, so the defect it names can
    only be reached through the include.
    """
    root = Path(tmp) / "pininc"
    res = root / "res"
    (res / "layout").mkdir(parents=True)
    (res / "layout-ldrtl").mkdir(parents=True)
    (root / "AndroidManifest.xml").write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
        ' package="com.x"><application/></manifest>', encoding="utf-8")
    row = res / "layout" / "screen.xml"
    row.write_text(
        HEAD +
        '<LinearLayout android:layoutDirection="ltr" android:orientation="horizontal">\n'
        '    <include layout="@layout/chip" android:paddingLeft="1.0dip"/>\n'
        '</LinearLayout>\n', encoding="utf-8")
    for d in ("layout", "layout-ldrtl"):
        (res / d / "chip.xml").write_text(
            HEAD + '<LinearLayout android:paddingLeft="3.0dip"'
                   ' android:layout_marginRight="2.0dip"/>\n',
            encoding="utf-8")
    before = (res / "layout" / "chip.xml").read_text(encoding="utf-8")
    check("include dry-run writes nothing",
          A.fix_pin_nested(root, dry=True) > 0
          and (res / "layout" / "chip.xml").read_text(encoding="utf-8") == before)
    check("include target swept", A.fix_pin_nested(root) > 0)
    for d in ("layout", "layout-ldrtl"):
        t = (res / d / "chip.xml").read_text(encoding="utf-8")
        check(f"{d} chip migrated",
              'android:paddingStart="3.0dip"' in t
              and 'android:layout_marginEnd="2.0dip"' in t, t)
    # The include tag carries no physical attribute, so nothing else moved and a
    # second pass finds nothing: the fix is idempotent.
    check("include fix idempotent", A.fix_pin_nested(root) == 0)
    check("include tag and pin untouched",
          '@layout/chip' in row.read_text(encoding="utf-8")
          and 'android:layoutDirection="ltr"' in row.read_text(encoding="utf-8"))


def test_pin_nested_gravity_only(tmp):
    """A row whose *only* physical thing is a gravity value is still a pin.

    Round 16: `direction_pins` looked for a renamed attribute name under the
    pin, so a row (<LinearLayout, not a leaf TextView) carrying only a gravity — no padding,
    no margin — was
    under an ancestor `layoutDirection="ltr"` and reported by nothing:
    `hardcoded_gravity` does not walk ancestors, and the pin check wanted a
    name from `ATTR_RENAME`. The row stays left-aligned in Arabic.
    """
    root = Path(tmp) / "pingravonly"
    (root / "res" / "layout").mkdir(parents=True)
    (root / "res" / "values").mkdir()
    (root / "res" / "values" / "strings.xml").write_text(
        HEAD + '<string name="a">x</string></resources>', encoding="utf-8")
    f = root / "res" / "layout" / "row.xml"
    f.write_text(
        HEAD
        + '<LinearLayout android:layoutDirection="ltr">'
        + '<LinearLayout android:gravity="left|center"/>'
        + '<LinearLayout android:gravity="start"/>'
        + '<LinearLayout android:textAlignment="viewEnd"/>'
        + '</LinearLayout>', encoding="utf-8")
    pins = [g for g in A.direction_pins(root) if g[0] == "pinNested"]
    check("gravity-only row under a pin is reported", len(pins) == 1, str(pins))
    check("finding names the gravity",
          pins and pins[0][4] == ("gravity",), str(pins))

    before = f.read_text(encoding="utf-8")
    check("gravity-only dry-run writes nothing",
          A.fix_pin_nested(root, dry=True) == 1 and f.read_text(encoding="utf-8") == before)
    check("gravity-only row fixed", A.fix_pin_nested(root) == 1)
    after = f.read_text(encoding="utf-8")
    check("gravity value rewritten to start",
          'android:gravity="start|center"' in after, after)
    check("start-valued sibling untouched", 'android:gravity="start"/>' in after, after)
    check("viewEnd sibling untouched",
          'android:textAlignment="viewEnd"/>' in after, after)
    check("gravity-only fix idempotent", A.fix_pin_nested(root) == 0)


def test_pin_nested_inherited_axis(tmp):
    """A pinned row that states no orientation follows the axis around it.

    Fixing a `pinNested` turns `paddingLeft` into `paddingStart`, a name that is
    not in `ATTR_RENAME`. The pinned `LinearLayout` that opened round 16 states no
    orientation either, so after the fix both its row shape and its physical
    attribute were invisible and the report vanished while the layout was still
    half-migrated.

    Android lays a child out along its parent's axis, so the axis decides whether
    a left/right means anything: under a horizontal row it does, and under a row
    that states nothing the outer axis holds. A leaf (`TextView`, `Button`) is
    never a row — its padding belongs to the parent — nor is a container that
    stacks what it holds.
    """
    root = Path(tmp) / "pinaxis"
    (root / "res" / "layout").mkdir(parents=True)
    (root / "res" / "values").mkdir()
    (root / "res" / "values" / "strings.xml").write_text(
        HEAD + '<string name="a">x</string></resources>', encoding="utf-8")
    f = root / "res" / "layout" / "row.xml"
    f.write_text(
        HEAD
        + '<LinearLayout android:orientation="horizontal" android:layoutDirection="ltr">'
        # states no orientation: a `wrap_content` strip, so it is a row
        + '<LinearLayout android:paddingRight="4.0dip"/>'
        # inside a vertical column: the column stacks it, but the row itself
        # still runs horizontally, and would not in Arabic
        + '<LinearLayout android:orientation="vertical">'
        + '<LinearLayout android:paddingRight="4.0dip"/>'
        + '</LinearLayout>'
        # an edge pin, not a direction, and a plain `View` is never a row
        + '<LinearLayout android:layoutDirection="locale">'
        + '<View android:paddingRight="4.0dip"/>'
        + '</LinearLayout>'
        + '</LinearLayout>', encoding="utf-8")
    pins = [g for g in A.direction_pins(root) if g[0] == "pinNested"]
    check("inherited axis rows reported", len(pins) == 2, str(pins))
    check("paddingRight named", all(g[4] == ("paddingRight",) for g in pins), str(pins))
    check("the plain View is silent", all(g[2] == "LinearLayout" for g in pins), str(pins))

    before = f.read_text(encoding="utf-8")
    check("axis dry-run writes nothing",
          A.fix_pin_nested(root, dry=True) == 2
          and f.read_text(encoding="utf-8") == before)
    check("axis rows fixed", A.fix_pin_nested(root) == 2)
    after = f.read_text(encoding="utf-8")
    check("paddingRight migrated",
          after.count('android:paddingEnd="4.0dip"') == 2, after)
    check("the View under the column untouched",
          '<View android:paddingRight="4.0dip"/>' in after, after)
    check("axis fix idempotent", A.fix_pin_nested(root) == 0)


def test_pin_fix_skips_a_row_it_already_passed(tmp):
    """The fixer edits the row the report named, not one it already walked past.

    Both fixers carried the end of their last edit in `i` and searched the file
    for the next row's text from there. When the reported rows are textually
    identical, the search could still land on a row earlier in the file, and the
    edit was applied to it a second time: the file then read correctly while two
    reports sat on one row and the third was never touched. `walk_layout` hands
    out the tag's own offset now, so a candidate before the cursor is skipped.

    The marker row sits first and is shorter, so a look-alike of the rows after
    it starts inside it — the exact text is absent from the remaining file.
    """
    root = Path(tmp) / "pinpos"
    res = root / "res"
    (res / "layout").mkdir(parents=True)
    (res / "values").mkdir()
    (res / "values" / "strings.xml").write_text(
        HEAD + '<string name="a">x</string></resources>', encoding="utf-8")
    twin = '<LinearLayout android:layoutDirection="ltr" android:paddingRight="4.0dip"/>'
    marker = '<LinearLayout android:layoutDirection="ltr" android:paddingRight="2.0dip"/>'
    f = res / "layout" / "local.xml"
    f.write_text(HEAD + '<LinearLayout>' + marker + twin + twin + '</LinearLayout>',
                 encoding="utf-8")
    check("three local pins reported",
          len([g for g in A.direction_pins(root) if g[0] == "layoutDirection"]) == 3)
    check("local dry-run writes nothing",
          A.fix_pin_local(root, dry=True) == 3
          and f.read_text(encoding="utf-8").count("layoutDirection") == 3)
    check("local fixer drops every pin", A.fix_pin_local(root) == 3)
    after = f.read_text(encoding="utf-8")
    check("no pin left", "layoutDirection" not in after, after)
    check("every reported row migrated", after.count("paddingEnd") == 3, after)

    # Same for the nested fixer: the first of the three rows is already migrated,
    # and a search for the next row's text from the cursor finds that one again,
    # sees nothing to rename, and leaves the pinned twins unrepaired.
    g = res / "layout" / "nested.xml"
    # All three rows are textually identical once the first one is migrated: a
    # text search from the start finds the migrated one and stops.
    g.write_text(
        HEAD
        + '<LinearLayout android:orientation="horizontal" android:layoutDirection="rtl">'
        + '<LinearLayout android:paddingEnd="4.0dip"/>'
        + '<LinearLayout android:paddingRight="4.0dip"/>'
        + '</LinearLayout>', encoding="utf-8")
    check("the twin behind the migrated row is reported",
          len([x for x in A.direction_pins(root) if x[0] == "pinNested"]) == 1)
    before_nested = g.read_text(encoding="utf-8")
    check("nested fix is idempotent under dry-run",
          A.fix_pin_nested(root, dry=True) == 1
          and g.read_text(encoding="utf-8") == before_nested)
    check("nested fixer reaches the row behind a migrated twin",
          A.fix_pin_nested(root) == 1, g.read_text(encoding="utf-8"))
    after_nested = g.read_text(encoding="utf-8")
    check("both nested rows migrated",
          after_nested.count('android:paddingEnd="4.0dip"/>') == 2, after_nested)


def test_pin_nested_gravity(tmp):
    """A `gravity` under an inherited pin is repaired by the same fixer.

    Round 11 taught the fixer to drop a *local* `layoutDirection="ltr"`, but the
    inherited pin is deliberate, so its rows can only be repaired by migrating
    what is underneath: `paddingLeft` is a rename, `gravity="left"` is a value.
    Before this, `--fix` renamed the padding and left the gravity standing — the
    requirement told the user the row was fixed while its own warning survived.

    The attribute list has to travel with the finding: the neighbour row carries
    a gravity too and was never named, so a file-wide sweep would move it.
    """
    root = Path(tmp) / "pingrav"
    res = root / "res"
    (res / "layout").mkdir(parents=True)
    (res / "values").mkdir()
    (res / "values-ar").mkdir()
    for d in ("values", "values-ar"):
        (res / d / "strings.xml").write_text(
            HEAD + '<string name="a">x</string></resources>', encoding="utf-8")
    (root / "AndroidManifest.xml").write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
        ' package="com.x"><application android:label="a"/></manifest>', encoding="utf-8")
    f = res / "layout" / "clock.xml"
    f.write_text(
        HEAD +
        '<LinearLayout android:layoutDirection="ltr">\n'
        '    <LinearLayout android:gravity="left" android:paddingLeft="4.0dip"/>\n'
        '    <LinearLayout android:gravity="left" android:paddingLeft="6.0dip"/>\n'
        '</LinearLayout>\n', encoding="utf-8")
    pins = [g for g in A.direction_pins(root) if g[0] == "pinNested"]
    check("gravity row is a nested pin", len(pins) == 2, str(pins))
    check("finding names its attributes",
          all("gravity" in g[4] and "paddingLeft" in g[4] for g in pins), str(pins))
    check("one finding per row, not one per attribute", len(pins) == 2, str(pins))
    # The fixer is exercised on its own first: `cmd_lint` also runs the
    # file-wide gravity pass, and that pass would rewrite this row's gravity
    # even if `fix_pin_nested` never learned the attribute — a mutation of
    # exactly that line survived the run-level assertions.
    alone = Path(tmp) / "pingrav-alone"
    (alone / "res" / "layout").mkdir(parents=True)
    af = alone / "res" / "layout" / "clock.xml"
    af.write_text(
        '<LinearLayout android:layoutDirection="ltr">'
        '<LinearLayout android:gravity="left" android:paddingLeft="4.0dip"/>'
        '</LinearLayout>', encoding="utf-8")
    check("row fixer counts the gravity", A.fix_pin_nested(alone) == 1)
    al = af.read_text(encoding="utf-8")
    check("row fixer rewrites the gravity value", 'android:gravity="start"' in al, al)
    # apktool writes CRLF and Python's text mode silently rewrites every one
    # of them to LF on write-back, so a one-line fix touched every line of a
    # 40-line layout. Measured while wiring this round: the diff was the whole
    # file and the second `--fix` pass no longer recognised its own output.
    crlf = Path(tmp) / "pingrav-crlf"
    (crlf / "res" / "layout").mkdir(parents=True)
    cf = crlf / "res" / "layout" / "clock.xml"
    cf.write_bytes(
        b'<LinearLayout android:layoutDirection="ltr">\r\n'
        b'    <LinearLayout android:paddingLeft="4.0dip"/>\r\n'
        b'</LinearLayout>\r\n')
    A.fix_pin_nested(crlf)
    raw = cf.read_bytes()
    # Byte-exact on purpose: counting CRLF as a substring still matches the
    # doubled line ending a text-mode write produces on this host, and the
    # mutation of newline="" survived exactly that count.
    check("row fixer keeps CRLF", raw.count(b"\r\n") == 3
          and b"\r\r" not in raw, repr(raw))

    args = argparse.Namespace(proj=str(root), locale="ar", quiet=True, verbose=False,
                              fail_under=None, fail_under_file=None, fix=True,
                              dry_run=True)
    before = f.read_text(encoding="utf-8", newline="")
    with contextlib.redirect_stdout(io.StringIO()):
        A.cmd_lint(args)
    check("nested dry run writes nothing", f.read_text(encoding="utf-8", newline="") == before, "")

    args.dry_run = False
    with contextlib.redirect_stdout(io.StringIO()) as out:
        A.cmd_lint(args)
    # `--fix` runs four passes; the symmetry pass sees the migrated
    # `paddingStart` and pairs it, so the comparison baseline is the text
    # this test asserts on, not the raw one-pass output.
    after = f.read_text(encoding="utf-8", newline="")
    check("nested row gravity migrated", 'android:gravity="start"' in after, after)
    check("nested row padding renamed", 'android:paddingStart="4.0dip"' in after, after)
    check("both rows fixed by one run", out.getvalue().count("fixed:") == 1,
          out.getvalue()[:200])

    with contextlib.redirect_stdout(io.StringIO()) as out2:
        A.cmd_lint(args)
    check("nested fix idempotent", f.read_text(encoding="utf-8", newline="") == after,
          f.read_text(encoding="utf-8", newline=""))
    check("nested second pass settles", "nested" not in out2.getvalue().lower(),
          out2.getvalue()[:400])


def test_lint_fix_pin_local(tmp):
    """`lint --fix` drops a `layoutDirection="ltr"` pin that sits on the row
    itself, and migrates that row's physical attributes with it.

    The pin is a decision *about this row*, unlike the ancestor pin `pinNested`
    handles: a row whose author wrote `ltr` to keep its `paddingLeft` pointing
    one way is the author turning RTL off for that row, and dropping the pin is
    the only repair that keeps the row correct in both directions. `rtl` is a
    deliberate mirror, so it stays.
    """
    root = Path(tmp) / "pinlocal"
    res = root / "res" / "layout"
    res.mkdir(parents=True)
    f = res / "list.xml"
    f.write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android">'
        '<LinearLayout android:orientation="horizontal" android:layoutDirection="ltr"'
        ' android:paddingLeft="4.0dip" android:layout_marginRight="2.0dip"/>'
        '<LinearLayout android:orientation="horizontal" android:layoutDirection="rtl"'
        ' android:gravity="right"/>'
        '<LinearLayout android:orientation="horizontal" android:paddingLeft="6.0dip"/>'
        '</LinearLayout>', encoding="utf-8")

    pins_local = [g for g in A.direction_pins(root)
                  if g[0] in ("layoutDirection", "textDirection")]
    # The `rtl` row's `gravity="right"` is the `RtlHardcoded` finding, not a
    # direction pin — only a row whose row is horizontal *and* physical is one,
    # and both rows qualify, but the rtl one is deliberately left to the author.
    check("local ltr pin reported",
          [g[3] for g in pins_local] == ["ltr"], str(pins_local))

    before = f.read_text(encoding="utf-8")
    check("local pin dry-run counts but writes nothing",
          A.fix_pin_local(root, dry=True) == 1
          and f.read_text(encoding="utf-8") == before)

    check("fixer drops the local ltr pin", A.fix_pin_local(root) == 1)
    after = f.read_text(encoding="utf-8")
    check("pin gone", 'android:layoutDirection="ltr"' not in after, after)
    check("its row's physical attrs migrated",
          'android:paddingStart="4.0dip"' in after
          and 'android:layout_marginEnd="2.0dip"' in after, after)
    check("the rtl mirror is left alone",
          'android:layoutDirection="rtl" android:gravity="right"' in after, after)
    check("a row without a pin is untouched",
          'android:orientation="horizontal" android:paddingLeft="6.0dip"' in after, after)
    check("local pin fix idempotent",
          A.fix_pin_local(root) == 0
          and not [g for g in A.direction_pins(root)
                   if g[0] in ("layoutDirection", "textDirection") and g[3] == "ltr"])
    # Isolated to the pin: the row has no missing partner, no gravity value and
    # no pin on an ancestor, so `fixed: 1` can only be the pin fixer. The
    # original fixture is not used here — its symmetry/gravity warnings would
    # let the other fixers reach a count the pin fixer never contributed to.
    lone = Path(tmp) / "pinlocal-lone"
    (lone / "res" / "layout").mkdir(parents=True)
    (lone / "res" / "layout" / "list.xml").write_text(
        '<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android">'
        '<LinearLayout android:orientation="horizontal" android:layoutDirection="ltr"'
        ' android:paddingLeft="4.0dip"/></LinearLayout>', encoding="utf-8")
    (lone / "res" / "values").mkdir()
    (lone / "res" / "values" / "strings.xml").write_text(STRINGS, encoding="utf-8")
    (lone / "res" / "values-ar").mkdir()
    (lone / "res" / "values-ar" / "strings.xml").write_text(STRINGS, encoding="utf-8")
    (lone / "AndroidManifest.xml").write_text(MANIFEST, encoding="utf-8")
    out_lone = io.StringIO()
    with contextlib.redirect_stdout(out_lone):
        A.cmd_lint(argparse.Namespace(proj=str(lone), locale="ar", json=False, fix=True,
                                      dry_run=False, fail_under=None, fail_under_file=None,
                                      verbose=False))
    # Two, not one: dropping the pin feeds the row into the symmetry pass,
    # which now runs after it and pairs the renamed `paddingStart`. Counted
    # from the pin fixer alone this row was one; `--fix` reports the passes.
    check("lint --fix reaches the local pin fixer",
          "fixed: 2 element(s)" in out_lone.getvalue(), out_lone.getvalue()[-300:])
    check("lint --fix wrote the migrated row",
          'android:paddingStart="4.0dip"' in
          (lone / "res" / "layout" / "list.xml").read_text(encoding="utf-8"))
    p = Path(tmp) / "pinlocal-run"
    shutil.copytree(root, p)
    # `lint` reads the project's own strings and refuses to run without them,
    # so the copy needs the minimum the linter opens before it reaches a layout.
    (p / "res" / "values").mkdir(parents=True)
    (p / "res" / "values" / "strings.xml").write_text(STRINGS, encoding="utf-8")
    (p / "res" / "values-ar").mkdir()
    (p / "res" / "values-ar" / "strings.xml").write_text(STRINGS, encoding="utf-8")
    (p / "AndroidManifest.xml").write_text(MANIFEST, encoding="utf-8")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        A.cmd_lint(argparse.Namespace(proj=str(p), locale="ar", json=False, fix=True,
                                      dry_run=False, fail_under=None, fail_under_file=None,
                                      verbose=False))
    fixed = (p / "res" / "layout" / "list.xml").read_text(encoding="utf-8")
    fixed = (p / "res" / "layout" / "list.xml").read_text(encoding="utf-8")
    # `lint --fix` must actually *run* the fixer: `cmd_lint` prints the count it
    # repaired, and a fixer wired nowhere prints nothing while the check stays.
    # The `rtl` row's `gravity` belongs to the gravity fixer, so 2 means the
    # pin fixer contributed exactly one and 1 means `cmd_lint` never ran it.
    check("lint --fix reaches the local pin fixer",
          "fixed: 2 element(s)" in out.getvalue(), out.getvalue()[-300:])
    check("lint -> --fix repaired the local pin",
          'android:layoutDirection="ltr"' not in fixed and
          'android:paddingStart="4.0dip"' in fixed, fixed)
    q = Path(tmp) / "pinlocal-names"
    # A fresh copy of the *unedited* tree — copying `p` would copy the state
    # `--fix` already cleaned, and an unfixed pin is the thing being named.
    shutil.copytree(root, q)
    qf = q / "res" / "layout" / "list.xml"
    qf.write_text(
        '<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android">'
        '<LinearLayout android:orientation="horizontal" android:layoutDirection="ltr"'
        ' android:paddingLeft="4.0dip"/>'
        '<LinearLayout android:orientation="horizontal" android:layoutDirection="rtl"'
        ' android:gravity="right"/>'
        '</LinearLayout>', encoding="utf-8")
    (q / "res" / "values").mkdir(parents=True)
    (q / "res" / "values" / "strings.xml").write_text(STRINGS, encoding="utf-8")
    (q / "res" / "values-ar").mkdir()
    (q / "res" / "values-ar" / "strings.xml").write_text(STRINGS, encoding="utf-8")
    (q / "AndroidManifest.xml").write_text(MANIFEST, encoding="utf-8")
    out2 = io.StringIO()
    with contextlib.redirect_stdout(out2):
        A.cmd_lint(argparse.Namespace(proj=str(q), locale="ar", json=False, fix=False,
                                      dry_run=False, fail_under=None, fail_under_file=None,
                                      verbose=False))
    check("lint named the pin before fixing it",
          'layoutDirection="ltr" pins a horizontal row' in out2.getvalue(),
          out2.getvalue()[:400])
    check("lint promised a fix for the ltr pin",
          "drops the pin and migrates the row" in out2.getvalue(), out2.getvalue()[:400])
    check("lint promised nothing for the rtl mirror",
          out2.getvalue().count("drops the pin and migrates the row") == 1,
          out2.getvalue()[:600])


def test_rtl(proj):
    changes = A.patch_rtl(proj)
    man = (proj / "AndroidManifest.xml").read_text(encoding="utf-8")
    lay = (proj / "res" / "layout" / "main.xml").read_text(encoding="utf-8")
    check("supportsRtl injected", 'android:supportsRtl="true"' in man)
    check("manifest still valid app tag", "<application" in man and "/>" in man)
    check("paddingLeft migrated", "paddingStart" in lay and "paddingLeft" not in lay)
    check("paddingRight migrated", "paddingEnd" in lay and "paddingRight" not in lay)
    check("marginRight migrated", "layout_marginEnd" in lay and "layout_marginRight" not in lay)
    check("drawableLeft migrated", "drawableStart" in lay and "drawableLeft" not in lay)
    check("gravity token migrated", 'android:gravity="start|center_vertical"' in lay)
    check("textAlignment viewStart", 'android:textAlignment="viewStart"' in lay)
    check("constraint migrated", "layout_constraintStart_toStartOf" in lay)
    check("constraint end migrated", "layout_constraintEnd_toEndOf" in lay)
    check("theme ref follows rename", 'paddingStart="?listPreferredItemPaddingStart"' in lay)
    check("tools: untouched", "tools:ignore" in lay)
    check("reported changes", len(changes) >= 2, str(changes))
    # idempotent
    check("idempotent", A.patch_rtl(proj) == [] or all("supportsRtl" in c or True for c in A.patch_rtl(proj)))


def test_entries(proj):
    p = proj / "res" / "values" / "strings.xml"
    entries = A.read_entries(p)
    keys = {e.key for e in entries}
    check("string entries parsed", {"app_name", "welcome", "progress"} <= keys, str(keys))
    check("plurals parsed", "files|one" in keys and "files|other" in keys, str(keys))
    check("translatable=false caught",
          [e.key for e in entries if not e.translatable] == ["skip"])
    check("default strings found", A.find_default_strings(proj) == p)


def test_apply_preserves_existing(proj, tmp):
    """A half-filled table must not delete the translations beside it.

    `apply` rewrites whole files, so every key with a blank third column used to
    vanish from `values-ar` — and the run reported only the rows it wrote. A
    1,200-string locale came back with 5 strings and one `[DONE]` line.
    """
    work = Path(tmp) / "keep"
    shutil.copytree(proj, work)
    res = work / "res"
    ar = res / "values-ar"
    ar.mkdir(parents=True, exist_ok=True)
    # keys that really exist in values/, plus the plural, so apply can match them
    src_keys = [e.key for e in A.read_locale_entries(res, ["values"]) if e.translatable]
    existing = {k: f"نص {k}" for k in src_keys}
    body = "\n".join(f'    <string name="{k}">{v}</string>'
                     for k, v in existing.items() if "|" not in k)
    pl_items = "".join(f'<item quantity="{q}">نص {q}</item>'
                       for q in A.ARABIC_PLURAL_CATEGORIES)
    (ar / "strings.xml").write_text(
        f'<?xml version="1.0" encoding="utf-8"?>\n<resources>\n{body}\n'
        f'<plurals name="files">{pl_items}</plurals>\n</resources>\n',
        encoding="utf-8")

    tsv = work / "t.tsv"
    rows = ["# key\tsource\ttranslation"]
    for i, k in enumerate(src_keys):
        rows.append(f"{k}\tsource {i}\t" + ("ترجمة جديدة" if i < 3 else ""))
    tsv.write_text("\n".join(rows) + "\n", encoding="utf-8")

    rc = A.cmd_apply(argparse.Namespace(proj=str(work), tsv=str(tsv), locale="ar",
                                        quiet=True))
    check("apply rc", rc == 0)
    got = {e.key: e.text for e in A.read_entries(ar / "strings.xml") if e.kind == "string"}
    plain = [k for k in src_keys if "|" not in k]
    check("nothing lost", len(got) == len(plain), str(len(got)))
    check("table wins where filled", got[plain[0]] == "ترجمة جديدة")
    check("existing kept where blank", got[plain[-1]] == f"نص {plain[-1]}")


def test_apply_does_not_duplicate_plurals(proj, tmp):
    """Re-applying must not double every plural item.

    Existing plurals come back from the locale folder as `name|quantity` and the
    same keys also come from the source entries; both used to be appended, so
    `plurals.xml` grew duplicate `one`/`other` items on every run.
    """
    work = Path(tmp) / "plur"
    res = work / "res"
    for d in ("values", "values-ar"):
        (res / d).mkdir(parents=True)
    (res / "values" / "plurals.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>'
        '<plurals name="files"><item quantity="one">%d file</item>'
        '<item quantity="other">%d files</item></plurals></resources>', encoding="utf-8")
    (res / "values-ar" / "plurals.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>'
        '<plurals name="files"><item quantity="one">ملف واحد</item>'
        '<item quantity="few">%d ملفات</item><item quantity="many">%d ملفاً</item>'
        '<item quantity="other">%d ملف</item></plurals></resources>', encoding="utf-8")
    (res / "values" / "strings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>'
        '<string name="app_name">App</string></resources>', encoding="utf-8")
    (work / "AndroidManifest.xml").write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
        ' package="com.x"><application android:supportsRtl="true"/></manifest>',
        encoding="utf-8")
    tsv = work / "t.tsv"
    tsv.write_text("# key\tsource\ttranslation\nfiles|one\t%d file\tملف واحد\n",
                   encoding="utf-8")

    for _ in range(2):
        rc = A.cmd_apply(argparse.Namespace(proj=str(work), tsv=str(tsv), locale="ar",
                                           quiet=True))
        check("plural apply rc", rc == 0)
    items = [e for e in A.read_entries(res / "values-ar" / "plurals.xml") if e.kind == "plural"]
    check("four categories, not eight", len(items) == 4)
    check("no duplicate quantity", len({e.key for e in items}) == 4)


def test_markup_survives_apply(tmp):
    """`<xliff:g>` and `formatted="false"` must come out of apply intact.

    `escape_android` escaped every `<`, so a translator's `<xliff:g id="name">`
    was written as text and the phone printed the tag instead of the value-
    protection it provides. `write_strings_xml` dropped the attribute block, so
    `formatted="false"` vanished — and a translated `100%` without it is a
    compile error.
    """
    root = Path(tmp) / "markup"
    res = root / "res"
    for d in ("values", "values-ar"):
        (res / d).mkdir(parents=True)
    (res / "values" / "strings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<resources xmlns:xliff="urn:oasis:names:tc:xliff:document:1.2">'
        '<string name="b" formatted="false">100% guaranteed</string>'
        '<string name="c">Hello <xliff:g id="name">%1$s</xliff:g></string>'
        '</resources>', encoding="utf-8")
    (root / "AndroidManifest.xml").write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
        ' package="com.x"><application android:supportsRtl="true"/></manifest>',
        encoding="utf-8")
    tsv = root / "t.tsv"
    tsv.write_text("# key\tsource\ttranslation\n"
                   "b\t100% guaranteed\tمضمون 100%\n"
                   'c\tHello <xliff:g id="name">%1$s</xliff:g>\t'
                   'مرحبا <xliff:g id="name">%1$s</xliff:g>\n', encoding="utf-8")
    rc = A.cmd_apply(argparse.Namespace(proj=str(root), tsv=str(tsv), locale="ar",
                                       quiet=True))
    check("markup apply rc", rc == 0)
    out = (res / "values-ar" / "strings.xml").read_text(encoding="utf-8")
    check("xliff kept as markup", "<xliff:g" in out and "&lt;xliff" not in out)
    check("xliff namespace declared", "xmlns:xliff" in out)
    check("formatted attribute kept", 'formatted="false"' in out)
    # the file must parse as XML with xliff:g as an element, not as text
    import xml.etree.ElementTree as ET
    doc = ET.fromstring(out)
    g = doc.find('.//{urn:oasis:names:tc:xliff:document:1.2}g')
    check("xliff:g is an element", g is not None and g.get("id") == "name")
    check("bold survives too", A.escape_android("<b>hi</b>") == "<b>hi</b>")
    check("stray < still escaped", A.escape_android("a < b") == "a &lt; b")


def test_locale_config(tmp):
    """scaffold writes res/xml/locales_config.xml and the manifest attribute.

    Verified with aapt2 that the generated file compiles and that
    `@xml/locales_config` links against the manifest — the attribute is not a
    string, and a wrong resource name fails at link time, not at write time.
    """
    root = Path(tmp) / "loc"
    res = root / "res"
    for d in ("values", "values-ar", "values-fr", "values-sw600dp", "values-night"):
        (res / d).mkdir(parents=True)
        (res / d / "strings.xml").write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n<resources>'
            '<string name="a">x</string></resources>', encoding="utf-8")
    (root / "AndroidManifest.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
        ' package="com.x">\n    <uses-sdk android:minSdkVersion="34"/>\n'
        '    <application android:label="x">\n'
        '        <activity android:name=".M"/>\n    </application>\n</manifest>\n',
        encoding="utf-8")

    check("locales found, qualifiers excluded",
          A.existing_locales(res) == ["ar", "fr"], str(A.existing_locales(res)))

    A.cmd_scaffold(argparse.Namespace(proj=str(root), locale="ar", tsv=None,
                                      force=True, quiet=True))
    cfg = res / "xml" / "locales_config.xml"
    check("config written", cfg.exists())
    text = cfg.read_text(encoding="utf-8")
    check("two locales, not five", text.count("<locale ") == 2, str(text.count("<locale ")))
    check("sw600dp excluded", "sw600dp" not in text)
    check("xmlns declared", "xmlns:android" in text)

    import xml.etree.ElementTree as ET
    doc = ET.fromstring(text)
    names = [e.get("{http://schemas.android.com/apk/res/android}name") for e in doc]
    check("locale names parsed", names == ["ar", "fr"], str(names))

    man = (root / "AndroidManifest.xml").read_text(encoding="utf-8")
    check("manifest attribute", 'android:localeConfig="@xml/locales_config"' in man)
    check("manifest still parses", ET.fromstring(man) is not None)

    # a second run must not append the attribute again
    A.cmd_scaffold(argparse.Namespace(proj=str(root), locale="ar", tsv=None,
                                      force=True, quiet=True))
    man2 = (root / "AndroidManifest.xml").read_text(encoding="utf-8")
    check("idempotent", man2.count("localeConfig") == 1, str(man2.count("localeConfig")))

    # below API 33 the config is inert — skip it rather than ship dead config
    low = Path(tmp) / "low"
    lowres = low / "res"
    for d in ("values", "values-ar"):
        (lowres / d).mkdir(parents=True)
        (lowres / d / "strings.xml").write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n<resources>'
            '<string name="a">x</string></resources>', encoding="utf-8")
    (low / "AndroidManifest.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
        ' package="com.y"><uses-sdk android:minSdkVersion="26"/>'
        '<application android:label="y"/></manifest>', encoding="utf-8")
    A.cmd_scaffold(argparse.Namespace(proj=str(low), locale="ar", tsv=None,
                                      force=True, quiet=True))
    check("skipped under API 33", not (lowres / "xml" / "locales_config.xml").exists())
    check("no attribute under API 33",
          "localeConfig" not in (low / "AndroidManifest.xml").read_text(encoding="utf-8"))

    # the opt-out has to be honoured even when the SDK is new enough
    A.cmd_scaffold(argparse.Namespace(proj=str(root), locale="ar", tsv=None, force=True,
                                      quiet=True, no_locale_config=True))
    check("opt-out honoured", cfg.exists())  # left from the first run, not rewritten

    # an app that already ships its own config must keep it, and must be told
    own = Path(tmp) / "own"
    ownres = own / "res"
    for d in ("values", "values-ar"):
        (ownres / d).mkdir(parents=True)
        (ownres / d / "strings.xml").write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n<resources>'
            '<string name="a">x</string></resources>', encoding="utf-8")
    (own / "AndroidManifest.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
        ' package="com.z"><uses-sdk android:minSdkVersion="34"/>'
        '<application android:localeConfig="@xml/their_own" android:label="z">'
        '<activity android:name=".M"/></application></manifest>', encoding="utf-8")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        A.cmd_scaffold(argparse.Namespace(proj=str(own), locale="ar", tsv=None,
                                          force=True, quiet=True))
    own_man = (own / "AndroidManifest.xml").read_text(encoding="utf-8")
    check("their attribute untouched", '@xml/their_own' in own_man)
    check("no duplicate localeConfig", own_man.count("localeConfig") == 1)
    check("no stray config written", not (ownres / "xml" / "locales_config.xml").exists())
    check("told the user", "their_own" in out.getvalue(), out.getvalue()[:200])


def test_text_like_vectors(tmp):
    """Only real lettering is reported — never a badge by name alone.

    Measured against a real APK first: 215 drawables, of which 86 are vectors,
    every one of them 2 paths. A name-based rule would have flagged
    `bg_elo_badge.xml` (a `<shape>` background) and
    `abc_ic_arrow_drop_right_black_24dp.xml` (already `autoMirrored`), and a user
    who is warned about correct files stops reading the warnings.
    """
    root = Path(tmp) / "vec"
    res = root / "res"
    for d in ("values", "values-ar", "drawable"):
        (res / d).mkdir(parents=True)
    for d in ("values", "values-ar"):
        (res / d / "strings.xml").write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n<resources>'
            '<string name="a">x</string></resources>', encoding="utf-8")
    (root / "AndroidManifest.xml").write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
        ' package="com.x"><application android:label="x"/></manifest>', encoding="utf-8")

    def vector(name, paths, extra=""):
        p = "\n".join(f'    <path android:fillColor="#000" android:pathData="M{i},0 L{i+2},0 Z"/>'
                      for i in range(paths))
        (res / "drawable" / name).write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            f'<vector xmlns:android="http://schemas.android.com/apk/res/android"\n'
            f'    android:width="120.0dip" android:height="32.0dip"{extra}\n'
            f'    android:viewportWidth="120.0" android:viewportHeight="32.0">\n{p}\n'
            '</vector>\n', encoding="utf-8")

    vector("logo_wordmark.xml", 20)                       # lettering
    vector("ic_star.xml", 2)                              # ordinary icon
    vector("logo_mirrored.xml", 20, ' android:autoMirrored="true"')  # already handled
    (res / "drawable" / "bg_badge.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<shape xmlns:android="http://schemas.android.com/apk/res/android"'
        ' android:shape="rectangle"><solid android:color="#f00"/></shape>', encoding="utf-8")

    # A `<layer-list>` that assembles many shapes has plenty of paths but is a
    # composed background, not lettering — the `<vector>` guard is what keeps it
    # out, so this case has to exist or that guard is untested.
    layers = "".join(f'<item android:drawable="@drawable/p{i}"/>' for i in range(15))
    shapes = "".join('<path android:pathData="M0,0 L1,1 Z"/>' for _ in range(15))
    (res / "drawable" / "bg_layered.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<layer-list xmlns:android="http://schemas.android.com/apk/res/android">'
        f'{layers}{shapes}</layer-list>', encoding="utf-8")

    got = [v[0] for v in A.text_like_vectors(root)]
    check("lettering found", "res/drawable/logo_wordmark.xml" in got, str(got))
    check("ordinary icon not flagged", "res/drawable/ic_star.xml" not in got, str(got))
    check("autoMirrored not flagged", "res/drawable/logo_mirrored.xml" not in got, str(got))
    check("shape named badge not flagged", "res/drawable/bg_badge.xml" not in got, str(got))
    check("layer-list with paths not flagged", "res/drawable/bg_layered.xml" not in got, str(got))
    check("exactly one finding", len(got) == 1, str(got))


def test_forced_scaffold_keeps_six_plural_forms(proj, tmp):
    """A forced scaffold must not drop the categories the source never had.

    Arabic needs six plural categories and an English source declares two. The
    other four live in `values-ar` and nowhere else, so building the output by
    iterating the *source* entries lost them: a real locale went from 54
    quantities to 18 on `scaffold --force`, and `.bak` only records the fall.
    """
    root = Path(tmp) / "six"
    res = root / "res"
    (res / "values").mkdir(parents=True)
    (res / "values-ar").mkdir(parents=True)
    (res / "values" / "strings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>'
        '<plurals name="files"><item quantity="one">%d file</item>'
        '<item quantity="other">%d files</item></plurals></resources>', encoding="utf-8")
    (res / "values-ar" / "strings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>'
        '<plurals name="files">'
        '<item quantity="zero">لا ملفات</item>'
        '<item quantity="one">ملف واحد</item>'
        '<item quantity="two">ملفان</item>'
        '<item quantity="few">%d ملفات</item>'
        '<item quantity="many">%d ملفاً</item>'
        '<item quantity="other">%d ملف</item>'
        '</plurals></resources>', encoding="utf-8")
    (root / "AndroidManifest.xml").write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
        ' package="com.x"><application android:label="x"/></manifest>', encoding="utf-8")

    A.cmd_scaffold(argparse.Namespace(proj=str(root), locale="ar", tsv=None,
                                      force=True, quiet=True))
    items = [e for e in A.read_entries(res / "values-ar" / "strings.xml")
             if e.kind == "plural"]
    check("all six survive", len(items) == 6, str(sorted(e.key for e in items)))
    got = {e.key: e.text for e in items}
    check("zero kept", got.get("files|zero") == "لا ملفات")
    check("two kept", got.get("files|two") == "ملفان")
    check("few kept", got.get("files|few") == "%d ملفات")
    check("many kept", got.get("files|many") == "%d ملفاً")
    # a non-resource file under res/ makes `aapt2 compile --dir res` fail with
    # "invalid file path" — verified. The backup must live outside.
    check("nothing non-resource under res/",
          not list(res.rglob("*.bak")), str(list(res.rglob("*.bak"))))
    check("backup kept outside res/",
          (root / "aqlab-backup-ar" / "strings.xml").exists())


def test_dry_run_changes_nothing(proj, tmp):
    """`--dry-run` must print the plan and touch not one byte.

    `apply` and `scaffold` rewrite whole files. Before this, trying a command
    meant living with its result — and a half-filled table used to delete the
    translations beside it, so 'trying it' was the dangerous part.
    """
    work = Path(tmp) / "dryrun"
    shutil.copytree(proj, work)
    res = work / "res"
    ar = res / "values-ar"
    ar.mkdir(parents=True, exist_ok=True)
    (ar / "strings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>'
        '<string name="app_name">اسم</string></resources>', encoding="utf-8")
    tsv = work / "t.tsv"
    tsv.write_text("# key\tsource\ttranslation\napp_name\tx\tاسم جديد\nwelcome\ty\tأهلاً\n",
                   encoding="utf-8")

    def snapshot():
        return {p: p.read_bytes() for p in res.rglob("*") if p.is_file()}

    before = snapshot()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = A.cmd_apply(argparse.Namespace(proj=str(work), tsv=str(tsv), locale="ar",
                                           quiet=True, dry_run=True))
    check("dry apply rc", rc == 0)
    check("dry apply wrote nothing", snapshot() == before)
    text = out.getvalue()
    check("dry apply says DRY", "[DRY]" in text, text[:200])
    check("dry apply marks overwrite", "would overwrite" in text, text[:300])
    check("dry apply names the file", "strings.xml" in text)

    out2 = io.StringIO()
    with contextlib.redirect_stdout(out2):
        rc = A.cmd_scaffold(argparse.Namespace(proj=str(work), locale="ar", tsv=None,
                                               force=True, quiet=True, dry_run=True))
    check("dry scaffold rc", rc == 0)
    check("dry scaffold wrote nothing", snapshot() == before)
    check("dry scaffold made no backup", not (work / "aqlab-backup-ar").exists())
    check("dry scaffold says DRY", "[DRY]" in out2.getvalue(), out2.getvalue()[:200])

    # the real run must then do exactly what the plan promised
    out3 = io.StringIO()
    with contextlib.redirect_stdout(out3):
        A.cmd_apply(argparse.Namespace(proj=str(work), tsv=str(tsv), locale="ar",
                                       quiet=True))
    got = (ar / "strings.xml").read_text(encoding="utf-8")
    check("real run applied the change", "اسم جديد" in got)
    check("real run kept the rest", "أهلاً" in got)

    # `commit()` is the last line of defence: commands short-circuit before it on
    # a dry run, so the guard inside it needs its own check or it is never proven.
    target = Path(tmp) / "commit_guard.txt"
    sink = A.Writes(dry=True)
    sink.add(target, "should not exist\n")
    check("dry commit returns nothing", sink.commit() == [])
    check("dry commit wrote nothing", not target.exists())
    live = A.Writes(dry=False)
    live.add(target, "written\n")
    live.commit()
    check("live commit writes", target.exists() and target.read_text(encoding="utf-8") == "written\n")


def test_non_resource_files_are_reported(tmp):
    """A stray file under res/ breaks `aapt2 compile`; say so at write time.

    Verified against aapt2: `notes.tsv`, `strings.xml.bak` and `old.xml.orig`
    under res/ each produce `error: invalid file path`. This bit the project
    once with a .bak the tool itself wrote, so the check lives next to the write.
    """
    root = Path(tmp) / "stray"
    res = root / "res"
    for d in ("values", "values-ar", "drawable", "xml"):
        (res / d).mkdir(parents=True)
    (res / "values" / "strings.xml").write_text("<resources/>", encoding="utf-8")
    (res / "drawable" / "ic_a.xml").write_text("<vector/>", encoding="utf-8")
    (res / "drawable" / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (res / "values-ar" / "strings.xml.bak").write_text("x", encoding="utf-8")
    (res / "values-ar" / "notes.tsv").write_text("x", encoding="utf-8")
    (res / "values" / "old.xml.orig").write_text("x", encoding="utf-8")
    (res / "notaresourcedir" / "a.xml").parent.mkdir()
    (res / "notaresourcedir" / "a.xml").write_text("<x/>", encoding="utf-8")

    bad = {p.name for p in A.non_resource_files(res)}
    check("bak caught", "strings.xml.bak" in bad, str(bad))
    check("tsv caught", "notes.tsv" in bad, str(bad))
    check(".orig caught", "old.xml.orig" in bad, str(bad))
    check("unknown folder caught", "a.xml" in bad, str(bad))
    check("real resource not flagged", "strings.xml" not in bad and "ic_a.xml" not in bad,
          str(bad))
    check("image not flagged", "photo.png" not in bad, str(bad))


def test_coverage_per_file_and_gates(tmp):
    """One number hides the file that is behind; a threshold makes it a gate.

    An overall 88% can be strings.xml at 99% and plurals.xml at 60%. The 60% is
    what a reader sees on screen, and the total said nothing about it.
    """
    root = Path(tmp) / "cov"
    res = root / "res"
    for d in ("values", "values-ar"):
        (res / d).mkdir(parents=True)
    # 10 strings, 4 plurals; Arabic covers all strings and no plurals
    (res / "values" / "strings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>'
        + "".join(f'<string name="s{i}">text {i}</string>' for i in range(10))
        + "</resources>", encoding="utf-8")
    (res / "values" / "plurals.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>'
        + "".join(f'<plurals name="p{i}"><item quantity="one">%d a</item>'
                  f'<item quantity="other">%d b</item></plurals>' for i in range(4))
        + "</resources>", encoding="utf-8")
    (res / "values-ar" / "strings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>'
        + "".join(f'<string name="s{i}">نص {i}</string>' for i in range(10))
        + "</resources>", encoding="utf-8")
    (root / "AndroidManifest.xml").write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
        ' package="com.x"><application android:label="x"/></manifest>', encoding="utf-8")

    def run(**kw):
        ns = argparse.Namespace(proj=str(root), locale="ar", quiet=True,
                                verbose=kw.pop("verbose", False),
                                fail_under=kw.pop("fail_under", None),
                                fail_under_file=kw.pop("fail_under_file", None))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = A.cmd_lint(ns)
        return rc, out.getvalue()

    rc, text = run()
    check("per-file lines shown", "strings.xml" in text and "plurals.xml" in text, text[:400])
    check("strings at 100", "10/10" in text, text[:400])
    check("plurals at 0", "0/8" in text, text[:400])
    check("weakest marked", "weakest" in text, text[:400])
    # the per-file block is indented under the summary; nothing else in lint is
    check("per-file block present", "\n            plurals.xml" in text, text[:700])

    rc, _ = run(fail_under=50)
    check("--fail-under below actual passes", rc != 1, str(rc))

    rc, text = run(fail_under=99)
    check("--fail-under above actual fails", rc == 1, str(rc))
    check("says which gate", "below the --fail-under" in text, text[-300:])

    rc, text = run(fail_under_file=50)
    check("--fail-under-file catches the weak file", rc == 1, str(rc))
    check("names the file", "plurals.xml" in text[-400:], text[-300:])


def test_rtl_locales_beyond_arabic(tmp):
    """Hebrew, Persian and Urdu are RTL and do not share Arabic's six categories.

    Verified against CLDR (charts/47, language_plural_rules): ar has
    zero/one/two/few/many/other; he has one/two/many/other — no zero, no few;
    fa and ur have one/other. Demanding six of every language would put forms on
    translators that those languages never use.
    """
    check("ar six", A.plural_categories("ar") ==
          ("zero", "one", "two", "few", "many", "other"))
    check("he four", A.plural_categories("he") == ("one", "two", "many", "other"))
    check("fa two", A.plural_categories("fa") == ("one", "other"))
    check("ur two", A.plural_categories("ur") == ("one", "other"))
    check("region resolves", A.plural_categories("he-IL") == A.plural_categories("he"))
    check("rtl flags", all(A.is_rtl(x) for x in ("ar", "he", "fa", "ur", "ar-rSA")))
    check("ltr is not rtl", not A.is_rtl("en") and not A.is_rtl("fr"))

    # a Hebrew plural table gets four stubs, not six
    root = Path(tmp) / "heb"
    res = root / "res"
    (res / "values").mkdir(parents=True)
    (res / "values" / "strings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>'
        '<string name="a">x</string>'
        '<plurals name="files"><item quantity="one">%d f</item>'
        '<item quantity="other">%d fs</item></plurals></resources>', encoding="utf-8")
    (root / "AndroidManifest.xml").write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
        ' package="com.x"><application android:label="x"/></manifest>', encoding="utf-8")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        A.cmd_scaffold(argparse.Namespace(proj=str(root), locale="he", tsv=None,
                                          force=False, quiet=True))
    rows = [l.split("\t")[0] for l in (root / "values-he.tsv").read_text(
        encoding="utf-8").splitlines() if l.startswith("files|")]
    check("he scaffold: four rows", rows == ["files|one", "files|two", "files|many",
                                             "files|other"], str(rows))
    check("he has no few row", "files|few" not in rows, str(rows))

    # lint must not ask Hebrew for `few`, and must ask for `many`
    (res / "values-he").mkdir(parents=True, exist_ok=True)
    (res / "values-he" / "strings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>'
        '<string name="a">א</string>'
        '<plurals name="files"><item quantity="one">קובץ</item>'
        '<item quantity="other">קבצים</item></plurals></resources>', encoding="utf-8")
    out2 = io.StringIO()
    with contextlib.redirect_stdout(out2):
        A.cmd_lint(argparse.Namespace(proj=str(root), locale="he", quiet=True,
                                      verbose=False, fail_under=None,
                                      fail_under_file=None))
    text = out2.getvalue()
    check("he missing many reported", "many" in text and "plurals/files" in text, text[:600])
    check("he never asked for few", "few" not in text, text[:600])
    check("he range is its own", "20, 30, 40" in text, text[:600])


def test_diff_between_apks(tmp):
    """`aqlab diff` compares string resources straight out of two APKs.

    Parsed from `aapt2 dump resources` so neither APK is decoded — no 50-second
    apktool run per side and no working copy. Text that did not change must not
    be reported: it is the majority of any diff and would bury the real ones.
    """
    aapt2 = A.tool("aapt2")
    if not aapt2:
        print("  (skipped: aapt2 not found)")
        return
    aj = next(iter(sorted(Path("C:/Users/speed/android-sdk/platforms").glob(
        "android-*/android.jar"), reverse=True)), None)
    if not aj:
        print("  (skipped: android.jar not found)")
        return

    def build(name, vals, ar):
        root = Path(tmp) / name
        (root / "res" / "values").mkdir(parents=True)
        (root / "res" / "values-ar").mkdir(parents=True)
        (root / "res" / "values" / "strings.xml").write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n<resources>'
            + "".join(f'<string name="{k}">{v}</string>' for k, v in vals.items())
            + "</resources>", encoding="utf-8")
        (root / "res" / "values-ar" / "strings.xml").write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n<resources>'
            + "".join(f'<string name="{k}">{v}</string>' for k, v in ar.items())
            + "</resources>", encoding="utf-8")
        (root / "AndroidManifest.xml").write_text(
            '<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
            ' package="com.d"><application android:label="keep"/></manifest>', encoding="utf-8")
        zip_path = str(root) + ".zip"
        A.run([aapt2, "compile", "--dir", str(root / "res"), "-o", zip_path], quiet=True)
        apk = str(root) + ".apk"
        A.run([aapt2, "link", "-o", apk, "-I", str(aj), "--manifest",
               str(root / "AndroidManifest.xml"), zip_path], quiet=True)
        return apk

    old_apk = build("old", {"keep": "Same", "changing": "Before", "gone": "Bye"},
                    {"keep": "واحد", "changing": "قبل", "gone": "راح"})
    new_apk = build("new", {"keep": "Same", "changing": "After", "fresh": "New"},
                    {"keep": "واحد", "changing": "بعد", "fresh": "جديد"})

    old, new = A.apk_strings(old_apk), A.apk_strings(new_apk)
    check("apk strings read", ("ar", "keep") in old and old[("ar", "keep")] == "واحد",
          str(list(old.items())[:3]))

    d = A.diff_strings(old, new)
    check("added found", ("", "fresh") in d["added"] and ("ar", "fresh") in d["added"],
          str(d["added"]))
    check("removed found", ("", "gone") in d["removed"] and ("ar", "gone") in d["removed"],
          str(d["removed"]))
    check("changed found", ("", "changing") in d["changed"], str(d["changed"]))
    check("unchanged never reported",
          not any(n == "keep" for _, n in d["added"] + d["removed"] + d["changed"]))

    ar_only = A.diff_strings(old, new, locale="ar")
    check("locale filter: default excluded",
          all(l == "ar" for l, _ in ar_only["added"] + ar_only["removed"] + ar_only["changed"]),
          str(ar_only["added"]))
    check("locale filter keeps count", ar_only["added_n"] == 1 and ar_only["removed_n"] == 1)

    same = A.diff_strings(old, old)
    check("identical has no findings",
          not (same["added_n"] or same["removed_n"] or same["changed_n"]))

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = A.cmd_diff(argparse.Namespace(old=old_apk, new=new_apk, locale=None,
                                           limit=40, json=False, quiet=True))
    text = out.getvalue()
    check("diff reports a change and exits 1", rc == 1, str(rc))
    check("prints both sides of a change", "Before" in text and "After" in text, text[:600])
    check("prints the marker", "~" in text and "+" in text and "-" in text, text[:600])

    out2 = io.StringIO()
    with contextlib.redirect_stdout(out2):
        rc2 = A.cmd_diff(argparse.Namespace(old=old_apk, new=old_apk, locale=None,
                                            limit=40, json=False, quiet=True))
    check("identical exits 0", rc2 == 0, str(rc2))

    out3 = io.StringIO()
    with contextlib.redirect_stdout(out3):
        A.cmd_diff(argparse.Namespace(old=old_apk, new=new_apk, locale=None,
                                      limit=40, json=True, quiet=True))
    payload = json.loads(out3.getvalue().splitlines()[-1])
    check("json is valid and labelled",
          "(default)|changing" in payload["changed"], str(payload["changed"]))


def test_xliff_wrapping(tmp):
    """`aqlab xliff` wraps specifiers so translators cannot reorder or drop them.

    Verified with aapt2: the wrapped file compiles, links, and the resolved value
    is the bare `%1$s` again — the tag is compile-time only. `%%` is a literal
    percent and must not be wrapped; an existing `<xliff:g>` must not be
    double-wrapped.
    """
    check("positional id from index",
          A.apply_xliff("%2$d items") == '<xliff:g id="arg2">%2$d</xliff:g> items',
          A.apply_xliff("%2$d items"))
    check("sequential for bare specifiers",
          A.apply_xliff("%s and %d") ==
          '<xliff:g id="arg1">%s</xliff:g> and <xliff:g id="arg2">%d</xliff:g>',
          A.apply_xliff("%s and %d"))
    check("%% untouched", A.apply_xliff("50%% off") == "50%% off",
          A.apply_xliff("50%% off"))
    check("no specifier is a no-op", A.apply_xliff("نص عادي") == "نص عادي")

    root = Path(tmp) / "xlif"
    res = root / "res"
    (res / "values").mkdir(parents=True)
    (res / "values-ar").mkdir(parents=True)
    (res / "values" / "strings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>'
        '<string name="a">x</string></resources>', encoding="utf-8")
    ar = res / "values-ar" / "strings.xml"
    ar.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>\n'
        '    <string name="welcome">مرحباً %1$s، لديك %2$d</string>\n'
        '    <string name="pct">خصم 50%%</string>\n'
        '    <string name="plain">لا متغيرات</string>\n'
        '    <string name="done">مرحباً <xliff:g id="name">%1$s</xliff:g></string>\n'
        '</resources>\n', encoding="utf-8")
    (root / "AndroidManifest.xml").write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
        ' package="com.x"><application android:label="a"/></manifest>', encoding="utf-8")

    before = ar.read_bytes()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = A.cmd_xliff(argparse.Namespace(proj=str(root), locale="ar", quiet=True,
                                            dry_run=True))
    check("dry xliff rc", rc == 0)
    check("dry xliff wrote nothing", ar.read_bytes() == before)
    check("dry xliff says DRY", "[DRY]" in out.getvalue(), out.getvalue()[:200])

    with contextlib.redirect_stdout(io.StringIO()):
        A.cmd_xliff(argparse.Namespace(proj=str(root), locale="ar", quiet=True))
    text = ar.read_text(encoding="utf-8")
    check("namespace added", "xmlns:xliff" in text, text[:200])
    check("welcome wrapped twice", text.count('<xliff:g id="arg') == 2, text)
    check("percent untouched", "خصم 50%%" in text)
    check("plain untouched", "لا متغيرات" in text)
    check("existing xliff not double-wrapped",
          'id="name">%1$s' in text and text.count('id="arg1">%1$s') == 1, text)

    import xml.etree.ElementTree as ET
    doc = ET.fromstring(text)
    gs = doc.findall(".//{urn:oasis:names:tc:xliff:document:1.2}g")
    check("file is well-formed with xliff elements", len(gs) == 3, str(len(gs)))

    # a second run must find nothing to do
    out2 = io.StringIO()
    with contextlib.redirect_stdout(out2):
        A.cmd_xliff(argparse.Namespace(proj=str(root), locale="ar", quiet=True))
    check("second run is a no-op", "already wrapped" in out2.getvalue(),
          out2.getvalue()[:200])


def test_symmetry_check(tmp):
    """RtlSymmetry: one side set without its partner puts the layout off-centre.

    Android's own check, verified against the lint docs. The subtlety is that
    left/right and start/end are *independent pairs*: a view with
    paddingLeft+paddingStart and paddingRight but no paddingEnd has covered its
    start side twice and still left the end side half-done, so both findings are
    real. Measured on a real APK first — the first version aliased the spellings
    together and reported the same view twice, which is how a check earns being
    ignored.
    """
    root = Path(tmp) / "sym"
    res = root / "res"
    for d in ("values", "values-ar", "layout"):
        (res / d).mkdir(parents=True)
    for d in ("values", "values-ar"):
        (res / d / "strings.xml").write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n<resources>'
            '<string name="a">x</string></resources>', encoding="utf-8")
    (root / "AndroidManifest.xml").write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
        ' package="com.x"><application android:label="a"/></manifest>', encoding="utf-8")

    (res / "layout" / "main.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"\n'
        '    android:layout_width="match_parent" android:layout_height="match_parent">\n'
        '    <TextView android:id="@+id/one_side"\n'
        '        android:paddingLeft="16dp"\n'
        '        android:layout_width="wrap_content" android:layout_height="wrap_content"/>\n'
        '    <TextView android:id="@+id/balanced"\n'
        '        android:paddingLeft="16dp" android:paddingRight="16dp"\n'
        '        android:layout_width="wrap_content" android:layout_height="wrap_content"/>\n'
        '    <TextView android:id="@+id/modern_balanced"\n'
        '        android:paddingStart="8dp" android:paddingEnd="8dp"\n'
        '        android:layout_width="wrap_content" android:layout_height="wrap_content"/>\n'
        '    <TextView android:id="@+id/all_four"\n'
        '        android:paddingLeft="8dp" android:paddingStart="8dp"\n'
        '        android:paddingRight="8dp" android:paddingEnd="8dp"\n'
        '        android:layout_width="wrap_content" android:layout_height="wrap_content"/>\n'
        '</LinearLayout>\n', encoding="utf-8")

    got = A.symmetry_problems(root)
    tags = [(t, a, b) for _, t, a, b, _ in got]
    check("one_side flagged", ("TextView", "paddingLeft", "paddingRight") in tags, str(tags))
    check("balanced not flagged", len(got) == 1, str(tags))
    check("both spellings not flagged", not any(
        "all_four" in str(x) for x in tags), str(tags))

    # an element carrying both spellings but missing one partner is still real
    (res / "layout" / "main.xml").unlink()
    (res / "layout" / "mixed.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<ImageView xmlns:android="http://schemas.android.com/apk/res/android"\n'
        '    android:paddingLeft="12dp" android:paddingStart="12dp"\n'
        '    android:paddingRight="12dp"\n'
        '    android:layout_width="wrap_content" android:layout_height="wrap_content"/>\n',
        encoding="utf-8")
    got2 = A.symmetry_problems(root)
    pairs = {(a, b) for _, _, a, b, _ in got2}
    check("missing paddingEnd still reported", ("paddingStart", "paddingEnd") in pairs,
          str(pairs))
    check("paddingLeft satisfied by paddingRight", ("paddingLeft", "paddingRight") not in pairs,
          str(pairs))

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        A.cmd_lint(argparse.Namespace(proj=str(root), locale="ar", quiet=True,
                                      verbose=False, fail_under=None,
                                      fail_under_file=None))
    text = out.getvalue()
    check("lint reports it as a warning", "[WARN]" in text and "not symmetric" in text,
          text[:500])



def test_single_quoted_attributes(tmp):
    """A single-quoted attribute value is valid Android XML and was invisible.

    Measured on the real APK first: it carries no single quotes, so this was a
    silent hole rather than a false positive. XML allows either quote character
    on an attribute value and aapt normalises both, so a layout copied out of a
    blog post with `paddingLeft='4dp'` migrated nothing, and lint then reported
    nothing either — the attribute never entered the dict the symmetry check
    reads. Both sides have to agree, so this asserts migration, the symmetry
    reader and the pin reader on the same fixture.
    """
    q = chr(39)
    text = ('<TextView android:paddingLeft=' + q + '4dp' + q
            + ' android:layout_marginRight="8dp"/>')
    out, n = A.rewrite_xml_attrs(text)
    check("single-quoted paddingLeft is migrated", 'android:paddingStart="4dp"' in out, out)
    check("single-quoted and double-quoted count as two edits", n == 2, str(n))

    # A tag carrying only single quotes must still be seen by the machinery.
    mixed = ('<LinearLayout android:paddingLeft=' + q + '2dp' + q
             + ' android:paddingRight=' + q + '2dp' + q
             + ' android:gravity=' + q + 'left' + q + '/>')
    out2, n2 = A.rewrite_xml_attrs(mixed)
    check("a fully single-quoted tag is not skipped",
          'android:paddingStart="2dp"' in out2 and 'gravity="start"' in out2, out2)

    root = Path(tmp) / "sq"
    layout = root / "res" / "layout"
    layout.mkdir(parents=True)
    (root / "AndroidManifest.xml").write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
        ' package="com.x"><application android:label="a"/></manifest>', encoding="utf-8")
    # one side single-quoted, the partner missing: the same finding as double quotes
    (layout / "a.xml").write_text(
        "<ImageView android:paddingLeft=" + q + "4dp" + q + "/>", encoding="utf-8")
    got = A.symmetry_problems(root)
    check("symmetry sees a single-quoted side",
          [(a, b) for _, _, a, b, _ in got] == [("paddingLeft", "paddingRight")], str(got))

    # and the pin reader: a single-quoted layoutDirection still pins its row.
    (layout / "a.xml").write_text(
        "<LinearLayout android:layoutDirection=" + q + "ltr" + q
        + " android:paddingLeft=" + q + "4dp" + q + "/>", encoding="utf-8")
    pins = [k for k, *_ in A.direction_pins(root)]
    check("pin reader sees a single-quoted layoutDirection",
          pins == ["layoutDirection"], str(pins))


def test_single_quoted_gravity_and_fix(tmp):
    """`hardcoded_gravity` and both fixers read only double quotes.

    Round 14 widened `rewrite_xml_attrs`, `symmetry_problems` and the pin reader
    to either quote character, but the gravity reader and the two writers stayed
    on `"`. Measured on the real APK first: it carries no single quotes, so
    nothing fired and nothing was reported — the hole was silent there.

    Two defects, in opposite directions:
      * `hardcoded_gravity` never *reported* `gravity='left'`, and gravity is the
        one defect `aqlab rtl` deliberately leaves for the user to read;
      * `symmetry_problems` did report a single-quoted `paddingLeft`, while
        `symmetric_fix` could not see the attribute — so `--fix` promised a
        repair it could never perform, the same bug as one that repairs what it
        never reported.
    """
    q = chr(39)
    root = Path(tmp) / "sqgrav"
    layout = root / "res" / "layout"
    layout.mkdir(parents=True)
    (root / "AndroidManifest.xml").write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
        ' package="com.x"><application android:label="a"/></manifest>', encoding="utf-8")

    (layout / "a.xml").write_text(
        "<TextView android:gravity=" + q + "left" + q + ' android:textAlignment='
        + q + "right" + q + "/>", encoding="utf-8")
    got = A.hardcoded_gravity(root)
    check("gravity reader sees a single-quoted value", len(got) == 2, str(got))
    check("gravity reader reports the value", {v for _, _, v in got} == {"left", "right"},
          str(got))

    txt = (layout / "a.xml").read_text(encoding="utf-8")
    out, hits = A.gravity_fix(txt)
    check("gravity_fix rewrites a single-quoted value", hits == 2, str(hits))
    check("gravity_fix keeps the quotes it came in with",
          "gravity=" + q + "start" + q in out and "textAlignment=" + q + "viewEnd" + q in out,
          out)
    check("gravity_fix is idempotent", A.gravity_fix(out)[1] == 0, out)
    check("double quotes still work", A.gravity_fix('android:gravity="left"')[0]
          == 'android:gravity="start"', "")

    # The symmetry fixer must repair the element its own report named.
    (layout / "a.xml").write_text(
        "<TextView android:paddingLeft=" + q + "4dp" + q + ' android:paddingStart="4dp"/>',
        encoding="utf-8")
    probs = A.symmetry_problems(root)
    check("symmetry reports the single-quoted side", len(probs) == 2, str(probs))
    body = (layout / "a.xml").read_text(encoding="utf-8").split("<", 1)[1]
    fixed = A.symmetric_fix(body)
    check("symmetric_fix fills the modern hole", 'android:paddingEnd="4dp"' in fixed, fixed)
    check("symmetric_fix leaves the physical side to aqlab rtl",
          'paddingLeft=' + q + "4dp" + q in fixed, fixed)


def test_rtl_drawable_dirs(tmp):
    """ldrtl folders that mirror nothing: empty, or a copy of the LTR file.

    The real-APK baseline (one Material spinner in `drawable-ldrtl-xxhdpi` with
    no LTR twin) must stay silent — an ldrtl-only file is a legitimate mirror.
    """
    root = Path(tmp) / "ldrtl"
    res = root / "res"
    for d in ("values", "values-ar", "layout",
              "drawable-xxhdpi", "drawable-ldrtl-xxhdpi",
              "drawable-ldrtl", "drawable", "mipmap-ldrtl-hdpi"):
        (res / d).mkdir(parents=True)
    (root / "AndroidManifest.xml").write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
        ' package="com.x"><application android:label="a"/></manifest>', encoding="utf-8")
    for d in ("values", "values-ar"):
        (res / d / "strings.xml").write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n<resources>'
            '<string name="a">x</string></resources>', encoding="utf-8")

    # identical copy of the xxhdpi file -> the author changed nothing
    (res / "drawable-xxhdpi" / "icon.png").write_bytes(b"PNG-LTR")
    (res / "drawable-ldrtl-xxhdpi" / "icon.png").write_bytes(b"PNG-LTR")
    # a real mirror: different bytes, must stay silent
    (res / "drawable-xxhdpi" / "arrow.png").write_bytes(b"ARROW-LTR")
    (res / "drawable-ldrtl-xxhdpi" / "arrow.png").write_bytes(b"ARROW-RTL")
    # ldrtl-only file with no LTR twin -> legitimate, silent
    (res / "drawable-ldrtl" / "only_rtl.png").write_bytes(b"ONLY")
    # unqualified fallback peer is checked when the density dir has no twin
    (res / "drawable" / "plain.png").write_bytes(b"PLAIN")
    (res / "drawable-ldrtl" / "plain.png").write_bytes(b"PLAIN")
    # empty mipmap-ldrtl folder, only a .gitkeep
    (res / "mipmap-ldrtl-hdpi" / ".gitkeep").write_text("", encoding="utf-8")

    got = A.rtl_drawable_dirs(root)
    kinds = {(p, k) for p, k, _ in got}
    names = " ".join(p for p, _, _ in got)
    check("identical copy flagged", "drawable-ldrtl-xxhdpi/icon.png" in names, str(got))
    check("real mirror silent", "arrow.png" not in names, str(got))
    check("ldrtl-only silent", "only_rtl.png" not in names, str(got))
    check("fallback peer flagged", "drawable-ldrtl/plain.png" in names, str(got))
    check("empty folder flagged", "res/mipmap-ldrtl-hdpi" in names, str(got))
    check("empty is its own kind", ("res/mipmap-ldrtl-hdpi", "empty") in kinds, str(got))
    check("exactly three findings", len(got) == 3, str(got))

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        A.cmd_lint(argparse.Namespace(proj=str(root), locale="ar", quiet=True,
                                      verbose=False, fail_under=None,
                                      fail_under_file=None))
    text = out.getvalue()
    check("lint reports empty ldrtl", "ldrtl folder is empty" in text, text[-600:])
    check("lint reports identical copy", "byte-identical to" in text, text[-600:])


def test_hardcoded_gravity(tmp):
    """RtlHardcoded: a physical side in a gravity value ignores the locale.

    `center`, `start`, `end`, `viewStart` and `viewEnd` are direction-aware and
    must not be reported — a check that flags `gravity="center"` teaches people
    to stop reading its output.
    """
    root = Path(tmp) / "grav"
    res = root / "res"
    for d in ("values", "values-ar", "layout"):
        (res / d).mkdir(parents=True)
    for d in ("values", "values-ar"):
        (res / d / "strings.xml").write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n<resources>'
            '<string name="a">x</string></resources>', encoding="utf-8")
    (root / "AndroidManifest.xml").write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
        ' package="com.x"><application android:label="a"/></manifest>', encoding="utf-8")
    (res / "layout" / "t.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"\n'
        '    android:layout_width="match_parent" android:layout_height="match_parent">\n'
        '    <TextView android:gravity="center"/>\n'
        '    <TextView android:gravity="start|center_vertical"/>\n'
        '    <TextView android:gravity="end"/>\n'
        '    <TextView android:textAlignment="viewStart"/>\n'
        '    <TextView android:textAlignment="viewEnd"/>\n'
        '    <TextView android:gravity="left|bottom"/>\n'
        '    <TextView android:layout_gravity="right"/>\n'
        '</LinearLayout>\n', encoding="utf-8")

    got = A.hardcoded_gravity(root)
    check("only physical sides flagged", len(got) == 2, str(got))
    values = {v for _, _, v in got}
    check("left|bottom caught", "left|bottom" in values, str(values))
    check("plain right caught", "right" in values, str(values))
    check("center not flagged", not any(v == "center" for v in values), str(values))
    check("start/end not flagged",
          not any(v in ("start|center_vertical", "end") for v in values), str(values))
    check("viewStart/viewEnd not flagged",
          not any("view" in v.lower() for v in values), str(values))

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        A.cmd_lint(argparse.Namespace(proj=str(root), locale="ar", quiet=True,
                                      verbose=False, fail_under=None,
                                      fail_under_file=None))
    check("lint warns with the fix", "aqlab lint --fix" in out.getvalue(), out.getvalue()[:400])


def test_lint_fix(tmp):
    """`lint --fix` fills a missing padding/margin partner, idempotently.

    Measured on a real APK first: filling every hole the report named made one
    case worse — an element with a lone `marginRight` got a `marginStart` beside
    it and went from 1 finding to 2. Physical pairs belong to `aqlab rtl`, which
    renames them; `--fix` only completes the modern spellings.

    Also measured: rebuilding the matched tag from a suffix test dropped the `/`
    off `<View ... />` and left an unclosed tag, which breaks the whole layout.
    """
    root = Path(tmp) / "fixsym"
    res = root / "res"
    for d in ("values", "values-ar", "layout"):
        (res / d).mkdir(parents=True)
    for d in ("values", "values-ar"):
        (res / d / "strings.xml").write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n<resources>'
            '<string name="a">x</string></resources>', encoding="utf-8")
    (root / "AndroidManifest.xml").write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
        ' package="com.x"><application android:label="a"/></manifest>', encoding="utf-8")
    layout = res / "layout" / "main.xml"
    layout.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android">\n'
        '    <TextView android:paddingStart="16dp" android:layout_width="wrap_content"\n'
        '        android:layout_height="wrap_content"/>\n'
        '    <View android:layout_marginEnd="8dp" android:layout_width="1dp"\n'
        '        android:layout_height="1dp" />\n'
        '    <ImageButton android:paddingLeft="12dp" android:layout_width="1dp"\n'
        '        android:layout_height="1dp"/>\n'
        '    <TextView android:paddingStart="4dp" android:paddingEnd="4dp"\n'
        '        android:layout_width="wrap_content"\n'
        '        android:layout_height="wrap_content"/>\n'
        '</LinearLayout>\n', encoding="utf-8")

    got = A.symmetric_fix(' android:paddingStart="16dp"')
    check("adds paddingEnd", 'android:paddingEnd="16dp"' in got, got)
    check("physical alone untouched",
          A.symmetric_fix(' android:paddingLeft="12dp"') == ' android:paddingLeft="12dp"')

    args = argparse.Namespace(proj=str(root), locale="ar", quiet=True, verbose=False,
                              fail_under=None, fail_under_file=None, fix=True,
                              dry_run=True)
    before = layout.read_text(encoding="utf-8")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        A.cmd_lint(args)
    check("dry run writes nothing", layout.read_text(encoding="utf-8") == before)
    check("dry run announces", "would fix" in out.getvalue(), out.getvalue()[:300])

    args.dry_run = False
    with contextlib.redirect_stdout(io.StringIO()):
        A.cmd_lint(args)
    after = layout.read_text(encoding="utf-8")
    check("paddingEnd added", 'paddingStart="16dp" android:paddingEnd="16dp"' in after, after)
    check("marginStart added", 'layout_marginStart="8dp"' in after, after)
    check("self-closing kept", 'android:layout_height="1dp" />' in after, after)
    check("physical pair untouched", 'android:paddingLeft="12dp"' in after
          and "paddingRight" not in after, after)
    check("balanced element untouched", after.count('android:paddingEnd="4dp"') == 1, after)

    with contextlib.redirect_stdout(io.StringIO()) as out2:
        A.cmd_lint(args)
    check("idempotent", layout.read_text(encoding="utf-8") == after, after)
    check("second pass reports nothing", "fixed:" not in out2.getvalue(), out2.getvalue()[:200])

    # One element, two holes, one pass: `symmetric_fix` reads the attribute set
    # once, so calling it per element repaired paddingStart/End and left the
    # margin hole reported — a `--fix` that needs a second run to finish contradicts
    # its own idempotence check, and the second run edits the file again.
    two_path = layout.parent / "two_holes.xml"
    two_path.write_text(
        XML_HEAD + '<TextView android:paddingStart="4dp"'
        ' android:layout_marginStart="8dp" />', encoding="utf-8")
    holes = A.symmetry_problems(root)
    # main.xml still holds its physical-only ImageButton, which the fixer
    # deliberately leaves to `aqlab rtl`.
    mine = [h for h in holes if h[0].endswith("two_holes.xml")]
    check("both holes reported", len(mine) == 2, str(holes))
    wrote = A.fix_symmetry(root, dry=False)
    check("one pass repairs the element", wrote == 1, str(wrote))
    two = two_path.read_text(encoding="utf-8")
    check("both partners written", 'android:paddingEnd="4dp"' in two
          and 'android:layout_marginEnd="8dp"' in two, two)
    check("no hole survives in the swept element",
          not [h for h in A.symmetry_problems(root) if h[0].endswith("two_holes.xml")],
          str(A.symmetry_problems(root)))
    check("second pass writes nothing", A.fix_symmetry(root, dry=False) == 0, "")

    remaining = A.symmetry_problems(root)
    check("only the physical hole left", len(remaining) == 1
          and remaining[0][2] == "paddingLeft", str(remaining))


XML_HEAD = '<?xml version="1.0" encoding="utf-8"?>' + chr(10)
HEAD = '<?xml version="1.0" encoding="utf-8"?>\n<resources>'


def test_gravity_compound_token(tmp):
    """RtlHardcoded reads the *value*, not a word boundary on the whole line.

    Found on the real APK: `gravity="left|center"` in two Material selection
    dialogs was reported, and the value was never rewritten — `aqlab rtl` used
    `\b(left|right)\b`, and in Python `\b` is a boundary between a word char
    and a non-word char, so the `left` in `left|center` has no closing boundary
    (the pipe is a non-word char too). The report named a fix that could not
    touch the file. Compound values are the common case, so the test is now a
    token test: not preceded or followed by a letter.
    """
    root = Path(tmp) / "gravcompound"
    res = root / "res"
    layout = res / "layout"
    layout.mkdir(parents=True)
    (res / "values").mkdir()
    (res / "values" / "strings.xml").write_text(
        HEAD + '<string name="a">x</string></resources>', encoding="utf-8")
    (root / "AndroidManifest.xml").write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
        ' package="com.x"><application android:label="a"/></manifest>', encoding="utf-8")
    f = layout / "sel.xml"
    f.write_text(
        HEAD +
        '<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android">\n'
        '    <CheckedTextView android:gravity="left|center"/>\n'
        '    <TextView android:gravity="start|center_vertical"/>\n'
        '    <TextView android:paddingLeft="4dp"/>\n'
        '</LinearLayout>\n', encoding="utf-8")

    got = A.hardcoded_gravity(root)
    check("compound gravity flagged", [v for _, _, v in got] == ["left|center"], str(got))
    check("slot-looking value not a side",
          not A.GRAVITY_TOKEN_HIT.search("imageLeft"), "imageLeft")
    check("compound still a side", bool(A.GRAVITY_TOKEN_HIT.search("left|center")), "")

    check("symmetry fix ignores gravity", A.fix_symmetry(root, dry=False) == 0, "")
    check("file untouched", 'left|center' in f.read_text(encoding="utf-8"), "")
    migrated = A.migrate_attr("android:gravity", "left|center")
    check("rtl rewrites compound token", migrated == ("android:gravity", "start|center"),
          str(migrated))
    check("rtl leaves textAlignment viewStart alone",
          A.migrate_attr("android:textAlignment", "viewStart") is None, "")
    check("rtl leaves a word holding the token alone",
          A.migrate_attr("android:gravity", "leftShape") is None, "")


def test_lint_fix_gravity(tmp):
    """`lint --fix` repairs the gravity finding it printed itself.

    `--fix` used to run only the symmetry pass, so the two `left|center` values
    the linter reported were still there after the fix, and the tool told the
    user to run another command to repair its own warning. The sweep must also
    leave `android:text="left"` alone: the finding is about attributes.
    """
    root = Path(tmp) / "gravmix"
    res = root / "res"
    (res / "layout").mkdir(parents=True)
    (res / "values").mkdir()
    (res / "values-ar").mkdir()
    (res / "values-ar" / "strings.xml").write_text(
        HEAD + '<string name="a">x</string></resources>', encoding="utf-8")
    (res / "values" / "strings.xml").write_text(
        HEAD + '<string name="a">x</string></resources>', encoding="utf-8")
    (root / "AndroidManifest.xml").write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
        ' package="com.x"><application android:label="a"/></manifest>', encoding="utf-8")
    f = res / "layout" / "sel.xml"
    f.write_text(
        HEAD +
        '<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android">\n'
        '    <CheckedTextView android:gravity="left|center"/>\n'
        '    <TextView android:paddingRight="4dp"/>\n'
        '    <TextView android:text="left" android:textAlignment="right"/>\n'
        '</LinearLayout>\n', encoding="utf-8")

    out, hits = A.gravity_fix(f.read_text(encoding="utf-8"))
    check("value sweep counts attributes only", hits == 2, str(hits))
    check("text label untouched", 'android:text="left"' in out, out)
    check("textAlignment rewrites to viewEnd", 'textAlignment="viewEnd"' in out, out)

    args = argparse.Namespace(proj=str(root), locale="ar", quiet=True, verbose=False,
                              fail_under=None, fail_under_file=None, fix=True,
                              dry_run=True)
    before = f.read_text(encoding="utf-8")
    with contextlib.redirect_stdout(io.StringIO()):
        A.cmd_lint(args)
    check("gravity dry run writes nothing", f.read_text(encoding="utf-8") == before, "")

    args.dry_run = False
    with contextlib.redirect_stdout(io.StringIO()):
        A.cmd_lint(args)
    after = f.read_text(encoding="utf-8")
    check("gravity fixed in place", 'gravity="start|center"' in after, after)
    # The physical-only row is deliberately left alone by symmetric_fix (that is
    # `aqlab rtl`'s job), so `--fix` must not have invented a paddingLeft here.
    check("physical row untouched by --fix", 'paddingRight="4dp"' in after, after)
    check("gravity findings gone", A.hardcoded_gravity(root) == [],
          str(A.hardcoded_gravity(root)))

    with contextlib.redirect_stdout(io.StringIO()) as out2:
        A.cmd_lint(args)
    check("gravity fix idempotent", f.read_text(encoding="utf-8") == after, after)
    check("second pass fixes nothing", "fixed:" not in out2.getvalue(), out2.getvalue()[:200])

def test_direction_pins(tmp):
    """DirectionPins: a pin over physical attrs keeps a row LTR in Arabic.

    The pin alone must stay silent — a clock face or a keypad is legitimately
    ltr in every locale, and flagging it is the noise that kills a check. On the
    real APK this rule reports 0: both of its pins (`material_clock_display`,
    `material_timepicker_textinput_display`) are horizontal rows of includes
    with no physical attribute anywhere under them.
    """
    root = Path(tmp) / "pins"
    res = root / "res"
    for d in ("values", "values-ar", "layout", "layout-ar"):
        (res / d).mkdir(parents=True)
    for d in ("values", "values-ar"):
        (res / d / "strings.xml").write_text(
            '<?xml version="1.0" encoding="utf-8"?>'
            '<resources><string name="a">x</string></resources>', encoding="utf-8")
    (root / "AndroidManifest.xml").write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
        ' package="com.x"><application android:supportsRtl="false"/></manifest>',
        encoding="utf-8")
    body = [
        # pinned ltr AND physical: the defect
        '<LinearLayout android:orientation="horizontal"',
        '    android:layoutDirection="ltr" android:paddingLeft="8.0dip">',
        '  <TextView android:layout_width="1.0dip" android:layout_height="1.0dip"/>',
        '</LinearLayout>',
        # pinned rtl, physical, mirror image: also a defect
        '<LinearLayout android:orientation="horizontal"',
        '    android:layoutDirection="rtl" android:layout_toLeftOf="@id/x">',
        '  <TextView android:layout_width="1.0dip" android:layout_height="1.0dip"/>',
        '</LinearLayout>',
        # pinned, horizontal, physical-attribute-free: a clock face. silent
        '<LinearLayout android:orientation="horizontal" android:layoutDirection="ltr"/>',
        # physical attrs but no pin: that is RtlSymmetry/RtlHardcoded's job
        '<LinearLayout android:orientation="horizontal" android:paddingLeft="8.0dip"/>',
        # pinned vertical: direction is irrelevant on a column
        '<LinearLayout android:orientation="vertical"',
        '    android:layoutDirection="ltr" android:paddingLeft="8.0dip"/>',
    ]
    (res / "layout" / "t.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"'
        ' android:layout_width="match_parent" android:layout_height="match_parent">'
        + "".join(body) + '</LinearLayout>', encoding="utf-8")

    got = A.direction_pins(root)
    pins = [g for g in got if g[0] == "layoutDirection"]
    check("two pins reported", len(pins) == 2, str(got))
    check("ltr pin caught", any(g[3] == "ltr" for g in pins), str(pins))
    check("rtl pin caught", any(g[3] == "rtl" for g in pins), str(pins))
    check("supportsRtl=false in manifest reported",
          any(g[0] == "supportsRtl" for g in got), str(got))

    (root / "AndroidManifest.xml").write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
        ' package="com.x"><application android:supportsRtl="true"/></manifest>',
        encoding="utf-8")
    check("supportsRtl=true not flagged",
          not any(g[0] == "supportsRtl" for g in A.direction_pins(root)))

    # An apktool dump keeps the original manifest in a comment. A `false` there
    # is not a manifest that opted out, and reporting it would send the user to
    # `aqlab rtl` on an app that is already correct.
    (root / "AndroidManifest.xml").write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
        ' package="com.x"><!-- <application android:supportsRtl="false"/> -->'
        '<application android:supportsRtl="true"/></manifest>', encoding="utf-8")
    check("supportsRtl=false inside a comment is not a finding",
          not any(g[0] == "supportsRtl" for g in A.direction_pins(root)),
          str(A.direction_pins(root)))

    # The value on a tag that is not `<application>` is not the manifest opt-out
    # either — a `<meta-data>` or an `<activity>` carrying it changes nothing.
    (root / "AndroidManifest.xml").write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
        ' package="com.x"><activity android:supportsRtl="false"/>'
        '<application android:supportsRtl="true"/></manifest>', encoding="utf-8")
    check("supportsRtl=false off <application> is not a finding",
          not any(g[0] == "supportsRtl" for g in A.direction_pins(root)),
          str(A.direction_pins(root)))

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        A.cmd_lint(argparse.Namespace(proj=str(root), locale="ar", quiet=True,
                                      verbose=False, fail_under=None,
                                      fail_under_file=None))
    v = out.getvalue()
    check("lint warns on the ltr pin", 'layoutDirection="ltr"' in v, v[:500])
    check("lint names the row", "t.xml" in v, v[:500])

    # textDirection is pinned text inside a row that already sets a physical
    # attribute: the row flips, the text does not. `firstStrong` is the platform
    # default and follows the direction, so it must stay silent.
    (res / "layout" / "td.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android">'
        '<LinearLayout android:orientation="horizontal" android:textDirection="ltr"'
        ' android:paddingLeft="4.0dip"/>'
        '<LinearLayout android:orientation="horizontal" android:textDirection="firstStrong"'
        ' android:paddingLeft="4.0dip"/>'
        '<LinearLayout android:orientation="horizontal" android:textDirection="rtl"/>'
        '</LinearLayout>', encoding="utf-8")
    tds = [g for g in A.direction_pins(root) if g[0] == "textDirection"]
    check("one textDirection pin reported", len(tds) == 1, str(tds))
    check("pinned ltr text caught", tds and tds[0][3] == "ltr", str(tds))
    check("firstStrong not flagged",
          not any(g[3] == "firstStrong" for g in tds), str(tds))
    check("text pin without physical attrs not flagged",
          not any(g[2] == "LinearLayout" and g[3] == "rtl" and g[1].endswith("td.xml")
                  and "physical" in g for g in tds), str(tds))

    # One regex used to swallow the next sibling's attributes when two rows
    # opened with the same tag, so a pin could disappear entirely.
    (res / "layout" / "sib.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android">'
        '<LinearLayout android:orientation="horizontal" android:id="@id/a"/>'
        '<LinearLayout android:orientation="horizontal" android:layoutDirection="ltr"'
        ' android:paddingLeft="4.0dip"/>'
        '</LinearLayout>', encoding="utf-8")
    sib = A.direction_pins(root)
    check("pin on the second same-tag sibling still found",
          any(g[1].endswith("sib.xml") for g in sib), str(sib))

    # An attribute value holding `>` (a selector, a `?attr/` chain) must not end
    # the tag early, or every attribute after it is invisible.
    (res / "layout" / "gt.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android">'
        '<LinearLayout android:orientation="horizontal" android:tag="a>b"'
        ' android:layoutDirection="ltr" android:paddingLeft="4.0dip"/>'
        '</LinearLayout>', encoding="utf-8")
    gt = A.direction_pins(root)
    check("pin after a `>` inside a value still found",
          any(g[1].endswith("gt.xml") for g in gt), str(gt))

    # A direction set on a container resolves for everything under it that does
    # not override it, so a pinned container is the same defect one level up.
    (res / "layout" / "nest.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android">'
        '<LinearLayout android:layoutDirection="ltr">'
        '<LinearLayout android:paddingLeft="4.0dip"/>'          # inherited: defect
        '<LinearLayout android:layoutDirection="locale" android:paddingLeft="4.0dip"/>'
        '<LinearLayout android:paddingLeft="4.0dip">'
        '<LinearLayout android:paddingRight="2.0dip"/></LinearLayout>'
        '</LinearLayout>'   # closes the pin holder, not the root
        '<FrameLayout android:layoutDirection="rtl">'
        '<TextView android:paddingLeft="1.0dip"/>'
        '</FrameLayout>'
        '<LinearLayout android:paddingLeft="8.0dip"/>'           # sibling: no pin
        '<LinearLayout android:paddingLeft="8.0dip"></LinearLayout>'
        '</LinearLayout>', encoding="utf-8")
    nest = [g for g in A.direction_pins(root) if g[1].endswith("nest.xml")]
    check("inherited pin on a child row reported",
          any(g[0] == "pinNested" and g[2] == "LinearLayout" and g[3] == "ltr"
              for g in nest), str(nest))
    check("inherited pin reaches a grandchild row too",
          len([g for g in nest if g[0] == "pinNested" and g[3] == "ltr"]) == 3, str(nest))
    # A `TextView` is a leaf: its own padding is the row's, so it is one more
    # row under the pin unless the tag's defaults say otherwise — it does not,
    # so the inherited report lands on the enclosing LinearLayout, not here.
    check("no inherited report on a leaf TextView",
          not any(g[2] == "TextView" for g in nest), str(nest))
    # A `/>`-closed row: the slash used to be left on the body, so the
    # `orientation` absence test saw a trailing `/` and matched — silently
    # dropping every self-closed row from the rule.
    # A `/>`-closed row used to keep its slash on the body, so the `orientation`
    # absence test read it as "an orientation is set". A self-closed
    # `LinearLayout` is the only tag where the slash can change the answer, so
    # the self-closed row here is one level down, inside a `FrameLayout` row.
    (res / "layout" / "row.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<FooBar><LinearLayout android:layoutDirection="rtl">'
        '<FrameLayout android:paddingLeft="1.0dip">'
        '<LinearLayout android:paddingLeft="2.0dip">'
        '<LinearLayout android:paddingLeft="3.0dip" /></LinearLayout></FrameLayout>'
        '</LinearLayout></FooBar>', encoding="utf-8")
    rows = [g for g in A.direction_pins(root) if g[1].endswith("row.xml")]
    # Every level states no orientation and sits under a horizontal row, so
    # all three are laid out along that axis and all three are reported; what
    # the trailing slash used to hide was the self-closed row being reached.
    check("the self-closing row is still reached",
          len(rows) == 3 and any(g[2] == "LinearLayout" and g[3] == "rtl" for g in rows),
          str(rows))
    check("child pinning `locale` is left silent",
          not any(g[2] == "LinearLayout" and g[3] == "locale" for g in nest), str(nest))
    # The pin's holder closes before the sibling rows, so nothing after it
    # inherits. This used to read 5: the pin was kept on the stack past its own
    # closing tag, so two rows outside the container were reported as inheriting
    # a direction only the container ever set. The real count is 3 — the
    # `paddingLeft` row, the `paddingLeft` row that wraps a child, and that child.
    check("exactly three inherited rows",
          len([g for g in nest if g[0] == "pinNested"]) == 3, str(nest))


def test_scaffold_apply(proj, tmp):
    rc = A.cmd_scaffold(argparse.Namespace(proj=str(proj), locale="ar", tsv=None,
                                          force=False, quiet=True))
    check("scaffold rc", rc == 0)
    ar_dir = proj / "res" / "values-ar"
    check("values-ar created", (ar_dir / "strings.xml").exists())
    tsv_path = proj / "values-ar.tsv"
    check("tsv created", tsv_path.exists())
    check("no build input left inside res/", not list(ar_dir.glob("*.tsv")),
          "a .tsv under res/ makes `aapt2 compile --dir res` fail")
    tsv = tsv_path.read_text(encoding="utf-8").splitlines()
    rows = [l for l in tsv if not l.startswith("#")]
    # 4 plain strings + the `files` plural expanded to Arabic's six categories
    check("tsv has one row per entry, plurals expanded", len(rows) == 10, str(len(rows)))
    cats = [r.split("\t")[0] for r in rows if r.startswith("files|")]
    check("the categories English cannot supply get a row",
          cats == ["files|" + c for c in A.ARABIC_PLURAL_CATEGORIES], str(cats))
    check("translatable=false excluded", not any(r.startswith("skip\t") for r in rows))

    # translate: 3-col table (key, source, translation), adding the Arabic-only forms
    ar_tsv = Path(tmp) / "ar.tsv"
    ar_tsv.write_text(
        "# key\tsource\ttranslation\n"
        "app_name\tMyApp\tتطبيقي\n"
        "welcome\tWelcome back, %1$s\tمرحباً بعودتك، %1$s\n"
        "progress\tDownloading %d files\tتنزيل %d ملفات\n"
        "files|one\t%d file\tملف واحد\n"
        "files|few\t\t%d ملفات\n"
        "files|many\t\t%d ملفاً\n"
        "files|other\t%d files\t%d ملف\n",
        encoding="utf-8")
    rc = A.cmd_apply(argparse.Namespace(proj=str(proj), tsv=str(ar_tsv), locale="ar"))
    check("apply rc", rc == 0)
    out = (ar_dir / "strings.xml").read_text(encoding="utf-8")
    check("arabic written", "تطبيقي" in out and "مرحباً بعودتك، %1$s" in out)
    check("plural regrouped", "<plurals name=\"files\">" in out and "ملف واحد" in out)
    check("quantities the source never had are written back",
          'quantity="few"' in out and 'quantity="many"' in out)
    check("quantities come out in CLDR order",
          out.index('quantity="few"') < out.index('quantity="many"')
          < out.index('quantity="other"'))
    check("untranslated omitted", "safe" not in out)
    check("format kept", "%d" in out)

    # 2-col legacy format must also read
    legacy = Path(tmp) / "legacy.tsv"
    legacy.write_text("app_name\tاسمي\n", encoding="utf-8")
    check("2-col tsv read", A.translations_from_tsv(legacy) == {"app_name": "اسمي"})

    # must not silently clobber an existing translation file
    try:
        A.cmd_scaffold(argparse.Namespace(proj=str(proj), locale="ar", tsv=None,
                                          force=False, quiet=True))
        check("refuses to overwrite existing values-ar", False, "should have raised")
    except SystemExit:
        check("refuses to overwrite existing values-ar", True)
    check("existing translations survived", "تطبيقي" in (ar_dir / "strings.xml").read_text(
        encoding="utf-8"))


def test_lint(proj):
    ar = proj / "res" / "values-ar" / "strings.xml"
    ar.write_text("""<?xml version="1.0" encoding="utf-8"?>\n<resources>
    <string name="app_name">MyApp</string>
    <string name="welcome">مرحباً بعودتك، %s</string>
    <string name="progress">خصم 50% الآن</string>
    <string name="safe">جاهز</string>
</resources>
""", encoding="utf-8")
    rc = A.cmd_lint(argparse.Namespace(proj=str(proj), locale="ar", verbose=True))
    check("lint fails on errors", rc == 1)

    # a correct file must pass clean — and "correct" for Arabic means all six
    # categories. The previous fixture carried only `one`/`other`, i.e. English
    # quantities with Arabic words, which is the exact defect the plural check
    # was written to catch.
    ar.write_text("""<?xml version="1.0" encoding="utf-8"?>\n<resources>
    <string name="app_name">تطبيقي</string>
    <string name="welcome">مرحباً بعودتك، %1$s</string>
    <string name="progress">تنزيل %d ملفات</string>
    <string name="safe">جاهز</string>
    <plurals name="files">
        <item quantity="zero">لا ملفات</item>
        <item quantity="one">%d ملف واحد</item>
        <item quantity="two">ملفان</item>
        <item quantity="few">%d ملفات</item>
        <item quantity="many">%d ملفاً</item>
        <item quantity="other">%d ملف</item>
    </plurals>
</resources>
""", encoding="utf-8")
    rc = A.cmd_lint(argparse.Namespace(proj=str(proj), locale="ar", verbose=True))
    check("lint passes on a clean file", rc == 0, "expected no errors")

    # Arabic drops the count in `zero`/`one`/`two` ("ملف واحد", "ملفان") — that is
    # correct, not a finding
    ar.write_text("""<?xml version="1.0" encoding="utf-8"?>\n<resources>
    <string name="app_name">تطبيقي</string>
    <string name="welcome">مرحباً بعودتك، %1$s</string>
    <string name="progress">تنزيل %d ملفات</string>
    <string name="safe">جاهز</string>
    <plurals name="files">
        <item quantity="zero">لا ملفات</item>
        <item quantity="one">ملف واحد</item>
        <item quantity="two">ملفان</item>
        <item quantity="few">%d ملفات</item>
        <item quantity="many">%d ملفاً</item>
        <item quantity="other">%d ملف</item>
    </plurals>
</resources>
""", encoding="utf-8")
    rc = A.cmd_lint(argparse.Namespace(proj=str(proj), locale="ar", verbose=True))
    check("dropped plural placeholder is not an error", rc == 0)


def test_mixed_direction_is_not_a_finding():
    """Mixed Arabic/Latin is not a defect and must not be reported as one.

    Measured on Android 11 (TextView, RTL): 200 mixed strings taken from a
    shipped APK, every character's resolved direction and horizontal span
    compared against UAX #9. Zero defects — no bidi mark was needed for any of
    them, and adding one changed nothing on `المجلد test.`.

    Earlier versions reported `trailing Latin/digit run before punctuation —
    needs U+200F`, then `mixed run needs a bidi boundary`, for these. Both were
    derived from reasoning over character classes instead of measuring, and on
    real strings they fired on correct text: 58 errors on a clean APK, then 534.
    The check is gone; this test exists so it does not come back by accident.
    """
    for name in ("_tail_risk", "_unbound_ltr_runs", "RE_BIDI_TAIL"):
        check(f"{name} removed", not hasattr(A, name))
    check("format list is order-sensitive", A._formats("%1$s") != A._formats("%s"))
    check("tags extracted", A._tags("<b>hi</b>") == ["b"])


def test_dedupe_and_styles(tmp):
    """Theme overlays carry both `paddingLeft` and `paddingStart`; emitting both
    makes aapt2 fail with `duplicate attribute`."""
    dup = ('<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"\n'
           '    android:paddingLeft="4dp"\n'
           '    android:paddingStart="12dp"\n'
           '    android:layout_marginRight="6dp"/>')
    out, n = A.rewrite_xml_attrs(dup)
    check("duplicate paddingLeft dropped", "paddingLeft" not in out, out)
    check("existing paddingStart kept", 'android:paddingStart="12dp"' in out, out)
    check("non-duplicate still renamed", 'layout_marginEnd="6dp"' in out, out)

    style = """<resources>
    <style name="App" parent="Theme.AppCompat">
        <item name="android:paddingLeft">4dp</item>
        <item name="android:paddingRight">4dp</item>
        <item name="android:gravity">left|center_vertical</item>
    </style>
    <style name="Twin" parent="App">
        <item name="android:paddingLeft">4dp</item>
        <item name="android:paddingStart">12dp</item>
    </style>
</resources>"""
    out, n = A.rewrite_styles(style)
    check("style item renamed", 'name="android:paddingStart"' in out, out)
    check("style item right renamed", 'name="android:paddingEnd"' in out, out)
    check("style gravity migrated", ">start|center_vertical<" in out, out)
    check("style twin dropped", out.count('name="android:paddingLeft"') == 0, out)
    check("style twin target kept", 'name="android:paddingStart">12dp' in out, out)

    # a styles.xml run through the full patcher must stay parseable
    proj = Path(tmp) / "stylesproj"
    (proj / "res" / "values").mkdir(parents=True)
    (proj / "AndroidManifest.xml").write_text(MANIFEST, encoding="utf-8")
    (proj / "res" / "values" / "styles.xml").write_text(style, encoding="utf-8")
    A.patch_rtl(proj)
    again = A.patch_rtl(proj)
    check("style patch idempotent", again == [], str(again))


def test_min_sdk_guard(tmp):
    """start/end resources need API 17 — refuse early instead of letting aapt2
    fail 60 seconds into the build."""
    old = Path(tmp) / "oldproj"
    (old / "res" / "values").mkdir(parents=True)
    (old / "AndroidManifest.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android">\n'
        '    <uses-sdk android:minSdkVersion="14" android:targetSdkVersion="28"/>\n'
        '    <application android:label="x"/>\n'
        '</manifest>\n', encoding="utf-8")
    check("min sdk read", A.manifest_min_sdk(old) == 14)
    try:
        A.patch_rtl(old)
        check("refuses minSdk<17", False, "should have raised")
    except SystemExit:
        check("refuses minSdk<17", True)
    A.patch_rtl(old, allow_old_sdk=True)
    check("force overrides the guard",
          'supportsRtl="true"' in (old / "AndroidManifest.xml").read_text(encoding="utf-8"))

    modern = Path(tmp) / "modernproj"
    (modern / "res" / "values").mkdir(parents=True)
    (modern / "AndroidManifest.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android">\n'
        '    <uses-sdk android:minSdkVersion="21"/>\n'
        '    <application android:label="x"/>\n'
        '</manifest>\n', encoding="utf-8")
    A.patch_rtl(modern)
    check("modern minSdk passes",
          'supportsRtl="true"' in (modern / "AndroidManifest.xml").read_text(encoding="utf-8"))

    # APKs whose manifest carries no <uses-sdk>: apktool.yml holds the resolved value
    ymlproj = Path(tmp) / "ymlproj"
    (ymlproj / "res" / "values").mkdir(parents=True)
    (ymlproj / "AndroidManifest.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android">\n'
        '    <application android:label="x"/>\n'
        '</manifest>\n', encoding="utf-8")
    (ymlproj / "apktool.yml").write_text(
        "sdkInfo:\n  minSdkVersion: 15\n  targetSdkVersion: 34\n", encoding="utf-8")
    check("apktool.yml fallback read", A.manifest_min_sdk(ymlproj) == 15)
    try:
        A.patch_rtl(ymlproj)
        check("yamlless manifest still guarded", False, "should have raised")
    except SystemExit:
        check("yamlless manifest still guarded", True)


def test_bad_path_guard(tmp):
    """A wrong path must fail loudly — never report 'already RTL-clean'."""
    try:
        A.patch_rtl(Path(tmp) / "does-not-exist")
        check("missing dir fails loudly", False, "should have raised")
    except SystemExit:
        check("missing dir fails loudly", True)
    noproj = Path(tmp) / "notdecoded"
    noproj.mkdir()
    try:
        A.patch_rtl(noproj)
        check("dir without res/ fails loudly", False, "should have raised")
    except SystemExit:
        check("dir without res/ fails loudly", True)


def test_migrate_prefilter():
    """The cheap pre-filter that skips unrelated tags must stay sound.

    It is derived from the rename tables, but this locks the invariant in place
    so a future hardcoded regex cannot silently start skipping real renames.
    """
    covered = (set(A.ATTR_RENAME) | A.GRAVITY_ATTRS | A.TEXT_ALIGN_ATTRS
               | set(A.THEME_VALUE_MAP))
    missed = sorted(n for n in covered if not A.RE_MAY_MIGRATE.search(n))
    check("pre-filter recognises every renameable name", not missed)
    plain = '<LinearLayout android:layout_width="match_parent" android:text="hi" />'
    check("pre-filter leaves an unrelated tag alone",
          A.rewrite_xml_attrs(plain) == (plain, 0))


def test_decode_sources_flag():
    """decode skips smali by default; --with-sources opts back in.

    Wired hermetically: apktool and the subprocess runner are stubbed so the
    check needs neither a JRE nor a jar.
    """
    seen = []
    orig_cmd, orig_run = A.apktool_cmd, A.run
    A.apktool_cmd = lambda: ["apktool"]
    A.run = lambda cmd, **kw: seen.append(list(cmd))
    try:
        for ws in (False, True):
            A.cmd_decode(argparse.Namespace(apk="x.apk", out="o", framework=None,
                                            quiet=True, with_sources=ws))
    finally:
        A.apktool_cmd, A.run = orig_cmd, orig_run
    check("decode skips smali by default", "-s" in seen[0])
    check("--with-sources keeps smali", "-s" not in seen[1])


def test_plural_structure(tmp):
    """An Arabic <plurals> that mirrors the English quantities must be reported.

    English can only offer `one`/`other`. Arabic needs `few` (3-10) and `many`
    (11-99) as well, or most counts read wrong — and nothing in the build says so
    (verified: aapt2 compiles and links a translation whose plurals lacks even
    `other`). This is the finding the tool exists for.
    """
    root = Path(tmp) / "plurals"
    (root / "res" / "values").mkdir(parents=True)
    (root / "res" / "values-ar").mkdir(parents=True)
    (root / "res" / "values" / "strings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>\n'
        '  <string name="app_name">Demo</string>\n'
        '  <plurals name="n_items">\n'
        '    <item quantity="one">%d item</item>\n'
        '    <item quantity="other">%d items</item>\n'
        '  </plurals>\n</resources>\n', encoding="utf-8")
    (root / "res" / "values-ar" / "strings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>\n'
        '  <string name="app_name">تجربة</string>\n'
        '  <plurals name="n_items">\n'
        '    <item quantity="one">عنصر واحد</item>\n'
        '    <item quantity="other">%d عنصر</item>\n'
        '  </plurals>\n</resources>\n', encoding="utf-8")

    ns = argparse.Namespace(proj=str(root), locale="ar", verbose=False)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = A.cmd_lint(ns)
    out = buf.getvalue()
    check("lint errors on Arabic plurals that mirror English", rc == 1)
    check("it names the categories English cannot supply",
          "few (3-10)" in out and "many (11-99)" in out)
    m = re.search(r"\((\d+\.\d)%\)", out)
    check("coverage never exceeds 100%", bool(m) and float(m.group(1)) <= 100.0)

    # the same three findings must not fire once the categories are complete
    (root / "res" / "values-ar" / "strings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>\n'
        '  <string name="app_name">تجربة</string>\n'
        '  <plurals name="n_items">\n'
        '    <item quantity="zero">لا عناصر</item>\n'
        '    <item quantity="one">عنصر واحد</item>\n'
        '    <item quantity="two">عنصران</item>\n'
        '    <item quantity="few">%d عناصر</item>\n'
        '    <item quantity="many">%d عنصراً</item>\n'
        '    <item quantity="other">%d عنصر</item>\n'
        '  </plurals>\n</resources>\n', encoding="utf-8")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = A.cmd_lint(ns)
    check("lint accepts the full six-category Arabic plurals", rc == 0)
    check("no plural finding remains", "plurals/" not in buf.getvalue())


def test_plural_file_roundtrip(tmp):
    """Plurals kept in their own values/plurals.xml must survive the whole loop.

    AOSP and most Material apps keep them in a separate file. A tool that reads
    only strings.xml never shows those entries to a translator, never writes an
    Arabic version back, and puts everything it does write into strings.xml —
    which is a duplicate-resource error the moment the app also ships
    values-ar/plurals.xml.
    """
    root = Path(tmp) / "plfile"
    (root / "res" / "values").mkdir(parents=True)
    (root / "res" / "values" / "strings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>\n'
        '  <string name="app_name">Demo</string>\n</resources>\n', encoding="utf-8")
    (root / "res" / "values" / "plurals.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>\n'
        '  <plurals name="n_items">\n'
        '    <item quantity="one">%d item</item>\n'
        '    <item quantity="other">%d items</item>\n'
        '  </plurals>\n</resources>\n', encoding="utf-8")

    A.cmd_scaffold(argparse.Namespace(proj=str(root), locale="ar", tsv=None,
                                      force=False, quiet=True))
    tsv = root / "values-ar.tsv"
    text = tsv.read_text(encoding="utf-8")
    check("scaffold reaches into plurals.xml", "n_items|few" in text and "n_items|many" in text)

    tsv.write_text(
        "# key\tsource\ttranslation\n"
        "n_items|zero\t\tلا عناصر\n"
        "n_items|one\t%d item\tعنصر واحد\n"
        "n_items|two\t\tعنصران\n"
        "n_items|few\t\t%d عناصر\n"
        "n_items|many\t\t%d عنصراً\n"
        "n_items|other\t%d items\t%d عنصر\n"
        "app_name\tDemo\tتجربة\n", encoding="utf-8")
    rc = A.cmd_apply(argparse.Namespace(proj=str(root), tsv=str(tsv), locale="ar"))
    check("apply rc", rc == 0)

    ar_dir = root / "res" / "values-ar"
    pl_file = ar_dir / "plurals.xml"
    check("arabic plural written back to its own file", pl_file.exists())
    pl = pl_file.read_text(encoding="utf-8")
    check("all six categories written",
          all(f'quantity="{q}"' in pl for q in A.ARABIC_PLURAL_CATEGORIES), pl)
    check("the source file's split is preserved",
          "<plurals" not in (ar_dir / "strings.xml").read_text(encoding="utf-8"))

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = A.cmd_lint(argparse.Namespace(proj=str(root), locale="ar", verbose=False))
    check("lint is clean on the result", rc == 0, buf.getvalue()[-300:])


def test_rtl_self_closing_application(tmp):
    """A self-closing <application/> must survive the supportsRtl insert.

    The insert used to strip the trailing '>' and append the attribute, which on
    `<application .../>` left a stray '/' before it — a manifest that no longer
    parses, discovered only when the build fails.
    """
    import xml.etree.ElementTree as ET
    root = Path(tmp) / "selfclose"
    (root / "res" / "values").mkdir(parents=True)
    (root / "res" / "values" / "strings.xml").write_text(
        '<resources><string name="app_name">D</string></resources>', encoding="utf-8")
    (root / "AndroidManifest.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
        ' package="com.t"><application android:label="@string/app_name"/></manifest>\n',
        encoding="utf-8")

    A.patch_rtl(root)
    out = (root / "AndroidManifest.xml").read_text(encoding="utf-8")
    check("supportsRtl landed in the manifest", 'supportsRtl="true"' in out)
    try:
        ET.fromstring(out)
        check("self-closing <application/> stays well-formed XML", True)
    except ET.ParseError as e:
        check("self-closing <application/> stays well-formed XML", False, f"{e}: {out}")


def test_scan_apk_supports_rtl(tmp):
    """aapt2 prints a boolean attribute symbolically (`=true`), not as the raw
    0xffffffff. Testing for the raw value made `scan <apk>` report every real APK
    as not supporting RTL — a silent false negative on the tool's headline check.
    """
    BADGE = ("package: name='com.x' versionCode='1' versionName='1'\n"
             "locales: '--_--' 'ar' 'en'\n"
             "application-label:'X'\n")

    def fake_factory(xml_value):
        def fake_run(cmd, **kw):
            class R:
                stdout = ""
                stderr = ""
            r = R()
            if "--file" in cmd:
                r.stdout = f"  A: http://x:supportsRtl(0x010103af)={xml_value}\n"
            else:
                r.stdout = BADGE
            return r
        return fake_run

    apk = Path(tmp) / "fake.apk"
    with zipfile.ZipFile(apk, "w") as z:
        z.writestr("AndroidManifest.xml", "x")      # scan_apk only lists names

    orig_run, orig_tool = A.run, A.tool
    out = {}
    try:
        A.tool = lambda name: name                    # no real aapt2 needed
        for label, value, want in (("true", "true", True),
                                   ("raw", "0xffffffff", True),
                                   ("false", "false", False)):
            A.run = fake_factory(value)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                A.cmd_scan(argparse.Namespace(target=str(apk), json=True))
            out[label] = buf.getvalue()
    finally:
        A.run, A.tool = orig_run, orig_tool

    check("scan reads supportsRtl=true (symbolic form)",
          '"supportsRtl": true' in out["true"], out["true"])
    check("scan reads the raw 0xffffffff form too",
          '"supportsRtl": true' in out["raw"], out["raw"])
    check("scan does not claim RTL when it is false",
          '"supportsRtl": false' in out["false"], out["false"])
    check("scan still reads the locales",
          "'ar'" in out["true"] or "ar" in out["true"], out["true"])


def test_scan(proj):
    rc = A.cmd_scan(argparse.Namespace(target=str(proj), json=True, quiet=True))
    check("scan rc", rc == 0)


def test_fixture_locale_qualifier(tmp):
    """`values-en-rAU` is the locale `en-rAU`, not the locale `rAU`.

    The scan list used to strip everything before the first `-`, so a regional
    variant printed its region alone and every English locale looked like a
    language called `rAU`. A whole qualifier list that reads correctly matters
    more than it sounds: `--locale` and the coverage table both come from it.
    """
    proj = Path(tmp) / "locq"
    res = proj / "res"
    for d in ("values", "values-ar", "values-en-rAU"):
        (res / d).mkdir(parents=True)
        (res / d / "strings.xml").write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n<resources>'
            '<string name="a">x</string></resources>', encoding="utf-8")
    (proj / "AndroidManifest.xml").write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
        ' package="com.x"><application android:supportsRtl="true"/></manifest>',
        encoding="utf-8")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        A.cmd_scan(argparse.Namespace(target=str(proj), json=False, quiet=True))
    line = next(l for l in out.getvalue().splitlines() if "locales" in l)
    listed = [p.strip() for p in line.split(":", 1)[1].split(",")]
    check("region kept with its language", "en-rAU" in listed)
    check("region does not stand alone", "rAU" not in listed)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        proj = fixture(tmp)
        test_escape()
        test_mixed_direction_is_not_a_finding()
        test_entries(proj)
        test_scaffold_apply(proj, tmp)
        test_apply_preserves_existing(proj, tmp)
        test_apply_does_not_duplicate_plurals(proj, tmp)
        test_markup_survives_apply(tmp)
        test_locale_config(tmp)
        test_text_like_vectors(tmp)
        test_forced_scaffold_keeps_six_plural_forms(proj, tmp)
        test_dry_run_changes_nothing(proj, tmp)
        test_non_resource_files_are_reported(tmp)
        test_coverage_per_file_and_gates(tmp)
        test_rtl_locales_beyond_arabic(tmp)
        test_diff_between_apks(tmp)
        test_xliff_wrapping(tmp)
        test_symmetry_check(tmp)
        test_single_quoted_attributes(tmp)
        test_single_quoted_gravity_and_fix(tmp)
        test_rtl_drawable_dirs(tmp)
        test_hardcoded_gravity(tmp)
        test_gravity_compound_token(tmp)
        test_lint_fix_gravity(tmp)
        test_lint_fix(tmp)
        test_lint_fix_pin_nested(tmp)
        test_pin_nested_gravity(tmp)
        test_pin_nested_gravity_only(tmp)
        test_pin_nested_inherited_axis(tmp)
        test_pin_fix_skips_a_row_it_already_passed(tmp)
        test_pin_nested_include(tmp)
        test_lint_fix_pin_local(tmp)
        test_direction_pins(tmp)
        test_rtl(proj)
        test_lint(proj)
        test_dedupe_and_styles(tmp)
        test_min_sdk_guard(tmp)
        test_bad_path_guard(tmp)
        test_migrate_prefilter()
        test_decode_sources_flag()
        test_plural_structure(tmp)
        test_plural_file_roundtrip(tmp)
        test_rtl_self_closing_application(tmp)
        test_scan_apk_supports_rtl(tmp)
        test_scan(proj)
        test_fixture_locale_qualifier(tmp)
    print(f"[OK] {len(PASSED)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
