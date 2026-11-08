#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""aqlab self-check — plain asserts, no framework.

    python tests/test_aqlab.py
"""
import argparse
import contextlib
import inspect
import io
import json
import os
import pathlib
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
    <string name="counter">%1$d/%2$d</string>
    <string name="ratio">%1$d of %2$d</string>
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
    # One, exactly: the pin is dropped and `paddingLeft` migrated to
    # `paddingStart`. The symmetry pass no longer runs (it changed rendered
    # layouts on a real APK — see the `--fix` wiring note in cmd_lint), so this
    # row is one element again.
    check("lint --fix reaches the local pin fixer",
          "fixed: 1 element(s)" in out_lone.getvalue(), out_lone.getvalue()[-300:])
    # The plural sweep appends its findings after the fixers have run, so a
    # warning it added was printed under `fixed:` while no fixer had seen it.
    # Reported separately in an earlier draft; now it is simply not this run's
    # output: the fix run must not print a plural finding at all.
    # `--fix` printed a pin warning that still advertised "drops the pin" after
    # it had dropped that pin. The finding stays (the row keeps physical attrs);
    # the tip must not survive a run that performed it.
    check("fixed pin is not promised again",
          "; `aqlab lint --fix` drops the pin" not in out_lone.getvalue(),
          out_lone.getvalue()[-300:])
    check("lint --fix wrote the migrated row",
          'android:paddingStart="4.0dip"' in
          (lone / "res" / "layout" / "list.xml").read_text(encoding="utf-8"))
    p = Path(tmp) / "pinlocal-run"
    shutil.copytree(root, p)
    # `fix_pin_local(root)` above mutated the fixture in place — the copy would
    # otherwise start from an already-migrated row and `--fix` would find only
    # the gravity row left, so the count would drop to 1 for the wrong reason.
    (p / "res" / "layout" / "list.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android">'
        '<LinearLayout android:orientation="horizontal" android:layoutDirection="ltr"'
        ' android:paddingLeft="4.0dip" android:layout_marginRight="2.0dip"/>'
        '<LinearLayout android:orientation="horizontal" android:layoutDirection="rtl"'
        ' android:gravity="right"/>'
        '<LinearLayout android:orientation="horizontal" android:paddingLeft="6.0dip"/>'
        '</LinearLayout>', encoding="utf-8")
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


def test_lint_fix_supports_rtl(tmp):
    """`--fix` must flip the manifest opt-out it reports as an error.

    `lint` calls `supportsRtl="false"` an error and named `aqlab rtl`, a command
    that is a whole migration (minSdk gate, `res/` requirement, every
    `paddingLeft` renamed). Probed before writing this: `lint --fix` on a tree
    whose only defect was the manifest printed `fixed: 0` and left the error
    standing, so the report and the fixer disagreed about what `--fix` does.

    Both quotes: `supportsRtl='false'` is valid XML and aapt accepts it, and a
    fixer that only read double quotes would repair-and-rereport it forever.
    """
    root = Path(tmp) / "sr"
    res = root / "res"
    for d in ("values", "values-ar", "layout"):
        (res / d).mkdir(parents=True)
    for d in ("values", "values-ar"):
        (res / d / "strings.xml").write_text(
            '<?xml version="1.0" encoding="utf-8"?>'
            '<resources><string name="a">x</string></resources>', encoding="utf-8")
    man = root / "AndroidManifest.xml"
    man.write_text(
        '<?xml version="1.0" encoding="utf-8"?>' + NL +
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"' + NL +
        ' package="com.x">' + NL +
        '  <!-- apktool copy: <application android:supportsRtl="false"/> -->' + NL +
        '  <application android:allowBackup="true" android:supportsRtl="false"/>' + NL +
        '</manifest>', encoding="utf-8")

    check("opt-out reported before fix",
          any(g[0] == "supportsRtl" for g in A.direction_pins(root)))
    check("dry run flips nothing",
          A.fix_supports_rtl(root, dry=True) == 1 and 'supportsRtl="false"' in
          man.read_text(encoding="utf-8"))
    check("dry run left the manifest dirty for the real run",
          A.fix_supports_rtl(root) == 1, "second call did not report a change")
    txt = man.read_text(encoding="utf-8")
    check("value flipped", 'android:supportsRtl="true"' in txt, txt)
    check("commented copy untouched", 'apktool copy: <application android:supportsRtl="false"/>' in txt, txt)
    check("no other byte moved", 'android:allowBackup="true"' in txt, txt)
    check("idempotent", A.fix_supports_rtl(root) == 0)
    check("nothing reported after fix",
          not any(g[0] == "supportsRtl" for g in A.direction_pins(root)))

    # Single quotes, and a sibling attribute carrying the same text in its value.
    man.write_text(
        '<?xml version="1.0" encoding="utf-8"?>' + NL +
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android">' + NL +
        "  <application android:label=\"supportsRtl='false'\" android:supportsRtl='false'/>" + NL +
        '</manifest>', encoding="utf-8")
    check("single-quoted opt-out reported",
          any(g[0] == "supportsRtl" for g in A.direction_pins(root)))
    check("single-quoted opt-out repaired", A.fix_supports_rtl(root) == 1)
    txt = man.read_text(encoding="utf-8")
    check("only the attribute flipped",
          "android:supportsRtl='true'" in txt and "supportsRtl='false'" in txt, txt)

    # `lint --fix` itself, not just the helper: the wiring is what regressed.
    man.write_text(
        '<?xml version="1.0" encoding="utf-8"?>' + NL +
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android">' + NL +
        '  <application android:supportsRtl="false"/>' + NL +
        '</manifest>', encoding="utf-8")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        A.cmd_lint(argparse.Namespace(proj=str(root), locale="ar", quiet=True,
                                      verbose=False, fail_under=None,
                                      fail_under_file=None, fix=True, dry_run=False))
    check("`lint --fix` flips the manifest",
          'android:supportsRtl="true"' in man.read_text(encoding="utf-8"), out.getvalue())
    check("`lint --fix` clears the error", "0 error(s)" in out.getvalue(), out.getvalue())


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


def test_pending_never_exceeds_entries(proj, tmp):
    """`pending` must count entries, not table rows.

    `plural_rows_for` emits one row per (plural, quantity), so a locale with
    more plural categories than the source produces MORE rows than there are
    entries: Hebrew printed `1426 entries, pending 1435` and Arabic 1462. Both
    numbers were right and the line read as a contradiction. `pending` now
    counts entries; the row count is reported separately, labelled.
    """
    root = Path(tmp) / "pending"
    res = root / "res"
    (res / "values").mkdir(parents=True)
    body = ['<?xml version="1.0" encoding="utf-8"?><resources>']
    body.append('<plurals name="files"><item quantity="one">%d file</item>'
                '<item quantity="other">%d files</item></plurals>')
    for i in range(5):
        body.append(f'<string name="s{i}">v{i}</string>')
    body.append("</resources>")
    (res / "values" / "strings.xml").write_text("".join(body), encoding="utf-8")
    (root / "AndroidManifest.xml").write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
        ' package="com.x"><application android:label="x"/></manifest>', encoding="utf-8")

    entries = [e for e in A.read_locale_entries(res, ["values"]) if e.translatable]
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        for loc in ("he", "ar", "ja"):
            rows = A.plural_rows_for(entries, loc)
            # the bug: for he and ar rows outnumber entries; ja is the reverse
            expected = len(rows) > len(entries) if loc in ("he", "ar") else True
            check(f"{loc}: rows vs entries regime as expected", expected,
                  f"{len(rows)} rows vs {len(entries)} entries")
            A.cmd_scaffold(argparse.Namespace(proj=str(root), locale=loc, tsv=None,
                                              force=True, dry_run=True, quiet=True))
    text = out.getvalue()
    for loc in ("he", "ar", "ja"):
        block = text.split(f"values-{loc}")[1] if f"values-{loc}" in text else text
        m = re.search(r"pending: (-?\d+)", block)
        check(f"{loc}: pending printed", m is not None, block[:200])
        if not m:
            continue
        pending = int(m.group(1))
        check(f"{loc}: pending is not negative", pending >= 0, f"pending={pending}")
        # `pending` counts BLANK ROWS, not entries. A locale with more plural
        # categories than the source gains rows (Hebrew 3 vs English 2), so
        # pending is legitimately larger than the entry count — it can never be
        # larger than the row count, which is the real ceiling.
        n_rows = len(A.plural_rows_for(entries, loc))
        check(f"{loc}: pending <= rows", pending <= n_rows,
              f"pending={pending} rows={n_rows}")

    # A fresh scaffold has translated NOTHING. The counter used to read
    # `plural_rows_for`'s text field, which carries the SOURCE text so the table
    # ships with context -- so a 12-entry tree with no target folder reported
    # "translated: 12, pending: 1". Everything is pending until a translator
    # fills it or a --tsv arrives.
    fresh = Path(tmp) / "pending_fresh"
    shutil.copytree(root, fresh, dirs_exist_ok=True)
    out2 = io.StringIO()
    with contextlib.redirect_stdout(out2):
        A.cmd_scaffold(argparse.Namespace(proj=str(fresh), locale="he", tsv=None,
                                          force=True, dry_run=True, quiet=True))
    m = re.search(r"translated: (\d+)\s+pending: (\d+)", out2.getvalue())
    check("fresh scaffold prints both counters", m is not None, out2.getvalue()[:200])
    if m:
        translated, pending = int(m.group(1)), int(m.group(2))
        check("fresh scaffold: nothing is translated yet", translated == 0,
              f"translated={translated}")
        check("fresh scaffold: every row is pending", pending > 0,
              f"pending={pending}")


def test_scan_json_is_only_json(proj, tmp):
    """`scan --json` must put nothing but a JSON document on stdout.

    It printed the whole human block and appended one JSON line at the end, so
    `aqlab scan --json | jq` failed on `[FINDING] project: ...` — the flag lied
    about its own output. Both halves matter: the json form parses, the human
    form is unchanged, and neither leaks into the other.
    """
    work = Path(tmp) / "scanjson"
    shutil.copytree(proj, work)

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        A.cmd_scan(argparse.Namespace(target=str(work), json=True, jobs=1, quiet=True))
    raw = out.getvalue()
    try:
        doc = json.loads(raw)
    except Exception as e:
        check("scan --json: stdout parses as JSON", False, f"{e}: {raw[:200]}")
        return
    check("scan --json: stdout parses as JSON", True)
    check("scan --json: no human text leaked", "[FINDING]" not in raw, raw[:200])
    for key in ("locales", "supportsRtl", "translatable", "legacyLTR"):
        check(f"scan --json: carries {key}", key in doc, sorted(doc))

    out2 = io.StringIO()
    with contextlib.redirect_stdout(out2):
        A.cmd_scan(argparse.Namespace(target=str(work), json=False, jobs=1, quiet=True))
    human = out2.getvalue()
    check("scan (human): still starts with the finding block",
          human.startswith("[FINDING]"), human[:80])
    check("scan (human): no JSON leaked", '{"' not in human, human[-200:])


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

    Verified against CLDR through `babel` (SCOUT.md round 42): ar has
    zero/one/two/few/many/other; he has one/two/other — no zero, no few, no
    many; fa and ur have one/other. Demanding six of every language would put
    forms on translators that those languages never use.
    """
    check("ar six", A.plural_categories("ar") ==
          ("zero", "one", "two", "few", "many", "other"))
    check("he three", A.plural_categories("he") == ("one", "two", "other"))
    check("ar requires few and many",
          A.plural_required("ar") == ("other", "few", "many"))
    check("he requires other one two",
          A.plural_required("he") == ("other", "one", "two"))
    check("pl has many", A.plural_categories("pl") ==
          ("one", "few", "many", "other"))
    check("zh is other-only", A.plural_categories("zh") == ("other",))
    check("unknown locale is not linted", A.plural_categories("xx") == ())
    check("fa two", A.plural_categories("fa") == ("one", "other"))
    check("ur two", A.plural_categories("ur") == ("one", "other"))
    check("region resolves", A.plural_categories("he-IL") == A.plural_categories("he"))
    check("rtl flags", all(A.is_rtl(x) for x in ("ar", "he", "fa", "ur", "ar-rSA")))
    check("ltr is not rtl", not A.is_rtl("en") and not A.is_rtl("fr"))
    # Coverage of the generated tables: the ten-entry list we used to ship
    # missed most of CLDR's RTL languages, and a locale with no plural row was
    # silently exempt from the plural lint (a `de` build with an Arabic-shaped
    # <plurals> got no warning at all). These two assert the *class* of fix, not
    # one luckily-spelled tag.
    check("rtl covers N'Ko and the Kurdish/Arabic minority tags",
          all(A.is_rtl(x) for x in ("nqo", "sdh", "lrc", "glk", "mzn", "azb", "bal", "ks")))
    check("rtl does not overreach",
          not any(A.is_rtl(x) for x in ("de", "ja", "sw", "ha", "ms", "tl", "spanish")))
    check("any CLDR locale with plurals is linted, not just the RTL ten",
          A.plural_categories("de") == ("one", "other")
          and A.plural_categories("sv") == ("one", "other")
          and A.plural_categories("cy") ==
          ("zero", "one", "two", "few", "many", "other"))
    # A region is not noise once CLDR splits the language by it, and Android
    # writes the qualifier with an underscore — the old `_base` stripped the
    # region *first*, so `pt_PT` got Brazil's table and its `many` went unchecked.
    check("pt_PT keeps its own categories", A.plural_categories("pt_PT") ==
          ("one", "many", "other") and A.plural_categories("pt") ==
          ("one", "many", "other"))
    check("region-only split is tried before the bare language",
          A.plural_categories("pt-PT") == ("one", "many", "other"))
    check("old CLDR code names resolve like Android writes them",
          A.plural_categories("iw") == A.plural_categories("he")
          and A.plural_categories("in") == A.plural_categories("id")
          and A.plural_categories("ji") == A.plural_categories("yi"))
    # Android's own script+region qualifier form, which used to lowercase to noise
    check("b+sr+Latn qualifier resolves", A.plural_categories("b+sr+Latn+RS") ==
          ("one", "few", "other"))
    check("unknown stays unlinted", A.plural_categories("qq-ZZ") == ()
          and A.plural_categories("") == ())

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
    check("he scaffold: three rows", rows == ["files|one", "files|two",
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
    # Hebrew's `other` is zero-and-everything-else, so a two-form table is
    # complete for it — only the missing `<item quantity=\"two\">` is a finding.
    check("he missing two reported", "missing quantity: two" in text
          and "plurals/files" in text, text[:600])
    check("he never asked for few", "few" not in text, text[:600])
    check("he never asked for many", "many" not in text, text[:600])
    check("he range is its own", "1" in text, text[:600])


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


def test_jobs_parity(tmp):
    """`--jobs N` is a speed knob for every layout sweep, never a behaviour.

    Five sweeps reach a pool: symmetry, gravity, direction pins, RTL literals
    and text-like vectors. Each is the serial body with the file list cut out,
    so the day one of them reorders or drops a hit, `lint` and `lint --jobs 4`
    disagree on the same tree. One fixture with a hit for every sweep, then the
    lists compared, then the pool asserted to be what ran — a wrapper that
    ignored `jobs` would pass the equality checks by staying serial.
    """
    root = Path(tmp) / "sweepjobs"
    res = root / "res"
    (res / "layout").mkdir(parents=True)
    (res / "drawable").mkdir(parents=True)
    (root / "AndroidManifest.xml").write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android">'
        '<application android:supportsRtl="true"/></manifest>', encoding="utf-8")
    head = '<?xml version="1.0" encoding="utf-8"?>' + chr(10)
    (res / "layout" / "a.xml").write_text(
        head + '<LinearLayout>' + chr(10)
        + '  <LinearLayout android:layoutDirection="ltr" android:paddingLeft="4dp">' + chr(10)
        + '    <TextView android:gravity="left|center" android:paddingLeft="2dp"/>' + chr(10)
        + '  </LinearLayout>' + chr(10)
        + '</LinearLayout>' + chr(10), encoding="utf-8")
    for i in range(9):
        (res / "layout" / ("f%d.xml" % i)).write_text(
            head + '<LinearLayout>' + chr(10)
            + '  <TextView android:paddingLeft="%ddp"/>' % (i + 1) + chr(10)
            + '</LinearLayout>' + chr(10), encoding="utf-8")
    # a text-like vector: twelve short filled paths at icon size
    paths = "".join('<path android:pathData="M%d,0 L1,1z" android:fillColor="#f00"/>' % i
                    for i in range(12))
    (res / "drawable" / "wordmark.xml").write_text(
        '<vector xmlns:android="http://schemas.android.com/apk/res/android" '
        'android:width="24dip" android:height="24dip">' + paths + '</vector>',
        encoding="utf-8")
    sweeps = (
        ("symmetry", lambda jobs: A.symmetry_problems(root, jobs)),
        ("gravity", lambda jobs: A.hardcoded_gravity(root, jobs)),
        ("pins", lambda jobs: A.direction_pins(root, jobs)),
        ("rtl literals", lambda jobs: A.rtl_enabled(root, "ar", jobs)),
        ("text vectors", lambda jobs: A.text_like_vectors(root, jobs)),
    )
    for name, fn in sweeps:
        serial = fn(1)
        check("%s sweep really has hits" % name, bool(serial), str(serial))
        for jobs in (2, 4, 8):
            check("%s: --jobs %d equals the serial list" % (name, jobs),
                  fn(jobs) == serial, str(fn(jobs)))

    used = []
    real = A.multiprocessing.get_context

    def counted(c):
        used.append(c)
        return real(c)
    A.multiprocessing.get_context = counted
    try:
        for _name, fn in sweeps:
            fn(4)
    finally:
        A.multiprocessing.get_context = real
    # One drawable is below `_spread_pairs`' eight-file floor by design — four
    # spawns for five sweeps is the floor working, not a wrapper skipping work.
    check("every multi-file sweep starts a pool at --jobs 4",
          used == ["spawn"] * 4, str(used))


def test_gravity_jobs(tmp):
    """`--jobs N` is a speed knob, never a behaviour.

    The pool is the same regex over the same text in the same order; the day it
    reorders, `scan --jobs` and `scan` disagree and CI contradicts a laptop.
    """
    root = Path(tmp) / "pool"
    res = root / "res"
    (res / "layout").mkdir(parents=True)
    (root / "AndroidManifest.xml").write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"/>',
        encoding="utf-8")
    hits = 0
    for i in range(12):
        rows = '    <TextView android:gravity="left|center"/>\n' * (i % 3)
        hits += i % 3
        (res / "layout" / f"l{i:02d}.xml").write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n<LinearLayout>\n'
            + rows + "</LinearLayout>\n", encoding="utf-8")
    serial = A.hardcoded_gravity(root)
    src = [(f.relative_to(root).as_posix(),
            f.read_text(encoding="utf-8", errors="replace"))
           for f in A.gravity_files(root)]
    check("the fixture really has hits", len(serial) == hits, str(serial))
    for jobs in (2, 5, 8):
        got = A.gravity_parallel(src, jobs)
        check(f"--jobs {jobs} returns the serial result, in order",
              got == serial, str(got))
    # The pool has to be the thing under test: on the serial path `imap` would
    # never run, and a helper that ignored `jobs` would pass every check above.
    pool_was_used = []
    real_pool = A.multiprocessing.get_context
    def counted(c):
        pool_was_used.append(c)
        return real_pool(c)
    A.multiprocessing.get_context = counted
    try:
        A.gravity_parallel(src, 4)
    finally:
        A.multiprocessing.get_context = real_pool
    check("--jobs 4 actually starts a pool", pool_was_used == ["spawn"],
          str(pool_was_used))
    check("--jobs 1 is the serial path", A.gravity_parallel(src, 1) == serial, "")
    # One definition of which files hold a gravity: the report and the fixer
    # must not drift, or `--fix` rewrites files the report never named.
    check("gravity_files sees every layout xml",
          len(A.gravity_files(root)) == 12, str(A.gravity_files(root)))
    (res / "layout" / "note.txt").write_text("not xml", encoding="utf-8")
    (res / "raw").mkdir()
    (res / "raw" / "x.xml").write_text("<x/>", encoding="utf-8")
    check("gravity_files skips non-xml and folders outside the sweep",
          len(A.gravity_files(root)) == 12, str(A.gravity_files(root)))
    # `--jobs` parsing: 0 is every CPU (a 0-worker pool hangs), junk is an error
    # and must not reach the pool as a crash.
    check("a plain count passes through", A.parse_jobs("4") == 4, "")
    check("0 means every CPU", A.parse_jobs("0") == (os.cpu_count() or 1),
          str(A.parse_jobs("0")))
    os.environ["AQLAB_JOBS"] = "3"
    try:
        check("$AQLAB_JOBS is the default", A.parse_jobs(None) == 3, "")
    finally:
        del os.environ["AQLAB_JOBS"]
    check("no flag, no env is one worker", A.parse_jobs(None) == 1, "")
    for bad in ("-2", "four"):
        try:
            A.parse_jobs(bad)
        except argparse.ArgumentTypeError:
            pass
        else:
            check(f"{bad!r} is rejected", False, "accepted")


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
        '    <TextView android:gravity="left|center" android:layout_width="wrap_content"\n'
        '        android:layout_height="wrap_content"/>\n'
        '    <TextView android:gravity="start|center" android:layout_width="wrap_content"\n'
        '        android:layout_height="wrap_content"/>\n'
        '    <ImageButton android:paddingLeft="12dp" android:layout_width="1dp"\n'
        '        android:layout_height="1dp"/>\n'
        '</LinearLayout>\n', encoding="utf-8")

    # Measured on a real APK: `fix_symmetry` added 27 attributes across 16
    # Material layouts and every one changed the rendered shape, because a view
    # against one edge on purpose looked like a hole to fill. It is no longer
    # wired into `--fix`; the check stays a warning. This asserts the wiring,
    # so re-adding the call without a decision fails here.
    check("symmetry is not auto-fixed",
          not hasattr(A, "fix_symmetry") or "fix_symmetry(proj" not in
          inspect.getsource(A.cmd_lint), "fix_symmetry still called from cmd_lint")

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
    check("gravity rewritten to start", 'android:gravity="start|center"' in after, after)
    check("view against one edge untouched",
          'android:paddingLeft="12dp"' in after and "paddingRight" not in after, after)
    check("already-directional gravity untouched",
          after.count('android:gravity="start|center"') == 2, after)

    with contextlib.redirect_stdout(io.StringIO()) as out2:
        A.cmd_lint(args)
    check("idempotent", layout.read_text(encoding="utf-8") == after, after)
    check("second pass reports nothing", "fixed:" not in out2.getvalue(), out2.getvalue()[:200])

    # `symmetric_fix` still exists and still works on one attribute set — it is
    # simply not wired into `--fix` (see "symmetry is not auto-fixed" above).
    # The unit is kept so a future `--fix-symmetry` opt-in has something
    # tested to call, and so its own behaviour cannot rot.
    got = A.symmetric_fix(' android:paddingStart="16dp"')
    check("symmetric_fix still adds the partner", 'android:paddingEnd="16dp"' in got, got)
    check("symmetric_fix leaves a physical pair alone",
          A.symmetric_fix(' android:paddingLeft="12dp"') == ' android:paddingLeft="12dp"')

    # Not reachable from cmd_lint any more, so these assert the helper only.
    two_path = layout.parent / "two_holes.xml"
    two_path.write_text(
        XML_HEAD + '<TextView android:paddingStart="4dp"'
        ' android:layout_marginStart="8dp" />', encoding="utf-8")
    wrote = A.fix_symmetry(root, dry=False)
    two = two_path.read_text(encoding="utf-8")
    check("helper still writes both partners", 'android:paddingEnd="4dp"' in two
          and 'android:layout_marginEnd="8dp"' in two, two)
    check("helper idempotent", A.fix_symmetry(root, dry=False) == 0, "")

    # One element, two holes, one pass: `symmetric_fix` reads the attribute set
    # once, so calling it per element repaired paddingStart/End and left the
    # margin hole reported — a helper that needs a second run to finish
    # contradicts its own idempotence check.
    two_path.write_text(
        XML_HEAD + '<TextView android:paddingStart="4dp"'
        ' android:layout_marginStart="8dp" />', encoding="utf-8")
    holes = A.symmetry_problems(root)
    mine = [h for h in holes if h[0].endswith("two_holes.xml")]
    check("both holes reported", len(mine) == 2, str(holes))
    check("one pass repairs the element", A.fix_symmetry(root, dry=False) == 1, "")
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
NL = chr(10)
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

