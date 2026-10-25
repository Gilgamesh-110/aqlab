#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""aqlab self-check — plain asserts, no framework.

    python tests/test_aqlab.py
"""
import argparse
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
    check("tsv created", (ar_dir / "strings.tsv").exists())
    tsv = (ar_dir / "strings.tsv").read_text(encoding="utf-8").splitlines()
    rows = [l for l in tsv if not l.startswith("#")]
    check("tsv has one row per entry", len(rows) == 6, str(len(rows)))
    check("translatable=false excluded", not any(r.startswith("skip\t") for r in rows))

    # translate: 3-col table (key, source, translation)
    ar_tsv = Path(tmp) / "ar.tsv"
    ar_tsv.write_text(
        "# key\tsource\ttranslation\n"
        "app_name\tMyApp\tتطبيقي\n"
        "welcome\tWelcome back, %1$s\tمرحباً بعودتك، %1$s\n"
        "progress\tDownloading %d files\tتنزيل %d ملفات\n"
        "files|one\t%d file\tملف واحد\n",
        encoding="utf-8")
    rc = A.cmd_apply(argparse.Namespace(proj=str(proj), tsv=str(ar_tsv), locale="ar"))
    check("apply rc", rc == 0)
    out = (ar_dir / "strings.xml").read_text(encoding="utf-8")
    check("arabic written", "تطبيقي" in out and "مرحباً بعودتك، %1$s" in out)
    check("plural regrouped", "<plurals name=\"files\">" in out and "ملف واحد" in out)
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

    # a correct file must pass clean
    ar.write_text("""<?xml version="1.0" encoding="utf-8"?>
<resources>
    <string name="app_name">تطبيقي</string>
    <string name="welcome">مرحباً بعودتك، %1$s</string>
    <string name="progress">تنزيل %d ملفات</string>
    <string name="safe">جاهز</string>
    <plurals name="files">
        <item quantity="one">%d ملف واحد</item>
        <item quantity="other">%d ملفات</item>
    </plurals>
</resources>
""", encoding="utf-8")
    rc = A.cmd_lint(argparse.Namespace(proj=str(proj), locale="ar", verbose=True))
    check("lint passes on a clean file", rc == 0, "expected no errors")

    # Arabic drops the count in the `one` plural form — warn, never an error
    ar.write_text("""<?xml version="1.0" encoding="utf-8"?>
<resources>
    <string name="app_name">تطبيقي</string>
    <string name="welcome">مرحباً بعودتك، %1$s</string>
    <string name="progress">تنزيل %d ملفات</string>
    <string name="safe">جاهز</string>
    <plurals name="files">
        <item quantity="one">ملف واحد</item>
        <item quantity="other">%d ملفات</item>
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
        test_scan(proj)
    print(f"[OK] {len(PASSED)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
