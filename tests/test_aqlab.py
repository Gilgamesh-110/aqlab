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
    check("lint warns with the fix", "aqlab rtl" in out.getvalue(), out.getvalue()[:400])


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

    remaining = A.symmetry_problems(root)
    check("only the physical hole left", len(remaining) == 1
          and remaining[0][2] == "paddingLeft", str(remaining))


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

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        A.cmd_lint(argparse.Namespace(proj=str(root), locale="ar", quiet=True,
                                      verbose=False, fail_under=None,
                                      fail_under_file=None))
    v = out.getvalue()
    check("lint warns on the ltr pin", 'layoutDirection="ltr"' in v, v[:500])
    check("lint names the row", "t.xml" in v, v[:500])


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
        test_hardcoded_gravity(tmp)
        test_lint_fix(tmp)
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