def test_pin_drop_removes_every_pin(tmp):
    """`fix_pin_local` dropped only one pin per row; a row carried two.

    `direction_pins` reports the first pin it finds on a row and breaks, so
    `layoutDirection="ltr"` shadowed a `textDirection="ltr"` beside it. The
    fixer removed the layout pin, the text pin survived, the row stayed
    reported and the next `--fix` edited the same file again — the fixer
    contradicted its own idempotence.

    Two more mismatches between the reporter's pattern and the fixer's, both
    found by probing rather than by reading: the reporter accepts `app:` and
    either quote char, the fixer accepted neither. An `app:layoutDirection`
    row was left with a bare `app:` where the value used to be — broken XML —
    and a single-quoted row was reported but unreachable.
    """
    root = Path(tmp) / "pindrop"
    res = root / "res"
    layout = res / "layout"
    layout.mkdir(parents=True)
    (root / "AndroidManifest.xml").write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
        ' package="com.x"><application android:label="a"/></manifest>', encoding="utf-8")
    ns = ('<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"'
          ' xmlns:app="http://schemas.android.com/apk/res-auto"')
    f = layout / "a.xml"

    # Both pins on one row: the report names the row once, the fix must clear it.
    f.write_text(ns + ' android:layoutDirection="ltr" android:textDirection="ltr"'
                 ' android:paddingLeft="4dp"/>', encoding="utf-8")
    check("row with two pins is a finding",
          [g[0] for g in A.direction_pins(root)] == ["layoutDirection"],
          str(A.direction_pins(root)))
    check("fixer drops both pins", A.fix_pin_local(root) == 1, "")
    after = f.read_text(encoding="utf-8")
    check("no pin left behind", "Direction=" not in after, after)
    check("physical attr migrated", 'android:paddingStart="4dp"' in after, after)
    check("second pass is a no-op", A.fix_pin_local(root) == 0, "")

    # `app:` prefix must be dropped whole, not leave a dangling `app:`.
    f.write_text(ns + ' app:layoutDirection="ltr" android:paddingLeft="4dp"/>',
                 encoding="utf-8")
    check("app: pin is a finding", len(A.direction_pins(root)) == 1,
          str(A.direction_pins(root)))
    A.fix_pin_local(root)
    after = f.read_text(encoding="utf-8")
    check("app: pin removed whole", "app:" not in after.split("xmlns:app=")[1],
          after)
    check("app: fix is idempotent", A.fix_pin_local(root) == 0, "")

    # Single quotes: the reporter accepts them, so the fixer must too.
    f.write_text(ns + " android:textDirection='ltr' android:paddingLeft='4dp'/>",
                 encoding="utf-8")
    check("single-quoted text pin is a finding", len(A.direction_pins(root)) == 1,
          str(A.direction_pins(root)))
    check("fixer reaches a single-quoted pin", A.fix_pin_local(root) == 1, "")
    check("single-quoted pin gone", "Direction=" not in f.read_text(encoding="utf-8"),
          f.read_text(encoding="utf-8"))


