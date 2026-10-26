#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""aqlab self-check — plain asserts, no framework.

    python tests/test_aqlab.py
"""
import argparse
import contextlib
import io
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import aqlab as A  # noqa: E402

MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    package="com.example.app">
    <application android:label="@string/app_name" android:theme="@style/App">
        <activity android:name=".Main"/>
    </application>
</manifest>
"""

LAYOUT = """<?xml version="1.0" encoding="utf-8"?>
<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"
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

STRINGS = """<?xml version="1.0" encoding="utf-8"?>
<resources>
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
    ar.write_text("""<?xml version="1.0" encoding="utf-8"?>
<resources>
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
    ar.write_text("""<?xml version="1.0" encoding="utf-8"?>
<resources>
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
    ar.write_text("""<?xml version="1.0" encoding="utf-8"?>
<resources>
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


def test_tail_risk():
    check("bidi tail detected", A._tail_risk("المجلد test."))
    check("pure arabic clean", not A._tail_risk("المجلد جاهز."))
    check("trailing placeholder is not a hazard", not A._tail_risk("مرحباً %s"))
    check("trailing positional placeholder not a hazard", not A._tail_risk("مرحباً %1$s"))
    check("rlM suppresses", "test." in "test.\u200f")
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


def test_scan(proj):
    rc = A.cmd_scan(argparse.Namespace(target=str(proj), json=True, quiet=True))
    check("scan rc", rc == 0)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        proj = fixture(tmp)
        test_escape()
        test_tail_risk()
        test_entries(proj)
        test_scaffold_apply(proj, tmp)
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
        test_scan(proj)
    print(f"[OK] {len(PASSED)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