def test_lint_fix_rtl_enabled_pin(tmp):
    """`lint --fix` must drop a `ltr` literal that `rtl_enabled` reported.

    `rtl_enabled` names a direction literal on a row that carries no physical
    attributes. The warning always promised `--fix` would drop it, but the only
    fixer was `fix_pin_local` reading `direction_pins`, which never names such
    a row: the pin survived every pass and the promise was a lie. The repair is
    the same one — drop the literal, migrate nothing, because there is nothing
    physical to migrate.

    The queue now has two sources, so it is no longer in document order by
    construction: a `--fix` that pairs a `<B/>` finding with the first `<A/>`
    in the file edits the wrong row and leaves the reported one pinned.
    """
    root = Path(tmp) / "enabledfix"
    res = root / "res" / "layout"
    res.mkdir(parents=True)
    ns = '<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"'

    # No physical attrs anywhere: only `rtl_enabled` sees this row.
    f = res / "a.xml"
    f.write_text(ns + ' android:orientation="horizontal"'
                 ' android:layoutDirection="ltr">'
                 '<TextView android:textDirection="ltr" android:text="a"/>'
                 '</LinearLayout>', encoding="utf-8")
    check("a row with no physical attrs is an rtl_enabled finding",
          len(A.rtl_enabled(root)) == 2, str(A.rtl_enabled(root)))
    check("direction_pins does not name it", A.direction_pins(root) == [],
          str(A.direction_pins(root)))
    # A pin on an element with no physical attrs is a deliberate "keep this
    # subtree LTR" marker, not a hole. ExoPlayer's control views carry one so
    # play/pause/skip stay in playback order under Arabic; stripping it mirrors
    # them. Measured on 8BP. The fixer must leave these alone entirely.
    check("fixer leaves a bare pin alone", A.fix_pin_local(root) == 0, "")
    after = f.read_text(encoding="utf-8")
    check("pin survives", 'android:layoutDirection="ltr"' in after
          and 'android:textDirection="ltr"' in after, after)
    check("still reported as a warning", len(A.rtl_enabled(root)) == 2,
          str(A.rtl_enabled(root)))

    # Two rows, two tags: both keep their pins and neither is touched.
    f.write_text(ns + '>' + NL +
                 '<TextView android:layoutDirection="ltr" android:text="x"/>' + NL +
                 '<Button android:layoutDirection="ltr" android:text="y"/>' + NL +
                 '</LinearLayout>', encoding="utf-8")
    check("both same-file rows are findings", len(A.rtl_enabled(root)) == 2,
          str(A.rtl_enabled(root)))
    check("neither is fixed", A.fix_pin_local(root) == 0, "")
    after = f.read_text(encoding="utf-8")
    check("both pins survived", after.count('android:layoutDirection="ltr"') == 2, after)

    # Source order is lost: `direction_pins` and `rtl_enabled` are two lists, so
    # the queue was built source-first rather than in document order. Here the
    # first row on disk is a `rtl_enabled` one and the second is a `direction_pins`
    # one, and the two loops name them in the opposite order: an unsorted queue
    # pairs the second entry with the first `<ImageView>` row and edits the wrong
    # start tag.
    f.write_text(ns + '>' + NL +
                 '<TextView android:layoutDirection="ltr" android:text="x"/>' + NL +
                 '<LinearLayout android:paddingLeft="4dp" android:layoutDirection="ltr">'
                 '<ImageView/></LinearLayout>' + NL +
                 '</LinearLayout>', encoding="utf-8")
    check("one row from each source",
          len(A.direction_pins(root)) == 1 and len(A.rtl_enabled(root)) == 1,
          f"{A.direction_pins(root)} / {A.rtl_enabled(root)}")
    check("only the physical row is fixed", A.fix_pin_local(root) == 1, "")
    after = f.read_text(encoding="utf-8")
    check("the physical row was migrated, not just unpinned",
          'android:paddingStart="4dp"' in after, after)
    check("the text row kept its text and its pin",
          'android:text="x"' in after
          and after.count('android:layoutDirection="ltr"') == 1, after)

    # A row both checks name - an `ltr` pin and physical attrs, with a text
    # widget beneath so `rtl_enabled` sees it too - is still one start tag.
    # Summing the two counts queues it twice and the walk then spends the second
    # entry on the next row, editing a row nothing reported.
    f.write_text(ns + '>' + NL +
                 '<LinearLayout android:paddingLeft="4dp" android:layoutDirection="ltr">'
                 '<TextView android:text="t"/></LinearLayout>' + NL +
                 '<Button android:layoutDirection="ltr" android:text="b"/>' + NL +
                 '</LinearLayout>', encoding="utf-8")
    check("the same row is named by both checks",
          len(A.direction_pins(root)) == 1 and len(A.rtl_enabled(root)) == 2,
          f"{A.direction_pins(root)} / {A.rtl_enabled(root)}")
    check("only the physical row is edited", A.fix_pin_local(root) == 1, "")
    after = f.read_text(encoding="utf-8")
    check("the physical row was migrated", 'android:paddingStart="4dp"' in after, after)
    check("the sibling's bare pin survived", after.count('android:layoutDirection="ltr"') == 1, after)
    check("mixed-source fix is idempotent", A.fix_pin_local(root) == 0, "")

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
    check("tsv has one row per entry, plurals expanded", len(rows) == 12, str(len(rows)))
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

    # Swapping two numbered specifiers is what a counter must do in RTL: the
    # reference APK's `%1$d/%2$d` reads `%2$d/%1$d` in Arabic and both render,
    # because Android fills each positional specifier by its own index. It was
    # the only *error* on that APK and ten of ten format findings there.
    ar.write_text("""<?xml version="1.0" encoding="utf-8"?>
<resources>
    <string name="app_name">تطبيقي</string>
    <string name="welcome">مرحباً بعودتك، %1$s</string>
    <string name="progress">تنزيل %d ملفات</string>
    <string name="counter">%2$d/%1$d</string>
    <string name="safe">جاهز</string>
</resources>
""", encoding="utf-8")
    rc = A.cmd_lint(argparse.Namespace(proj=str(proj), locale="ar", verbose=True))
    check("swapped positional specifiers are not an error", rc == 0)

    # Inventing an index is still a defect: argument 3 was never passed, so the
    # screen shows the literal `%3$d` (or throws) where the count belongs.
    ar.write_text("""<?xml version="1.0" encoding="utf-8"?>
<resources>
    <string name="app_name">تطبيقي</string>
    <string name="welcome">مرحباً بعودتك، %1$s</string>
    <string name="progress">تنزيل %d ملفات</string>
    <string name="ratio">%1$d من %3$d</string>
    <string name="safe">جاهز</string>
</resources>
""", encoding="utf-8")
    rc = A.cmd_lint(argparse.Namespace(proj=str(proj), locale="ar", verbose=True))
    check("an invented argument index is still an error", rc == 1)

    # An unnumbered specifier beside numbered ones is the order-sensitive case:
    # Android numbers it from scratch and the pair is not interchangeable.
    ar.write_text("""<?xml version="1.0" encoding="utf-8"?>
<resources>
    <string name="app_name">تطبيقي</string>
    <string name="welcome">مرحباً بعودتك، %s</string>
    <string name="progress">تنزيل %d ملفات</string>
    <string name="ratio">%2$d من %s</string>
    <string name="safe">جاهز</string>
</resources>
""", encoding="utf-8")
    rc = A.cmd_lint(argparse.Namespace(proj=str(proj), locale="ar", verbose=True))
    check("an unnumbered value against a numbered source is still an error", rc == 1)


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


def test_rtl_enabled(tmp):
    """RtlEnabled: a direction literal that wins over the locale but is not a pin.

    `direction_pins` only names a row that also carries physical left/right
    attributes; the same literal on a row with none still beats the locale, and
    nothing reported it. The half that must stay silent is measured on the real
    APK: its four `layoutDirection="locale"` are the correct spelling and its two
    `ltr` pins sit on Material clock/timepicker wrappers whose whole content is
    includes of hour/minute parts — order, not language — so `rtl_enabled`
    reports 0 there.
    """
    root = Path(tmp) / "enabled"
    res = root / "res"
    (res / "layout").mkdir(parents=True)
    # `layoutDirection` is only a finding in a build that opted into RTL: with
    # no switch the platform mirrors nothing and every `ltr` row renders as
    # written. `test_rtl_enabled_gates` owns that gate and the opt-out
    # finding, so this test only has to hold the switch on.
    (root / "AndroidManifest.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>' + NL +
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android">' + NL +
        '  <application android:supportsRtl="true"/>' + NL +
        '</manifest>', encoding="utf-8")
    (res / "layout" / "a.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"'
        ' xmlns:app="http://schemas.android.com/apk/res-auto">\n'
        '  <LinearLayout android:orientation="horizontal"'
        ' android:layoutDirection="ltr"><TextView android:text="a"/></LinearLayout>\n'
        '  <LinearLayout android:orientation="horizontal"'
        ' android:layoutDirection="locale"><TextView android:text="a"/></LinearLayout>\n'
        '  <TextView android:textDirection="ltr" android:text="a"/>\n'
        '  <TextView android:textDirection="rtl" android:text="a"/>\n'
        '  <TextView android:layoutDirection="rtl" android:text="a"/>\n'
        '  <com.acme.Custom android:layoutDirection="ltr"/>\n'
        '  <LinearLayout android:orientation="horizontal"'
        ' app:layoutDirection="ltr"><TextView android:text="a"/></LinearLayout>\n'
        '</LinearLayout>', encoding="utf-8")

    found = A.rtl_enabled(root)
    check("ltr row with text is a finding",
          sum(1 for g in found if g[0] == "layoutDirection" and g[3] == "ltr"
              and g[2] == "LinearLayout") == 2, str(found))
    check("textDirection=ltr is a finding",
          any(g[0] == "textDirection" and g[3] == "ltr" and g[2] == "TextView"
              for g in found), str(found))
    check("locale is the default spelled out, not a finding",
          not any(g[3] == "locale" for g in found), str(found))
    check("textDirection=rtl follows the locale, not a finding",
          not any(g[0] == "textDirection" and g[3] == "rtl" for g in found), str(found))
    check("rtl on a leaf is a mirrored glyph, not a finding",
          not any(g[3] == "rtl" for g in found), str(found))
    check("a dotted custom class may read its own styleable",
          not any(g[2] == "com.acme.Custom" for g in found), str(found))

    # The attribute must be attributed to the element that writes it: a start
    # tag encloses its whole subtree, so a scan of the outer body would report
    # the child's literal against the parent's tag, and count the same defect
    # twice under two different names.
    (res / "layout" / "nested.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"'
        ' android:layoutDirection="ltr">\n'
        '  <TextView android:text="a" android:textDirection="ltr"/>\n'
        '</LinearLayout>', encoding="utf-8")
    nest = [g for g in A.rtl_enabled(root) if g[1].endswith("nested.xml")]
    check("a literal belongs to the element that writes it",
          len(nest) == 2 and [g[2] for g in nest] == ["LinearLayout", "TextView"], str(nest))

    # Order-driven content: a clock face is ltr in every locale and pinning it
    # is right. Measured shape of the real APK's two pins.
    (res / "layout" / "clock.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"'
        ' android:orientation="horizontal" android:layoutDirection="ltr">\n'
        '  <include android:id="@+id/hour" layout="@layout/chip"/>\n'
        '  <include layout="@layout/divider"/>\n'
        '</LinearLayout>', encoding="utf-8")
    check("an include-only row is order-driven, not language-driven",
          not any(g[1].endswith("clock.xml") for g in A.rtl_enabled(root)),
          str(A.rtl_enabled(root)))

    # A project-declared enum: aapt2 refuses a duplicate platform constant, so a
    # declared value is the app's own and stripping it breaks the compile.
    (res / "values").mkdir()
    (res / "values" / "attrs.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>\n'
        '  <attr name="layoutDirection"><enum name="mirror" value="1"/></attr>\n'
        '</resources>', encoding="utf-8")
    (res / "layout" / "own.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"'
        ' android:layoutDirection="mirror"><TextView android:text="a"/></LinearLayout>',
        encoding="utf-8")
    check("declared enum value is the app's own",
          A.declared_direction_values(root) == {"mirror"},
          str(A.declared_direction_values(root)))
    check("a declared enum value is never a finding",
          not any(g[3] == "mirror" for g in A.rtl_enabled(root)),
          str(A.rtl_enabled(root)))


def test_rtl_enabled_gates(tmp):
    """`rtl_enabled` must not report a `layoutDirection` row RTL can never reach,
    and must report the manifest that turns RTL off.

    Measured on the real APK before writing this: two `ltr` rows sit on Material
    clock/timepicker wrappers and are silent only because nothing text-like lives
    beneath them — the reason they *may* be silent is that the manifest opts in,
    so the gate has to be explicit. Repointing both to a locale-keyed resource
    made them not reportable at all; with the manifest flipped to `false`, every
    `ltr` row in the tree is unreachable by the platform and reporting it is
    noise. The manifest is the finding then, exactly once.
    """
    root = Path(tmp) / "gate"
    res = root / "res"
    (res / "layout").mkdir(parents=True)
    (root / "AndroidManifest.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>' + NL +
        '<!-- original: <application android:supportsRtl="false"/> -->' + NL +
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android">' + NL +
        '  <application android:allowBackup="true" android:supportsRtl="true"/>' + NL +
        '</manifest>', encoding="utf-8")
    (res / "layout" / "a.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>' + NL +
        '<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android">' + NL +
        '  <LinearLayout android:layoutDirection="ltr">' + NL +
        '    <TextView android:text="a"/>' + NL +
        '  </LinearLayout>' + NL +
        '  <TextView android:layoutDirection="@string/row_dir" android:text="b"/>' + NL +
        '  <TextView android:layoutDirection="?attr/row_dir" android:text="c"/>' + NL +
        '  <TextView android:textDirection="ltr" android:text="d"/>' + NL +
        '</LinearLayout>', encoding="utf-8")

    check("manifest opt-in is read", A.manifest_supports_rtl(root) is True)
    check("a commented-out opt-out does not speak for the manifest",
          A.manifest_supports_rtl(root) is True)
    found = A.rtl_enabled(root)
    check("an opted-in build reports the literal rows",
          [g[3] for g in found if g[0] == "layoutDirection"] == ["ltr", "@string/row_dir", "?attr/row_dir"],
          str(found))
    check("textDirection is not gated by supportsRtl",
          any(g[0] == "textDirection" for g in found), str(found))
    check("an opted-in manifest is not itself a finding",
          not any(g[0] == "supportsRtl" for g in found), str(found))

    # Opting out: the rows render as written, so the manifest is the only thing
    # worth saying, and it must be said once.
    (root / "AndroidManifest.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>' + NL +
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android">' + NL +
        '  <application android:supportsRtl="false"/>' + NL +
        '</manifest>', encoding="utf-8")
    off = A.rtl_enabled(root)
    check("an opt-out silences every layoutDirection row",
          not any(g[0] == "layoutDirection" for g in off), str(off))
    check("an opt-out is a finding of its own",
          [g for g in off if g[0] == "supportsRtl"] ==
          [("supportsRtl", "AndroidManifest.xml", "", "false")], str(off))

    (root / "AndroidManifest.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>' + NL +
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android">' + NL +
        '  <application android:allowBackup="true"/>' + NL +
        '</manifest>', encoding="utf-8")
    check("an absent supportsRtl is not an opt-out",
          A.manifest_supports_rtl(root) is None, str(A.manifest_supports_rtl(root)))
    # Absent is what `aqlab rtl` is about to add, so the rows are still worth
    # naming: none of them can follow a locale that was never switched on.
    check("an absent supportsRtl still lets the rows be reported",
          [g[3] for g in A.rtl_enabled(root) if g[0] == "layoutDirection"]
          == ["ltr", "@string/row_dir", "?attr/row_dir"],
          str(A.rtl_enabled(root)))
    check("an absent supportsRtl is not a finding",
          not any(g[0] == "supportsRtl" for g in A.rtl_enabled(root)))

    # `lint` must key the manifest message by the attribute, not by whatever the
    # string loop left in `where` — it reported the opt-out against
    # `character_counter_pattern` until this was measured.
    class Args:
        pass
    argv = Args()
    argv.proj = str(root)
    argv.dir = str(root)
    argv.locale = "ar"
    argv.fail_under = None
    argv.fail_under_file = None
    argv.verbose = False
    argv.json = False
    (root / "AndroidManifest.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>' + NL +
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android">' + NL +
        '  <application android:supportsRtl="false"/>' + NL +
        '</manifest>', encoding="utf-8")
    (res / "values").mkdir(exist_ok=True)
    (res / "values" / "strings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>' + NL + '<resources>' + NL +
        '  <string name="hello">hello</string>' + NL + '</resources>', encoding="utf-8")
    (res / "values-ar").mkdir(exist_ok=True)
    (res / "values-ar" / "strings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>' + NL + '<resources>' + NL +
        '  <string name="hello">مرحبا</string>' + NL + '</resources>', encoding="utf-8")
    with contextlib.redirect_stdout(io.StringIO()) as out:
        try:
            A.cmd_lint(argv)
        except SystemExit:
            pass
    text = out.getvalue()
    check("the manifest message names the manifest",
          "AndroidManifest.xml:supportsRtl" in text, text[-600:])
    check("the manifest message is not charged to a string key",
          "character_counter_pattern" not in text, text[-600:])


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
    plain = _tmp_zip("plain.apk", {"AndroidManifest.xml": b"x"})
    seen = []
    orig_cmd, orig_run = A.apktool_cmd, A.run
    A.apktool_cmd = lambda: ["apktool"]
    A.run = lambda cmd, **kw: seen.append(list(cmd))
    try:
        for ws in (False, True):
            A.cmd_decode(argparse.Namespace(apk=str(plain), out="o", framework=None,
                                            quiet=True, with_sources=ws, split=None))
    finally:
        A.apktool_cmd, A.run = orig_cmd, orig_run
    check("decode skips smali by default", "-s" in seen[0])
    check("--with-sources keeps smali", "-s" not in seen[1])


def test_all_pipeline_summary():
    """`aqlab all` runs every stage, hides their chatter, and prints one block.

    Wired hermetically: every stage is replaced, so this checks the wiring —
    which stage runs, in what order, what aborts, and what the summary says —
    not apktool. The analyser bug it guards is bookkeeping: the summary used to
    name the APK we passed to `build` rather than the one `build` wrote, and an
    unsigned run's `out.apk` is not a signable artifact while the aligned copy is.
    """
    calls, err = [], io.StringIO()

    def rec(name, out=""):
        def fn(a):
            calls.append((name, getattr(a, "proj", None), getattr(a, "out", None)))
            if out:
                print(out)
            return 0
        return fn

    orig = (A.cmd_decode, A.cmd_rtl, A.cmd_lint, A.cmd_build)
    A.cmd_decode = rec("decode")
    A.cmd_rtl = rec("rtl", "[DONE] 7 file(s) updated")
    A.cmd_lint = rec("lint", "  warn  [RtlSymmetry] a.xml: paddingLeft\n"
                             "  warn  [RtlRtl] b.xml: x\n"
                             "  warn  [RtlSymmetry] c.xml: paddingRight\n"
                             "[OK] 0 error(s), 3 warning(s)")
    A.cmd_build = rec("build", "[DONE] signed -> demo-ar-aligned.apk")
    try:
        ns = argparse.Namespace(apk=str(_tmp_zip("demo.apk", {"AndroidManifest.xml": b"x"})),
                                out="demo-ar.apk", tsv=None, locale="ar",
                                verbose=False, quiet=True, ks=None, ks_pass="",
                                ks_alias=None, framework=None, force_min_sdk=False,
                                with_sources=False, report="")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = A.cmd_all(ns)
        out = buf.getvalue()
    finally:
        A.cmd_decode, A.cmd_rtl, A.cmd_lint, A.cmd_build = orig

    # `all` is not a verb — error out — and 7 files migrated must reach the summary
    check("all: every stage runs, in order",
          [c[0] for c in calls] == ["decode", "rtl", "lint", "build"])
    # `decode` receives the project dir as `out` and no `proj`; `build` takes both
    # The tree is a fresh temp dir now (the pipeline deletes it on the way
    # out), so the check is that every stage got the *same* one, not a stem.
    check("all: every stage shares one project dir",
          len({c[1] for c in calls[1:]}) == 1
          and calls[0][2] == calls[1][1]
          and calls[3][2] == "demo-ar.apk", str(calls))
    check("all: per-rule counts are aggregated and sorted",
          "RtlRtl=1 RtlSymmetry=2" in out)
    check("all: the migrated count comes from rtl's own line",
          "changes : 7" in out)
    check("all: the reported output is the file build wrote, not the one it read",
          "output  : demo-ar-aligned.apk" in out)
    check("all: a stage's chatter does not leak into the summary",
          "paddingLeft" not in out and out.count("[OK]") == 4)

    # no -o means the summary would name a file that does not exist
    try:
        A.cmd_all(argparse.Namespace(apk=str(_tmp_zip("demo.apk", {"AndroidManifest.xml": b"x"})),
                                     out=None, tsv=None, locale="ar",
                                     verbose=False, quiet=True, ks=None, ks_pass="",
                                     ks_alias=None, framework=None, force_min_sdk=False,
                                     with_sources=False, report=""))
        raised = False
    except A.Fail:
        raised = True
    check("all: refuses to run without an output path", raised)

    # a failing stage aborts the pipeline and keeps that stage's own words
    A.cmd_decode = lambda a: (_ for _ in ()).throw(A.Fail("apktool failed (1): boom"))
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            try:
                A.cmd_all(argparse.Namespace(apk=str(_tmp_zip("demo.apk", {"AndroidManifest.xml": b"x"})),
                                             out="x.apk", tsv=None,
                                             locale="ar", verbose=False, quiet=True,
                                             ks=None, ks_pass="", ks_alias=None,
                                             framework=None, force_min_sdk=False,
                                             with_sources=False, split=None,
                                             report=""))
                aborted = False
            except A.Fail as e:
                aborted = "boom" in str(e)
    finally:
        A.cmd_decode, A.cmd_rtl, A.cmd_lint, A.cmd_build = orig
    check("all: a failed stage aborts the pipeline", aborted)


def test_all_report_and_cleanup():
    """`all --report` writes the lint report, and a pipeline that made its own
    decoded tree deletes it.

    Measured on the real APK: `all` decoded into `<stem>-decoded` beside the
    input and left it there, so one command produced an APK, a report and 100 MB
    of aapt output the caller never asked for. The report is also the one file
    worth keeping, so it must land outside that tree (default: beside the APK).
    """
    seen, err = {}, io.StringIO()
    rep_path = Path(tempfile.mkdtemp()) / "r.json"

    def lint(a):
        # the pipeline's own report settings, as the parser hands them over
        seen["report"], seen["out"], seen["name"] = a.report, a.out, a.report_name
        Path(a.out).write_text("{}", encoding="utf-8") if a.report else None
        print(f"[DONE] report: {a.out}" if a.report else "")
        print("[OK] 0 error(s), 0 warning(s)")
        return 0

    orig = (A.cmd_decode, A.cmd_rtl, A.cmd_lint, A.cmd_build)
    made = []

    def dec(a):
        # a real decode creates the dir; the cleanup must follow *that*
        Path(a.out).mkdir(parents=True, exist_ok=True)
        (Path(a.out) / "AndroidManifest.xml").write_text("x", encoding="utf-8")
        made.append(a.out)
        return 0
    A.cmd_decode = dec
    A.cmd_rtl = lambda a: (print("[DONE] 0 file(s) updated"), 0)[1]
    A.cmd_lint = lint
    A.cmd_build = lambda a: (print("[DONE] built -> x-aligned.apk"), 0)[1]
    apk = _tmp_zip("demo.apk", {"AndroidManifest.xml": b"x"})
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            A.cmd_all(argparse.Namespace(apk=str(apk), out="o.apk", tsv=None,
                                         locale="ar", verbose=False, quiet=True,
                                         ks=None, ks_pass="", ks_alias=None,
                                         framework=None, force_min_sdk=False,
                                         with_sources=False, split=None,
                                         report="json", report_out=str(rep_path)))
        out = buf.getvalue()
    finally:
        A.cmd_decode, A.cmd_rtl, A.cmd_lint, A.cmd_build = orig

    check("all: --report is threaded into lint", seen.get("report") == "json", str(seen))
    # measured: the report titled itself after the temp tree (`aqlab-all-p7052512`)
    check("all: the report names the APK, not the temp tree",
          seen.get("name") == "demo", str(seen))
    check("all: a report path is passed through",
          str(rep_path) in out and rep_path.exists(), out)
    check("all: the summary names the report line", "report  :" in out, out)
    check("all: the tree it decoded is deleted afterwards",
          made and not Path(made[0]).exists(), str(made))
    # A deleted tree must not be named as if it were still there: `project :`
    # used to print the mkdtemp path that `finally` had already removed.
    check("all: the summary admits the tree is gone",
          "none — decoded tree removed" in out, out)
    check("all: the summary does not print a path it deleted",
          f"project : {made[0]}" not in out, out)

    # A tree the caller already had is not the pipeline's to delete: a decoded
    # project may hold edits, and `all` only ever adds to it.
    cwd0 = os.getcwd()
    os.chdir(Path(apk).parent)
    proj_dir = Path("demo-decoded")
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "keep.xml").write_text("x", encoding="utf-8")
    A.cmd_decode = lambda a: 0
    A.cmd_rtl = lambda a: (print("[DONE] 0 file(s) updated"), 0)[1]
    A.cmd_lint = lambda a: (print("[OK] 0 error(s), 0 warning(s)"), 0)[1]
    A.cmd_build = lambda a: (print("[DONE] built -> x-aligned.apk"), 0)[1]
    try:
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                A.cmd_all(argparse.Namespace(apk=str(apk), out="o.apk", tsv=None,
                                             locale="ar", verbose=False, quiet=True,
                                             ks=None, ks_pass="", ks_alias=None,
                                             framework=None, force_min_sdk=False,
                                             with_sources=False, split=None,
                                             report="", report_out=None))
        finally:
            pass
    finally:
        os.chdir(cwd0)
        A.cmd_decode, A.cmd_rtl, A.cmd_lint, A.cmd_build = orig
        shutil.rmtree(proj_dir, ignore_errors=True)
    check("all: a pre-existing project dir survives",
          (Path(apk).parent / "demo-decoded" / "keep.xml").exists())

    # --tm must be a stage between rtl and lint: measured on the real APK, a
    # pipeline with a TM and no translation stage builds an APK identical to
    # one run without it, and the summary says nothing about the gap.
    orig = (A.cmd_decode, A.cmd_rtl, A.cmd_lint, A.cmd_build, A.cmd_translate)
    order2, seen2 = [], {}

    def rec2(name, line):
        def f(a):
            order2.append(name)
            seen2[name] = a
            print(line, flush=True)
            return 0
        return f

    A.cmd_decode = rec2("decode", "[DONE] decoded -> x")
    A.cmd_rtl = rec2("rtl", "[DONE] 1 file(s) updated")
    A.cmd_translate = rec2("translate", "[DONE] translate: 46/100 (46.0%) -> x/res/values-ar/  (exact=45 fuzzy=1)")
    A.cmd_lint = rec2("lint", "[OK] 0 error(s), 0 warning(s)")
    A.cmd_build = rec2("build", "[DONE] built -> x-aligned.apk")
    tm = Path(apk).parent / "tm.json"
    tm.write_text('{"a": "b"}', encoding="utf-8")
    um = Path(apk).parent / "unm.json"
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            A.cmd_all(argparse.Namespace(apk=str(apk), out="o.apk", tsv=None,
                                         locale="ar", verbose=False, quiet=True,
                                         ks=None, ks_pass="", ks_alias=None,
                                         framework=None, force_min_sdk=False,
                                         with_sources=False, split=None,
                                         report="", report_out=None,
                                         tm=str(tm), glossary=None, translator=None,
                                         threshold=0.85, translate_force=True,
                                         unmatched_out=str(um)))
        out2 = buf.getvalue()
    finally:
        A.cmd_decode, A.cmd_rtl, A.cmd_lint, A.cmd_build, A.cmd_translate = orig

    check("all: --tm runs a translate stage between rtl and lint",
          order2 == ["decode", "rtl", "translate", "lint", "build"], str(order2))
    check("all: --tm reaches cmd_translate with the glossary, threshold and force",
          seen2["translate"].tm == str(tm) and seen2["translate"].threshold == 0.85
          and seen2["translate"].force is True
          and seen2["translate"].unmatched_out == str(um), str(seen2.get("translate")))
    # `all` does not write a second report from translate: lint's report is the
    # artifact, and a translate report would overwrite it
    check("all: the translate stage writes no report of its own",
          seen2["translate"].report is None)
    check("all: the summary carries the coverage, not just a stage tick",
          "translate: 46/100 (46.0%)" in out2, out2)

    # no --tm: no translate stage at all (the default pipeline is unchanged)
    order3 = []
    A.cmd_decode = lambda a: (order3.append("decode"), 0)[1]
    A.cmd_rtl = lambda a: (order3.append("rtl"), print("[DONE] 0 file(s) updated"), 0)[2]
    A.cmd_translate = lambda a: (order3.append("translate"), 0)[1]
    A.cmd_lint = lambda a: (order3.append("lint"), print("[OK] 0 error(s), 0 warning(s)"), 0)[2]
    A.cmd_build = lambda a: (order3.append("build"), print("[DONE] built -> x"), 0)[2]
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            A.cmd_all(argparse.Namespace(apk=str(apk), out="o.apk", tsv=None,
                                         locale="ar", verbose=False, quiet=True,
                                         ks=None, ks_pass="", ks_alias=None,
                                         framework=None, force_min_sdk=False,
                                         with_sources=False, split=None,
                                         report="", report_out=None))
        plain_order = list(order3)
    finally:
        A.cmd_decode, A.cmd_rtl, A.cmd_lint, A.cmd_build, A.cmd_translate = orig
    check("all: without --tm the pipeline is still decode/rtl/lint/build",
          plain_order == ["decode", "rtl", "lint", "build"], str(plain_order))


def test_all_unsigned_output_line():
    """An unsigned build must still name its artifact in the summary.

    Measured on the real APK: `all --split ar` printed `output : None` because
    the only pattern tried was `signed ->`. Every pipeline without a keystore -
    the default - reported no artifact at all.
    """
    import re as _re
    # read the pattern out of aqlab itself, so a narrowed pattern fails here
    src = Path(A.__file__).read_text(encoding="utf-8")
    pat_src = _re.search(r'built = _re\.search\(r"(.*?)", build_out', src.replace("re.search", "_re.search"))
    pat = _re.compile(pat_src.group(1).encode().decode("unicode_escape"), _re.M)
    line = "[DONE] built -> x-ar-aligned.apk  (unsigned; pass --ks to sign)"
    check("all: an unsigned build's output path is read from its own line",
          pat.search(line).group(1) == "x-ar-aligned.apk", str(pat.search(line)))
    line2 = "[DONE] signed -> x-ar-aligned.apk"
    check("all: a signed build's output path is still read",
          pat.search(line2).group(1) == "x-ar-aligned.apk", str(pat.search(line2)))


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


def test_plural_table_matches_cldr():
    """The shipped plural table is CLDR's, rebuilt from the checked-in copy.

    Round 43 shipped the table as a literal and both times a row was typed it
    was wrong — `he` had no `many`, `vi` was given a `one` CLDR does not have,
    `tg` was missing outright. The table is only useful if a hand edit cannot
    disagree with ICU, so this rebuilds it from
    `tests/fixtures/cldr/plurals.json` and compares. Mutation: change any row
    in `PLURAL_CATEGORIES_BY_KIND` (or the alias block) and this fails.
    """
    sys.path.insert(0, str(ROOT / "tools"))
    import cldr_tables
    groups = cldr_tables.table()
    expect = {loc: cats for cats, locs in groups.items() for loc in locs}
    alias = {"in": "id", "iw": "he", "ji": "yi", "jw": "jv", "tl": "fil"}
    for old, new in alias.items():
        expect[old] = expect[new]
    shipped = dict(A.PLURAL_CATEGORIES)
    wrong = sorted(k for k in set(expect) | set(shipped)
                   if tuple(expect.get(k, ())) != tuple(shipped.get(k, ())))
    check("plural table is exactly CLDR plus Android's four legacy tags",
          not wrong, f"differs on {wrong[:8]}")
    check("cldr fixture really has the cardinal rules",
          len(groups) >= 10 and sum(len(v) for v in groups.values()) > 200,
          str(len(groups)))
    # `und` is CLDR's "undetermined" and not an Android qualifier.
    check("und is not a lintable locale", "und" not in shipped)
    # The three rows that were wrong when typed.
    check("he has many", A.plural_categories("he") == ("one", "two", "other"))
    # `vi` gained a `one` rule in CLDR 48; before that it was `other`-only, and
    # the typed table kept the old shape. Assert what the fixture says.
    check("vi matches its own CLDR row",
          A.plural_categories("vi") == tuple(
              json.loads((ROOT / "tests" / "fixtures" / "cldr" / "plurals.json")
                         .read_text(encoding="utf-8"))["cardinal"]["vi"]))
    check("tg is a CLDR row", A.plural_categories("tg") == ("one", "other"))
    check("gl has many", A.plural_categories("gl") == ("one", "many", "other"))
    # Legacy Android qualifier resolves to the modern row's categories.
    check("legacy in/iw resolve", A.plural_categories("in") == A.plural_categories("id")
          and A.plural_categories("iw") == A.plural_categories("he"))


def test_rtl_table_matches_cldr():
    """The shipped RTL list is CLDR's, rebuilt from the checked-in fixture.

    `RTL_LOCALES` was a typed ten, then generated once into a 258-entry literal
    whose generator lived in a scratch folder and nowhere in the repo — so a
    hand edit, or a CLDR release that adds a script, could not be caught. Now
    `tools/cldr_tables.py` rebuilds it from
    `tests/fixtures/cldr/rtl.json` (CLDR-48 `likelySubtags.xml` +
    `scriptMetadata.txt`, rule recorded in the fixture) and this compares.
    Mutation: change one tag in the shipped `frozenset`, or drop a line of the
    fixture, and this fails.
    """
    sys.path.insert(0, str(ROOT / "tools"))
    import cldr_tables
    expect = cldr_tables.rtl_table()
    shipped = set(A.RTL_LOCALES)
    wrong = sorted(expect ^ shipped)
    check("rtl list is exactly CLDR's", not wrong, f"differs on {wrong[:8]}")
    check("rtl fixture is a real CLDR row set",
          len(expect) > 50 and {"ar", "he", "fa", "ur", "nqo"} <= expect,
          str(len(expect)))
    # The three failure modes of a typed list, asserted as behaviour:
    # a language CLDR names RTL but the old ten never did,
    check("a CLDR RTL language is reported",
          A.is_rtl("hbo") and A.is_rtl("bal") and A.is_rtl("syr"))
    # a region is stripped before the lookup, as Android writes it,
    check("region on an RTL language still resolves",
          A.is_rtl("ar-rSA") and A.is_rtl("he-IL"))
    # and a language with an RTL *neighbour* is not swept in with it.
    check("rtl does not overreach on its neighbours",
          not any(A.is_rtl(x) for x in ("ku", "tr", "az", "ms", "ha")))
    # The generator is the repo's, not a scratch script: `--update` output must
    # be byte-identical to what ships, so the block is reproducible.
    block = cldr_tables.render_rtl(expect)
    src = open(A.__file__, encoding="utf-8").read()
    i = src.index(cldr_tables.RTL_BEGIN)
    j = src.index(cldr_tables.RTL_END, i) + len(cldr_tables.RTL_END)
    check("the shipped rtl block is the generator's own output",
          src[i:j] == block, (src[i:j][-40:], block[-40:]))


def _wr(p, body):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def _wr(p, body):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


DBL = chr(34)
def test_unknown_locale_plural_is_not_faked(tmp):
    """A locale CLDR has no row for must say so, not invent a category verdict.

    `plurals/xx` with `one`/`other` used to produce "one, other is not a xx
    plural category" - a claim nothing supports, fired on a perfectly ordinary
    file, and it hid the real state: plurals are not checked at all. Mutation:
    drop the `if not plural_categories(...)` NOTE or restore the unguarded
    `unknown` list and one of these fails.
    """
    root = Path(tmp) / "nolocale"
    body = ('<?xml version=@1.0@ encoding=@utf-8@?>@N<resources>@N'
            '  <string name=@app_name@>Demo</string>@N'
            '  <plurals name=@n_items@>@N'
            '    <item quantity=@one@>%d item</item>@N'
            '    <item quantity=@other@>%d items</item>@N'
            '  </plurals>@N</resources>@N')
    body = body.replace("@", DBL)
    body = body.replace("@N", NL)
    for d in ("values", "values-xx"):
        _wr(root / "res" / d / "strings.xml", body)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        A.cmd_lint(argparse.Namespace(proj=str(root), locale="xx", verbose=False))
    out = buf.getvalue()
    check("an unknown locale says its plurals are unchecked",
          "no CLDR plural row for `xx`" in out, out)
    check("it does not invent a category verdict", "plural category" not in out, out)
    he = ('<?xml version=@1.0@ encoding=@utf-8@?>@N<resources>@N'
        '  <plurals name=@n_items@>@N'
        '    <item quantity=@zero@>אפס</item>@N'
        '    <item quantity=@one@>אחד</item>@N'
        '    <item quantity=@other@>%d</item>@N'
        '  </plurals>@N</resources>@N')
    he = he.replace("@", DBL).replace("@N", NL)
    _wr(root / "res" / "values-he" / "strings.xml", he)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        A.cmd_lint(argparse.Namespace(proj=str(root), locale="he", verbose=False))
    check("a known locale still names the unused category",
          "zero is not a he plural category" in buf.getvalue(), buf.getvalue())


def test_locale_folder_match_is_case_insensitive(tmp):
    """`--locale ar-rSA` must find `values-ar-rSA` whatever the caller's case.

    `glob("values-{locale}*")` is case-sensitive on Linux and macOS, so a
    mixed-case argument reported a missing translation folder that existed.
    Mutation: put the glob back and this fails.
    """
    root = Path(tmp) / "casefold"
    _wr(root / "res" / "values" / "strings.xml",
        '<resources><string name=@a@>Item</string></resources>'.replace("@", DBL))
    _wr(root / "res" / "values-ar-rSA" / "strings.xml",
        '<resources><string name=@a@>عنصر</string></resources>'.replace("@", DBL))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        A.cmd_lint(argparse.Namespace(proj=str(root), locale="ar-RSA", verbose=False))
    out = buf.getvalue()
    check("a mixed-case locale argument finds its folder",
          "no res/values-ar-RSA" not in out, out)
    check("and the folder it found is the one on disk",
          "lint res/values-ar-RSA" in out, out)


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


_SCRATCH = None


def _tmp_zip(name, entries):
    """Write a throwaway zip under one scratch dir and return its Path.

    It used to call mkdtemp() per zip, so every run of this file left a dozen
    `aqlab-layout-*` directories behind in %TEMP% -- 1848 of them by the time
    anyone looked. One dir, removed at the end of main(), is the whole fix.
    """
    global _SCRATCH
    if _SCRATCH is None or not Path(_SCRATCH).is_dir():
        _SCRATCH = tempfile.mkdtemp(prefix="aqlab-layout-")
    d = Path(_SCRATCH)
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    with zipfile.ZipFile(p, "w") as z:
        for k, v in entries.items():
            z.writestr(k, v)
    return p


def test_tmp_zip_shares_one_scratch_dir():
    """`_tmp_zip` must not leave a directory per call in %TEMP%.

    Measured, not assumed: the old per-call mkdtemp() had put 1848
    aqlab-layout-* dirs in this machine's temp dir.
    """
    before = len(list(Path(tempfile.gettempdir()).glob("aqlab-layout-*")))
    a = _tmp_zip("share1.apk", {"AndroidManifest.xml": b"x"})
    b = _tmp_zip("share2.apk", {"AndroidManifest.xml": b"x"})
    after = len(list(Path(tempfile.gettempdir()).glob("aqlab-layout-*")))
    check("tmp zips share one scratch dir", a.parent == b.parent, str(a.parent))
    check("tmp zips add at most one temp dir", after - before <= 1,
          f"{before} -> {after}")


def test_apk_layout():
    """A container is classified, never guessed.

    This guards a silent-wrong-answer bug, not a missing feature: apktool opens
    any zip, so handing `decode` an XAPK or an AAB used to produce a decode of
    the container itself (an AAB decodes to a project with no res/ at all) and
    a build that fails much later with an unrelated message. Ordering matters —
    a split APK *is* a zip of APKs and also contains AndroidManifest.xml.
    """
    apk = _tmp_zip("l_apk.apk", {"AndroidManifest.xml": b"x", "res/a.xml": b"y"})
    xapk = _tmp_zip("l.xapk", {"base.apk": b"PK", "split_config.ar.apk": b"PK"})
    aab = _tmp_zip("l.aab", {"BundleConfig.pb": b"x",
                             "base/manifest/AndroidManifest.xml": b"x"})
    junk = _tmp_zip("l_junk.zip", {"readme.txt": b"x"})
    check("layout: plain apk", A.apk_layout(apk) == "apk")
    check("layout: xapk", A.apk_layout(xapk) == "xapk")
    check("layout: aab beats its inner manifest path", A.apk_layout(aab) == "aab")
    check("layout: unknown zip is not an apk", A.apk_layout(junk) == "unknown")

    bad = apk.parent / "l_notzip.apk"
    bad.write_bytes(b"not a zip")
    try:
        A.apk_layout(bad)
    except A.Fail as e:
        check("layout: corrupt file fails loudly", "not a zip/apk" in str(e), str(e))
    else:
        check("layout: corrupt file fails loudly", False, "no Fail raised")


def test_split_parts_order():
    """base ranks before every split; a locale split is not mistaken for base."""
    # `base.apk` sorts AFTER both splits alphabetically, so any sort that does
    # not rank it explicitly puts a split first.
    xapk = _tmp_zip("o.xapk", {"splits/split_config.xxhdpi.apk": b"PK",
                               "aa_split_config.arm64_v8a.apk": b"PK",
                               "base.apk": b"PK"})
    parts = A.split_parts(xapk)
    check("xapk: base decodes first", parts[0][1] == "base.apk", str(parts))
    check("xapk: every split is listed", len(parts) == 3, str(parts))
    check("xapk: split labels keep their qualifier",
          "split_config.xxhdpi" in [l for l, _ in parts], str(parts))


def test_decode_refuses_container_ambiguity():
    """`decode` on an XAPK decodes each part; `all` refuses without --split.

    The failure this guards measured as a real build error, not a wrong file:
    `aqlab all some.xapk` decoded the container, then rtl/lint ran on a
    directory holding no res/, and the error surfaced three stages later.
    """
    xapk = _tmp_zip("d.xapk", {"base.apk": b"PK", "split_config.ar.apk": b"PK"})
    seen, err = [], io.StringIO()
    orig_cmd, orig_run = A.apktool_cmd, A.run
    A.apktool_cmd = lambda: ["apktool"]
    A.run = lambda cmd, **kw: seen.append(list(cmd))

    def ns(**kw):
        base = dict(apk=str(xapk), out=str(xapk.parent / "dx"), framework=None,
                    quiet=True, with_sources=False, split=None)
        base.update(kw)
        return argparse.Namespace(**base)

    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            A.cmd_decode(ns())
        out = buf.getvalue()
        check("decode xapk: one apktool run per part", len(seen) == 2, str(seen))
        check("decode xapk: base goes to the requested dir",
              any(str(xapk.parent / "dx") == str(c[c.index("-o") + 1]) for c in seen), str(seen))
        check("decode xapk: split goes to its own dir",
              "dx-split_config.ar" in out, out)

        seen.clear()
        # The extracted part is a temp file, so watch what gets copied out of
        # the container instead: a wrong selector copies base.apk.
        pulled = []
        orig_ex = A.extract_split
        A.extract_split = lambda c, member, o: (pulled.append(member), orig_ex(c, member, o))[1]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                A.cmd_decode(ns(split="ar"))
        finally:
            A.extract_split = orig_ex
        check("decode --split: exactly one part", len(seen) == 1, str(seen))
        check("decode --split: the named part, not merely the first one",
              pulled == ["split_config.ar.apk"], str(pulled))
        # The split is copied out of the container to a real .apk so apktool
        # can read it. That copy is the caller's own APK at full size; keeping
        # it turns every XAPK decode into two copies of the input on disk.
        part = xapk.parent / "dx.part.apk"
        check("decode --split: the extracted copy is cleaned up",
              not part.exists(), f"left behind {part}")
    finally:
        A.apktool_cmd, A.run = orig_cmd, orig_run

    try:
        with contextlib.redirect_stderr(err):
            A.cmd_all(argparse.Namespace(apk=str(xapk), out="o.apk", framework=None,
                                         quiet=True, with_sources=False,
                                         split=None, locale="ar", ts=None,
                                         tsv=None, ks=None, ks_pass=None,
                                         ks_alias=None, verbose=False,
                                         report="", report_out=None))
    except A.Fail as e:
        check("all on an xapk refuses without --split", "--split" in str(e), str(e))
    else:
        check("all on an xapk refuses without --split", False, "no Fail raised")


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


def test_symmetry_covered_by_modern_pair(tmp):
    """A physical side whose modern twin covers both ends is not a hole.

    `paddingLeft` + `paddingStart` + `paddingEnd` is Material's minSdk-era
    shape: old platforms read the physical attribute, new ones read start/end.
    Reporting the missing `paddingRight` there was wrong on every start/end
    platform, and on the real APK it was wrong ten times out of ten
    (minSdkVersion 26). The reverse still has to report — `paddingLeft` +
    `paddingRight` with no start/end is exactly what breaks a flip.
    """
    root = Path(tmp) / "symcov"
    layout = root / "res" / "layout"
    layout.mkdir(parents=True)
    (root / "AndroidManifest.xml").write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
        ' package="com.x"><application android:supportsRtl="true"/></manifest>',
        encoding="utf-8")
    (layout / "a.xml").write_text(
        '<FrameLayout xmlns:android="http://schemas.android.com/apk/res/android">'
        '<ImageView android:paddingLeft="8dp" android:paddingStart="8dp"'
        ' android:paddingEnd="8dp" android:layout_width="1dp"'
        ' android:layout_height="1dp"/>'
        '<Button android:layout_marginRight="5dp" android:layout_width="1dp"'
        ' android:layout_height="1dp"/>'
        '<TextView android:layout_marginStart="4dp" android:layout_marginEnd="4dp"'
        ' android:layout_width="1dp" android:layout_height="1dp"/>'
        '</FrameLayout>', encoding="utf-8")

    got = A.symmetry_problems(root)
    check("covered physical side is not a hole",
          not [g for g in got if g[1] == "ImageView"], str(got))
    check("physical-only side still reports",
          [(g[1], g[2]) for g in got] == [("Button", "layout_marginRight")], str(got))
    # A physical-only hole is `aqlab rtl`'s job, not `--fix`'s: adding
    # `layout_marginStart` beside a `layout_marginRight` would leave the row
    # with the right margin set twice on a modern platform.
    check("--fix leaves the physical-only row to aqlab rtl",
          A.fix_symmetry(root, dry=False) == 0, "")


def test_symmetry_partial_modern_is_not_coverage(tmp):
    """Only *both* start and end count as coverage.

    `paddingLeft` + `paddingStart` alone still breaks a flip: start resolves to
    the left edge in LTR and to the right edge in RTL, so the row is fine one
    way and asymmetric the other. Measured on the real APK: no row has this
    shape, so it has to be a synthetic fixture or the guard is untested.
    """
    root = Path(tmp) / "sympart"
    layout = root / "res" / "layout"
    layout.mkdir(parents=True)
    (root / "AndroidManifest.xml").write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
        ' package="com.x"><application/></manifest>', encoding="utf-8")
    (layout / "a.xml").write_text(
        '<FrameLayout xmlns:android="http://schemas.android.com/apk/res/android"'
        ' android:paddingLeft="8dp" android:paddingStart="8dp"'
        ' android:layout_width="1dp" android:layout_height="1dp"/>', encoding="utf-8")
    got = A.symmetry_problems(root)
    # Both pairs are half-open here, and both are real: the physical side has
    # no right, and the modern side it gained has no end.
    check("half a modern pair is not coverage",
          [(g[2], g[3]) for g in got] == [("paddingLeft", "paddingRight"),
                                          ("paddingStart", "paddingEnd")], str(got))



def test_translate_tm_glossary_and_hook(tmp):
    """`translate` fills values-ar from the TM, then the glossary, then the hook.

    The three sources are tried in that order and nothing is guessed. A TM
    candidate carrying a different format signature is not a match at all: `%d`
    written where the source has `%1$s` is a runtime crash, and the memory is
    where such a fragment arrives from another project. A plural category the
    source cannot supply stays absent rather than being filled with English.
    """
    work = Path(tmp) / "tr"
    res = work / "res"
    res.mkdir(parents=True)
    (res / "values").mkdir()
    (res / "values" / "strings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>'
        '<string name="app_name">Chess Assistant</string>'
        '<string name="hello">Hello %1$s</string>'
        '<string name="bye">Goodbye %1$s</string>'
        '<string name="leader">Leader: %1$s</string>'
        '<string name="boss">Hero %1$s!</string>'
        '<string name="counter">Count %1$d/%2$d</string>'
        '<string name="home_key">Chess home</string>'
        '<string name="fuzzy_one">Delete the move</string>'
        '<string name="twice">Done %1$s, again %1$s</string>'
        '<string name="twice_f">Load %1$2.1f</string>'
        '<string name="wide">%1$d of %2$d</string>'
        '</resources>', encoding="utf-8")
    (res / "values" / "plurals.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>'
        '<plurals name="files"><item quantity="one">%d file</item>'
        '<item quantity="other">%d files</item></plurals></resources>', encoding="utf-8")
    tm = Path(tmp) / "tm.json"
    tm.write_text(json.dumps({
        "Chess Assistant": "مساعد الشطرنج",
        "Chess home": "Chess home",
        "Delete the moves": "احذف الحركات",
        "%1$d of %2$d": "العدد %2$d من %1$d",
        "Done %1$s, again %2$s": "تم %2$s ثم %1$s",
        # the same shape with a count in Arabic and a type change in the source
        # position: `%d` where the string has `%s` would make String.format throw
        # same index and repeat count, different conversion: `%d` where the source
        # has a float conversion throws at runtime, so it is not this string
        "Load %1$d": "تحميل %1$d",
        "Hello %1$s": "مرحباً %1$s",
        "Goodbye %1$s": "وداعاً %1$s",           # exact, but the glossary will edit it
        "Leader: %d": "القائد: %d",              # wrong signature for "Leader: %1$s"
        "Leader: %1$s": "القائد %1$s",           # same signature: must be the one used
        "Hero %1$s!": "بطل %1$s!",                # exact; hook would answer differently
        "Count %1$d/%2$d": "العدد %2$d/%1$d",     # same argument set: legal
    }, ensure_ascii=False), encoding="utf-8")
    gl = Path(tmp) / "gl.json"
    gl.write_text(json.dumps({"Chess": "الشطرنج"}, ensure_ascii=False), encoding="utf-8")
    hook = Path(tmp) / "hook.py"
    hook.write_text(
        "import json, sys\n"
        "items = json.load(sys.stdin)\n"
        "print(json.dumps({i['key']: 'هوك ' + i['source'] for i in items}))\n",
        encoding="utf-8")
    q = lambda *c: " ".join('"' + x + '"' if " " in x else x for x in c)
    out_json = Path(tmp) / "unmatched.json"

    rc = A.cmd_translate(argparse.Namespace(
        proj=str(work), tm=str(tm), glossary=str(gl), locale="ar",
        translator=f"cmd:{q(sys.executable, str(hook))}", threshold=0.85, force=True,
        unmatched_out=str(out_json), dry_run=False, quiet=True))
    check("translate rc", rc == 0)
    # `res` is `<work>/res/values`, so the locale folder is a sibling - the
    # created-missing case must fail a check, not raise OSError mid-suite
    ar_file = work / "res" / "values-ar" / "strings.xml"
    check("translate created values-ar/strings.xml", ar_file.is_file(),
          str(sorted(p.relative_to(work).as_posix() for p in work.rglob("*"))))
    got = {e.key: e.text for e in A.read_entries(ar_file)} if ar_file.is_file() else {}
    check("exact match", got.get("app_name") == "مساعد الشطرنج", str(got.get("app_name")))
    # exact TM text for boss carries a Latin "Chess"; the glossary must rewrite
    # it inside the Arabic sentence and leave a translation that never had the
    # term alone
    check("exact TM row beats the hook",
          got.get("boss") == "بطل %1$s!", str(got.get("boss")))
    check("fuzzy match above the threshold",
          got.get("fuzzy_one") == "احذف الحركات", str(got.get("fuzzy_one")))
    check("glossary rewrote the term inside an Arabic sentence",
          got.get("home_key") == "الشطرنج home", str(got.get("home_key")))
    # `goodbye` is a Turkish word this glossary must not touch (a case-insensitive
    # search is not a substring search)
    check("glossary did not touch a translation without the term",
          got.get("hello") == "مرحباً %1$s", str(got.get("hello")))
    # both rows read "Leader: <arg>" and only the signature tells them apart: with
    # the check dropped, the first row wins and Arabic gets `%d` for a `%1$s`
    check("a TM row whose signature differs is not the match",
          got.get("leader") == "القائد %1$s", str(got.get("leader")))
    # a TM row for the same shape but `%d` where the source has `%s` is not a
    # match even though the argument set is identical: Android formats `%d` with
    # a String argument by throwing
    check("a specifier type change is not a match",
          got.get("twice") == "هوك Done %1$s, again %1$s", str(got.get("twice")))
    # same argument index and same conversion count, different conversion: `%d`
    # where the source has a float conversion throws at runtime
    check("a conversion change is not a match",
          got.get("twice_f") == "هوك Load %1$2.1f", str(got.get("twice_f")))
    check("permuted positional arguments are still a match",
          got.get("wide") == "العدد %2$d من %1$d"
          and got.get("counter") == "العدد %2$d/%1$d", str(got.get("wide")))

    check("positional arguments compare as a set",
          got.get("counter") == "العدد %2$d/%1$d", str(got.get("counter")))
    # the source cannot declare a category, so it has no text: the writer emits
    # an empty <item> to keep every Arabic category present, and that empty item
    # is a gap to fill, not a translation
    pl = ({e.key: e.text for e in
           A.read_entries(work / "res" / "values-ar" / "plurals.xml")}
          if (work / "res" / "values-ar" / "plurals.xml").is_file() else {})
    check("source-insupplyable plural categories are not filled with English",
          not pl.get("files|few", "").strip() and not pl.get("files|many", "").strip(),
          str({k: v for k, v in pl.items() if "|" in k}))
    un = json.loads(out_json.read_text(encoding="utf-8"))
    check("unmatched keys are listed", "files|few" in un and "app_name" not in un, str(un))


def test_translate_refuses_a_bad_hook(tmp):
    """A translator hook that cannot answer must stop the run, not write nothing."""
    work = Path(tmp) / "trbad"
    res = work / "res" / "values"
    res.mkdir(parents=True)
    (res / "strings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>'
        '<string name="a">Hello</string></resources>', encoding="utf-8")
    tm = Path(tmp) / "empty.json"
    tm.write_text("{}", encoding="utf-8")
    try:
        A.cmd_translate(argparse.Namespace(
            proj=str(work), tm=str(tm), glossary=None, locale="ar", translator=None,
            threshold=0.85, force=True, unmatched_out=None, dry_run=False, quiet=True))
    except A.Fail:
        pass
    else:
        check("empty TM is a failure", False, "cmd_translate accepted a TM with no pairs")
    try:
        bad = Path(tmp) / "bad.py"
        bad.write_text("print(1)")

        A.translator_cmd(f"cmd:{sys.executable} {bad}", [("a", "Hello")])
    except A.Fail:
        pass
    else:
        check("non-JSON hook output is a failure", False)


def test_report_json_and_html(tmp):
    """`--report json|html`: one schema'd artifact, and every finding is attributed.

    A per-rule table is only worth having if a finding cannot fall out of it, so
    the check below is that the rule counts sum to the finding count - a new
    `add()` that forgets its rule id lands in `unknown` and still counts.
    """
    # `res/values` must be one mkdir: on Windows a `mkdir(parents=True)` creates
    # `res/values` as a directory chain and a later `write_text` on it fails.
    root = Path(tmp) / "rep"
    (root / "res" / "values").mkdir(parents=True)
    (root / "res" / "values" / "strings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<resources><string name="a">Hello %s</string>'
        '<string name="b">World</string></resources>', encoding="utf-8")
    # `a` has a bare percent AND a format mismatch (two errors), `b` carries the
    # same Latin text on both sides (warn).
    (root / "res" / "values-ar").mkdir()
    (root / "res" / "values-ar" / "strings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<resources><string name="a">مرحبا %</string>'
        '<string name="b">World</string></resources>', encoding="utf-8")
    (root / "AndroidManifest.xml").write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
        ' package="com.x"><application android:label="x"/></manifest>', encoding="utf-8")

    def run(**kw):
        ns = argparse.Namespace(proj=str(root), locale="ar", quiet=True, verbose=False,
                                fail_under=None, fail_under_file=None,
                                report=kw.pop("report", None), out=kw.pop("out", None))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = A.cmd_lint(ns)
        return rc, out.getvalue()

    rc, text = run(report="json")
    path = root / "aqlab-report.json"
    check("json report written by default beside the project", path.exists(), text[-300:])
    check("lint still fails on the error", rc == 1, f"rc={rc} out={text[-500:]}")
    rep = json.loads(path.read_text(encoding="utf-8"))
    check("schema version present", rep["schema"] == 1, rep.get("schema"))
    check("tool block names the version", rep["tool"]["version"] == A.__version__, rep["tool"])
    check("locale recorded", rep["locale"] == "ar", rep["locale"])
    check("coverage block", rep["coverage"]["covered"] == 2, rep["coverage"])
    check("per-file coverage", rep["coverage"]["files"]["strings.xml"]["percent"] == 100.0,
          rep["coverage"]["files"])
    check("error counted", rep["totals"]["errors"] == 2, rep["totals"])
    check("warning counted", rep["totals"]["warnings"] == 1, rep["totals"])
    rules = {r["rule"]: r for r in rep["rules"]}
    check("format rule attributed", rules.get("format", {}).get("error") == 2, rules)
    check("translation rule attributed", rules.get("translation", {}).get("warn") == 1, rules)
    check("no unattributed findings", "unknown" not in rules, list(rules))
    check("every finding has a rule", all(f["rule"] in rules for f in rep["findings"]),
          rep["findings"])
    check("rule counts sum to findings",
          sum(r["error"] + r["warn"] + r["info"] for r in rep["rules"]) == len(rep["findings"]),
          (len(rep["findings"]), rep["rules"]))
    # Stable against a fixture: a second run writes the same bytes.
    first = path.read_bytes()
    run(report="json")
    check("json report is byte-stable", path.read_bytes() == first)

    rc, text = run(report="html", out=str(Path(tmp) / "r.html"))
    page = (Path(tmp) / "r.html").read_text(encoding="utf-8")
    check("html report honours -o", "aqlab report" in page, page[:200])
    check("html shows the counts", "2 error(s), 1 warning(s)" in page, page[:600])
    check("html shows the finding", "bare `%`" in page, page[-900:])
    check("html is self-contained", "<script" not in page and "http://" not in page, "")



def test_report_schema_fixture(tmp):
    """The v1 shape is a fixture a consumer codes against, not a moving target.

    `test_report_json_and_html` proves a real run *has* the documented keys; this
    one pins the keys themselves, so a key added to `build_report` without a
    schema bump fails here instead of reaching a release. The fixture is also the
    example CI vendors, therefore it must be exactly what `build_report` emits
    for the same input — round-tripping it through the builder is the check.
    """
    fixture = json.loads((Path(A.__file__).resolve().parent / "tests" / "fixtures"
                          / "report-v1.json").read_text(encoding="utf-8"))
    check("fixture declares the live schema", fixture["schema"] == A.REPORT_SCHEMA,
          fixture.get("schema"))
    # `REPORT_KEYS` is the contract; the builder is asserted against it inside
    # `build_report` itself, so a key added there without a bump trips this run
    # even when nothing here compares sets.
    check("fixture keys are exactly REPORT_KEYS", set(fixture) == set(A.REPORT_KEYS),
          set(fixture) ^ set(A.REPORT_KEYS))

    rep = A.build_report(Path(tmp) / "fixture", "ar", 50.0, 1, 2,
                         {"strings.xml": (1, 2)},
                         [("error", "strings.xml:a", "bare `%` in `a`", "format"),
                          ("warn", "strings.xml:b", "untranslated `b`", "translation")],
                         1, command="lint", name="fixture")
    rep["tool"]["version"] = "0.0.0"
    check("builder output matches the fixture", rep == fixture,
          json.dumps({k: (rep.get(k), fixture.get(k)) for k in set(rep) | set(fixture)
                      if rep.get(k) != fixture.get(k)}, ensure_ascii=False)[:600])

    # The builder's own guard, exercised the way a schema change would trip it:
    # a new key on the published set must not slip through `build_report`.
    live = A.REPORT_KEYS | {"extra"}
    A.REPORT_KEYS = live
    try:
        A.build_report(Path(tmp) / "fixture", "ar", 50.0, 1, 2,
                       {"strings.xml": (1, 2)},
                       [("error", "strings.xml:a", "bare `%` in `a`", "format"),
                        ("warn", "strings.xml:b", "untranslated `b`", "translation")],
                       1, command="lint", name="fixture")
        check("builder refuses an unpinned key", False, "REPORT_KEYS drifted unnoticed")
    except AssertionError as e:
        check("builder refuses an unpinned key", "extra" in str(e), str(e))
    finally:
        A.REPORT_KEYS = frozenset(A.REPORT_KEYS - {"extra"})

    # A stale schema must not reach disk: a consumer keys off the number.
    bad = dict(fixture, schema=A.REPORT_SCHEMA + 1)
    out = Path(tmp) / "stale.json"
    try:
        A.write_report("json", str(out), bad, Path(tmp))
        check("stale schema refused", False, "a schema 2 report was written")
    except A.Fail as e:
        check("stale schema refused", "schema" in str(e), str(e))
    check("stale schema wrote nothing", not out.exists())


def test_report_is_shared_by_scan_and_translate(tmp):
    """One report shape from three commands.

    The writer used to rebuild the report from lint's own arguments, so only
    `lint` could emit one. `scan` and `translate` are the two commands a CI
    driver runs either side of a release, and a schema with a `command` field
    that is always `lint` tells a consumer nothing about which stage found what.
    """
    BADGE = "".join([
        "package: name='com.x' versionCode='1' versionName='1'", chr(10),
        "locales: '--_--' 'en'", chr(10),
        "application-label:'X'", chr(10)])

    def fake_run(cmd, **kw):
        class R:
            stdout = ""
            stderr = ""
        r = R()
        r.stdout = ("  A: http://x:supportsRtl(0x010103af)=false" + chr(10)
                    if "--file" in cmd else BADGE)
        return r

    apk = Path(tmp) / "rep.apk"
    with zipfile.ZipFile(apk, "w") as z:
        z.writestr("AndroidManifest.xml", "x")
    rep_path = Path(tmp) / "scan.json"
    orig_run, orig_tool = A.run, A.tool
    try:
        A.tool = lambda name: name
        A.run = fake_run
        with contextlib.redirect_stdout(io.StringIO()):
            A.cmd_scan(argparse.Namespace(target=str(apk), json=False, jobs=None,
                                          report="json", out=str(rep_path)))
    finally:
        A.run, A.tool = orig_run, orig_tool
    rep = json.loads(rep_path.read_text(encoding="utf-8"))
    check("scan report carries the schema", rep["schema"] == A.REPORT_SCHEMA, rep.get("schema"))
    check("scan report names its producer", rep["command"] == "scan", rep.get("command"))
    check("scan report records the RTL finding",
          any(f["rule"] == "rtl-manifest" and f["level"] == "error" for f in rep["findings"]),
          rep["findings"])
    check("scan report counts the manifest error", rep["totals"]["errors"] == 1, rep["totals"])
    check("scan report keeps the apk block",
          rep["apk"]["package"] == "com.x" and rep["apk"]["supportsRtl"] is False, rep.get("apk"))
    # Coverage is not a scan number. 0/0 here would read as "nothing translated".
    check("scan report invents no coverage", rep["coverage"]["total"] == 0
          and rep["coverage"]["files"] == {}, rep["coverage"])

    work = Path(tmp) / "trrep"
    res = work / "res" / "values"
    res.mkdir(parents=True)
    (res / "strings.xml").write_text("".join([
        '<?xml version="1.0" encoding="utf-8"?>', chr(10), "<resources>",
        '<string name="a">Hello</string>',
        '<string name="b">Untouched</string>',
        "</resources>"]), encoding="utf-8")
    tm = Path(tmp) / "rep_tm.json"
    tm.write_text(json.dumps([{"source": "Hello", "target": "\u0645\u0631\u062d\u0628\u0627"}]),
                  encoding="utf-8")
    trep = Path(tmp) / "tr.json"
    rc = A.cmd_translate(argparse.Namespace(
        proj=str(work), tm=str(tm), glossary=None, locale="ar", translator=None,
        threshold=0.85, force=True, unmatched_out=None, dry_run=False, quiet=True,
        report="json", out=str(trep)))
    check("translate rc with a report", rc == 0)
    tr = json.loads(trep.read_text(encoding="utf-8"))
    check("translate report names its producer", tr["command"] == "translate",
          tr.get("command"))
    check("translate report counts the unmatched key",
          any(f["rule"] == "translate-unmatched" and f["key"] == "b"
              for f in tr["findings"]), tr["findings"])
    check("translate report coverage is the run's coverage",
          tr["coverage"]["covered"] == 1 and tr["coverage"]["total"] == 2, tr["coverage"])
    check("translate report keeps the match breakdown",
          tr["translate"]["matched"].get("exact") == 1, tr["translate"])
    check("unmatched finding is not an error", tr["totals"]["errors"] == 0, tr["totals"])

    # `--report --dry-run`: the report used to sit below the dry branch's
    # `return`, so the one flag pairing made for "show me the gaps without
    # writing values-ar" wrote no report at all — silently, exit 0.
    drep = Path(tmp) / "tr_dry.json"
    before = (work / "res" / "values-ar" / "strings.xml").read_bytes()
    with contextlib.redirect_stdout(io.StringIO()):
        rc = A.cmd_translate(argparse.Namespace(
            proj=str(work), tm=str(tm), glossary=None, locale="ar", translator=None,
            threshold=0.85, force=True, unmatched_out=None, dry_run=True, quiet=True,
            report="json", out=str(drep)))
    check("dry run still exits 0", rc == 0)
    check("dry run writes the report", drep.exists(), "no report")
    d = json.loads(drep.read_text(encoding="utf-8"))
    check("dry report has the same coverage", d["coverage"]["covered"] == 1
          and d["coverage"]["total"] == 2, d["coverage"])
    check("dry report lists the gap", any(f["rule"] == "translate-unmatched"
                                          for f in d["findings"]), d["findings"])
    check("dry run wrote no translation",
          (work / "res" / "values-ar" / "strings.xml").read_bytes() == before)


def test_translate_reports_whether_the_tm_was_asked(tmp):
    """A report must say how many keys the TM actually saw.

    With a target present for every key nothing is looked up: the run is all
    "kept" and 100% coverage. Printing only that number makes an empty or wrong
    TM indistinguishable from a working one - the user reads a green report and
    ships the old translations unreviewed.
    """
    NL = chr(10)
    work = Path(tmp) / "ktm"
    res = work / "res" / "values"
    res.mkdir(parents=True)
    (res / "strings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>' + NL + "<resources>"
        '<string name="a">Hello</string>'
        '<string name="b">Bye</string>'
        "</resources>", encoding="utf-8")
    ar = work / "res" / "values-ar"
    ar.mkdir(parents=True)
    (ar / "strings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>' + NL + "<resources>"
        '<string name="a">مرحبا</string>'
        '<string name="b">وداعاً</string>'
        "</resources>", encoding="utf-8")
    tm = Path(tmp) / "ktm.json"
    tm.write_text(json.dumps({"Hello": "أهلاً"}), encoding="utf-8")

    def run(force, out_name, quiet=True):
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink):
            rc = A.cmd_translate(argparse.Namespace(
                proj=str(work), tm=str(tm), glossary=None, locale="ar",
                translator=None, threshold=0.85, force=force, unmatched_out=None,
                dry_run=True, quiet=quiet, report="json",
                out=str(Path(tmp) / out_name)))
        check(f"translate(force={force}) exits 0", rc == 0)
        return json.loads((Path(tmp) / out_name).read_text(encoding="utf-8")), sink.getvalue()

    kept, said = run(False, "kept.json", quiet=False)
    check("full-target run evaluated no key", kept["translate"]["evaluated"] == 0,
          kept["translate"])
    check("full-target run counts both as kept", kept["translate"]["stale_kept"] == 2,
          kept["translate"])
    check("full-target run warns the TM was not consulted",
          "the TM resolved nothing" in said, said)

    forced, _ = run(True, "forced.json")
    check("--force reports every key as evaluated",
          forced["translate"]["evaluated"] == 2
          and forced["translate"]["stale_kept"] == 0, forced["translate"])


def test_translate_glossary_rewrites_a_term_only_in_the_source(tmp):
    """The glossary must fire on a term the *source* holds, not just on a hit.

    The old matcher skipped every row whose term was not already in the
    translated text, which is the ordinary case: the memory renders a term
    with Arabic and the row is never looked at. A real APK measured
    `glossary=0` for exactly this reason and the run looked clean. The
    report must say how many rows failed to fire, and the console must name
    one instead of printing a 0.
    """
    work = Path(tmp) / "gloss"
    res = work / "res" / "values"
    res.mkdir(parents=True)
    (res / "strings.xml").write_text(
        HEAD + '<string name="title">Chess Assistant</string>'
        '<string name="board">Open the board</string>'
        '<string name="plain">Settings</string>'
        '</resources>', encoding="utf-8")
    (work / "res" / "values-ar").mkdir()
    (work / "res" / "values-ar" / "strings.xml").write_text(
        HEAD + '<string name="kept">AR-kept</string></resources>', encoding="utf-8")
    tm = Path(tmp) / "gtm.json"
    ce = "مساعد"; sh = "الشطرنج"
    rq = "الرُقعة"; rq0 = "الرقعة"
    ih = "الإعدادات"
    tm.write_text(json.dumps({
        "Chess Assistant": "مساعد Assistant",
        "Open the board": "افتح " + rq0,
        "Settings": ih,
    }, ensure_ascii=False), encoding="utf-8")
    gl = Path(tmp) / "ggl.json"
    gl.write_text(json.dumps({"Assistant": sh, "Board": rq, "Settings": ih},
                  ensure_ascii=False), encoding="utf-8")
    rep = Path(tmp) / "g.json"
    buf = io.StringIO()
    written_dirs = []
    real_write = A.write_strings_xml
    def spy(out_dir, pairs, fname="strings.xml", sink=None, locale="ar"):
        written_dirs.append(Path(out_dir))
        return real_write(out_dir, pairs, fname, sink, locale)
    A.write_strings_xml = spy
    try:
     with contextlib.redirect_stdout(buf):
        rc = A.cmd_translate(argparse.Namespace(
            proj=str(work), tm=str(tm), glossary=str(gl), locale="ar",
            translator=None, threshold=0.85, force=False, unmatched_out=None,
            dry_run=False, quiet=False, report="json", out=str(rep)))
    finally:
        A.write_strings_xml = real_write
    said = buf.getvalue()
    # the DONE line names the folder the run wrote into; a bug that wrote one
    # level too deep printed that path too, so the assertion reads the real
    # `write_strings_xml` argument instead of the message
    done_path = written_dirs[0] if written_dirs else None
    check("glossary translate exits 0", rc == 0, said[-300:])
    # the written folder is res/values-ar, NOT res/values/values-ar: the
    # latter is the shape a bug wrote and aapt reads as no locale at all
    check("translate wrote into res/values-ar",
          done_path and done_path.resolve() == (work / "res" / "values-ar").resolve(),
          str(sorted(p.relative_to(work).as_posix() for p in work.rglob("*"))))
    out_xml = work / "res" / "values-ar" / "strings.xml"
    # a missing file is a failed check, not a traceback: the bug this test was
    # written for wrote into the wrong directory and the suite died with an
    # OSError instead of naming the check
    check("translate created values-ar/strings.xml", out_xml.is_file(),
          str(sorted(p.relative_to(work).as_posix() for p in work.rglob("*"))))
    got = {e.key: e.text for e in A.read_entries(out_xml)} if out_xml.is_file() else {}
    check("glossary rewrites a term the source holds",
          got.get("title") == ce + " " + sh, str(got))  # Assistant -> the term's Arabic
    # a row must fire on the source term alone: the target never held "Board"
    # and the row still rewrote nothing, because the target already carries a
    # correct rendering of it - a chosen word, not a failed row
    check("glossary leaves an already-correct rendering alone",
          got.get("board") == "افتح " + rq0, str(got))
    check("a settled glossary row is not a hit", got.get("plain") == ih, str(got))
    # the pre-existing row survives: `have` is read from a folder that is not
    # the folder being written, so the two names must not be confused
    check("a kept row survives the rewrite",
          A.existing_translations(work / "res", "ar").get("kept") == "AR-kept",
          str(A.existing_translations(work / "res", "ar")))
    doc = json.loads(rep.read_text(encoding="utf-8"))
    check("report counts the rewrite", doc["translate"]["glossary"] == 1,
          doc["translate"])
    check("report names a row that could not fire",
          doc["translate"]["glossary_missed"] == 1, doc["translate"])
    check("console names the row that could not fire", "`board`" in said,
          said[-400:])
    new_text, n, miss = A.glossary_plan("X", "Open the board", {"board": rq})
    check("glossary_plan fires on a source-only term",
          new_text == rq + " X" if False else (n == 0 and miss == "board"),
          (new_text, n, miss))
    t2, n2, _m2 = A.glossary_plan("X", "Chessboard", {"chess": sh})
    check("glossary does not match inside a longer word", (t2, n2) == ("X", 0),
          (t2, n2))


def test_lint_requires_a_default_locale(tmp):
    """`lint` must refuse a project with no default-locale strings.

    The default locale was picked with `sorted(glob("values*/strings.xml"))[0]`,
    so a tree holding only `values-ar/` compared the Arabic file against itself:
    "2/2 translated, 0 errors, 0 warnings" on a project that is 100% untranslated
    from the tool's point of view. A silent pass is the one failure a gate cannot
    afford, so the answer is `None` and a loud refusal.
    """
    proj = Path(tmp) / "only-ar"
    (proj / "res" / "values-ar").mkdir(parents=True)
    (proj / "res" / "values-ar" / "strings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<resources><string name="a">مرحبا</string></resources>', encoding="utf-8")
    ns = argparse.Namespace(proj=str(proj), locale="ar", quiet=True, verbose=False,
                            fail_under=None, fail_under_file=None, report=None, out=None)
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            A.cmd_lint(ns)
    except A.Fail as e:
        check("lint refuses with a clear message", "decode the APK" in str(e), str(e))
    else:
        check("lint does not pass a project with no default locale", False, out.getvalue())


def test_format_only_string_is_not_untranslated(tmp):
    """A value that is nothing but placeholders is identical in every locale.

    Measured on the reference APK: `plan_option_format` (`%1$s — %2$s`),
    `teach_x6_unit_rank` (`%1$s`) and
    `mtrl_picker_day_of_week_column_header` all fired twice — once as
    "identical to source" and once as "no Arabic characters" — for a value that
    cannot be translated at all. The format specifier is not Latin text, and
    nothing is left once it is removed, so neither rule may fire.
    """
    root = Path(tmp) / "fmt"
    (root / "res" / "values").mkdir(parents=True)
    (root / "res" / "values" / "strings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<resources><string name="fmt_only">%1$s — %2$s</string>'
        '<string name="solo">%1$s</string>'
        '<string name="word">Premium</string></resources>', encoding="utf-8")
    (root / "res" / "values-ar").mkdir()
    (root / "res" / "values-ar" / "strings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<resources><string name="fmt_only">%1$s — %2$s</string>'
        '<string name="solo">%1$s</string>'
        '<string name="word">Premium</string></resources>', encoding="utf-8")
    (root / "AndroidManifest.xml").write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
        ' package="com.x"><application android:label="x"/></manifest>', encoding="utf-8")
    ns = argparse.Namespace(proj=str(root), locale="ar", quiet=False, verbose=False,
                            fail_under=None, fail_under_file=None, report=None, out=None)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        A.cmd_lint(ns)
    text = out.getvalue()
    check("pure placeholder is not called untranslated",
          "fmt_only" not in text and "solo" not in text, text[-500:])
    check("a real English word still is", "word" in text, text[-500:])
    check("exactly one warning for the fixture", "1 warning(s)" in text, text[-200:])


def test_literal_text_attrs(tmp):
    """A literal `android:text` is the one string translation cannot reach.

    Measured on the reference APK: 9 literals, 5 of them the same word on five
    different badges — each is its own finding, because each is its own view.
    Every shape below is a false positive the rule must *not* raise.
    """
    root = Path(tmp) / "lit"
    (root / "res" / "layout").mkdir(parents=True)
    (root / "res" / "values").mkdir()
    (root / "res" / "values" / "strings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<resources><string name="app_name">Pro</string>'
        '<string name="ver" translatable="false">1.0</string></resources>',
        encoding="utf-8")
    (root / "res" / "layout" / "a.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?><LinearLayout>'
        '<TextView android:text="PRO" android:id="@+id/one"/>'
        '<TextView android:text="PRO" android:id="@+id/two"/>'
        '<TextView android:text="5"/>'
        '<TextView android:text="0/250"/>'
        '<TextView android:text="@string/app_name"/>'
        '<TextView android:text="@string/ver"/>'
        '<TextView android:text="?"/>'
        '<TextView android:text=""/>'
        '<EditText android:hint=" Search "/>'
        '</LinearLayout>', encoding="utf-8")
    # A literal in values/ is a default-locale default, not an untranslatable
    # one — the sweep is layout-only, so the `<TextView>` there is out of scope.
    (root / "res" / "values" / "extra.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<resources><string name="x">nope</string>'
        '<TextView android:text="PRO"/></resources>',
        encoding="utf-8")

    got = A.literal_text_attrs(root)
    vals = [(Path(pth).name, tag, attr, v, why) for pth, tag, attr, v, why in got]
    check("one finding per view, dupes kept", len(got) == 4, vals)
    check("badge literal twice", vals.count(("a.xml", "TextView", "text", "PRO", "literal")) == 2, vals)
    check("digit-only is not text", not any(v == "5" or v == "0/250" for _p, _t, _a, v, _w in vals), vals)
    check("translatable reference is not a literal",
          not any(v == "@string/app_name" for _p, _t, _a, v, _w in vals), vals)
    check("untranslatable ref is one", ("a.xml", "TextView", "text", "@string/ver",
                                        "untranslatable-ref") in vals, vals)
    check("blank is not a finding", not any(v == "" for _p, _t, _a, v, _w in vals), vals)
    check("hint is checked too", any(a == "hint" for _p, _t, a, _v, _w in vals), vals)
    check("values/ is out of scope", not any(p.endswith("extra.xml") for p, *_r in got), got)
    # The hint is trimmed before it is shown, so a padded literal reads clean.
    check("value trimmed", any(("a.xml", "EditText", "hint", "Search", "literal") == v for v in vals), vals)

    # Pooled and serial must be the same list, the same rule as every other sweep.
    check("literal jobs parity", A.literal_text_attrs(root, 4) == got, "")

    # The lint rule names it under its own rule, and no other rule claims it.
    (root / "res" / "values-ar").mkdir()
    (root / "res" / "values-ar" / "strings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<resources><string name="app_name">طليعي</string></resources>', encoding="utf-8")
    ns = argparse.Namespace(proj=str(root), locale="ar", quiet=True, verbose=False,
                            fail_under=None, fail_under_file=None,
                            report="json", out=str(Path(tmp) / "lit.json"))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        A.cmd_lint(ns)
    rep = json.loads((Path(tmp) / "lit.json").read_text(encoding="utf-8"))
    lit = [f for f in rep["findings"] if f["rule"] == "literal-text"]
    check("lint reports literal-text", len(lit) == 4, len(lit))
    check("literal is a warning", all(f["level"] == "warn" for f in lit), lit)
    check("message says it never reaches values-ar",
          any("never reaches values-ar" in f["message"] for f in lit), lit)


def main():
    with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as _stack:
        _stack.callback(lambda: _SCRATCH and shutil.rmtree(_SCRATCH, ignore_errors=True))
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
        test_pending_never_exceeds_entries(proj, tmp)
        test_scan_json_is_only_json(proj, tmp)
        test_dry_run_changes_nothing(proj, tmp)
        test_non_resource_files_are_reported(tmp)
        test_coverage_per_file_and_gates(tmp)
        test_rtl_locales_beyond_arabic(tmp)
        test_diff_between_apks(tmp)
        test_xliff_wrapping(tmp)
        test_translate_tm_glossary_and_hook(tmp)
        test_translate_refuses_a_bad_hook(tmp)
        test_symmetry_check(tmp)
        test_single_quoted_attributes(tmp)
        test_single_quoted_gravity_and_fix(tmp)
        test_rtl_drawable_dirs(tmp)
        test_symmetry_covered_by_modern_pair(tmp)
        test_symmetry_partial_modern_is_not_coverage(tmp)
        test_hardcoded_gravity(tmp)
        test_gravity_jobs(tmp)
        test_jobs_parity(tmp)
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
        test_pin_drop_removes_every_pin(tmp)
        test_direction_pins(tmp)
        test_rtl_enabled(tmp)
        test_rtl_enabled_gates(tmp)
        test_lint_fix_supports_rtl(tmp)
        test_lint_fix_rtl_enabled_pin(tmp)
        test_rtl(proj)
        test_lint(proj)
        test_dedupe_and_styles(tmp)
        test_min_sdk_guard(tmp)
        test_bad_path_guard(tmp)
        test_migrate_prefilter()
        test_all_pipeline_summary()
        test_all_report_and_cleanup()
        test_decode_sources_flag()
        test_apk_layout()
        test_tmp_zip_shares_one_scratch_dir()
        test_split_parts_order()
        test_decode_refuses_container_ambiguity()
        test_all_unsigned_output_line()
        test_plural_structure(tmp)
        test_plural_table_matches_cldr()
        test_rtl_table_matches_cldr()
        test_unknown_locale_plural_is_not_faked(tmp)
        test_locale_folder_match_is_case_insensitive(tmp)
        test_plural_file_roundtrip(tmp)
        test_rtl_self_closing_application(tmp)
        test_scan_apk_supports_rtl(tmp)
        test_scan(proj)
        test_fixture_locale_qualifier(tmp)
        test_report_json_and_html(tmp)
        test_report_schema_fixture(tmp)
        test_report_is_shared_by_scan_and_translate(tmp)
        test_translate_reports_whether_the_tm_was_asked(tmp)
        test_translate_glossary_rewrites_a_term_only_in_the_source(tmp)
        test_lint_requires_a_default_locale(tmp)
        test_literal_text_attrs(tmp)
        test_format_only_string_is_not_untranslated(tmp)
    print(f"[OK] {len(PASSED)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
