#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aqlab — localize any Android app into any language, at any text direction.

Single-file, stdlib-only. Wraps apktool + aapt2/zipalign/apksigner; all the
localization logic (RTL attribute migration, BiDi linting, CLDR plural tables,
resource escaping) is implemented here. Arabic is the hardest case and the one
every check was built against, not the limit: --locale takes any BCP-47 tag.

    aqlab scan    <apk|dir>            read-only report, changes nothing
    aqlab decode  <apk>                unpack to a project dir
    aqlab rtl     <dir>                manifest supportsRtl + left/right -> start/end
    aqlab scaffold<dir> --locale he    build he.tsv + res/values-he/strings.xml
    aqlab apply   <dir> --tsv he.tsv   import translated rows
    aqlab lint    <dir>                format, escaping, coverage — CI friendly
    aqlab build   <dir>                apktool b -> zipalign -> apksigner
    aqlab all     <apk>                decode + rtl + apply + lint + build

Exit code is non-zero when `lint` finds errors, so it drops straight into CI.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import difflib
import html
import io
import json
import multiprocessing
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

__version__ = "0.2.1"

# --------------------------------------------------------------------------
# Unicode classes
# --------------------------------------------------------------------------

def read_text(p):
    """Read a file with newlines preserved, on every supported Python.

    `Path.read_text(newline=...)` only exists from 3.13, so the 3.9-3.12 CI legs
    died with `TypeError: read_text() got an unexpected keyword argument
    'newline'` — a failure invisible on a 3.14 dev box. Passing newline="" is what
    stops universal-newline translation from rewriting the CRLFs an APK ships.
    """
    with open(p, "r", encoding="utf-8", newline="") as f:
        return f.read()


def write_text(p, text):
    """Write preserving the caller's newlines exactly; see read_text()."""
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(text)


RE_ARABIC = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
RE_LATIN = re.compile(r"[A-Za-z]")
RE_ANY_ALNUM = re.compile(r"[0-9A-Za-z\u0600-\u06FF\u0660-\u0669\u06F0-\u06F9]")
# Android format specifiers: %s %d %1$s %2$d %-5.2f %%
RE_FORMAT = re.compile(r"%(?:(\d+)\$)?[-#+ 0,(]*\d*(?:\.\d+)?[a-zA-Z%]")
RE_BARE_PERCENT = re.compile(r"%(?![%nsdioxXeEfgGaAcbhp]|(?:\d+\$)?[-#+ 0,(]*\d*(?:\.\d+)?[a-zA-Z])")
# Markup tags inside a string value (<b>, <i>, <xliff:g>, ...) — must survive translation.
RE_MARKUP_TAG = re.compile(r"</?([A-Za-z][\w:.]*)")


def _formats(text):
    """Full format specifiers, order-sensitive: %1$s and %s are NOT equal."""
    return [m.group(0) for m in RE_FORMAT.finditer(text)]


def _tags(text):
    return sorted(set(RE_MARKUP_TAG.findall(text)))


RE_SECTION = re.compile(r"%([0-9]+)\$")


def _sections(fmts):
    """Which argument numbers a value reads, with repeats dropped.

    `%1$d/%2$d` and `%2$d/%1$d` are the same set of arguments and both render
    correctly: Android fills each specifier from its own positional index, so a
    translator reordering two numbered placeholders loses nothing. Only an index
    the source never mentions is a defect — that one reads an argument the
    caller did not pass. An unnumbered `%s` beside numbered specifiers is a
    different bug (Android counts it as argument 1 anyway and throws) and keeps
    the old order-sensitive comparison, so the answer there is None.
    """
    seen, out = set(), []
    for f in fmts:
        m = RE_SECTION.match(f)
        if m is None:
            return None
        n = int(m.group(1))
        if n not in seen:
            seen.add(n)
            out.append(n)
    return sorted(out)


# --------------------------------------------------------------------------
# RTL attribute migration tables
# --------------------------------------------------------------------------

# Attribute *name* renames: left->Start, right->End.
# Tags whose `orientation` defaults to horizontal; a row of these with no
# explicit orientation is horizontal all the same.
HORIZONTAL_BY_DEFAULT = ("LinearLayout", "RadioGroup", "TableRow")

# Tags that are never a row, whatever the axis around them: a `TextView` is a
# leaf whose padding belongs to its parent row, and a container that stacks
# everything it holds has no left side of its own. The list is by base name, so
# `androidx.appcompat.widget.AppCompatTextView` is covered too. A
# `HorizontalScrollView` is deliberately absent: it runs horizontally in *every*
# direction, so a left/right inside it still swaps sides in Arabic.
NON_ROW_TAGS = {
    "TextView", "Button", "EditText", "ImageView", "ImageButton", "CheckBox",
    "RadioButton", "Switch", "ProgressBar", "SeekBar", "Space", "View",
    "ScrollView",
}
# The name's own words split by case, so a custom `IconButton` or
# `CaptionTextView` is a leaf as well.
_BASE_WORDS = re.compile(r"[A-Z][a-z0-9]*")


def looks_leaf(tag):
    return bool(NON_ROW_TAGS.intersection(_BASE_WORDS.findall(tag)))

ATTR_RENAME = {
    "layout_marginLeft": "layout_marginStart",
    "layout_marginRight": "layout_marginEnd",
    "paddingLeft": "paddingStart",
    "paddingRight": "paddingEnd",
    "drawableLeft": "drawableStart",
    "drawableRight": "drawableEnd",
    "layout_alignLeft": "layout_alignStart",
    "layout_alignRight": "layout_alignEnd",
    "layout_alignParentLeft": "layout_alignParentStart",
    "layout_alignParentRight": "layout_alignParentEnd",
    "layout_toLeftOf": "layout_toStartOf",
    "layout_toRightOf": "layout_toEndOf",
    "layout_constraintLeft_toLeftOf": "layout_constraintStart_toStartOf",
    "layout_constraintLeft_toRightOf": "layout_constraintStart_toEndOf",
    "layout_constraintRight_toLeftOf": "layout_constraintEnd_toStartOf",
    "layout_constraintRight_toRightOf": "layout_constraintEnd_toEndOf",
    "layout_constraintLeft_creator": "layout_constraintStart_creator",
    "layout_constraintRight_creator": "layout_constraintEnd_creator",
}

# Attributes whose *value* holds gravity tokens: left->start, right->end.
GRAVITY_ATTRS = {"gravity", "layout_gravity", "foregroundGravity"}
# textAlignment uses viewStart/viewEnd, not start/end.
TEXT_ALIGN_ATTRS = {"textAlignment"}

LEFT_TOKEN = {"left": "start", "right": "end"}
# A whole token or a word inside a compound one. `leftShape` and `right_of`
# are not gravity values, but `imageLeft`/`navRight` legitimately are (a custom
# attr holding a layout slot), so a bare word boundary is the wrong test.
GRAVITY_TOKEN = r"(?<![A-Za-z])(left|right)(?![A-Za-z])"
GRAVITY_TOKEN_HIT = re.compile(GRAVITY_TOKEN)
TEXT_ALIGN_TOKEN = {"left": "viewStart", "right": "viewEnd"}

# Theme references that must follow the rename, or `paddingStart` would still
# resolve to the *left* padding in RTL. Curated on purpose: rewriting an
# arbitrary `?fooRight` could point at an attribute that does not exist and
# break the build.
THEME_VALUE_MAP = {
    "listPreferredItemPaddingLeft": "listPreferredItemPaddingStart",
    "listPreferredItemPaddingRight": "listPreferredItemPaddingEnd",
}


def map_theme_refs(val):
    for old, new in THEME_VALUE_MAP.items():
        if old in val:
            val = re.sub(rf"\?((?:android:)?attr/|@\*android:attr/)?{old}\b",
                         lambda g: "?" + (g.group(1) or "") + new, val)
    return val


# An opening element tag, attribute values may contain `>` so quotes are matched.
RE_ELEMENT = re.compile(r"<([A-Za-z_][\w.:-]*)((?:[^<>\"']|\"[^\"]*\"|'[^']*')*?)(/?)>",
                        re.DOTALL)
# Android accepts either quote character on an attribute value and aapt normalises
# both, so a single-quoted `paddingLeft` is the same defect as a double-quoted one.
# Backreference `\2` keeps the value from ending on the wrong quote.
RE_ATTR = re.compile(r"([A-Za-z_][\w.:-]*)\s*=\s*[\x22\x27](.*?)[\x22\x27]", re.DOTALL)

# Cheap pre-filter: a tag can only be migrated if one of the names the tables
# above know about actually appears in it. Almost every tag in a real tree
# (``layout_width``, ``text``, ``id`` …) matches nothing, so this skips both the
# attribute scan and a ``migrate_attr`` call per attribute. Derived from the
# tables, so a new rename is covered automatically and it cannot drift.
RE_MAY_MIGRATE = re.compile("|".join(sorted(
    set(ATTR_RENAME) | GRAVITY_ATTRS | TEXT_ALIGN_ATTRS | set(THEME_VALUE_MAP))))

# Language qualifiers only. `res/` is full of other qualifiers — values-ldrtl,
# values-night, values-h480dp, values-sw600dp, values-v31, values-w320dp-land,
# values-watch — and reporting those as locales buries the real ones.
# `b+sr+Latn` is the BCP 47 form aapt writes for the newer syntax.
RE_LOCALE_Q = re.compile(r"^(?:b\+[a-z0-9+]+|[a-z]{2,3}(?:-r?[A-Z]{2}|-[A-Z][a-z]{3})?)$")
# 2–3 lowercase letters is also how a few *non*-locale qualifiers are spelled.
NON_LOCALE_QUALIFIERS = {"car", "any"}


def migrate_attr(name, value):
    """Return (new_name, new_value) for an RTL migration, or None if untouched."""
    ns, sep, local = name.rpartition(":")
    if ns == "tools":
        return None
    new_local, new_val = local, value
    if local in ATTR_RENAME:
        new_local = ATTR_RENAME[local]
    elif local in GRAVITY_ATTRS:
        new_val = re.sub(GRAVITY_TOKEN, lambda g: LEFT_TOKEN[g.group(1)], value)
    elif local in TEXT_ALIGN_ATTRS:
        new_val = re.sub(GRAVITY_TOKEN, lambda g: TEXT_ALIGN_TOKEN[g.group(1)], value)
    new_val = map_theme_refs(new_val)
    if new_local == local and new_val == value:
        return None
    return ((ns + sep + new_local) if sep else new_local), new_val


def rewrite_xml_attrs(text):
    """Migrate left/right attributes inside every element tag.

    A renamed attribute is *dropped* when the same tag already declares its
    start/end twin — Android theme overlays routinely carry both, and emitting
    both makes aapt2 fail with `duplicate attribute`."""
    total = [0]

    def do_tag(m):
        attrs = m.group(2)
        if not RE_MAY_MIGRATE.search(attrs):
            return m.group(0)
        head, tail = m.group(1), m.group(3)
        matches = list(RE_ATTR.finditer(attrs))
        if not matches:
            return m.group(0)
        planned = [(a, migrate_attr(a[1], a[2])) for a in matches]
        reserved = {a[1] for a, mig in planned if mig is None}
        edits = []
        for a, mig in planned:
            if mig is None:
                continue
            if mig[0] in reserved:
                start = a.start()
                while start > 0 and attrs[start - 1] in " \t\r\n":
                    start -= 1
                edits.append((start, a.end(), ""))
            else:
                edits.append((a.start(), a.end(), f'{mig[0]}="{mig[1]}"'))
        if not edits:
            return m.group(0)
        new_attrs = attrs
        for s, e, rep in reversed(edits):
            new_attrs = new_attrs[:s] + rep + new_attrs[e:]
        total[0] += len(edits)
        return f"<{head}{new_attrs}{tail}>"

    return RE_ELEMENT.sub(do_tag, text), total[0]


# Style bodies carry the direction in a *value*, not an attribute name:
#   <item name="android:paddingLeft">12dp</item>
#   <item name="android:gravity">left</item>
RE_STYLE_BLOCK = re.compile(r"<style\b[^>]*>(.*?)</style>", re.DOTALL)
RE_ITEM_FULL = re.compile(r"<item\b(?P<attrs>[^>]*?)>(?P<body>.*?)</item>", re.DOTALL)


def rewrite_styles(text):
    """Migrate <item name="…Left/Right"> inside <style> blocks, skipping any
    rename whose target is already declared in the same style."""
    total = [0]

    def do_style(sm):
        body = sm.group(1)
        items = list(RE_ITEM_FULL.finditer(body))
        if not items:
            return sm.group(0)
        names, plans = set(), []
        for it in items:
            nm = RE_ATTR_NAME.search(it.group("attrs"))
            if not nm:
                continue
            names.add(nm.group(1))
            plans.append((it, nm, nm.group(1)))

        edits = []
        for it, nm, name in plans:
            ns, sep, local = name.rpartition(":")
            target = ATTR_RENAME.get(local) if sep else None
            new_name = f"{ns}{sep}{target}" if target else None
            if new_name and new_name not in names:
                base = it.start("attrs")
                edits.append((base + nm.start(1), base + nm.end(1), new_name))
            elif new_name:
                edits.append((it.start(), it.end(), ""))  # twin already declared
            elif local in GRAVITY_ATTRS:
                new_body = re.sub(GRAVITY_TOKEN,
                                  lambda g: LEFT_TOKEN[g.group(1)], it.group("body"))
                if new_body != it.group("body"):
                    edits.append((it.start("body"), it.end("body"), new_body))
        if not edits:
            return sm.group(0)
        new_body = body
        for s, e, rep in reversed(edits):  # already relative to `body`
            new_body = new_body[:s] + rep + new_body[e:]
        total[0] += len(edits)
        return sm.group(0).replace(sm.group(1), new_body, 1)

    return RE_STYLE_BLOCK.sub(do_style, text), total[0]

# --------------------------------------------------------------------------
# Process / tooling helpers
# --------------------------------------------------------------------------


class Fail(SystemExit):
    def __init__(self, msg):
        super().__init__(f"aqlab: {msg}")


def run(cmd, quiet=False, check=True):
    if not quiet:
        print("  $ " + " ".join(str(c) for c in cmd), file=sys.stderr)
    p = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if check and p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip().splitlines()
        raise Fail(f"{Path(str(cmd[0])).name} failed ({p.returncode}): " + " | ".join(tail[-4:]))
    return p


def _first_of(*cands):
    for c in cands:
        if not c:
            continue
        if Path(c).exists() or shutil.which(str(c)):
            return str(c)
    return None


def sdk_build_tools():
    """Locate the newest Android build-tools dir (aapt2/zipalign/apksigner)."""
    roots = [
        os.environ.get("AQLAB_SDK"),
        os.environ.get("ANDROID_HOME"),
        os.environ.get("ANDROID_SDK_ROOT"),
        Path.home() / "android-sdk",
        Path.home() / "AppData/Local/Android/Sdk",
        Path("/usr/lib/android-sdk"),
    ]
    best = None
    for r in roots:
        if not r:
            continue
        bt = Path(r) / "build-tools"
        if bt.is_dir():
            for d in sorted(bt.iterdir()):
                if d.is_dir() and (best is None or d.name > best.name):
                    best = d
    return best


_BT = None


def tool(name):
    """Resolve an SDK tool by name, honouring AQLAB_<TOOL> overrides."""
    global _BT
    env = os.environ.get("AQLAB_" + name.upper())
    if env and Path(env).exists():
        return env
    found = shutil.which(name)
    if found:
        return found
    if _BT is None:
        _BT = sdk_build_tools()
    if _BT:
        for cand in (_BT / name, _BT / (name + ".exe"), _BT / (name + ".bat")):
            if cand.exists():
                return str(cand)
    raise Fail(f"cannot find `{name}` — set AQLAB_{name.upper()} or ANDROID_HOME")


def apktool_cmd():
    env = os.environ.get("AQLAB_APKTOOL")
    jar = _first_of(env, Path.home() / "apktool.jar", "apktool.jar")
    java = _first_of(os.environ.get("AQLAB_JAVA"), "java")
    if jar and jar.endswith(".jar"):
        if not java:
            raise Fail("apktool.jar found but no `java` on PATH")
        return [java, "-jar", jar]
    exe = shutil.which("apktool")
    if exe:
        return [exe]
    raise Fail("cannot find apktool — set AQLAB_APKTOOL=/path/to/apktool.jar")


# --------------------------------------------------------------------------
# Resource parsing — regex, in-place, no XML round-trip
# (Android resource XML is not safely round-trippable through a generic XML
#  writer: escaping, CDATA and format specifiers all get mangled.)
# --------------------------------------------------------------------------

RE_STRING = re.compile(r"<string\b(?P<attrs>[^>]*?)>(?P<body>.*?)</string>", re.DOTALL)
RE_PLURALS = re.compile(r"<plurals\b(?P<attrs>[^>]*?)>(?P<body>.*?)</plurals>", re.DOTALL)
RE_ITEM = re.compile(r"<item\b(?P<attrs>[^>]*?)>(?P<body>.*?)</item>", re.DOTALL)
RE_ATTR_NAME = re.compile(r'\bname\s*=\s*"([^"]+)"')
RE_ATTR_QTY = re.compile(r'\bquantity\s*=\s*"([^"]+)"')
RE_ATTR_TRANSLATABLE = re.compile(r'\btranslatable\s*=\s*"false"')
RE_CDATA = re.compile(r"^\s*<!\[CDATA\[(.*?)\]\]>\s*$", re.DOTALL)

# Arabic is one of the few languages that uses all six CLDR plural categories.
# The two that cover the most counts — `few` (3-10) and `many` (11-99) — are
# exactly the two an English source cannot supply, so an Arabic <plurals> copied
# from English builds fine and reads wrong for almost every number.
def _base(locale):
    return (locale or "").replace("_", "-").split("-")[0].lower()


def _plural_locale(locale):
    """The `PLURAL_CATEGORIES` key for a locale, or "" when CLDR has no row.

    Two different things were collapsed into `_base`. `pt-PT` has a `many`
    category that Brazil's `pt` does not, and `pt_PT` is the exact string Android
    writes as the resource qualifier — so the full tag has to be tried *before*
    the region is stripped, or a European Portuguese app gets the Brazilian table
    and is never warned that its `many` form is missing. Script subtags are kept
    for the same reason: CLDR keys `kok_Latn` as its own row.

    `_base` also lowercased, which made `b+sr+Latn+RS` resolve to nothing;
    Android writes a script+region qualifier that way, so it is split properly
    and then goes down the same exact -> language_SCRIPT -> language chain.
    """
    tag = (locale or "").strip()
    if not tag:
        return ""
    if tag.startswith("b+"):
        tag = "_".join(tag.split("+")[1:])
    cand = tag.replace("-", "_")
    parts = cand.split("_")
    for n in (len(parts), min(2, len(parts)), 1):
        probe = "_".join(parts[:n])
        if probe in PLURAL_CATEGORIES:
            return probe
        low = probe.lower()
        if low in PLURAL_CATEGORIES:
            return low
    return ""


ARABIC_PLURAL_CATEGORIES = ("zero", "one", "two", "few", "many", "other")
PLURAL_RANGE = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "few": "3-10",
    "many": "11-99",
    "other": "100, 101, 102 …",
}
# `many` means different counts per language — Arabic 11-99, Hebrew 20/30/40… —
# so the hint in a message has to come from the locale, not from Arabic.
# A locale missing from this table is not linted for plurals at all, so the
# table is a set of hints, not a gate — adding a language can only add checks.
PLURAL_RANGE_BY_LOCALE = {
    "ar": {"zero": "0", "one": "1", "two": "2", "few": "3-10", "many": "11-99",
           "other": "100, 101, 102 …"},
    "he": {"one": "1", "two": "2", "other": "everything else"},
    "fa": {"one": "0, 1", "other": "everything else"},
    # Slavic: `few` is the last digit (2-4), `many` everything else
    "ru": {"one": "1, 21, 31, 101 …", "few": "2-4, 22-24, 102-104 …",
           "many": "0, 5-20, 25-30 …", "other": "fractions"},
    "uk": {"one": "1, 21, 31, 101 …", "few": "2-4, 22-24, 102-104 …",
           "many": "0, 5-20, 25-30 …", "other": "fractions"},
    "pl": {"one": "1", "few": "2-4, 22-24, 102-104 …",
           "many": "0, 5-21, 25-31 …", "other": "fractions"},
    "ro": {"one": "1", "few": "0, 2-19, 101-119 …", "other": "20-100, 120 …"},
    "lv": {"zero": "0, 10-20, 30, 40 …", "one": "1, 21, 31, 101 …",
           "other": "2-9, 22-29 …"},
    "cy": {"zero": "0", "one": "1", "two": "2", "few": "3",
           "many": "6", "other": "everything else"},
}


def plural_range(locale, cat):
    """Count range hint for a category, in the target locale's own terms."""
    return PLURAL_RANGE_BY_LOCALE.get(_plural_locale(locale), PLURAL_RANGE).get(cat, PLURAL_RANGE.get(cat, "?"))

# Every CLDR plural row, grouped by category tuple. Generated from CLDR release-48
# `supplemental/plurals.json` — the same file ICU reads — by
# `tools/cldr_tables.py`, which is re-run by `tests/test_aqlab.py` against a
# checked-in copy of that file, so a hand edit or a stale row fails a check
# instead of shipping. Indexed by tuple: one group is one row here, and a
# language missing from every group is not linted for plurals at all.
PLURAL_CATEGORIES_BY_KIND = {
    ('zero', 'one', 'two', 'few', 'many', 'other'): ('ar', 'ars', 'cy', 'kw',),
    ('one', 'two', 'few', 'many', 'other'): ('br', 'ga', 'gv', 'mt', 'sgs',),
    ('one', 'few', 'many', 'other'): ('be', 'cs', 'lt', 'pl', 'ru', 'sk', 'uk',),
    ('one', 'two', 'few', 'other'): ('dsb', 'gd', 'hsb', 'sl',),
    ('zero', 'one', 'other'): ('blo', 'cv', 'ksh', 'lag', 'lv', 'prg',),
    ('one', 'few', 'other'): ('bs', 'hr', 'mo', 'ro', 'sh', 'shi', 'sr',),
    ('one', 'many', 'other'): ('ca', 'es', 'fr', 'gl', 'it', 'lld', 'pt', 'pt_PT', 'scn', 'vec',),
    ('one', 'two', 'other'): ('he', 'iu', 'naq', 'sat', 'se', 'sma', 'smi', 'smj', 'smn', 'sms',),
    ('one', 'other'): ('af', 'ak', 'am', 'an', 'as', 'asa', 'ast', 'az', 'bal', 'bem', 'bez', 'bg', 'bho', 'bn', 'brx', 'ce', 'ceb', 'cgg', 'chr', 'ckb', 'csw', 'da', 'de', 'doi', 'dv', 'ee', 'el', 'en', 'eo', 'et', 'eu', 'fa', 'ff', 'fi', 'fil', 'fo', 'fur', 'fy', 'gsw', 'gu', 'guw', 'ha', 'haw', 'hi', 'hu', 'hy', 'ia', 'ie', 'io', 'is', 'jgo', 'jmc', 'ka', 'kab', 'kaj', 'kcg', 'kk', 'kkj', 'kl', 'kn', 'kok', 'kok_Latn', 'ks', 'ksb', 'ku', 'ky', 'lb', 'lg', 'lij', 'ln', 'mas', 'mg', 'mgo', 'mk', 'ml', 'mn', 'mr', 'nah', 'nb', 'nd', 'ne', 'nl', 'nn', 'nnh', 'no', 'nr', 'nso', 'ny', 'nyn', 'om', 'or', 'os', 'pa', 'pap', 'pcm', 'ps', 'rm', 'rof', 'rwk', 'saq', 'sc', 'sd', 'sdh', 'seh', 'si', 'sn', 'so', 'sq', 'ss', 'ssy', 'st', 'sv', 'sw', 'syr', 'ta', 'te', 'teo', 'tg', 'ti', 'tig', 'tk', 'tl', 'tn', 'tr', 'ts', 'tzm', 'ug', 'ur', 'uz', 've', 'vi', 'vo', 'vun', 'wa', 'wae', 'xh', 'xog', 'yi', 'zu',),
    ('other',): ('bm', 'bo', 'dz', 'hnj', 'id', 'ig', 'ii', 'ja', 'jbo', 'jv', 'jw', 'kde', 'kea', 'km', 'ko', 'lkt', 'lo', 'ms', 'my', 'nqo', 'osa', 'sah', 'ses', 'sg', 'su', 'th', 'to', 'tpi', 'wo', 'yo', 'yue', 'zh',),
}
# Android's resource qualifiers still spell four languages the way CLDR no longer
# does (`in`/`id`, `iw`/`he`, `ji`/`yi`, `jw`/`jv`), and Android's own `b+` tags
# use the modern one, so both have to resolve. The legacy alias is never in the
# CLDR file, so it cannot be generated from it.
# Android resource qualifiers and ICU's newer names both resolve: CLDR spells
# `he` as `he` and Android's folders say `iw` (`in`/`id`, `ji`/`yi`, `jw`/`jv`).
PLURAL_CATEGORIES = {k: t for t, ks in PLURAL_CATEGORIES_BY_KIND.items() for k in ks}
for _alias, _now in (("in", "id"), ("iw", "he"), ("ji", "yi"), ("jw", "jv"),
                     ("tl", "fil")):
    PLURAL_CATEGORIES[_alias] = PLURAL_CATEGORIES[_now]
del _alias, _now

# The categories that carry the most counts, so missing ones are errors: for
# Arabic `few` (3-10) and `many` (11-99); for Hebrew `many` (20, 30, …). `other`
# always covers everything the named categories do not, so it is always required.
# Derived, not hand-listed: take `other` plus the two multi-count categories.
_MOST_COUNTS = ("few", "many", "one", "two", "zero")
PLURAL_REQUIRED = {
    loc: tuple(c for c in ("other",) + _MOST_COUNTS if c in cats)[:3]
    for loc, cats in PLURAL_CATEGORIES.items()
}
PLURAL_OPTIONAL = {
    loc: tuple(c for c in cats if c not in PLURAL_REQUIRED[loc])
    for loc, cats in PLURAL_CATEGORIES.items()
}
# Languages that read right-to-left, generated from CLDR release-48 on
# 2026-11-08: every language whose `likelySubtags` script is marked RTL in
# `scriptMetadata.txt` (Bidi_Class of its letters) — 258 of them, against the ten
# this list used to hold. The ones that mattered were not exotic: `sdh`/`lrc`/
# `glk`/`mzn`/`azb`/`bal`/`ks` are Kurdish and Iranian languages whose own
# speakers ship localised Android apps, and `nqo` (N'Ko) is a whole right-to-left
# script Latin-adjacent readers never think of.
#
# 258 entries is bloat only if `is_rtl` were a display hint. It decides whether a
# project that has *one* of these folders is treated as right-to-left at all, and
# a missing entry is a whole locale of reversed-direction layouts nobody gets told
# about. Generated rather than typed, by `tools/cldr_tables.py` from the
# checked-in `tests/fixtures/cldr/rtl.json`; `tests/test_aqlab.py` rebuilds it.
RTL_LOCALES = frozenset((
    "aao", "abh", "abv", "acm", "acq", "acw", "acx", "adf", "ae", "aeb", "aec", "aee", "aeq", "afb", "aib", "aii", "aij", "aiq", "amw", "apc", "apd", "ar", "arc", "arq", "ars", "ary", "arz", "ask", "atn", "auj", "auz", "avd", "avl", "ayh", "ayl", "ayn", "ayp", "azb", "bal", "bdz", "bej", "bft", "bgn", "bgp", "bhe", "bhm", "bhn", "bjf", "bjm", "bqi", "brh", "brk", "bsh", "bsk", "chg", "cja", "ckb", "clh", "czk", "dcc", "def", "deh", "dgl", "dmk", "dml", "dv", "ecy", "esh", "fa", "fay", "faz", "fia", "fub", "gbz", "ggg", "gha", "ghr", "gig", "gjk", "gju", "glh", "glk", "grr", "gwc", "gwf", "gwt", "gzi", "hac", "haz", "hbo", "he", "hkh", "hnd", "hno", "hoh", "hrt", "hrz", "hss", "huy", "isk", "itk", "iw", "jad", "jat", "jbe", "jbn", "jdg", "ji", "jnd", "jog", "jpa", "jpr", "jrb", "jye", "kbu", "kby", "kcy", "kfm", "khw", "klj", "kmz", "kqd", "ks", "ktl", "kvx", "kxp", "lad", "lah", "lhs", "lki", "lrc", "lrk", "lrl", "lsa", "lsd", "lss", "luv", "luz", "mby", "mde", "mey", "mfa", "mfi", "mhj", "mid", "mki", "mnj", "mve", "mvy", "myz", "mzb", "mzn", "nli", "nlm", "nqo", "ntz", "nyq", "oar", "obm", "odk", "oru", "ota", "otk", "oui", "pal", "pbt", "pgd", "phl", "phn", "phr", "phv", "plk", "prc", "prd", "prx", "ps", "psh", "psi", "pst", "qxq", "rdb", "rhg", "rmt", "sam", "sbn", "scl", "sd", "sdb", "sdf", "sdg", "sdh", "sds", "sgr", "sgy", "shd", "shm", "shu", "shv", "siy", "siz", "skr", "smp", "smy", "sog", "sqo", "sqt", "srh", "srz", "ssh", "sts", "swb", "syc", "syn", "syr", "tjo", "tks", "tmr", "tov", "tra", "trg", "trm", "trw", "ug", "ur", "ush", "uzs", "vaf", "vgr", "vmh", "wbk", "wlo", "wne", "wni", "wsv", "xco", "xhe", "xka", "xkc", "xkj", "xkp", "xld", "xly", "xmn", "xmr", "xna", "xpr", "xsa", "xsd", "xvi", "ydg", "yhd", "yi", "yih", "yud", "zba", "zdj", "zrp", "zum",
))



def plural_categories(locale):
    """CLDR categories for a locale, empty when it has no distinct plurals.

    A base tag (`ar-rSA`, `he`) resolves on the language subtag; `pt-BR` style
    only matters for languages that differ by region, and none of the RTL ones do.
    """
    return PLURAL_CATEGORIES.get(_plural_locale(locale), ())


def plural_required(locale):
    return PLURAL_REQUIRED.get(_plural_locale(locale), ())


def plural_optional(locale):
    return PLURAL_OPTIONAL.get(_plural_locale(locale), ())


def is_rtl(locale):
    # `_base`, not `_plural_locale`: the latter only knows tags CLDR has a
    # plural row for, and most RTL languages have none (lrc, glk, mzn) —
    # their direction is a script fact, not a plural one.
    return _base(locale) in RTL_LOCALES


class Entry:
    __slots__ = ("key", "kind", "attrs", "body", "translatable", "src")

    def __init__(self, key, kind, attrs, body, translatable, src=None):
        self.key, self.kind, self.attrs = key, kind, attrs
        self.body, self.translatable = body, translatable
        # Basename of the file it came from, so a translation can be written back
        # to the same file the source keeps it in rather than all of them landing
        # in strings.xml — two files declaring the same <plurals name> in one
        # locale is a duplicate-resource error.
        self.src = src

    @property
    def text(self):
        m = RE_CDATA.match(self.body)
        return m.group(1) if m else self.body


# `<TextView android:text="OK" ...>`: the tag the attribute sits on, the
# attribute, and its quoted value. A literal newline is escaped in smali-built
# XML in practice, so single-line matching is enough and stays cheap.
RE_LITERAL_TEXT = re.compile(
    r'<([A-Za-z_][\w.]*)\b[^>]*?\b(?:android:)?'
    r'(text|hint|title|summary|subtitle|contentDescription|label|tabText)\s*=\s*'
    r'"([^"]*)"')
RE_REF_NAME = re.compile(r"@string/([A-Za-z0-9_.]+)")
RE_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)


def read_entries(path):
    """Extract <string> and <plurals><item> entries with their source text."""
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    name = Path(path).name
    out = []
    for m in RE_STRING.finditer(raw):
        nm = RE_ATTR_NAME.search(m.group("attrs"))
        if nm:
            out.append(Entry(nm.group(1), "string", m.group("attrs"), m.group("body"),
                             not RE_ATTR_TRANSLATABLE.search(m.group("attrs")), name))
    for blk in RE_PLURALS.finditer(raw):
        nm = RE_ATTR_NAME.search(blk.group("attrs"))
        if not nm:
            continue
        for it in RE_ITEM.finditer(blk.group("body")):
            q = RE_ATTR_QTY.search(it.group("attrs"))
            if q:
                out.append(Entry(f"{nm.group(1)}|{q.group(1)}", "plural",
                                 it.group("attrs"), it.group("body"), True, name))
    return out


def split_by_file(entries):
    """Group entries by the source file they came from, preserving order."""
    groups = {}
    for e in entries:
        groups.setdefault(e.src or "strings.xml", []).append(e)
    return groups


def plural_rows_for(entries, locale):
    """Entries plus a TSV row for every plural category the target locale needs
    but the source cannot supply.

    Arabic uses six categories and an English source has two, so rows for `few`
    (3-10), `many` (11-99) and the rest have to exist somewhere or the translator
    has nowhere to put them and `apply` has nothing to read back. Each plural is
    emitted as all six categories in CLDR order, carrying the source text where
    the source has it and left blank where it cannot — which is also the shape the
    finished file will take.
    """
    cats = plural_categories(locale)
    if not cats:
        return entries
    by_name = {}
    for e in entries:
        if e.kind == "plural":
            by_name.setdefault(e.key.split("|", 1)[0], {})[e.key.rpartition("|")[2]] = e
    if not by_name:
        return entries
    out, emitted = [], set()
    for e in entries:
        if e.kind != "plural":
            out.append(e)
            continue
        name = e.key.split("|", 1)[0]
        if name in emitted:
            continue
        emitted.add(name)
        got = by_name[name]
        for cat in cats:
            out.append(got.get(cat)
                       or Entry(f"{name}|{cat}", "plural", e.attrs, "", True, e.src))
    return out


def read_locale_entries(res, folders):
    """Every <string>/<plurals> entry across the given res folders.

    Callers must read the whole folder rather than one file: plurals routinely
    live in their own `plurals.xml` (AOSP and most Material apps ship them that
    way), so `strings.xml` alone quietly omits the entries a plurals check needs.
    """
    out = []
    for folder in folders:
        d = Path(res) / folder
        if d.is_dir():
            for f in sorted(d.glob("*.xml")):
                out.extend(read_entries(f))
    return out


def existing_translations(res, locale):
    """`key -> text` for what is already translated in `values-<locale>`.

    `apply` writes whole files, so without this every key absent from the TSV
    column would be dropped from the locale — a half-filled table silently
    deleted the completed translations beside it. Plurals come back as
    `name|quantity`, the same key shape the TSV uses.
    """
    d = Path(res) / f"values-{locale}"
    if not d.is_dir():
        return {}
    out = {}
    for f in sorted(d.glob("*.xml")):
        for e in read_entries(f):
            if e.kind == "string":
                out[e.key] = e.text
            elif e.kind == "plural":
                name, _, qty = e.key.partition("|")
                out[f"{name}|{qty}"] = e.text
    return out


def find_default_strings(proj):
    """Best default-locale strings.xml inside a decoded project."""
    res = Path(proj) / "res"
    for name in ("values/strings.xml", "values-en/strings.xml"):
        p = res / name
        if p.exists():
            return p
    # A fallback to *any* locale is worse than no answer: with only
    # `values-ar/strings.xml` present, lint read the Arabic file as its own
    # source, reported 100% translated and 0 findings, and a caller had no way
    # to tell that from a clean project. The default locale is the thing lint
    # compares against, so an absent one is `None` — `cmd_lint` raises for it.
    return None


# --------------------------------------------------------------------------
# Arabic-aware escaping
# --------------------------------------------------------------------------


def _keeps_markup():
    """Tag names that must survive as markup instead of being escaped to text."""
    return ("b", "i", "u", "xliff:g", "a", "annotation", "font", "tt", "big", "small",
            "sup", "sub", "strike", "br")


def escape_android(text):
    """Escape a translated string for a values-ar resource file.

    Markup Android itself understands (`<b>`, `<xliff:g id="…">`, `<a href>`)
    is kept as markup: escaping it makes the phone print the tag instead of
    applying it, and a translator's `<xliff:g>` is there to protect an argument
    from being translated at all.
    """
    stash = []

    def hold(m):
        stash.append(m.group(0))
        return f"\x00{len(stash) - 1}\x00"

    keep = "|".join(_keeps_markup())
    t = re.sub(rf"</?(?:{keep})(?:\s[^>]*)?/?>", hold, text)
    t = re.sub(r"&#(\d+);|&#x([0-9a-fA-F]+);|&(amp|lt|gt|quot|apos);",
               lambda m: hold(m), t)

    t = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    # a real newline is a line break in the resource; the XML writer must not
    # keep it as whitespace, where Android collapses it
    t = t.replace("\n", "\\n").replace("\t", "\\t")
    t = t.replace("'", "\\'").replace('"', '\\"')
    if t[:1] in ("@", "?"):
        t = "\\" + t
    if t != t.strip():  # Android trims; make intentional spaces explicit
        lead = " ".join("\\u0020" for _ in re.match(r"\s*", t).group(0))
        trail = " ".join("\\u0020" for _ in re.search(r"\s*$", t).group(0))
        t = lead + t.strip() + trail
    for i, s in enumerate(stash):
        t = t.replace(f"\x00{i}\x00", s)
    return t


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def apk_layout(path):
    """What a container actually holds: `apk`, `xapk` (zip of APKs) or `aab`.

    A split build ships a base APK plus `split_config.<abi>.apk` and
    `split_config.<locale>.apk`. Decoding one of those silently produces a
    project whose build fails or, worse, a base-APK-only decode that looks
    complete and is missing Arabic because the strings live in a locale split.
    So the shape is always classified, and `decode` refuses an ambiguous one
    rather than picking."""
    p = Path(path)
    try:
        with zipfile.ZipFile(p) as z:
            names = z.namelist()
    except (zipfile.BadZipFile, OSError) as e:
        raise Fail(f"{p.name} is not a zip/apk: {e}")
    if "BundleConfig.pb" in names or "base/manifest/AndroidManifest.xml" in names:
        return "aab"
    if "AndroidManifest.xml" in names:
        return "apk"
    inner = [n for n in names if n.lower().endswith(".apk")]
    if inner:
        return "xapk"
    return "unknown"


def split_parts(path, kind=None):
    """[(label, display_name)] of the APKs in an XAPK, base first."""
    with zipfile.ZipFile(path) as z:
        inner = [n for n in z.namelist() if n.lower().endswith(".apk")]
    def rank(n):
        # The directory prefix is ignored on purpose: an XAPK may keep its base
        # at the root and the splits in `splits/`, and a raw string sort would
        # then put `splits/...` first and hand the base to stage two last.
        base = Path(n).name.lower()
        is_split = "split" in base
        return (0 if not is_split else 1, base, n.lower())
    out = []
    for n in sorted(inner, key=rank):
        base = Path(n).name
        m = re.match(r"split_config\.([A-Za-z0-9_\-]+)\.apk$", base)
        # The label is the qualifier alone, so `--split ar` is enough to name a
        # locale part without spelling out the whole filename.
        if base.startswith("split_config") and m:
            label = "split_config." + m.group(1)
        else:
            label = base if base.startswith("split") else "base"
        out.append((label, n))
    return out


def extract_split(path, member, out):
    """Pull one APK out of an XAPK container to a real file."""
    with zipfile.ZipFile(path) as z, z.open(member) as src, open(out, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return out


def decode_one(apk, out, args):
    """apktool decode one APK into one project dir."""
    ap = apktool_cmd() + ["d", "-f", "--no-debug-info"]
    if not getattr(args, "with_sources", False):
        ap.append("-s")
    if getattr(args, "framework", None):
        ap += ["-p", args.framework]
    run(ap + [apk, "-o", out], quiet=getattr(args, "quiet", False))


def cmd_decode(args):
    out = args.out or (Path(args.apk).stem + "-decoded")
    kind = apk_layout(args.apk)
    if kind == "aab":
        exe = shutil.which("bundletool") or os.environ.get("AQLAB_BUNDLETOOL")
        if not exe or not Path(exe).exists():
            raise Fail(
                "this is an AAB (app bundle), not an installable APK - apktool cannot "
                "open it. Convert it first: bundletool build-apks --bundle=<aab> "
                "--output=<x.apks> --mode=universal, then decode the extracted "
                "universal.apk. (install bundletool or set AQLAB_BUNDLETOOL to point at it)")
        run([exe, "build-apks", f"--bundle={args.apk}",
             f"--output={out}.apks", "--mode=universal", "--overwrite"],
            quiet=args.quiet)
        raise Fail(f"converted to {out}.apks - decode its universal.apk next")
    if kind == "xapk":
        parts = split_parts(args.apk)
        if args.split:
            want = [p for p in parts if args.split in p[0] or p[1] == args.split]
            if not want:
                raise Fail(f"no split matching '{args.split}' - has: "
                           + ", ".join(l for l, _ in parts))
            label, member = want[0]
            tmp = Path(out).with_suffix(".part.apk")
            extract_split(args.apk, member, tmp)
            try:
                decode_one(str(tmp), out, args)
            finally:
                # the extracted split is our copy of the caller's APK: leaving
                # it behind doubles the input size next to the project.
                tmp.unlink(missing_ok=True)
            print(f"[DONE] decoded split '{label}' -> {out}")
            return 0
        # The default is every split, each into its own dir: a split APK is not
        # a full app and no single one of them is a correct "the app" answer.
        base_out = Path(out)
        made = []
        for label, member in parts:
            if label == "base":
                sub = base_out
            elif len(parts) == 1:
                # A lone part is the whole of this container (the common
                # config-split dump), so caller naming one dir must win.
                sub = base_out
            else:
                sub = base_out.parent / f"{base_out.name}-{label}"
            tmp = base_out.parent / f".{label}.apk"
            extract_split(args.apk, member, tmp)
            decode_one(str(tmp), sub, args)
            tmp.unlink(missing_ok=True)
            made.append(f"{label} -> {sub}")
        print(f"[DONE] XAPK with {len(made)} part(s):")
        for line in made:
            print("  " + line)
        return 0
    if kind != "apk":
        raise Fail(f"{Path(args.apk).name} is neither an APK, an XAPK nor an AAB")
    decode_one(args.apk, out, args)
    print(f"[DONE] decoded -> {out}")
    return 0


def manifest_min_sdk(proj):
    """minSdkVersion from a decoded project, or None when absent/unresolvable.
    Prefers the manifest; falls back to apktool.yml, which carries the resolved
    value for APKs whose manifest declares no <uses-sdk> element."""
    man = Path(proj) / "AndroidManifest.xml"
    if man.exists():
        txt = man.read_bytes().decode("utf-8", "replace")
        m = re.search(r'minSdkVersion\s*=\s*"([^"]+)"', txt)
        if m and not m.group(1).startswith("@"):
            try:
                return int(m.group(1))
            except ValueError:
                pass
    yml = Path(proj) / "apktool.yml"
    if yml.exists():
        m = re.search(r"minSdkVersion:\s*'?(\d+)'?", yml.read_text(encoding="utf-8", errors="replace"))
        if m:
            return int(m.group(1))
    return None


def patch_rtl(proj, dry=False, quiet=False, allow_old_sdk=False):
    """Manifest supportsRtl + left/right -> start/end across res/**."""
    changes = []
    res = Path(proj) / "res"
    manifest = Path(proj) / "AndroidManifest.xml"

    # A wrong path must fail loudly. Reporting "already RTL-clean" for a directory
    # that does not exist is the worst possible answer.
    if not Path(proj).is_dir():
        raise Fail(f"{proj}: not a directory — pass the project dir from `aqlab decode`")
    if not res.is_dir():
        raise Fail(f"{proj}: no res/ directory — is this a decoded project? "
                   f"Run `aqlab decode <apk> -o <dir>` first.")

    # 0. start/end resources only exist from API 17 — fail loudly rather than
    #    letting the user discover it from an aapt2 error 60 seconds later.
    msk = manifest_min_sdk(proj)
    if msk is not None and msk < 17 and not allow_old_sdk:
        raise Fail(f"minSdkVersion={msk}: the start/end attributes need API 17. "
                   f"Raise minSdk to 17+, or pass --force-min-sdk to migrate anyway "
                   f"(the build will fail unless minSdk is raised too).")

    # 1. android:supportsRtl="true" on <application>
    if manifest.exists():
        txt = manifest.read_bytes().decode("utf-8", "replace")
        m = re.search(r"<application\b[^>]*>", txt, re.DOTALL)
        if m:
            tag = m.group(0)
            if "supportsRtl" in tag:
                new = re.sub(r'(supportsRtl\s*=\s*)"[^"]*"', r'\1"true"', tag)
            elif tag.endswith("/>"):
                # <application .../> has to keep its slash at the end — inserting
                # before it is the difference between a valid manifest and one
                # that fails to parse.
                new = tag[:-2].rstrip() + '\n        android:supportsRtl="true"/>'
            else:
                new = tag[:-1].rstrip() + '\n        android:supportsRtl="true">'
            if new != tag:
                txt = txt[:m.start()] + new + txt[m.end():]
                changes.append("AndroidManifest.xml: android:supportsRtl=\"true\"")
                if not dry:
                    manifest.write_bytes(txt.encode("utf-8"))

    # 2. attribute migration in layout/style/value XML
    targets = []
    for pat in ("layout*", "values*", "menu*", "drawable*", "anim*"):
        targets.extend(res.glob(f"{pat}/*.xml"))

    for f in sorted(set(targets)):
        try:
            txt = f.read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        new, n = rewrite_xml_attrs(txt)
        if f.parent.name.startswith("values"):
            new, n2 = rewrite_styles(new)
            n += n2
        if new != txt:
            changes.append(f"{f.relative_to(proj).as_posix()}: {n} attribute(s)")
            if not dry:
                f.write_bytes(new.encode("utf-8"))
    return changes


def cmd_rtl(args):
    changes = patch_rtl(args.proj, dry=args.dry_run, quiet=args.quiet,
                        allow_old_sdk=getattr(args, "force_min_sdk", False))
    if not changes:
        print("[FINDING] already RTL-clean — nothing to migrate")
        return 0
    for c in changes:
        print(("  dry: " if args.dry_run else "  [PATCH] ") + c)
    print(f"[{'DRY' if args.dry_run else 'DONE'}] {len(changes)} file(s) updated")
    return 0


def translations_from_tsv(path):
    """Read translation rows. Accepts 2-col `key<TAB>translation` (the format
    already used by hand-rolled workflows) and 3-col `key<TAB>source<TAB>translation`.
    Blank values and `#` comments are skipped.

    A `\\n` written in a cell means a newline in the resource — the table is
    line-based, so the escape is the only way to carry one.
    """
    out = {}
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        key = parts[0].strip()
        val = parts[-1].replace("\\n", "\n").replace("\\t", "\t")
        if key and val.strip():
            out[key] = val
    return out


def _attr_pairs(attrs):
    """`a="1" b="2"` -> [("a","1"), ("b","2")], keeping the source's own values.

    `formatted="false"` must be carried into the translated file unchanged: it
    tells aapt the value has no format specifiers, and without it a translated
    `100%` is a compile error. `name` is dropped — the caller writes it.
    """
    out = []
    for m in re.finditer(r'([\w:-]+)\s*=\s*"([^"]*)"', attrs or ""):
        if m.group(1) != "name":
            out.append((m.group(1), m.group(2)))
    return out


def write_strings_xml(out_dir, pairs, fname="strings.xml", sink=None, locale="ar"):
    """Emit res/values-<locale>/<fname> from (entry, translated_text) pairs.

    `sink`, when given, records the write instead of performing it — that is how
    `--dry-run` works without a second copy of every code path.
    """
    out_dir = Path(out_dir)
    lines = ['<?xml version="1.0" encoding="utf-8"?>',
             f"<!-- generated by aqlab {__version__} -->",
             "<resources>"]
    pl = {}
    seen = set()
    uses_xliff = any("xliff:" in (t or "") for _, t in pairs)
    for e, text in pairs:
        if e.key in seen:
            continue
        seen.add(e.key)
        esc = escape_android(text)
        if e.kind == "string":
            # `formatted="false"` and `translatable="…"` change how aapt reads
            # the value and whether a build accepts it — a translated `100%`
            # without `formatted="false"` fails to compile.
            attrs = "".join(f' {k}="{v}"' for k, v in _attr_pairs(e.attrs))
            lines.append(f'    <string name="{e.key}"{attrs}>{esc}</string>')
        else:
            name, qty = e.key.split("|", 1)
            pl.setdefault(name, []).append((qty, esc))
    # CLDR order for the folder's own locale, so a generated file reads like a
    # hand-written one. Falls back to the wider order for a locale we do not
    # table — the categories present still sort consistently.
    cats = plural_categories(locale)
    order = ({q: i for i, q in enumerate(cats)} if cats
             else {q: i for i, q in enumerate(ARABIC_PLURAL_CATEGORIES)})
    for name, items in pl.items():
        # canonical CLDR order, so a generated file reads like a hand-written one
        items.sort(key=lambda t: order.get(t[0], len(order)))
        lines.append(f'    <plurals name="{name}">')
        for qty, esc in items:
            lines.append(f'        <item quantity="{qty}">{esc}</item>')
        lines.append("    </plurals>")
    lines.append("</resources>")
    if uses_xliff:
        # <xliff:g> is a namespaced element; without the declaration the file is
        # not well-formed and aapt2 refuses it.
        lines[2] = '<resources xmlns:xliff="urn:oasis:names:tc:xliff:document:1.2">'
    path = out_dir / fname
    body = "\n".join(lines) + "\n"
    if sink is not None:
        sink.add(path, body, sum(1 for e, _ in pairs))
    else:
        out_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def write_tsv(path, entries, tsv_map, sink=None):
    """key, source, translation — column 1 must be preserved exactly.

    Every cell is single-line: a literal newline in a cell splits the row in
    two when the table is read back, so the translation after it is lost. Tabs
    and newlines are written as the escapes a resource file would use anyway.
    """
    def cell(text):
        return (text or "").replace("\t", " ").replace("\r\n", "\\n") \
                           .replace("\n", "\\n").replace("\r", "\\n")

    rows = ["# aqlab translation table — fill the LAST column, keep column 1 EXACT",
            "# key\tsource\ttranslation"]
    for e in entries:
        rows.append(f"{e.key}\t{cell(e.text)}\t{cell(tsv_map.get(e.key, ''))}")
    body = "\n".join(rows) + "\n"
    if sink is not None:
        sink.add(Path(path), body, len(entries))
    else:
        Path(path).write_text(body, encoding="utf-8")


class Writes:
    """Collects every file a command would write, then applies them at once.

    Two reasons it is a list and not a `dry` boolean threaded through: `--dry-run`
    can print an exact plan, and a failure halfway through `apply` no longer
    leaves the locale folder half-rewritten — the writes land together or not at
    all.

    Paths that already exist and would change are marked, so a dry run says
    "would overwrite" rather than "would create".
    """

    def __init__(self, dry=False):
        self.dry = dry
        self.items = []          # [(path, body, count)]

    def add(self, path, body, count=0):
        self.items.append((Path(path), body, count))

    def conflicted(self):
        return [(p, b, n) for p, b, n in self.items
                if p.exists() and p.read_text(encoding="utf-8", errors="replace") != b]

    def unchanged(self):
        return [(p, b, n) for p, b, n in self.items
                if p.exists() and p.read_text(encoding="utf-8", errors="replace") == b]

    def commit(self):
        """Write everything. Returns the paths actually written."""
        if self.dry:
            return []
        self.items.sort(key=lambda it: str(it[0]))
        done = []
        for path, body, _ in self.items:
            if path.exists() and path.read_text(encoding="utf-8", errors="replace") == body:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
            done.append(path)
        return done

    def report(self, verbose=False):
        """Print what happened (or would), relative to nothing — callers add that."""
        if self.dry:
            for path, _, n in self.items:
                mark = "would overwrite" if path.exists() else "would create"
                extra = f" ({n} entries)" if n else ""
                print(f"  {mark}: {path}{extra}")
            if not self.items:
                print("  nothing to write")
            return
        changed = self.commit()
        for path in changed:
            print(f"  wrote: {path}")


def non_resource_files(res):
    """Files under `res/` that aapt2 will reject with "invalid file path".

    `aapt2 compile --dir res` accepts only images, XML resources and the folder
    names it knows. A stray `.bak`, `.tsv` or `.orig` inside `res/` fails the
    whole build — this bit us twice, so the commands check before they finish
    rather than leaving it for the user to discover.
    """
    res = Path(res)
    if not res.is_dir():
        return []
    ok_dir = re.compile(r"^(?:values|drawable|mipmap|layout|xml|menu|anim|animator|"
                        r"raw|color|font|navigation|transition|interpolator)"
                        r"(?:-[A-Za-z0-9+_-]+)*$")
    ok_ext = {".xml", ".png", ".jpg", ".jpeg", ".webp", ".9.png", ".gif",
              ".ttf", ".otf", ".ttc", ".mp3", ".ogg", ".wav", ".mp4", ".json",
              ".txt", ".html", ".css", ".js"}
    bad = []
    for d in res.rglob("*"):
        if not d.is_file():
            continue
        rel = d.relative_to(res)
        top = rel.parts[0]
        if not ok_dir.match(top):
            bad.append(d)
            continue
        if d.suffix.lower() not in ok_ext and not d.name.endswith(".9.png"):
            bad.append(d)
    return bad


def existing_locales(res):
    """Locale qualifiers that already have a `res/values-*` folder.

    `values-en-rAU` sorts before `values-fr`, so the list is ordered by the
    locale string rather than by folder name — the config is easier to read and
    to diff that way.
    """
    res = Path(res)
    if not res.is_dir():
        return []
    out = set()
    for d in res.iterdir():
        if not d.is_dir() or not d.name.startswith("values-"):
            continue
        qual = d.name[len("values-"):]
        # skip density/size/version qualifiers — only language tags go in the file
        if not re.match(r"^[a-z]{2,3}(-r[A-Z]{2}|-r\d{3}|-b\+[A-Za-z0-9+]+)?$", qual):
            continue
        out.add(qual.replace("-r", "-").replace("-b+", "-"))
    return sorted(out)


def write_locale_config(proj, locales, dry=False):
    """res/xml/locales_config.xml + android:localeConfig on <application>.

    Android 13 (API 33) reads this file to populate the per-app language picker
    in Settings. Without it the app still follows the system locale, but the user
    cannot pick Arabic for this app alone — which is the whole point of shipping
    a translation for a device set to another language.

    Returns the list of changes, same shape as patch_rtl.
    """
    proj = Path(proj)
    changes = []
    if not locales:
        return changes

    # Look at the manifest before writing anything: if the app already points at
    # its own config, ours would be dead config and their attribute must not be
    # rewritten. Deciding this after writing the file leaves a stray resource.
    man = proj / "AndroidManifest.xml"
    man_txt = man.read_bytes().decode("utf-8", "replace") if man.exists() else ""
    m = re.search(r"<application\b[^>]*>", man_txt, re.DOTALL)
    tag = m.group(0) if m else ""
    if "localeConfig" in tag:
        existing = re.search(r'localeConfig\s*=\s*"([^"]*)"', tag)
        changes.append(f"skipped — app already declares "
                       f"{existing.group(1) if existing else 'localeConfig'}; "
                       f"add the new locales there")
        return changes

    xml_dir = proj / "res" / "xml"
    cfg = xml_dir / "locales_config.xml"
    body = ['<?xml version="1.0" encoding="utf-8"?>', "<locale-config>"]
    for loc in locales:
        body.append(f'    <locale android:name="{loc}"/>')
    body.append("</locale-config>")
    text = "\n".join(body) + "\n"

    # xmlns is required on the root or aapt2 rejects the file
    text = text.replace("<locale-config>",
                        '<locale-config xmlns:android='
                        '"http://schemas.android.com/apk/res/android">')

    old = cfg.read_text(encoding="utf-8") if cfg.exists() else None
    if old != text:
        changes.append(f"res/xml/locales_config.xml: {len(locales)} locale(s)")
        if not dry:
            xml_dir.mkdir(parents=True, exist_ok=True)
            cfg.write_text(text, encoding="utf-8")

    if m:
        if tag.endswith("/>"):
            new = tag[:-2].rstrip() + '\n        android:localeConfig="@xml/locales_config"/>'
        else:
            new = tag[:-1].rstrip() + '\n        android:localeConfig="@xml/locales_config">'
        if new != tag:
            updated = man_txt[:m.start()] + new + man_txt[m.end():]
            changes.append('AndroidManifest.xml: android:localeConfig="@xml/locales_config"')
            if not dry:
                man.write_bytes(updated.encode("utf-8"))
    return changes


def cmd_scaffold(args):
    res = Path(args.proj) / "res"
    if not find_default_strings(args.proj):
        raise Fail("no values/strings.xml found — decode the APK first")

    # Every values file, not just strings.xml. An app that keeps its plurals in
    # values/plurals.xml — AOSP and most Material apps do — would otherwise never
    # hand those entries to a translator, and the Arabic build falls back to the
    # English plural forms with nothing reporting it.
    entries = [e for e in read_locale_entries(res, ["values"]) if e.translatable]
    groups = split_by_file(entries)
    out_dir = res / f"values-{args.locale}"

    # Each source file keeps its shape in the target locale, so no resource name
    # ends up declared twice in one locale (aapt2: duplicate resource).
    clobber = [f for f in sorted(groups) if (out_dir / f).exists()]
    if clobber and not args.force:
        raise Fail(f"{', '.join(str(out_dir / f) for f in clobber)} already exists — "
                   f"refusing to overwrite existing translations. Re-run with "
                   f"--force, or use `apply` to update it.")

    merged = existing_translations(res, args.locale) if clobber else {}
    if args.tsv and Path(args.tsv).exists():
        # the table wins where it has text, the folder fills in the rest
        for k, v in translations_from_tsv(args.tsv).items():
            if v.strip():
                merged[k] = v
    have = [(e, merged[e.key]) for e in entries if merged.get(e.key)]
    # Arabic has six plural categories and the English source declares two, so
    # the four the source cannot supply (`zero`, `two`, `few`, `many`) are in the
    # locale folder but not in `entries`. Iterating entries alone dropped them on
    # every forced scaffold: 54 quantities became 18, and 36 already-translated
    # forms were lost. They have no source entry, so one is synthesised.
    known = {e.key for e in entries}
    pl_home = {}
    for e in entries:
        if e.kind == "plural":
            pl_home.setdefault(e.key.split("|", 1)[0], e)
    for key, text in merged.items():
        if key in known or not text.strip():
            continue
        name, sep, qty = key.partition("|")
        anchor = pl_home.get(name) if sep else None
        if anchor and qty in plural_categories(args.locale):
            have.append((Entry(key, "plural", anchor.attrs, "", True, anchor.src), text))

    # `--force` rewrites the locale folder from the table. If the folder already
    # held translations the table does not, they are about to disappear: keep a
    # copy and say how many, so a forced scaffold is never silent.
    #
    # The copy goes OUTSIDE res/. A file that is not a resource under res/ makes
    # `aapt2 compile --dir res` fail with "invalid file path", which is the same
    # trap the .tsv used to fall into — verified: a single .bak in values-ar
    # breaks the build.
    if clobber and not getattr(args, "dry_run", False):
        had = existing_translations(res, args.locale)
        losing = [k for k, v in had.items() if v.strip() and k not in merged]
        backup_dir = Path(args.proj) / f"aqlab-backup-{args.locale}"
        for f in clobber:
            backup_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(out_dir / f, backup_dir / f)
        if losing:
            print(f"[WARN] {len(losing)} existing translation(s) not in the table — "
                  f"keeping them in {backup_dir}/")

    sink = Writes(dry=getattr(args, "dry_run", False))
    by_file = {}
    for e, t in have:
        by_file.setdefault(e.src or "strings.xml", []).append((e, t))
    write_strings_xml(out_dir, by_file.get("strings.xml", []), sink=sink,
                      locale=args.locale)
    for fname, group in by_file.items():
        if fname != "strings.xml":
            write_strings_xml(out_dir, group, fname, sink=sink, locale=args.locale)
    rows = plural_rows_for(entries, args.locale)
    # The table stays OUT of res/: it is not a resource, and anything that
    # compiles the tree directly (aapt2 compile --dir res) fails on it with
    # "invalid file path". apktool happens to skip it, which is why this never
    # showed up until now.
    tsv = Path(args.proj) / f"values-{args.locale}.tsv"
    write_tsv(tsv, rows, merged, sink=sink)

    # `pending` counts rows that still need a translator. Neither obvious
    # formulation is right:
    #
    #   len(rows) - len(have)  goes negative, because `have` holds synthesised
    #                          plural categories the source cannot declare
    #                          (2 Arabic entries became 6 rows: pending -4)
    #   r.text per row          counts the SOURCE, because plural_rows_for()
    #                          fills every row with the source text so the table
    #                          ships with context. A fresh scaffold of a 12-entry
    #                          tree read "12 of 13 translated" — nothing had been
    #                          translated at all.
    #
    # What is actually known is `merged`: the translations that exist in the
    # target locale folder or arrived in --tsv. Rows not in it are the work.
    def _counts():
        filled = sum(1 for r in rows if (merged.get(r.key) or "").strip())
        return f"translated: {filled}   pending: {len(rows) - filled}"

    if sink.dry:
        print(f"[DRY] would scaffold {len(entries)} entries -> {out_dir}")
        print("      " + _counts() + f"   ({len(rows)} plural/string rows total)")
        sink.report()
        return 0
    sink.commit()
    print(f"[DONE] scaffolded {len(entries)} entries from {len(groups)} file(s) -> {out_dir}")
    print("       " + _counts() + f"   ({len(rows)} plural/string rows total)")
    print(f"       translate:  {tsv}")
    print(f"       then:       aqlab apply {args.proj} --tsv {tsv}")

    stray = non_resource_files(res)
    if stray:
        print(f"[WARN] {len(stray)} file(s) under res/ will break the build — "
              f"aapt2 rejects anything that is not a resource:")
        for p in stray[:5]:
            print(f"         {p.relative_to(args.proj).as_posix()}")

    # The per-app language picker is API 33+. Below that the config is inert, so
    # writing it would only add a file to explain — say why instead of failing.
    if not getattr(args, "no_locale_config", False):
        msk = manifest_min_sdk(args.proj)
        locales = existing_locales(res)
        if msk is not None and msk < 33:
            print(f"       localeConfig: skipped (minSdkVersion={msk}, needs 33+)")
        else:
            cfg = write_locale_config(args.proj, locales)
            for c in cfg:
                print(f"       localeConfig: {c}")
    return 0


def cmd_apply(args):
    res = Path(args.proj) / "res"
    if not find_default_strings(args.proj):
        raise Fail("no values/strings.xml found — decode the APK first")
    merged = translations_from_tsv(args.tsv)
    if not merged:
        raise Fail(f"no usable rows in {args.tsv}")
    all_entries = [e for e in read_locale_entries(res, ["values"]) if e.translatable]
    pairs = [(e, merged[e.key]) for e in all_entries if merged.get(e.key)]

    # Arabic has six plural categories and the source may have two, so a
    # translator must be able to supply quantities the source never had. Accept
    # any `name|quantity` whose plural name exists in the source and whose
    # quantity is a real CLDR category for the target locale.
    home = {}
    for e in all_entries:
        if e.kind == "plural":
            home.setdefault(e.key.split("|", 1)[0], e)
    known = {e.key for e in all_entries}
    for key, text in merged.items():
        if key in known or not text.strip():
            continue
        name, sep, qty = key.partition("|")
        anchor = home.get(name) if sep else None
        if anchor and qty in plural_categories(args.locale):
            pairs.append((Entry(key, "plural", anchor.attrs, "", True, anchor.src), text))

    if not pairs:
        raise Fail("TSV keys match nothing in the default values/ resources")

    # Carry over what is already translated but absent from (or blank in) the
    # TSV. `apply` rewrites whole files, so a table with five filled rows used to
    # delete the other 1,195 translations sitting in the locale folder — and
    # report only `5 translation(s)`, with nothing saying the rest were gone.
    have = dict(existing_translations(res, args.locale))
    supplied = {e.key: t for e, t in pairs}
    kept = 0
    for e in all_entries:
        if e.key in supplied or not have.get(e.key):
            continue
        pairs.append((e, have[e.key]))
        supplied[e.key] = have[e.key]
        kept += 1
    for key, text in have.items():
        if key in supplied or not text.strip():
            continue
        name, sep, qty = key.partition("|")
        anchor = home.get(name) if sep else None
        if anchor and qty in plural_categories(args.locale):
            pairs.append((Entry(key, "plural", anchor.attrs, "", True, anchor.src), text))
            supplied[key] = text
            kept += 1

    out_dir = res / f"values-{args.locale}"
    by_file = {}
    for e, t in pairs:
        by_file.setdefault(e.src or "strings.xml", []).append((e, t))
    sink = Writes(dry=getattr(args, "dry_run", False))
    for fname, group in by_file.items():
        write_strings_xml(out_dir, group, fname, sink=sink, locale=args.locale)
    write_tsv(Path(args.proj) / f"values-{args.locale}.tsv",
              plural_rows_for(all_entries, args.locale),
              {e.key: t for e, t in pairs}, sink=sink)

    if sink.dry:
        print(f"[DRY] would write {len(sink.items)} file(s) from {len(pairs)} translation(s)")
        sink.report()
        return 0
    sink.commit()
    note = f", {kept} kept from existing {args.locale} translations" if kept else ""
    print(f"[DONE] {len(pairs)} translation(s) -> {out_dir}/{' + '.join(sorted(by_file))}{note}")

    # a stray file under res/ breaks `aapt2 compile --dir res`, and the user only
    # finds out at build time — say it here, where the mistake was made
    stray = non_resource_files(res)
    if stray:
        print(f"[WARN] {len(stray)} file(s) under res/ will break the build — "
              f"aapt2 rejects anything that is not a resource:")
        for p in stray[:5]:
            print(f"         {p.relative_to(args.proj).as_posix()}")
    return 0


def load_tm(path):
    """`{"en": "ar"}` or `[{"source":..., "target":...}]` -> list of pairs.

    Two shapes, because the tables people already keep come in both: a JSON
    object keyed by the source string, and the array Gettext and Android Studio
    export use. Order is kept - an exact match is unambiguous, but a fuzzy one
    scores every candidate, and a stable order makes the report reproducible.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return [(str(k), str(v)) for k, v in data.items()
                if str(k).strip() and str(v).strip()]
    out = []
    for row in data if isinstance(data, list) else []:
        if not isinstance(row, dict):
            continue
        src = row.get("source", row.get("src", row.get("en", row.get("msgid"))))
        dst = row.get("target", row.get("dst", row.get("ar", row.get("msgstr"))))
        if src and dst and str(src).strip() and str(dst).strip():
            out.append((str(src), str(dst)))
    return out


def load_glossary(path):
    """`{"term": "..."}` or `[["term", "..."]]` -> dict, lowercased keys.

    The glossary rewrites terms *inside* a matched sentence; it is never a match
    source, so a row cannot resolve a whole string to a bare term it contains.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        return {str(a).strip().lower(): str(b).strip() for a, b in data
                if str(a).strip() and str(b).strip()}
    return {str(k).strip().lower(): str(v).strip() for k, v in data.items()
            if str(k).strip() and str(v).strip()}


def _fmt_sig(text):
    """Comparable format signature: the argument set when fully positional,
    the exact specifier list otherwise (same rule as `lint`).

    Translation memory is the one place a fragment arrives from another project
    carrying `%d` where this string needs `%1$s` - that is a crash at runtime,
    so a candidate with a different signature is never a match.
    """
    fmts = _formats(text)
    sect = _sections(fmts)
    if sect is None:
        return ("list", tuple(fmts))
    # Positional arguments compare as a set (a translator may reorder them, see
    # `_sections`), but the *type* of each argument is not free: `%1$d` where the
    # source has `%1$s` reaches String.format with the wrong object and throws.
    # So the set is compared per index with its conversion character kept.
    kind = {}
    for f in fmts:
        m = RE_SECTION.match(f)
        if m:
            kind.setdefault(int(m.group(1)), f[-1])
    return ("set", tuple(sorted(kind.items())))


def tm_lookup(source, pairs, threshold=0.85):
    """Exact first, then the closest fuzzy pair at or above `threshold`.

    Fuzzy is `difflib.SequenceMatcher` on normalised text - stdlib, and good
    enough for the near-duplicate strings an app is full of. Returns
    `(translation, kind, score)` or None.
    """
    want = re.sub(r"\s+", " ", (source or "").strip().lower())
    if not want:
        return None
    sig = _fmt_sig(source)
    for src, dst in pairs:
        if re.sub(r"\s+", " ", src.strip().lower()) == want and _fmt_sig(src) == sig:
            return dst, "exact", 1.0
    best, score = None, 0.0
    for src, dst in pairs:
        if _fmt_sig(src) != sig:
            continue
        got = difflib.SequenceMatcher(None, want,
                                      re.sub(r"\s+", " ", src.strip().lower())).ratio()
        if got > score:
            best, score = dst, got
    if best is not None and score >= threshold:
        return best, "fuzzy", round(score, 3)
    return None


def function_words(text):
    """True when the string carries something a translator must actually write.

    Format-only and punctuation-only values (`%1$s`, `%1$d/%2$d`, `:`, `999+`)
    cannot be translated and must not count as gaps: counting them inflates the
    report and hides the real ones.
    """
    bare = re.sub(r"<[^>]*>", "", RE_FORMAT.sub("", text or ""))
    return bool(RE_ARABIC.search(bare) or RE_LATIN.search(bare))


def term_in(term, text):
    """Does `text` hold `term` as a whole word, case-insensitively?"""
    if not term or not text:
        return False
    return re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text,
                     re.IGNORECASE) is not None


def glossary_plan(text, source, glossary):
    """Apply the glossary to `text` translated from `source`; return (text, hits, miss).

    The old matcher skipped every row whose term was not already in the
    translated text (`if not pat.search(text): continue`), so a row whose term
    lives in the *source* never matched a translation that renders it with
    Arabic - the ordinary case, and the reason a real APK measured
    `glossary=0`. A row is tried against the target first and the source
    second; the sentence is rewritten in place, so a word swapped for one the
    glossary also lists is not looked up again (see `apply_glossary`).

    `miss` names the row that should have fired and did not: its term is in the
    source, its stock Arabic is not in the target, and the target does not
    already carry another spelling of the term - that is the row to fix, not
    the report to hide.
    """
    if not glossary:
        return text, 0, None
    hits, miss = 0, None
    for term in sorted(glossary, key=len, reverse=True):
        pat = re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.IGNORECASE)
        if pat.search(text):
            new = pat.sub(glossary[term], text)
            if new != text:
                hits += 1
                text = new
        elif miss is None and term_in(term, source) \
                and not term_in(glossary[term], text):
            miss = term
    return text, hits, miss


def apply_glossary(text, glossary):
    """Replace glossary terms in a translated sentence, case-insensitively.

    Longest term first, so "Chess Assistant" is not eaten by "Chess". Only the
    candidate that is being replaced is re-scanned: running every row over the
    rewritten sentence let a replacement feed another row, and running it over
    an untouched one would charge a hit to a translation the glossary never had
    a reason to edit. A translation that chose another word stays as the
    translator wrote it.
    """
    text, hits, _miss = glossary_plan(text, "", glossary)
    return text, hits


def split_argv(body):
    """Split a `--translator` command line into argv, Windows paths included.

    shlex.split in posix mode eats every backslash, so a Windows path to
    python.exe arrives with its separators gone. Only the quoting is wanted
    here, not the POSIX escaping, so backslashes are doubled before the split.
    """
    if os.name != "nt":
        return shlex.split(body)
    return shlex.split(body.replace("\\", "\\\\"))


def translator_cmd(spec, items):
    """`--translator cmd:<argv>` - JSON on stdin, JSON on stdout, nothing else.

    The hook is what keeps this command offline: aqlab never calls a translation
    service, it offers the strings to whatever the user wired up. Accepted
    stdout: `{"key": "text"}` or `[{"key": ..., "text": ...}]`. A hook that
    fails, or answers something unparsable, raises - an empty answer written
    silently would look like a finished locale.
    """
    body = spec.split(":", 1)[1] if spec.lower().startswith("cmd:") else spec
    argv = split_argv(body)
    if not argv:
        raise Fail("--translator cmd: needs a command")
    payload = json.dumps([{"key": k, "source": s} for k, s in items], ensure_ascii=False)
    try:
        p = subprocess.run(argv, input=payload, capture_output=True, text=True)
    except OSError as e:
        raise Fail(f"--translator command not runnable: {e}")
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip().splitlines()
        raise Fail(f"--translator exited {p.returncode}: " + " | ".join(tail[-3:]))
    try:
        data = json.loads(p.stdout or "null")
    except ValueError:
        raise Fail("--translator stdout is not JSON")
    if not isinstance(data, (dict, list)):
        raise Fail("--translator stdout is JSON but not a map or a list of rows")
    out = {}
    rows = data.items() if isinstance(data, dict) else [
        (r.get("key") or r.get("source"), r.get("text") or r.get("target"))
        for r in (data or []) if isinstance(r, dict)]
    for k, v in rows:
        if k and v and str(v).strip():
            out[str(k)] = str(v)
    return out


def cmd_translate(args):
    """Fill `values-<locale>` from translation memory, glossary and an optional
    external translator; print coverage and name every unmatched key.

    Nothing is invented. A key with no exact match, no fuzzy match above the
    threshold and no translator answer stays unmatched and is printed, because
    a lookup that guesses is worse than a gap the user can see.
    """
    proj = Path(args.proj)
    # `res` is the *default-locale* folder below, not the res root: joining
    # `values-<locale>` onto it wrote `res/values/values-ar/`, a directory aapt
    # reads as no locale at all, so every translate run produced a folder
    # nothing would ever load. Both names are derived from the project root.
    res = proj / "res"
    if not find_default_strings(proj):
        raise Fail("no values/strings.xml found - decode the APK first")
    entries = [e for e in read_locale_entries(res, ["values"]) if e.translatable]
    if not entries:
        raise Fail("no translatable entries in values/")

    # Arabic needs six plural categories and the source declares two, so the
    # four it cannot supply have no source entry. The TM is keyed by TEXT, and
    # the source text of `name|few` IS `name|other` - writing the bare source
    # text there is what the fallback already does, and it silently replaces a
    # proper Arabic `few`/`many` with English. Those rows are skipped unless the
    # TM happens to carry an exact `name|category` key of its own.
    entries = plural_rows_for(entries, args.locale)

    pairs = load_tm(args.tm) if args.tm else []
    if args.tm and not pairs:
        raise Fail(f"{args.tm} holds no source/target pairs")
    glossary = load_glossary(args.glossary) if args.glossary else {}
    have = {} if args.force else existing_translations(res, args.locale)

    kinds = collections.Counter()
    filled, unmatched, todo = {}, [], []
    sources = {}
    for e in entries:
        # a plural category the source cannot supply has no text at all; it is
        # kept so the file still declares every Arabic category, but only an
        # exact TM key may fill it (see the lookup loop below)
        if not function_words(e.text) and "|" not in e.key:
            continue
        sources[e.key] = e.text
        if have.get(e.key, "").strip():
            filled[e.key] = have[e.key]
            kinds["kept"] += 1
        else:
            todo.append((e.key, e.text))

    # The TM is asked first: it is the project's own memory, and a hook wired to
    # a service is there to fill what the memory could not. Reversing the order
    # also means a hook that answers everything hides every TM hit in the stats
    # - and the run reports coverage as if the memory were empty.
    for key, text in todo:
        if "|" in key and not text.strip():
            # A category the source cannot supply has no text of its own: the
            # source text of `name|few` is `name|other`, so a text match here
            # would write the English fallback into a real Arabic form. Only an
            # exact `name|category` TM key may fill it.
            exact = dict(pairs).get(key, "").strip()
            if exact:
                filled[key] = exact
                kinds["exact"] += 1
            else:
                unmatched.append(key)
            continue
        hit = tm_lookup(text, pairs, args.threshold)
        if not hit:
            # No TM row for this text. It used to `continue` here, which dropped
            # the key from `unmatched` too — so the "N key(s) unmatched" summary
            # and `--unmatched-out` only ever listed plural categories the source
            # cannot supply, and a plain untranslated string vanished from both.
            unmatched.append(key)
            continue
        filled[key], kind, _score = hit
        kinds[kind] += 1
    if args.translator:
        # only real text goes to the hook: an empty plural item has nothing to
        # translate, and a service that answers anyway would put its own guess
        # into a category Arabic needs a counted form for
        rest = [(k, t) for k, t in todo if k not in filled and t.strip()]
        if rest:
            answer = translator_cmd(args.translator, rest)
            for key, _text in rest:
                if answer.get(key):
                    filled[key] = answer[key]
                    kinds["cmd"] += 1

    # The glossary runs over every filled row - a translation whose source text
    # never held the term included: the translator chose the Arabic word, and
    # that is exactly what a glossary row pins down. A kept row (an existing
    # values-ar entry) is written back unchanged and is not edited on the quiet.
    gloss, gloss_missed = 0, []
    if glossary:
        for key in filled:
            if key not in sources:
                continue
            text, n, miss = glossary_plan(filled[key], sources[key], glossary)
            # a kept row names no entry when it came from another file, and the
            # rewrite must still land in `filled` - assigning back through
            # `filled[key]` on its own lost the text the match just produced
            filled[key] = text
            gloss += n
            if miss and key not in have:
                gloss_missed.append((key, miss))

    # one file per source file, so a plural lands where its source keeps it
    # `filled` holds every kept key as well, and `entries` is the file being
    # *written* - a kept key from another file was silently dropped from the
    # output here, so values-ar lost rows it already had. Only what this run
    # sourced or produced is written; a kept row is left where it lives.
    written = set(filled) - set(have)
    by_file = {}
    for e in entries:
        if e.key in written:
            by_file.setdefault(e.src or "strings.xml", []).append((e, filled[e.key]))
    # `res` here is already `<proj>/res/values` (the default-locale folder the
    # entries were read from), so joining `values-<locale>` onto it wrote into
    # `res/values/values-ar/` — a directory aapt reads as no locale at all, and
    # every translate run silently produced a folder nothing would ever load.
    out_dir = res / f"values-{args.locale}"
    sink = Writes(dry=getattr(args, "dry_run", False))
    for fname, group in by_file.items():
        # the folder may already hold rows this run did not produce or source
        # (`kept`, or another file's translations). `written` emits exactly the
        # pairs it is handed, so writing only `group` deleted them: a translate
        # pass run to fill one gap wiped the finished file beside it. Existing
        # rows in the same file are re-emitted untouched and the freshly
        # produced ones replace their own key.
        fresh = {e.key for e, _t in group}
        target = out_dir / fname
        carried = [(e, t) for e, t in
                   ((e, have.get(e.key, "")) for e in read_entries(target))
                   if e.key not in fresh] if target.is_file() else []
        write_strings_xml(out_dir, carried + group, fname, sink=sink,
                          locale=args.locale)

    total = len(filled) + len(unmatched)
    done = len(filled)
    pct = 100.0 * done / total if total else 100.0
    if args.unmatched_out:
        sink.add(Path(args.unmatched_out),
                 json.dumps(unmatched, ensure_ascii=False, indent=2) + "\n", len(unmatched))
    rep = None
    fmt = getattr(args, "report", None)
    if fmt:
        findings = [("warn", key, "no TM/glossary/translator match — falls back to the "
                     "source text", "translate-unmatched") for key in unmatched]
        per_file = {f: (sum(1 for e, _t in g), len(g)) for f, g in by_file.items()}
        rep = build_report(proj, args.locale, pct, done, total, per_file,
                           findings, 0, False, command="translate")
        # `kept` rows were never shown to the TM: the target already had text.
        # Counting them as coverage is right, but a report that shows 97% while
        # the memory resolved nothing reads as "the TM works" - and the user
        # ships the old unreviewed translations. So the report says out loud
        # how many keys the TM was actually asked about.
        rep["translate"] = {"matched": dict(sorted(kinds.items())),
                            "glossary": gloss,
                            "glossary_missed": len(gloss_missed),
                            "evaluated": total - kinds.get("kept", 0),
                            "stale_kept": kinds.get("kept", 0)}
    # every key had a target already, so the TM and the hook were never
    # consulted; say it, or the next run "improves" nothing and looks green.
    # Printed above the dry branch on purpose: a dry run is the run that is
    # *meant* to show gaps, and it returns before the DONE line.
    if kinds.get("kept") and not (kinds.get("exact", 0) + kinds.get("fuzzy", 0)
                                  + kinds.get("cmd", 0)):
        print("[NOTE] every key with a target kept it - the TM resolved nothing; "
              "use --force to re-run the lookup")
    if sink.dry:
        print(f"[DRY] would write {done}/{total} ({pct:.1f}%) -> {out_dir}")
        sink.report()
        # A dry run is exactly when a report is wanted: it is the only way to
        # see the coverage and the gaps without touching values-ar. Building it
        # above the `return` is what makes `--report --dry-run` work at all.
        if rep:
            write_report(fmt, getattr(args, "out", None), rep, proj)
        return 0
    sink.commit()
    detail = " ".join(f"{k}={v}" for k, v in sorted(kinds.items()) if v)
    if gloss:
        detail += f", glossary={gloss} rewrite(s)"
    print(f"[DONE] translate: {done}/{total} ({pct:.1f}%) -> {out_dir}/"
          + (f"  ({detail})" if detail else ""))
    if rep:
        write_report(fmt, getattr(args, "out", None), rep, proj)
    # A row that should have fired and did not is the one thing a glossary
    # cannot tell you about itself: the term is in the source, the translation
    # does not carry its Arabic, and nothing was rewritten. Naming one row is
    # enough to show which row to fix - a silent hit-rate of 0 is what shipped
    # a real APK with an untested glossary.
    if gloss_missed:
        key, miss = gloss_missed[0]
        print(f"[NOTE] glossary rewrote {gloss} of {gloss + len(gloss_missed)} "
              f"candidate row(s) - `{miss}` is in the source of `{key}` and its "
              "Arabic is not in the target, so the row cannot fire")
    if unmatched:
        print(f"[FINDING] {len(unmatched)} key(s) unmatched - they render in the "
              f"source language until the TM, glossary or translator covers them:")
        for key in unmatched[:10]:
            print(f"         {key}")
        if len(unmatched) > 10:
            print(f"         ... and {len(unmatched) - 10} more")
    return 0

def cmd_scan(args):
    target = Path(args.target)
    if target.is_dir():
        return scan_project(target, args)
    return scan_apk(target, args)


def parse_jobs(value):
    """`--jobs N`: 0 is "every CPU", anything >0 is itself.

    A string glob (`res/**`) is the classic way to get 4,096 workers and an
    OOM in place of a result, so the cap is a parse error on some machines and
    a busy machine everywhere else — pinned to the CPU count instead.
    """
    env = os.environ.get("AQLAB_JOBS")
    try:
        n = int(value if value is not None else (env or 1))
    except ValueError:
        raise argparse.ArgumentTypeError(f"--jobs wants an integer, got {value!r}")
    if n < 0:
        raise argparse.ArgumentTypeError("--jobs must be >= 0")
    if n == 0:
        return os.cpu_count() or 1
    return n


def _scan_project_json(proj, args, res, entries, loc, manifest, rtl_on,
                       lefts, ar_dirs):
    """`scan --json`: one JSON document on stdout and nothing else.

    It used to print the whole human block first and append a one-line summary
    at the end, so `aqlab scan --json | jq` died on `[FINDING] project: ...`.
    A flag that says json has to be the only thing on the pipe.
    """
    jobs = parse_jobs(getattr(args, "jobs", None))
    doc = {
        "target": str(proj),
        "kind": "project",
        "locales": loc,
        "supportsRtl": rtl_on,
        "translatable": sum(1 for e in entries if e.translatable),
        "legacyLTR": lefts,
        "asymmetric": len(symmetry_problems(proj, jobs)),
        "hardcodedGravity": len(hardcoded_gravity(proj, jobs)),
        "literalText": len(literal_text_attrs(proj, jobs)),
        "textVectors": len(text_like_vectors(proj, jobs)),
        "localeDirs": ar_dirs,
    }
    print(json.dumps(doc, ensure_ascii=False, indent=2))
    return 0


def scan_project(proj, args):
    # `--jobs N`: every layout sweep below is a spread of its serial body.
    jobs = parse_jobs(getattr(args, "jobs", None))
    if not find_default_strings(proj):
        raise Fail("no values/strings.xml — is this a decoded project?")
    res = Path(proj) / "res"
    entries = read_locale_entries(res, ["values"])
    # res/ has far more qualified directories than locales — values-ldrtl, values-night,
    # values-h480dp, values-sw600dp, values-v31, values-w320dp-land, values-watch. Listing
    # them as "locales" buries the seven real ones under thirty qualifiers.
    loc = sorted({d.name[len("values-"):] for d in res.glob("values-*")
                  if d.is_dir() and RE_LOCALE_Q.match(d.name[len("values-"):])
                  and d.name[len("values-"):] not in NON_LOCALE_QUALIFIERS})
    manifest = (Path(proj) / "AndroidManifest.xml").read_text(encoding="utf-8", errors="replace")
    rtl_on = bool(re.search(r'supportsRtl\s*=\s*"true"', manifest))
    lefts = count_legacy_attrs(proj)
    ar_dirs = sorted(p.name for p in res.glob("values-ar*") if p.is_dir())
    if args.json:
        return _scan_project_json(proj, args, res, entries, loc, manifest, rtl_on,
                                  lefts, ar_dirs)
    print(f"[FINDING] project: {proj}")
    print(f"  locales        : {', '.join(loc) or 'default only'}")
    print(f"  supportsRtl    : {'yes' if rtl_on else 'NO — run `aqlab rtl`'}")
    print(f"  translatable   : {sum(1 for e in entries if e.translatable)}")
    print(f"  legacy L/R     : {lefts} attribute(s) still left/right — run `aqlab rtl`")
    sym = symmetry_problems(proj, jobs)
    if sym:
        print(f"  asymmetric     : {len(sym)} side(s) set without the opposite — "
              f"offsets off-centre when the layout flips")
    # `--jobs` only ever spreads the serial passes; the serial pass stays the
    # one definition of a hit, and `test_jobs_parity` asserts the equality.
    grav = hardcoded_gravity(proj, jobs)
    if grav:
        print(f"  hardcoded grav : {len(grav)} value(s) pin text to a physical side")
    pins = direction_pins(proj, jobs)
    for kind, where, *_rest in pins:
        if kind == "supportsRtl":
            print(f"  supportsRtl    : false — Arabic gets the LTR layout; "
                  f"run `aqlab rtl`")
        else:
            tag_, val = _rest[0], _rest[1]
            print(f"  direction pin  : {where}:<{tag_}> layoutDirection=\"{val}\" on a "
                  f"horizontal row with physical attrs")
    lit = literal_text_attrs(proj, jobs)
    if lit:
        print(f"  literal text   : {len(lit)} view(s) hold a literal — never reaches "
              f"the locale values, move to `@string/`")
    vec = text_like_vectors(proj, jobs)
    if vec:
        print(f"  text vectors   : {len(vec)} — Latin glyphs baked into geometry, check by eye")
        for pth, n, w, h in vec:
            print(f"                   {pth}  ({n} paths, {w}x{h}dp)")
    print(f"  arabic res     : {'yes (' + ', '.join(ar_dirs) + ')' if ar_dirs else 'none — run `aqlab scaffold`'}")
    return 0


def _spread_pairs(sources, fn, jobs):
    """`fn` over pre-read (relative path, text) pairs, rows joined in order.

    One floor for every layout sweep: below eight files a pool costs more than
    the sweep it would run (spawn is ~50ms a worker on Windows), so a small
    project stays serial whatever `--jobs` says. Above it, `imap` yields in
    submission order — that is why the pooled list *is* the serial list, and
    `test_jobs_parity` asserts it for every sweep.
    """
    if jobs <= 1 or len(sources) < 8:
        return [hit for pair in sources for hit in fn(pair)]
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(min(jobs, len(sources))) as pool:
        return [hit for chunk in pool.imap(fn, sources) for hit in chunk]


def _pins_chunk(pair):
    """`pins_in` for one (relative path, text) pair, picklable for `spawn`."""
    name, txt = pair
    return pins_in(txt, name)


def text_sources(proj, patterns):
    """(relative path, text) for every file matching `patterns` under res/.

    One reader for every sweep that reaches a pool: the parent holds the disk, a
    child holds only the strings it was handed, and the order is sorted so the
    serial pass and the pooled pass see the same list.
    """
    res = Path(proj) / "res"
    if not res.is_dir():
        return []
    seen, rows = set(), []
    for pat in patterns:
        for f in sorted(res.glob(pat)):
            if not f.is_file() or f in seen:
                continue
            seen.add(f)
            try:
                rows.append((f.relative_to(proj).as_posix(),
                             f.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                continue
    return rows


def gravity_parallel(sources, jobs):
    """`hardcoded_gravity` over pre-read (relative path, text) pairs, spread.

    `imap` yields in submission order, so the result is the serial result — the
    equality is asserted in `test_gravity_jobs`, and the serial
    path stays the only definition of what a hit is.
    """
    if jobs <= 1 or len(sources) < 8:
        return [hit for pair in sources for hit in _gravity_chunk(pair)]
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(min(jobs, len(sources))) as pool:
        return [hit for chunk in pool.imap(_gravity_chunk, sources) for hit in chunk]


def literal_text_attrs(proj, jobs=1):
    """`android:text`/`hint`/`title`... holding a literal instead of a reference.

    A literal never reaches `values-ar`, so it is the one class of string that
    translation cannot cover however complete the table is — the usual way an
    otherwise finished Arabic build still shows English. Only layout files: a
    literal in `values/` is a default-locale default, not an untranslatable one.

    A reference still counts as a literal when its name marks it
    `translatable="false"`: the referenced string is never translated either.
    A literal with no letter in it (`"5"`, `"0/250"`) is not text anyone
    translates. Everything else, one letter or twenty, is a finding — measured
    on the reference APK, all 9 were real.

    Returns [(path, tag, attr, value, reason)] where reason is `literal` or
    `untranslatable-ref`, and the whole list is report-only: `--fix` cannot
    write a string resource into `values/` under a name it just invented.
    """
    refs = untranslatable_names(proj)
    return _spread_pairs(text_sources(proj, ("layout*/*.xml",)),
                         lambda pair: _literal_chunk(pair, refs), jobs)


def untranslatable_names(proj):
    """String names declared `translatable="false"` in the default locale."""
    res = Path(proj) / "res"
    out = set()
    for folder in ("values",):
        d = res / folder
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.xml")):
            for e in read_entries(f):
                if not e.translatable:
                    out.add(e.key)
    return out


def _literal_chunk(pair, refs):
    """`literal_text_attrs` for one (relative path, text) pair, picklable."""
    name, txt = pair
    out = []
    for m in RE_LITERAL_TEXT.finditer(txt):
        tag, attr, val = m.group(1), m.group(2), m.group(3)
        val = val.strip()
        if not val:
            continue
        # A reference is checked *before* the `@` skip: a `@string/` whose name
        # is `translatable="false"` is never translated either, and it was
        # exactly the row the earlier order silently dropped.
        ref = RE_REF_NAME.fullmatch(val)
        if ref:
            if ref.group(1) in refs:
                out.append((name, tag, attr, val, "untranslatable-ref"))
            continue
        if val.startswith("@") or val.startswith("?"):
            continue
        if RE_LETTER.search(val) is None:
            continue
        out.append((name, tag, attr, val, "literal"))
    return out


def text_like_vectors(proj, jobs=1):
    """Vector drawables that probably carry baked-in Latin glyphs.

    A heuristic, so every signal has to agree before anything is reported — the
    cost of a false positive is that the user learns to ignore the warning,
    which is worse than not having it.

    Three conditions, all required:
      1. it is a `<vector>`, not a `<shape>`/`<selector>` (a badge background is
         not a badge with text in it, however the file is named)
      2. it does not already set `android:autoMirrored="true"` — a vector that
         mirrors itself is handled, and there is nothing to warn about
      3. its geometry looks like lettering: many short filled paths, not one or
         two large icon shapes

    Returns [(path, path_count, width, height)] relative to the project.
    """
    return _spread_pairs(text_sources(proj, ("drawable*/*.xml",)),
                         _vectors_chunk, jobs)


def _vectors_chunk(pair):
    """`text_like_vectors` for one (relative path, text) pair.

    Module level and argument-only so a `spawn` pool can pickle it. 12 paths is
    above every Material icon in a real APK (measured: 2 each) and below what
    even a short wordmark needs.
    """
    name, txt = pair
    if "<vector" not in txt:
        return []
    if "autoMirrored" in txt and 'autoMirrored="true"' in txt:
        return []
    n = txt.count("<path")
    if n < 12:
        return []
    w = re.search(r'android:width="([0-9.]+)dip"', txt)
    h = re.search(r'android:height="([0-9.]+)dip"', txt)
    return [(name, n, w.group(1) if w else "?", h.group(1) if h else "?")]


def hardcoded_gravity(proj, jobs=1):
    """`gravity`/`textAlignment` values that pin text to a physical side.

    Android's `RtlHardcoded` check. `gravity="left"` is a left edge in every
    locale, so Arabic gets a paragraph hugging the wrong margin — unlike
    `paddingLeft`, it is not renamed by `aqlab rtl` silently, it is a value that
    has to be read.

    Returns [(path, attr, value)] and never rewrites anything: a value like
    `left|bottom` needs its tokens translated, not its attribute renamed, and
    guessing which token the author meant is how a tool breaks a layout.

    `--jobs` only spreads `_gravity_chunk` over text read here, serially; the
    chunk is the serial body, so the two lists are the same list.
    """
    rows = []
    # One read per file, in `gravity_files` order: the pool never touches disk.
    for f in gravity_files(proj):
        try:
            rows.append((f.relative_to(proj).as_posix(),
                         f.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return gravity_parallel(rows, jobs)


def _gravity_chunk(pair):
    """Gravity hits in one (relative path, text) pair.

    Module level and argument-only so a `spawn` pool can pickle it. The worker
    never reads the disk: the parent reads each file exactly once, serially,
    and only the regex sweep is spread out.
    """
    attr_re = re.compile(r'\b(?:android:)?(gravity|layout_gravity|foregroundGravity|'
                         r'textAlignment)\s*=\s*(["\x22\x27])([^\x22\x27]*)\2')
    name, txt = pair
    out = []
    for m in attr_re.finditer(txt):
        attr, val = m.group(1), m.group(3)
        if GRAVITY_TOKEN_HIT.search(val):
            out.append((name, attr, val))
    return out


def gravity_files(proj):
    """Every layout/value/menu/anim file a gravity value can live in.

    One definition for the report and the fixer: when `lint --fix` scanned a
    wider set than the check, it rewrote files the report never named.
    """
    res = Path(proj) / "res"
    if not res.is_dir():
        return []
    return [f for f in sorted(res.glob("**/*.xml"))
            if any(part.startswith(("layout", "values", "menu", "anim"))
                   for part in f.relative_to(res).parts[:1])]


def walk_layout(txt, full=False):
    """Yield (tag, body, depth, self_closing) for every start tag, depth being
    how many elements enclose it. `full=True` adds the body's offset in `txt`.

    `iter_start_tags` drops closing tags, so nesting is recovered here from the
    text itself: a `/>` opens and closes at once, a `</name>` closes the
    innermost open element with that name, and everything else stays open.
    """
    stack, i, n = [], 0, len(txt)
    while True:
        i = txt.find("<", i)
        if i < 0:
            return
        if txt.startswith("</", i):
            j = txt.find(">", i)
            if j < 0:
                return
            name = txt[i + 2:j].strip()
            for k in range(len(stack) - 1, -1, -1):
                if stack[k] == name:
                    del stack[k:]
                    break
            i = j + 1
            continue
        if txt.startswith("<!", i) or txt.startswith("<?", i):
            j = txt.find(">", i)
            if j < 0:
                return
            i = j + 1
            continue
        j, quote = i + 1, None
        while j < n:
            ch = txt[j]
            if quote:
                if ch == quote:
                    quote = None
            elif ch in (chr(34), chr(39)):
                quote = ch
            elif ch == ">":
                break
            j += 1
        if j >= n:
            return
        body = txt[i + 1:j]
        end = 0
        while end < len(body) and not body[end].isspace() and body[end] != "/":
            end += 1
        tag = body[:end]
        attrs = body[end:]
        self_closing = attrs.rstrip().endswith("/")
        if self_closing:
            attrs = attrs.rstrip()[:-1]  # keep the `row` tests off the slash
        if full:
            yield tag, attrs, len(stack), self_closing, i + 1 + end
        else:
            yield tag, attrs, len(stack), self_closing
        if not self_closing:
            stack.append(tag)
        i = j + 1


def direction_pins(proj, jobs=1):
    """A layout direction pinned on a row that also carries physical attributes,
    and a manifest that opts out of RTL entirely.

    `layoutDirection="ltr"` is not a defect by itself. A clock face, a numeric
    keypad, a media timeline — those are the same shape in every language, and
    pinning them is exactly right. The pin only becomes a problem when it sits on
    a horizontal row that *also* sets a left/right attribute: then it is not a
    design decision, it is the author turning RTL off for that row and papering
    over the physical attributes underneath. That row stays LTR in Arabic.

    Measured on a real APK: its two pins are Material's `material_clock_display`
    and `material_timepicker_textinput_display` — horizontal rows of hour/minute
    includes with no physical attributes anywhere under them, so both are
    silent here. Pinning alone costs nothing; pinning over physical attrs does.

    `layoutDirection="rtl"` mirrors; reported by the same rule.

    `textDirection="ltr"`/`"rtl"` is the same mistake one level down and is
    reported by this rule rather than a check of its own: inside a row that
    already carries left/right attributes it keeps the text pinned while the row
    flips. `firstStrong` (the default), `anyRtl`, `locale` and `inherit` follow
    the direction and stay silent. Measured on a real APK: no layout sets
    `textDirection` at all, so this half is silent too.

    Manifest: `android:supportsRtl="false"` is the only wrong value — absence is
    the platform default and `aqlab scan` already flags that separately.

    A pin does not stop at its own row: a direction set on a container becomes the
    resolved direction of every descendant that does not override it. So a pinned
    `<FrameLayout>` whose child row carries `paddingLeft` is the same defect as a
    pinned row, one level up — measured on a real APK, `material_clock_display`
    pins an outer `LinearLayout` whose four children are the actual rows. Reported
    as kind `pinNested`, and only when the child holds a physical attribute and no
    `layoutDirection`/`textDirection` of its own at the same level or nearer
    (otherwise the child already overrode the pin and the row flips normally).

    `layoutDirection="locale"` is not a pin — it is the default spelled out — and
    is skipped by both the local and the nested half.

    The pin is not always on a row that *is* horizontal: fixing `pinNested` turns
    `paddingLeft` into `paddingStart`, and the pin then sits on a row whose own
    shape says nothing — its axis is the one it inherits, and that is what
    decides whether a left/right beneath it still means anything. So an
    ancestor's direction is tracked too, and a row with no orientation counts as
    horizontal when the row enclosing it is.

    Returns [("supportsRtl", path, "")], [("layoutDirection", path, tag, value)],
    [("textDirection", path, tag, value)] and [("pinNested", path, tag, value)].
    """
    out = []
    manifest = Path(proj) / "AndroidManifest.xml"
    if manifest.exists():
        txt = manifest.read_text(encoding="utf-8", errors="replace")
        # Guarded by the tag itself: an apktool dump carries the original
        # manifest in a `<!-- ... -->` block, and a comment saying
        # `supportsRtl="false"` is not a manifest that opts out.
        # `iter_start_tags` walks the text, comments included — apktool writes
        # the original manifest into one. Blank every `<!-- -->` out first, so a
        # comment cannot speak for the manifest.
        # Same predicate the fixer reads: one spelling of "the manifest opted
        # out", so a report never names a value `--fix` refuses to flip. It also
        # accepts the single-quoted form, which aapt does.
        if manifest_supports_rtl(proj) is False:
            out.append(("supportsRtl", "AndroidManifest.xml", ""))


    # `pins_in` is the serial body, spread over text read here; the lambda is
    # the only unpicklable bit and `_spread_pairs` keeps the spawn context.
    out.extend(_spread_pairs(text_sources(proj, ("layout*/*.xml",)),
                             _pins_chunk, jobs))
    return out


def _direction_pins_re():
    """The four regexes `direction_pins` and `pins_in` share.

    Compiled once at import: a per-call compile cost 3x the sweep it feeds.
    """
    # Any start tag, self-closing or not: the interesting case is a container
    # wrapping children, which is a plain `<LinearLayout ...>`. Attributes are
    # matched with plain `\s`, not `` — apktool puts the first attribute on
    # the tag's own line, and a line start is not a word boundary before `a`.
    # The tag is found by `iter_start_tags`, not by a regex: a layout holds
    # several siblings that open with the same tag, and one regex match used to
    # swallow the next element's attributes, so a pin could vanish.
    pin_re = re.compile(r'(?<![\w.-])(?:android:|app:)?layoutDirection\s*=\s*[\x22\x27](?!locale)(ltr|rtl)[\x22\x27]')
    txt_re = re.compile(r'(?<![\w.-])(?:android:|app:)?textDirection\s*=\s*[\x22\x27](?!locale)(ltr|rtl)[\x22\x27]')
    any_dir_re = re.compile(r'(?<![\w.-])(?:android:|app:)?(?:layout|text)Direction\s*=')
    # A physical name *or* a physical value: `gravity="left"` is the same defect
    # with the direction in the value instead of the name, and `--fix` repairs
    # it. Without the value half here, a child row carrying only a gravity under
    # an ancestor pin was reported by neither rule: `direction_pins` wanted a
    # renamed attribute, and `hardcoded_gravity` does not look at ancestors.
    phys_re = re.compile(
        r"(?<![\w.-])(?:android|app):(%s)\s*=" % "|".join(ATTR_RENAME)
        + r"|(?<![\w.-])(%s)\s*=\s*[\x22\x27][^\x22\x27]*\b(?:left|right)\b"
          % "|".join(GRAVITY_ATTRS | TEXT_ALIGN_ATTRS))

    return pin_re, txt_re, any_dir_re, phys_re


PIN_RE, TXT_RE, ANY_DIR_RE, PHYS_RE = _direction_pins_re()


def pins_in(txt, name):
    """`direction_pins` for one file: the walk, the axis tracking, the chain.

    Module level and argument-only so a `spawn` pool can pickle it. The
    manifest half stays in `direction_pins` — one file, read once.
    """
    out = []
    pin_re, txt_re, any_dir_re, phys_re = PIN_RE, TXT_RE, ANY_DIR_RE, PHYS_RE
    pinned = []  # [(depth, tag, value)] still in scope
    # [(depth, horizontal?, pinned?)]: the axis in force, and whether the
    # direction is pinned. A `vertical` row that says nothing does not clear
    # the axis — a child of it with no orientation is a `wrap_content` row,
    # laid out along the horizontal axis of the row above.
    axis = []
    for tag, body, depth, self_closing in walk_layout(txt):
        # `>=`, not `>`: the pin belongs to an element that closed as soon as a
        # new start tag appears at the same depth — its sibling is outside it,
        # so the pin must not reach it. Measured: a pinned row's following
        # sibling was reported as inheriting a direction nobody set on it.
        while pinned and pinned[-1][0] >= depth:
            pinned.pop()
        while axis and axis[-1][0] >= depth:
            axis.pop()
        # `horizontal` here means "a left edge exists", `vertical` the
        # opposite, `locale` makes the axis follow the locale — which is the
        # same flip `pinNested` is about, so only a pinned axis is tracked.
        inherited = axis[-1] if axis and axis[-1][1] else None
        # `locale` is the platform default spelled out, not a pin.
        local = None
        for kind, rx in (("layoutDirection", pin_re), ("textDirection", txt_re)):
            pm = rx.search(body)
            if pm:
                local = (kind, pm.group(1))
                break
        if not self_closing:
            # The row's own orientation, not its tag's default: a
            # `LinearLayout` that states `vertical` stacks its children, and
            # whatever it leaves unstated is a horizontal `wrap_content`
            # strip. `TableRow` is horizontal without saying so, and the
            # remaining tags have no `orientation` to state.
            stated = re.search(r'(?<![\w.-])orientation\s*=\s*[\x22\x27](\w+)',
                               body)
            if stated:
                axis.append((depth, stated.group(1) == "horizontal", False))
            elif tag in HORIZONTAL_BY_DEFAULT:
                axis.append((depth, True, False))
        if local:
            # A direction pin decides whether the axis flips, not whether it
            # exists: a pinned vertical column still stacks its children.
            # Recording it as horizontal is what the `row` test below
            # already does for the row's own shape.
            axis.append((depth, True, True))
            while pinned and pinned[-1][0] >= depth:
                pinned.pop()
            # A self-closed row encloses nothing: pushing it would leak the
            # pin onto its following siblings, which are outside it. Found
            # by a `<TextView ... />` pin whose next sibling row was reported
            # as inheriting a direction that no ancestor set.
            if not self_closing:
                pinned.append((depth, tag, local[1]))
        # A row is horizontal when it says so, when its tag's default
        # orientation is horizontal — `LinearLayout`, `RadioGroup`,
        # `TableRow` — or when it says nothing and the axis in force at it
        # is horizontal: a `wrap_content` child of a vertical column is a
        # row, and a child of a horizontal row is measured along that same
        # axis. Everything else is a leaf or a vertical container:
        # a `TextView`'s own padding belongs to its parent row and is
        # reported with that row's shape, and a `ScrollView` stacks its
        # children whatever the direction. Only a pin on a *horizontal* row
        # moves the axis, so only that is a finding.
        # A bare `<include>` counts only when it carries the physical
        # attribute itself: the row it pulls in lives in another file and is
        # linted there.
        row = (bool(re.search(r'(?<![\w.-])orientation\s*=\s*[\x22\x27]horizontal',
                              body))
               or (not re.search(r'(?<![\w.-])orientation\s*=', body)
                   and (tag in HORIZONTAL_BY_DEFAULT
                        or (not looks_leaf(tag)
                            and inherited is not None and inherited[0])))
               or tag == "include")
        # A local pin judges the renamed half alone: a row that pins its own
        # direction already opted out of the locale, and when only its text
        # align is stuck that is `RtlHardcoded`, which owns it and knows the
        # physical-token rule. Under an inherited pin both halves count — the
        # row cannot follow the locale any more, so a physical token in a
        # gravity keeps the text on one side and has to be reported.
        phys_names = tuple(m.group(1) or m.group(2) for m in phys_re.finditer(body))
        if local:
            phys = any(m.group(1) for m in phys_re.finditer(body))
        else:
            phys = bool(phys_names)
        if not phys or not row:
            continue
        # A local pin and an inherited one are not the same defect: a row
        # pinning its *own* direction already opted out of the locale and
        # `--fix` has a decision to make about it (drop an `ltr` pin), while
        # an inherited pin is only visible through the physical attrs the
        # row itself carries. Reporting a locally pinned row under the
        # inherited one as well double-counts it — and the rtl-mirror row
        # came back as a `pinNested` it never was.
        chain = None if local else (pinned[-1][2] if pinned else None)
        if local:
            kind, val = local
            out.append((kind, name, tag, val))
        elif chain and not any_dir_re.search(body):
            # A child that sets its own direction breaks the chain: it and
            # everything under it follow that value, not the outer pin.
            # The attribute names travel with the finding: the fixer has to
            # migrate exactly what the report named, and the row may hold a
            # padding and a gravity at once.
            names = tuple(sorted({m.group(1) or m.group(2)
                                  for m in phys_re.finditer(body)}))
            out.append(("pinNested", name, tag,
                        chain, names))
    return out


# `values/attrs.xml` is where a project declares its own `layoutDirection`
# attribute — aapt2 rejects the file if the enum repeats a platform constant, so
# a declared enum is the author's own and dropping it is what breaks the build.
# The platform's own five values are spelled out for the docs reader.
# A direction pin is only a language decision where there is language to flow.
# A clock face, a time picker, a numeric keypad and a media timeline are the same
# shape in every locale and pinning them is right. Measured on a real APK: the
# only two `ltr` pins are Material's `material_clock_display` and
# `material_timepicker_textinput_display`, whose whole content is `<include>`s of
# hour/minute parts — no text widget anywhere beneath, so neither is a finding.
RE_TEXT_WIDGET = re.compile(r"<(?:\w+\.)*"
                        r"(?:TextView|EditText|Button|AutoCompleteTextView)\b")


RE_DECLARES_DIRECTION_ENUM = re.compile(
    r"<attr\b[^>]*\bname\s*=\s*[\x22\x27](?:android:)?layoutDirection[\x22\x27]"
    r"[^>]*?>(?P<flags>.*?)</attr>", re.DOTALL)


def declared_direction_values(proj):
    """Project-local `layoutDirection` enum values a pin must not be stripped of.

    Returns the set of value names declared by the app itself. Empty for every
    app that only uses the platform attribute — which is nearly all of them.
    """
    out = set()
    for f in (Path(proj) / "res").glob("values*/**/*.xml"):
        try:
            txt = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in RE_DECLARES_DIRECTION_ENUM.finditer(txt):
            out.update(re.findall(r"<enum\b[^>]*\bname\s*=\s*[\x22\x27]([\w-]+)",
                                  m.group("flags")))
    return out


def _rtl_chunk(pair):
    """`rtl_enabled_in` for one (relative path, text) pair, picklable for `spawn`.

    The declared-enum set and the manifest flip are project-wide gates, so they
    stay in `rtl_enabled`; a child file can only answer for its own text.
    """
    name, txt = pair
    return rtl_enabled_in(txt, name, set(), None)


def rtl_enabled(proj, locale="ar", jobs=1):
    """`layoutDirection`/`textDirection` set to a literal that is *not* a pin.

    `direction_pins` reports the one case that is unambiguously a defect: a pin
    laid over physical left/right attributes. This check is the other half of
    Android's RtlEnabled: any direction literal on an element that holds nothing
    physical still wins over the locale, so the element keeps the direction of the
    language it was written in — the Arabic screen shows it mirrored.

    Three literals are not defects and are skipped before anything is measured:
      * `locale` — the default spelled out; it is the correct way to say "follow
        the language" and the platform resolves it to the current direction.
      * `firstStrong`, `anyRtl`, `inherit` — text directions that resolve from
        the content or the parent, so they follow RTL by themselves.
      * `rtl` on a *leaf* — a mirrored icon is a decision about that glyph, not
        about a container, and `direction_pins` already covers the row case.

    `ltr` is reported on any element. It is the direction a string resource is
    written in, not the direction the app is displayed in, and it is the one
    literal `aqlab lint --fix` can drop safely (`fix_pin_local` already does).

    Not reported, measured rather than assumed:
      * a value the project declares in its own `values/attrs.xml` — aapt2
        refuses a duplicate enum, so an app that declares one is using it, and a
        `--fix` that strips it breaks the compile. `declared_direction_values`.
      * an `app:` namespace attribute on a view whose class is a known custom
        one — the app's own class may read the value from its styleable, so it is
        the same promise as a declared enum, one level further out.

    Measured on a real APK: four `layoutDirection="locale"` Literals in Material
    layouts (silent, they are the correct spelling) and two `ltr` pins, both
    already reported by `direction_pins`.

    A resource reference (`@string/x`, `?attr/x`) is a value too, and a worse
    one: `isRtlResource` in Android's `TextView` shows the platform resolves it
    off the deprecated `Configuration`, so a locale-keyed resource never reaches
    a widget that re-resolved after a locale change.

    `layoutDirection` literals are only reported when the manifest opts into
    RTL; when it opts out, the manifest is the finding and the rows are not.

    Returns [(kind, path, tag, value)] with kind "layoutDirection",
    "textDirection", or "supportsRtl" (path `AndroidManifest.xml`, value
    "false").
    """
    harmless = {"locale", "firstStrong", "anyRtl", "inherit"}
    declared = declared_direction_values(proj)
    supports_rtl = manifest_supports_rtl(proj)
    out = []
    sources = text_sources(proj, ("layout*/**/*.xml",))
    if jobs <= 1 or len(sources) < 8:
        for rel, txt in sources:
            out.extend(rtl_enabled_in(txt, rel, declared, supports_rtl))
    else:
        out.extend(_spread_pairs(sources, _rtl_chunk, jobs))
    # An opt-out is a `lint` finding, not a `--fix` one: the attribute is a
    # build-wide switch, so the warning points at the manifest instead of
    # promising a row rewrite.
    if supports_rtl is False:
        out.append(("supportsRtl", "AndroidManifest.xml", "", "false"))
    return out



def rtl_enabled_in(txt, rel, declared, supports_rtl):
    """`rtl_enabled` for one file: the walk, the subtree, the literal gates.

    Module level and argument-only so a `spawn` pool can pickle it. `declared`
    and `supports_rtl` are the parent's (a values file and the manifest), passed
    through unchanged — that is what makes the pooled answer the serial answer.
    """
    harmless = {"locale", "firstStrong", "anyRtl", "inherit"}
    out = []
    bounds = [(off, tag, body, dep, sc) for tag, body, dep, sc, off
              in walk_layout(txt, full=True)]
    for n, (off, tag, body, dep, sc) in enumerate(bounds):
        # The subtree ends at the next start tag that is not deeper — the
        # next sibling, an uncle, or (for a leaf) the tag right after it.
        stop = len(txt)
        if not sc:
            for m2 in range(n + 1, len(bounds)):
                if bounds[m2][3] <= dep:
                    stop = bounds[m2][0]
                    break
        subtree = txt[off:stop]
        for m in re.finditer(r"(?<![\w.\-])((?:\w+:)?)(layoutDirection|textDirection)"
                             r"\s*=\s*[\x22\x27]([\w@?][\w\-./@+:]*)", body):
            # One attribute per match, on the element that actually carries
            # it: `body` is the start tag alone.
            prefix, attr, val = m.group(1), m.group(2), m.group(3)
            if val in harmless or val in declared:
                continue
            if attr == "textDirection" and val == "rtl":
                continue  # follows the locale on an RTL screen anyway
            if attr == "layoutDirection" and val == "rtl" and looks_leaf(tag):
                continue  # a mirrored glyph, not a container direction
            # A `layoutDirection` literal cannot beat a locale that never
            # turned RTL on. Measured, not assumed: the gate fires only on an
            # explicit opt-out. An absent `supportsRtl` keeps the row
            # reported — the default is pitfall (see `rtl`), but `lint`
            # runs on projects the author may switch on later, and a silent
            # row there is a defect nobody sees.
            if attr == "layoutDirection" and supports_rtl is False:
                continue
            if prefix and "." in tag:
                continue  # a dotted class is custom: it may read its own styleable
            # The subtree, plus the element's own tag: `walk_layout` yields
            # only the tag's own text, so a wrapper reads as empty and its
            # children are the only evidence; a leaf's subtree starts after
            # its `<`, so the tag has to be tested separately or every leaf
            # looks empty and its own pin is dropped.
            if not (RE_TEXT_WIDGET.search(subtree)
                    or RE_TEXT_WIDGET.match("<" + tag)):
                continue  # see `RE_TEXT_WIDGET` — order, not language
            out.append((attr, rel, tag, val))
    return out


def manifest_supports_rtl(proj):
    """`android:supportsRtl` as the manifest states it.

    True/False, or None when no `<application>` writes the attribute at all.
    None is not False: an absent attribute is what `aqlab rtl` adds, so it is
    not a defect, while an explicit `false` is the author turning RTL off for
    the whole build.
    """
    manifest = Path(proj) / "AndroidManifest.xml"
    if not manifest.exists():
        return None
    txt = manifest.read_text(encoding="utf-8", errors="replace")
    # Comments blanked first: an apktool dump keeps the original manifest in a
    # `<!-- -->` block, and a commented-out `supportsRtl="false"` is not one.
    live = re.sub(r"<!--.*?-->", "", txt, flags=re.DOTALL)
    for tag, body in iter_start_tags(live):
        if tag != "application":
            continue
        # Anchored on the attribute *name* and closed by its own quote, so a
        # neighbour whose value merely spells `supportsRtl='false'` is not the
        # manifest (measured: the flip landed inside `android:label`).
        m = re.search(r"(?:^|\s)((?:[\w.-]+:)?supportsRtl\s*=\s*)"
                      r"([\x22\x27])(true|false)\2", body)
        if m:
            return m.group(3) == "true"
    return None


def rtl_drawable_dirs(proj, jobs=1):
    """`res/drawable-ldrtl-*` folders that mirror nothing.

    A `ldrtl` qualifier is only meaningful if the file inside differs from the
    one the LTR build picks. Aapt happily merges an identical copy, so the build
    stays green while the author's intent ("this icon flips") is silently lost —
    either the folder was scaffolded and never filled, or a file was copied in
    unchanged instead of redrawn.

    Two conditions, both cheap and both certain:
      * the folder holds no drawable at all (empty, or only leftover `.gitkeep`)
      * a file is byte-identical to the same-named file in the unqualified peer
        directory — `drawable-ldrtl-xxhdpi` is compared against
        `drawable-xxhdpi` first, then against `drawable`

    Skipping honest cases is deliberate: a file with no LTR counterpart at all
    (ldrtl-only, e.g. a mirrored spinner Android ships that way) is not touched,
    and a `.9.png` that differs in any byte passes. Measured on a real APK:
    `drawable-ldrtl-xxhdpi` holds one apktool-decoded Material spinner with no
    twin, so it stays silent.

    Returns [(path, kind, detail)] with kind "empty" or "identical".
    """
    res = Path(proj) / "res"
    if not res.is_dir():
        return []
    out = []
    # Only the two resource types that are drawables. `values-ldrtl` is a
    # different story (strings) and mirroring geometry is not what it means.
    # `--jobs` has nothing to spread here: the work per file is one `read_bytes`
    # of a few hundred bytes against a name it already has, and the pool would
    # cost more than the compare. Accepted for call-site symmetry and ignored,
    # deliberately — a knob that changed nothing is honest; a knob that spawned
    # workers for 8 files is not.
    del jobs
    for d in sorted(res.glob("drawable*-ldrtl*")) + sorted(res.glob("mipmap*-ldrtl*")):
        if not d.is_dir():
            continue
        files = [f for f in d.iterdir()
                 if f.is_file() and not f.name.startswith(".")]
        rel = d.relative_to(proj).as_posix()
        if not files:
            out.append((rel, "empty", ""))
            continue
        # drawable-ldrtl-xxhdpi -> ("drawable", "-xxhdpi"); the pre-qualifier is
        # plain string surgery, not a parsing problem, and both halves are
        # exactly what reconstructs the peer directory name.
        head = d.name.split("-ldrtl")[0]
        tail = d.name.split("-ldrtl", 1)[1]
        peers = [res / f"{head}{tail}", res / head]
        for f in sorted(files):
            for peer in peers:
                twin = peer / f.name
                if not twin.is_file():
                    continue
                try:
                    if f.read_bytes() == twin.read_bytes():
                        out.append((f"{rel}/{f.name}", "identical",
                                    peer.relative_to(proj).as_posix()))
                except OSError:
                    pass
                break
    return out


def iter_start_tags(txt):
    """Yield (tag, body) for every start tag in an XML text.

    A scanner, not a regular expression. `body` runs from just after the tag
    name to the closing `>`, and a `>` inside a quoted value does not end it, so
    `android:tag="a>b"` cannot cut a tag in half.

    This replaced a regex. On CPython 3.14's `re` an alternation whose first
    branch starts with a whitespace class silently stops repeating: `(?:\\s+[^<>]|
    "[^"]*")*` matched only the first attribute, every later check saw a
    truncated element, and the `DirectionPins` check found nothing at all. A
    scanner has no such failure mode, so this walks the text once instead.
    """
    n, i = len(txt), 0
    while True:
        i = txt.find("<", i)
        if i < 0:
            return
        if txt.startswith("</", i) or txt.startswith("<!", i) or txt.startswith("<?", i):
            i += 2
            continue
        j, quote = i + 1, None
        while j < n:
            ch = txt[j]
            if quote:
                if ch == quote:
                    quote = None
            elif ch == chr(34) or ch == chr(39):
                quote = ch
            elif ch == ">":
                break
            j += 1
        if j >= n:
            return
        body = txt[i + 1:j]
        end = 0
        while end < len(body) and not body[end].isspace() and body[end] != "/":
            end += 1
        yield body[:end], body[end:]
        i = j + 1

def count_legacy_attrs(proj):
    n = 0
    res = Path(proj) / "res"
    if not res.is_dir():
        return 0
    pat = re.compile(r"\b(?:android|app):(%s)\s*=" % "|".join(ATTR_RENAME))
    for f in res.rglob("*.xml"):
        try:
            n += len(pat.findall(f.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            pass
    return n


# Android's own `RtlSymmetry` check. Padding or margin on one side without the
# other is not an error a compiler sees — it renders as an off-centre list or a
# title jammed against one edge once the layout flips. Google's rule, verified
# against the lint docs: if paddingLeft is set, paddingRight probably should be.
SYMMETRY_PAIRS = (
    ("paddingLeft", "paddingRight", "padding"),
    ("paddingStart", "paddingEnd", "padding"),
    ("layout_marginLeft", "layout_marginRight", "margin"),
    ("layout_marginStart", "layout_marginEnd", "margin"),
)


def symmetry_problems(proj, jobs=1):
    """Elements with one side's padding/margin set and the other missing.

    Scans element-by-element rather than file-wide: the pair has to be missing
    from the *same* view, and a file with paddingLeft on one row and paddingRight
    on another is not symmetric in any useful sense.

    left/right and start/end are independent pairs. Within one spelling, a side
    without its partner is the finding — unless the *other* spelling covers both
    of that pair's sides. Measured on a real APK: `paddingLeft`+`paddingStart`
    +`paddingEnd` is Material's minSdk-era shape, and reporting it was a false
    positive on all ten of that APK's symmetry rows (minSdkVersion 26, where
    start/end are always honoured). The reverse is not true: `paddingLeft`+
    `paddingRight` alone is still reported, because `paddingStart`/`paddingEnd`
    are what a flip actually needs.
    """
    return _spread_pairs(text_sources(proj, ("layout*/*.xml", "menu*/*.xml")),
                         _symmetry_chunk, jobs)


def _symmetry_chunk(pair):
    """Symmetry holes in one (relative path, text) pair.

    The element loop of `symmetry_problems`, in one body so the pooled and the
    serial answer cannot drift: the parent spells the file list, the child only
    runs this regex over the text it was handed.
    """
    found = []
    elem = re.compile(r"<([A-Za-z][\w.]*)\b([^>]*?)/?>", re.DOTALL)
    name, txt = pair
    for m in elem.finditer(txt):
        tag, body = m.group(1), m.group(2)
        have = {}
        # Both quote characters: Android accepts either, and `aqlab rtl` now
        # migrates a single-quoted side, so the two must agree on what they see.
        for a in re.finditer(
                r"\b(?:android:)?([\w.]+)\s*=\s*[\x22\x27](.*?)[\x22\x27]", body):
            have[a.group(1)] = a.group(2)
        # A physical pair is covered by the modern pair: `paddingLeft` with
        # both `paddingStart` and `paddingEnd` needs no `paddingRight` on a
        # device that honours start/end. Measured on the real APK: all ten of
        # its symmetry rows are that shape (minSdkVersion 26), so every one of
        # them was a false positive.
        # `paddingStart`+`paddingEnd` present means the physical side has a
        # working modern twin: pair the spellings by their kind word.
        covered = set()
        for phys_l, phys_r, kind in SYMMETRY_PAIRS:
            mod = "layout_margin" if kind == "margin" else kind
            if (mod + "Start") in have and (mod + "End") in have:
                covered.add(phys_l)
                covered.add(phys_r)
        for left, right, _kind in SYMMETRY_PAIRS:
            if left in have and right not in have:
                if left in covered:
                    continue
                found.append((name, tag, left, right,
                              have[left]))
            elif right in have and left not in have:
                if right in covered:
                    continue
                found.append((name, tag, right, left,
                              have[right]))
    return found


GRAVITY_RTL = {"left": "start", "right": "end"}


def symmetric_fix(body):
    """Add the missing partner of every padding/margin pair in one element body.

    Only *adds* where `symmetry_problems` found a hole, so `aqlab lint --fix` and
    `symmetry_problems` can never disagree — a fixer that repairs something the
    report never names is indistinguishable from one that invents attributes.

    The missing side is written as `start`/`end`, not as the `left`/`right` it
    mirrors: copying the spelling back would leave the flip broken and the next
    lint run reporting the same element forever.

    An element already carrying any modern spelling is left alone. The author is
    appcompat-aware, and rewriting their `left` is `aqlab rtl`'s job, not the
    linter's. Measured on a real APK: 40 of its 49 symmetry findings are
    ImageView rows carrying both spellings, so this one guard is what keeps
    `--fix` from touching code that was already right.

    Pure: body in, body out. Idempotent — a fixed body has no holes left.
    """
    # Both quote characters, like `symmetry_problems`: it reports a
    # single-quoted `paddingLeft`, so the fixer has to see it too or the
    # report names an element `--fix` can never repair.
    have = set(re.findall(r'\b(?:android:)?([\w.]+)\s*=\s*[\x22\x27]', body))
    # An element that already uses start/end is appcompat-aware: fill the missing
    # modern side, leave its physical attributes to . An element with
    # only physical attributes is not ours to touch at all.
    modern = {a for pair in SYMMETRY_PAIRS[1::2] for a in pair[:2]}
    if not have & modern:
        return body
    for left, right, _kind in SYMMETRY_PAIRS[1::2]:
        if (left in have) == (right in have):
            continue
        have_a, miss_b = (left, right) if left in have else (right, left)
        m = re.search(r'\b(?:android:)?%s\s*=\s*([\x22\x27])([^\x22\x27]*)\1' % have_a, body)
        if not m:
            return body          # reported but our regex lost it: do not guess
        target = ATTR_RENAME.get(miss_b, miss_b)
        value = re.sub(GRAVITY_TOKEN, lambda t: GRAVITY_RTL[t.group(1)],
                       m.group(2))
        # Right after the attribute it mirrors. Appending at the end of a 40-line
        # tag hides the pair where nobody looks; one long line stays one line.
        body = body[:m.end()] + ' android:%s="%s"' % (target, value) + body[m.end():]

    return body


def scan_apk(apk, args):
    if not Path(apk).exists():
        raise Fail(f"{apk} not found")
    aapt2 = tool("aapt2")
    p = run([aapt2, "dump", "badging", apk], quiet=True, check=False)
    locales, pkg, label = [], "?", "?"
    for line in p.stdout.splitlines():
        if line.startswith("package:"):
            pkg = line.split("name='", 1)[-1].split("'", 1)[0]
        elif line.startswith("locales:"):
            locales = re.findall(r"'([^']+)'", line)
        elif line.startswith("application-label:"):
            label = line.split(":", 1)[1].strip()
    xml = run([aapt2, "dump", "xmltree", "--file", "AndroidManifest.xml", apk],
              quiet=True, check=False).stdout
    # aapt2 renders a boolean attribute symbolically — supportsRtl(0x010103af)=true —
    # not as the raw 0xffffffff, so testing for the raw value never matched and
    # every real APK was reported as not supporting RTL. Accept both renderings,
    # and treat an explicit =false as false rather than as absence.
    m_rtl = re.search(r"supportsRtl\(0x[0-9a-fA-F]+\)\s*=\s*(true|false|0x[0-9a-fA-F]+)", xml)
    if m_rtl:
        rtl_on = m_rtl.group(1).lower() in ("true", "0xffffffff")
    else:
        rtl_on = bool(re.search(r'supportsRtl[^\n]*\(Raw:\s*"?true"?\)', xml))
    with zipfile.ZipFile(apk) as z:
        names = z.namelist()
    has_dpi = any(n.startswith("lib/") for n in names)
    print(f"[FINDING] apk: {Path(apk).name}")
    print(f"  package        : {pkg}")
    print(f"  label          : {label}")
    print(f"  locales        : {len(locales)} -> {', '.join(locales[:14]) or 'none'}")
    print(f"  arabic present : {'yes' if any(x.startswith('ar') for x in locales) else 'NO'}")
    print(f"  supportsRtl    : {'yes' if rtl_on else 'no (or not detectable in binary XML)'}")
    print(f"  native libs    : {len([n for n in names if n.startswith('lib/')])} "
          f"(renaming packages in native apps can break self-checks)")
    if args.json:
        print(json.dumps({"package": pkg, "locales": locales,
                          "arabic": any(x.startswith("ar") for x in locales),
                          "supportsRtl": rtl_on}, ensure_ascii=False))
    fmt = getattr(args, "report", None)
    if fmt:
        # `scan` has no translation coverage to report, so the coverage block is
        # empty rather than invented: 0/0 would read as "nothing translated" on
        # a finished app. Manifest facts are findings, so they land in the same
        # findings table under their own rule ids.
        findings = [("info" if rtl_on else "error", "AndroidManifest.xml:supportsRtl",
                     "RTL supported" if rtl_on else "supportsRtl is off — run `aqlab rtl`",
                     "rtl-manifest"),
                    ("info" if any(x.startswith("ar") for x in locales) else "warn",
                     "resources:ar", "Arabic resources present"
                     if any(x.startswith("ar") for x in locales)
                     else f"no Arabic resources ({len(locales)} locale(s))",
                     "translation")]
        rep = build_report(Path(apk), "ar", 0.0, 0, 0, {}, findings,
                           sum(f[0] == "error" for f in findings), False,
                           command="scan")
        rep["apk"] = {"package": pkg, "label": label,
                      "locales": locales, "supportsRtl": rtl_on}
        write_report(fmt, getattr(args, "out", None), rep, Path(apk).parent)
    return 0


def apk_strings(apk):
    """`{(locale, name): value}` straight out of a compiled APK.

    `aapt2 dump resources` prints the resolved value per configuration, so two
    APKs can be compared without decoding either one — no 50-second apktool run
    per side, and no working copy to clean up.

    The locale is `""` for the default configuration. A value that aapt2 could
    not resolve (a reference to another resource) comes back as `@0x…` and is
    skipped: it says nothing about the text and would show up as a change on
    every single diff.
    """
    aapt2 = tool("aapt2")
    out = run([aapt2, "dump", "resources", str(apk)], quiet=True, check=False).stdout
    table = {}
    cur = None
    for line in out.splitlines():
        m = re.match(r"\s*resource 0x[0-9a-fA-F]+ (\w+)/(\S+)", line)
        if m:
            kind, name = m.group(1), m.group(2)
            cur = name if kind == "string" else None
            continue
        if cur is None:
            continue
        m = re.match(r'\s*\(([^)]*)\)\s*"(.*)"\s*$', line)
        if m:
            loc, val = m.group(1), m.group(2)
            # `(ar)` for a locale, `()` for the default configuration; a
            # qualifier like `(ar-night)` is not a locale and is ignored.
            if loc and not re.match(r"^[a-z]{2,3}(-r[A-Z]{2})?$", loc):
                continue
            if val.startswith("@"):
                continue
            table[(loc, cur)] = val
        elif not line.startswith(" "):
            cur = None
    return table


def diff_strings(old, new, locale=None, limit=40):
    """Added / removed / changed string resources between two APK tables.

    Returns a dict of lists so the caller can print, count or gate on them.
    A resource present in both but pointing at the same text is not a change —
    identical text is the majority of every diff and would bury the real ones.
    """
    def for_locale(table):
        return {(loc, name): val for (loc, name), val in table.items()
                if locale is None or loc == locale}

    a, b = for_locale(old), for_locale(new)
    added = sorted(k for k in b if k not in a)
    removed = sorted(k for k in a if k not in b)
    changed = sorted(k for k in a if k in b and a[k] != b[k])
    return {"added": added, "removed": removed, "changed": changed,
            "added_n": len(added), "removed_n": len(removed), "changed_n": len(changed)}


def apply_xliff(text):
    """Wrap each format specifier in `<xliff:g>`, so a translator cannot touch it.

    `<xliff:g id="…">%1$s</xliff:g>` tells translation tooling that the content is
    a placeholder rather than words. A specifier left bare gets reordered,
    pluralised or dropped by a machine translator, and the app then crashes at
    `String.format` with an argument mismatch — one of the most common
    localisation crashes there is.

    Verified with aapt2: the tag compiles, links, and resolves back to the bare
    `%1$s`, so wrapping changes nothing at runtime.

    An id is required by the schema. `argN` is positional, taken from the
    specifier's own index where it has one (`%2$d` → `arg2`) so the ids stay
    stable across a re-translation instead of shifting with word order.
    `%%` is a literal percent, not a placeholder, and is left alone.
    """
    counter = [0]

    def wrap(m):
        spec = m.group(0)
        if spec == "%%":
            return spec
        pos = re.match(r"%(\d+)\$", spec)
        if pos:
            name = f"arg{pos.group(1)}"
        else:
            counter[0] += 1
            name = f"arg{counter[0]}"
        return f'<xliff:g id="{name}">{spec}</xliff:g>'
    return RE_FORMAT.sub(wrap, text)


def has_xliff(text):
    return "<xliff:g" in text


def cmd_xliff(args):
    """`aqlab xliff <dir>` — wrap format specifiers across a locale folder."""
    proj = Path(args.proj)
    res = proj / "res"
    if not res.is_dir():
        raise Fail(f"{res} not found — is this a decoded project?")
    locale = args.locale
    targets = sorted((res / f"values-{locale}").glob("*.xml")) if locale else []
    if not targets:
        raise Fail(f"no res/values-{locale}/*.xml — run `scaffold` first")

    sink = Writes(dry=getattr(args, "dry_run", False))
    total = files = 0
    for f in targets:
        raw = f.read_text(encoding="utf-8", errors="replace")
        body = raw
        # only the text between tags; attribute values are never specifiers
        def fix(m):
            nonlocal total
            inner = m.group(2)
            if has_xliff(inner) or not _formats(inner):
                return m.group(0)
            new = apply_xliff(inner)
            total += new.count("<xliff:g")
            return m.group(1) + new + m.group(3)
        body = re.sub(r"(<(?:string|item)\b[^>]*>)(.*?)(</(?:string|item)>)",
                      fix, body, flags=re.DOTALL)
        if body != raw:
            if "<xliff:g" in body and "xmlns:xliff" not in body:
                # the namespace has to be declared or the file is not well-formed
                body = re.sub(r"<resources(\s|>)",
                              r'<resources xmlns:xliff="urn:oasis:names:tc:xliff:document:1.2"\1',
                              body, count=1)
            sink.add(f, body)
            files += 1

    if not files:
        print("[OK] every format specifier is already wrapped")
        return 0
    if sink.dry:
        print(f"[DRY] would wrap {total} specifier(s) in {files} file(s)")
        sink.report()
        return 0
    sink.commit()
    print(f"[DONE] wrapped {total} specifier(s) in {files} file(s) -> res/values-{locale}")
    return 0


def cmd_diff(args):
    for p in (args.old, args.new):
        if not Path(p).exists():
            raise Fail(f"{p} not found")
    old, new = apk_strings(args.old), apk_strings(args.new)
    if not old or not new:
        raise Fail("no string resources found — is this an APK? "
                   "a decoded project needs `lint`, not `diff`")
    d = diff_strings(old, new, locale=getattr(args, "locale", None))

    tag = f" [{args.locale}]" if getattr(args, "locale", None) else ""
    print(f"[FINDING] diff{tag}: {Path(args.old).name} -> {Path(args.new).name}")
    print(f"  strings        : {len(old)} -> {len(new)}")
    print(f"  added          : {d['added_n']}")
    print(f"  removed        : {d['removed_n']}")
    print(f"  changed        : {d['changed_n']}")

    if args.json:
        def keyed(keys):
            return [f"{loc or '(default)'}|{name}" for loc, name in keys]
        print(json.dumps({"added": keyed(d["added"]),
                          "removed": keyed(d["removed"]),
                          "changed": keyed(d["changed"])}, ensure_ascii=False))
        return 0 if not (d["added_n"] or d["removed_n"] or d["changed_n"]) else 1

    for label, keys, mark in (("added", d["added"], "+"),
                              ("removed", d["removed"], "-"),
                              ("changed", d["changed"], "~")):
        if not keys:
            continue
        print(f"\n  {label}:")
        for loc, name in keys[:args.limit]:
            print(f"    {mark} {'(' + loc + ')' if loc else '(default)'} {name}")
            if mark == "~":
                print(f"        - {old[(loc, name)]}")
                print(f"        + {new[(loc, name)]}")
        if len(keys) > args.limit:
            print(f"    … and {len(keys) - args.limit} more (--limit to change)")

    # exit code is 0 when the two are identical, so a CI step can gate on drift
    return 0 if not (d["added_n"] or d["removed_n"] or d["changed_n"]) else 1


def cmd_lint(args):
    proj = Path(args.proj)
    # `lint --jobs N`: one read per file, then every layout sweep below is
    # spread. 1 (the default) keeps the whole run serial.
    jobs = getattr(args, "jobs", None)
    jobs = 1 if jobs is None else max(0, jobs) or 1
    res = proj / "res"
    if not find_default_strings(proj):
        raise Fail("no values/strings.xml — decode the APK first")

    # Read the whole values folder, not one file. Plurals routinely live in their
    # own `plurals.xml` — AOSP and most Material apps ship them that way — so
    # reading only strings.xml silently skipped exactly the entries the plural
    # check exists to inspect. Region variants (values-ar-rSA) count as `ar` too.
    src = {e.key: e for e in read_locale_entries(res, ["values"]) if e.translatable}
    # Match on the `values-` prefix, not on `values-{locale}`: Android lowercases
    # the qualifier it writes (`values-ar`, `values-b+sr+Latn`) but a caller may
    # pass `ar-rSA`-style mixed case, and `glob("values-ar-rSA*")` is
    # case-sensitive on Linux/macOS — it found nothing and lint reported a
    # missing translation folder that was right there.
    want = args.locale.lower()
    ar_dirs = sorted(d.name for d in res.iterdir()
                     if d.is_dir() and d.name.lower().startswith(f"values-{want}"))
    issues, errors = [], 0

    if not ar_dirs:
        print(f"[FINDING] no res/values-{args.locale}/ — run `aqlab scaffold`")
        return 1
    tr = {e.key: e for e in read_locale_entries(res, ar_dirs)}

    def add(level, key, msg, rule=None):
        nonlocal errors
        if level == "error":
            errors += 1
        issues.append((level, key, msg, rule))

    for key, e in src.items():
        t = tr.get(key)
        if t is None:
            continue
        a = t.text
        if a == e.text and RE_LATIN.search(RE_FORMAT.sub("", a)):
            add("warn", key, "identical to source and contains Latin — probably untranslated",
                "translation")
        elif (RE_ARABIC.search(e.text) is None
              and RE_ANY_ALNUM.search(RE_FORMAT.sub("", e.text))
              and not RE_ARABIC.search(a)):
            add("warn", key, "no Arabic characters in the Arabic value", "translation")
        if RE_BARE_PERCENT.search(RE_FORMAT.sub("", a)):
            add("error", key, "bare `%` — must be `%%` or a format specifier, aapt will reject it",
                "format")
        d_fmt, a_fmt = _formats(e.text), _formats(a)
        # Positional arguments are a set, not a sequence: writing `%2$d/%1$d`
        # for `%1$d/%2$d` renders the counter in RTL reading order and Android
        # fills both — it is the correct translation of `%1$d/%2$d`, and every
        # format error on the reference APK was exactly this pair. A value that
        # drops or invents an argument still differs as a set; that is the
        # finding, and the message names the lists as before.
        d_set, a_set = _sections(d_fmt), _sections(a_fmt)
        same_args = d_set is not None and a_set is not None and d_set == a_set
        if d_fmt != a_fmt and not same_args:
            # The count is legitimately absent from Arabic `zero`/`one`/`two`
            # ("لا عناصر", "عنصر واحد", "عنصران"), so a specifier the Arabic value
            # does not need is not a finding. Inventing one never is.
            no_count = (e.kind == "plural"
                        and e.key.rpartition("|")[2] in ("zero", "one", "two")
                        and len(a_fmt) < len(d_fmt))
            if not no_count:
                lvl = "error" if (e.kind == "string" or len(a_fmt) > len(d_fmt)) else "warn"
                add(lvl, key, f"format specifiers differ: source {d_fmt or '-'} "
                              f"vs arabic {a_fmt or '-'}", "format")
        if e.text.endswith(" ") != a.endswith(" "):
            add("warn", key, "leading/trailing space mismatch — concatenated strings will run together",
                "whitespace")
        if re.search(r"(?<!\\)'", a):
            add("error", key, "unescaped apostrophe — use \\' inside Android string resources", "escape")
        if a[:1] in ("@", "?"):
            add("error", key, "value starts with `@` or `?` — must be escaped as \\@ / \\?", "escape")
        if RE_ARABIC.search(a) and RE_LATIN.search(a):
            add("info", key, "mixed Arabic + Latin — verify order on a real device", "mixed-script")
        if _tags(e.text) != _tags(a):
            add("error", key, f"markup tags differ: source {_tags(e.text) or '-'} "
                              f"vs arabic {_tags(a) or '-'}", "markup")

    # Coverage is "how many of the source entries have an Arabic counterpart".
    # It used to be len(tr)/len(src), which is not the same number: a correct
    # Arabic <plurals> has six quantities where the English source has two, so
    # the ratio could exceed 100% (233% on a seven-entry file).
    covered = sum(1 for k in src if k in tr)
    cov = 100.0 * covered / max(len(src), 1)

    # One number hides the file that is actually behind. A locale at 88% overall
    # can have strings.xml at 99% and plurals.xml at 60%, and the 60% is what a
    # reader sees on screen. Report per source file, and let CI gate on it.
    per_file = {}
    for k, e in src.items():
        f = e.src or "strings.xml"
        got, total = per_file.get(f, (0, 0))
        per_file[f] = (got + (1 if k in tr else 0), total + 1)

    # --- Arabic plurals, checked as a structure and not as loose keys ---------
    # `read_entries` flattens <plurals> into one key per quantity, which is right
    # for format/escaping checks and completely blind to the thing that actually
    # breaks Arabic: an English source can only offer `one` and `other`, so an
    # Arabic file that mirrors it is missing the two categories that cover almost
    # every count. Nothing else in the tool would ever notice.
    def by_plural(entries):
        out = {}
        for e in entries:
            if e.kind == "plural":
                name, _, q = e.key.partition("|")
                out.setdefault(name, {})[q] = e
        return out

    # Baked-in lettering in vector geometry: `lint` cannot fix it and a
    # heuristic must never fail a build, so it is a warning with the path so the
    # user can look at the drawing itself.
    for pth, n, w, h in text_like_vectors(proj, jobs):
        add("warn", pth, f"{n} paths at {w}x{h}dp — looks like lettering baked into "
                         f"geometry; it will not mirror with the UI, check it by eye",
            "vector-text")

    # Android's RtlSymmetry: one side without the other renders off-centre once
    # the layout flips, and nothing in the build says so.
    for path, tag, have_a, missing_b, value in symmetry_problems(proj, jobs):
        add("warn", f"{path}:<{tag}>",
            f"{have_a}=\"{value}\" without {missing_b} — not symmetric when the "
            f"layout flips", "rtl-symmetry")

    # RtlHardcoded: a physical side in a gravity value does not follow the
    # locale, so it has to be renamed by hand — say where.
    for path, attr, value in hardcoded_gravity(proj, jobs):
        add("warn", f"{path}:{attr}", f'"{value}" pins text to a physical side; '
                                      f'`aqlab lint --fix` rewrites it to start/end',
            "rtl-hardcoded")

    # A literal `android:text` never reaches `values-<locale>`, so it is the one
    # class of string the table cannot cover. Reported, never fixed: `--fix`
    # would have to invent a resource name and write into `values/`.
    for path, tag, attr, value, why in literal_text_attrs(proj, jobs):
        shown = value if len(value) <= 40 else value[:37] + "..."
        if why == "untranslatable-ref":
            add("warn", f"{path}:<{tag}>",
                f'{attr}="{shown}" points at a string marked translatable="false" '
                f"— it stays English in {args.locale}", "literal-text")
        else:
            add("warn", f"{path}:<{tag}>",
                f'{attr}="{shown}" is a literal in the layout — it never reaches '
                f"values-{args.locale}; move it to `@string/` and translate",
                "literal-text")

    # A pinned direction on a row that also carries left/right attributes is the
    # author working around RTL instead of supporting it — the pin wins over the
    # locale and the row stays LTR in Arabic.
    for kind, where, *rest in direction_pins(proj, jobs):
        if kind == "supportsRtl":
            # Reported by rtl_enabled and nowhere else: two rules naming the
            # same manifest printed the same error twice and counted two errors
            # for one byte. rtl_enabled owns it because it also owns the
            # reference-valued rows beside it.
            pass
        elif kind == "layoutDirection":
            tag_, val = rest
            # Only `ltr` is repairable: dropping it lets the row follow the
            # locale. An `rtl` pin is a deliberate mirror, so the message must
            # not promise a fix `--fix` leaves alone.
            tip = ("; `aqlab lint --fix` drops the pin and migrates the row"
                   if val == "ltr" else "")
            add("warn", f"{where}:<{tag_}>",
                f'layoutDirection="{val}" pins a horizontal row that already sets '
                f'left/right attrs — it will not flip in {args.locale}{tip}',
                "rtl-direction")
        elif kind == "pinNested":
            tag_, val, names = rest
            add("warn", f"{where}:<{tag_}>",
                f'inherits layoutDirection="{val}" from an ancestor and sets '
                f'{", ".join(names)} — the ancestor pin keeps this row {val} in '
                f'{args.locale}; `aqlab lint --fix` migrates the row, not the pin',
                "rtl-direction")
        else:
            tag_, val = rest
            add("warn", f"{where}:<{tag_}>",
                # `ltr` is repairable by `fix_pin_local` and `rtl` is left
                # alone, exactly like the `layoutDirection` message above —
                # the tip promised a repair only when there is one.
                f'textDirection="{val}" pins the text of a row that already sets '
                f'left/right attrs — it stays {val} in {args.locale}'
                + ('; `aqlab lint --fix` drops the pin and migrates the row'
                   if val == 'ltr' else ''), "rtl-direction")

    # RtlEnabled: a direction literal that is not a pin but still wins over the
    # locale. `direction_pins` only names the row that also carries physical
    # attributes; this is the same literal on a row that carries none, where the
    # default would have followed the language and the literal does not.
    for attr, path, tag_, val in rtl_enabled(proj, args.locale, jobs):
        if attr == "supportsRtl":
            # Not a row and not a `--fix` job: the switch has to be on before
            # any row can mirror, so the message names the command that flips it.
            # Keyed by the attribute, not by `where`: `where` is the loop's
            # leftover string resource key and pointed the message at a string.
            add("error", f"{path}:supportsRtl",
                'android:supportsRtl="false" — the manifest opts out of RTL '
                'entirely; run `aqlab rtl`', "rtl-enabled")
            continue
        ref = val.startswith(("@", "?"))
        add("warn", f"{path}:<{tag_}>",
            f'{attr}="{val}" overrides the locale on an element with no physical '
            f'attrs — {args.locale} shows it {val}'
            # A reference is repairable nowhere: dropping it changes which value
            # the row picks, so the tip would promise a rewrite it cannot make.
            + ("; `aqlab lint --fix` drops the pin" if val == "ltr" and not ref
               else ""), "rtl-enabled")

    # A `ldrtl` folder that mirrors nothing is not a translation problem, so it
    # is reported without the locale gate above — it costs the same in any RTL
    # language and the author meant it to flip.
    for where, kind, detail in rtl_drawable_dirs(proj, jobs):
        if kind == "empty":
            add("warn", where, "ldrtl folder is empty — it mirrors nothing; fill it "
                               "or delete it so the build stops looking RTL-ready",
                "rtl-drawable")
        else:
            add("warn", where, f"byte-identical to {detail}/ — an unchanged copy does "
                               f"not mirror; redraw it or delete it", "rtl-drawable")

    print(f"[FINDING] lint res/values-{args.locale}: {covered}/{len(src)} translated ({cov:.1f}%)")
    # Per file only when there is more than one, so the common case stays one line.
    if len(per_file) > 1 or args.verbose:
        worst = min(per_file.items(), key=lambda kv: kv[1][0] / max(kv[1][1], 1))
        for f in sorted(per_file, key=lambda k: per_file[k][0] / max(per_file[k][1], 1)):
            got, total = per_file[f]
            pct = 100.0 * got / max(total, 1)
            tag = "  <- weakest" if f == worst[0] and pct < cov else ""
            print(f"            {f:<20} {got}/{total}  {pct:5.1f}%{tag}")
    # Plural sweep runs before the fixers so its errors count, but its findings
    # are held back from the printed list: no fixer can repair one, and a
    # warning printed under `fixed:` reads as work the fixer declined.
    plural_mark = len(issues)
    # A locale CLDR has no plural row for (or that we cannot resolve at all —
    # `xx`, a typo) makes every plural check below vacuous: `plural_required`
    # and `plural_categories` both return () so a <plurals> copied from English
    # passes clean. Say so once rather than reporting a clean bill of health
    # nobody earned.
    if not plural_categories(args.locale):
        print(f"[NOTE] no CLDR plural row for `{args.locale}` — plural forms in "
              "this locale are not checked (the coverage and RTL rows still are)")
    ar_pl = by_plural(tr.values())
    for name in sorted(by_plural(src.values())):
        got = ar_pl.get(name)
        label = f"plurals/{name}"
        if not got:
            add("warn", label, f"no {args.locale} <plurals> for this name — every count "
                               "will fall back to the source form", "plurals")
            continue

        # Which categories are required is a property of the target language, not
        # of Arabic: Hebrew has no `few`, Persian and Urdu have neither `few` nor
        # `zero`. Assuming all six would demand forms those languages never use.
        hard = [q for q in plural_required(args.locale) if q not in got]
        if hard:
            # Not a build failure: aapt2 compiles and links a translation whose
            # <plurals> lacks `other` (verified). It is worse than that — the
            # build is silent and the counts simply have no value in that locale.
            why = ("it covers every count the other categories do not (100, 101, …) "
                   "and the build will not warn" if "other" in hard
                   else "those counts fall back to `other`")
            add("error", label, "missing "
                f"{'quantity' if len(hard) == 1 else 'quantities'}: "
                + ", ".join(f"{q} ({plural_range(args.locale, q)})" for q in hard)
                + f" — {why}", "plurals")

        soft = [q for q in plural_optional(args.locale) if q not in got]
        if soft:
            add("warn", label, "no "
                + ", ".join(f"{q} ({plural_range(args.locale, q)})" for q in soft)
                + " form — those counts use `other`", "plurals")

        # A category the locale does not use is not wrong to ship (aapt2 and
        # ICU both accept it; the count simply falls through to `other`), but it
        # is text nobody will ever see and the shape the source file suggested.
        # No row for the locale at all (typo, or a language CLDR has never
        # described): "not a xx plural category" is a claim we cannot support,
        # and it fired on every perfectly ordinary `one`/`other` file — the
        # NOTE above already said plurals are unchecked here.
        cats = plural_categories(args.locale)
        if cats and (unknown := [q for q in got if q not in cats]):
            add("warn", label, f"{', '.join(sorted(unknown))} is not a "
                               f"{args.locale} plural category — those counts fall through "
                               "to `other`, so the form is never shown", "plurals")

        # Distinct categories carrying identical text is the tell that nobody
        # actually pluralised anything. Compared within one <plurals>, so the
        # set of categories has to come from this locale, not from Arabic: a
        # Hebrew `other` at 0 and 3 legitimately repeats, and Hebrew has no
        # `few`/`many` to compare in the first place.
        same = {}
        for q in plural_categories(args.locale):
            if q in got and q != "other":
                same.setdefault(got[q].text.strip(), []).append(q)
        for qs in same.values():
            if len(qs) > 1:
                add("warn", label, f"{', '.join(qs)} carry identical text — "
                                   "the plural forms are not actually pluralised", "plurals")

    plural_issues = issues[plural_mark:]
    del issues[plural_mark:]

    fixed = 0
    if getattr(args, "fix", False):
        # Recorded before the fixers run: each one below reads `issues` to decide
        # what to repair, so any finding added after this line is dropped from
        # this run's report — `--fix` never had a chance to look at it.
        seen = len(issues)
        # The manifest first: every row finding below is downstream of it, and a
        # warning promising "`--fix` drops the pin" is a lie on an app whose
        # `supportsRtl="false"` makes every pin unreachable anyway.
        fixed += fix_supports_rtl(proj, dry=getattr(args, "dry_run", False))
        # Pin migration first: it renames `paddingLeft` to `paddingStart`, and
        # the symmetry pass then pairs the *new* name in the same run. Run in
        # the other order the symmetry pass works from a stale finding whose
        # `paddingLeft` no longer exists, so `--fix` reported four elements
        # and wrote two (measured), leaving one run short of a clean tree.
        fixed += fix_pin_local(proj, dry=getattr(args, "dry_run", False))
        fixed += fix_pin_nested(proj, dry=getattr(args, "dry_run", False))
        # `fix_symmetry` is deliberately NOT called. Measured on a real APK: it
        # added 27 attributes across 16 Material layouts, and every one of them
        # changed the rendered shape — a `statusDot` with marginEnd=8 only
        # gained marginStart=8, a Spinner with marginStart=8 only gained
        # marginEnd=8. The layout had those elements against one edge *on
        # purpose*; the "missing" partner was not a hole to fill, it was the
        # design. Android's own RtlSymmetry is a warning for this exact reason
        # and never auto-fixes. The check stays in `lint` as a warning; the
        # fixer does not touch it.
        # (kept: `fix_symmetry` below, unused, for the day a `--fix-symmetry`
        # opt-in needs it — see its docstring)

        # Removed by an earlier edit that ate this line while inserting the
        # truncation below: the gravity sweep stopped being called, so `--fix`
        # printed its own finding and left the value in the file.
        fixed += fix_gravity(proj, dry=getattr(args, "dry_run", False))

        # Late findings are dropped from this run's list, not printed. The plural
        # sweep and any post-fix re-read land here, and the count above already
        # publishes what `--fix` repaired; printing a warning beside it reads as
        # work the fixer declined, and `--fix` was never handed these.
        issues = issues[:seen]

        # A promise already kept is a lie in the other direction: a pin finding
        # built before the repair still says "`--fix` drops the pin" on the run
        # that dropped it. The finding itself stays — the row does keep its
        # physical attrs — only the tip goes.
        if fixed and "layoutDirection=\"ltr\"" not in _pin_scan(proj):
            issues = [i if "`aqlab lint --fix` drops the pin" not in i[2]
                      else (i[0], i[1], i[2].split("; `aqlab lint --fix`")[0], i[3])
                      for i in issues]

        # `issues` was built before the repairs, so a row this run fixed would
        # still be printed as a finding — the report and the fixer disagreeing
        # about the same byte. The manifest case is the one that also carries an
        # error count, so it is re-derived here; a row finding is repaired and
        # re-read by the next run, exactly as the row fixers always behaved.
        if fixed and manifest_supports_rtl(proj) is not False:
            if any(key == "AndroidManifest.xml:supportsRtl"
                   for _l, key, _m, _r in issues):
                issues = [i for i in issues
                          if i[1] != "AndroidManifest.xml:supportsRtl"]
                errors -= 1
        if fixed:
            print(f"  {'would fix' if getattr(args, 'dry_run', False) else 'fixed'}: "
                  f"{fixed} element(s)")

    # Reported after the repairs, not before: a fixer runs below the same
    # `issues` list this loop reads, so printing first showed a warning the run
    # had just repaired beside a `fixed: 1` and an error count that disagreed.
    # Plural findings are deliberately absent here — they are appended after
    # the fixers and printed with the coverage line, so a run with `--fix` does
    # not report a warning no fixer could have been handed.
    for lvl, key, msg, _rule in issues:
        if lvl == "info" and not args.verbose:
            continue
        print(f"  [{lvl.upper()}] {key}: {msg}")
    for lvl, key, msg, _rule in plural_issues:
        if lvl == "info" and not args.verbose:
            continue
        print(f"  [{lvl.upper()}] {key}: {msg}")
    issues = issues + plural_issues
    if not issues:
        print("  clean — no hazards detected")

    # A threshold turns "how are we doing" into a gate. `--fail-under` counts the
    # overall number; `--fail-under-file` catches the one file dragging it down.
    gate_failed = False
    floor = getattr(args, "fail_under", None)
    if floor is not None and cov + 1e-9 < floor:
        print(f"[FAIL] coverage {cov:.1f}% is below the --fail-under {floor}%")
        gate_failed = True
    floor_file = getattr(args, "fail_under_file", None)
    if floor_file is not None:
        for f in sorted(per_file):
            got, total = per_file[f]
            pct = 100.0 * got / max(total, 1)
            if pct + 1e-9 < floor_file:
                print(f"[FAIL] {f} at {pct:.1f}% is below the --fail-under-file {floor_file}%")
                gate_failed = True
    print(f"[{'FAIL' if (errors or gate_failed) else 'OK'}] {errors} error(s), "
          f"{sum(1 for i in issues if i[0] == 'warn')} warning(s)")
    fmt = getattr(args, "report", None)
    if fmt:
        rep = build_report(proj, args.locale, cov, covered, len(src), per_file,
                           issues, errors, gate_failed,
                           name=getattr(args, "report_name", None))
        write_report(fmt, getattr(args, "out", None), rep, proj)
    return 1 if (errors or gate_failed) else 0


REPORT_SCHEMA = 1
# Pinned next to the schema number, not derived from `build_report`: the fixture
# is what a consumer codes against, and a key added to the builder without a
# schema bump must break a test, not silently widen the contract.
REPORT_KEYS = frozenset({"schema", "tool", "command", "project", "locale",
                         "coverage", "totals", "rules", "findings"})


def build_report(proj, locale, cov, covered, total, per_file, issues, errors,
                 gate_failed=False, command="lint", name=None):
    """The lint result as plain data, so `--report` has one shape to emit.

    Rule ids come from the `add()` calls. An issue added without one is not a
    rule finding (there is none today — `test_lint` pins that), and a finding
    whose rule id is missing would silently vanish from the per-rule table, so
    it is grouped under `unknown` rather than dropped.
    """
    rows = [{"rule": r or "unknown", "level": lvl, "key": key, "message": msg}
            for lvl, key, msg, r in issues]
    by_rule = {}
    for row in rows:
        d = by_rule.setdefault(row["rule"], {"rule": row["rule"],
                                             "error": 0, "warn": 0, "info": 0})
        d[row["level"]] += 1
    rep = {
        "schema": REPORT_SCHEMA,
        "tool": {"name": "aqlab", "version": __version__},
        # which subcommand produced the file: one schema, three producers, and
        # a consumer has to be able to tell a lint result from a translate one
        "command": command,
        # `name` exists for `all`, whose tree is a temp dir: a report titled
        # `aqlab-all-p7052512` names nothing a human can find again.
        "project": name or proj.name,
        "locale": locale,
        "coverage": {"covered": covered, "total": total,
                     "percent": round(cov, 2),
                     "files": {f: {"covered": g, "total": t,
                                   "percent": round(100.0 * g / max(t, 1), 2)}
                               for f, (g, t) in sorted(per_file.items())}},
        "totals": {"errors": errors,
                   "warnings": sum(r["level"] == "warn" for r in rows),
                   "infos": sum(r["level"] == "info" for r in rows),
                   "gate_failed": bool(gate_failed)},
        "rules": [by_rule[k] for k in sorted(by_rule)],
        "findings": rows,
    }
    # The published shape and the built one are the same set: a consumer that
    # reads this file keys off the top level, so a key that exists on one side
    # only is a schema change, not an internal detail.
    assert set(rep) == REPORT_KEYS, set(rep) ^ REPORT_KEYS
    return rep


def _report_html(rep):
    """A self-contained page: no CDN, no template engine, nothing to install."""
    esc = html.escape
    rows = "".join(
        f"<tr class='{esc(r['level'])}'><td>{esc(r['rule'])}</td><td>{esc(r['level'])}</td>"
        f"<td><code>{esc(r['key'])}</code></td><td>{esc(r['message'])}</td></tr>"
        for r in rep["findings"])
    cov = rep["coverage"]
    files = "".join(
        f"<tr><td>{esc(f)}</td><td>{d['covered']}/{d['total']}</td>"
        f"<td>{d['percent']:.1f}%</td></tr>" for f, d in cov["files"].items())
    t = rep["totals"]
    return (
        "<!doctype html><meta charset='utf-8'>"
        f"<title>aqlab report — {esc(rep['project'])}</title>"
        "<style>body{font:14px system-ui;margin:24px;color:#111}"
        "table{border-collapse:collapse;margin:8px 0 24px}"
        "td,th{border:1px solid #ddd;padding:4px 8px;text-align:start}"
        "tr.error td{background:#fee}tr.warn td{background:#fffbe6}"
        "code{font-size:12px}</style>"
        f"<h1>{esc(rep['project'])} <small>[{esc(rep['locale'])}]</small></h1>"
        f"<p>aqlab {esc(rep['tool']['version'])} · schema {rep['schema']}</p>"
        f"<p><b>Coverage {cov['covered']}/{cov['total']} ({cov['percent']:.1f}%)</b> · "
        f"{t['errors']} error(s), {t['warnings']} warning(s)</p>"
        "<h2>Per file</h2><table><tr><th>file</th><th>covered</th><th>%</th></tr>"
        f"{files}</table>"
        "<h2>Per rule</h2><table><tr><th>rule</th><th>error</th><th>warn</th>"
        "<th>info</th></tr>"
        + "".join(f"<tr><td>{esc(r['rule'])}</td><td>{r['error']}</td>"
                  f"<td>{r['warn']}</td><td>{r['info']}</td></tr>" for r in rep["rules"])
        + "</table><h2>Findings</h2><table><tr><th>rule</th><th>level</th><th>key</th>"
        f"<th>message</th></tr>{rows or '<tr><td colspan=4>clean</td></tr>'}</table>")


def write_report(fmt, out, rep, proj):
    """`--report json|html`. Without `--out` it writes beside the project.

    Takes the finished dict, not the lint arguments: `scan` and `translate`
    produce the same shape, and a writer that rebuilt the report from lint
    state could not serve them.
    """
    if fmt == "json":
        if rep.get("schema") != REPORT_SCHEMA:
            raise Fail(f"refusing to write a schema {rep.get('schema')!r} report "
                       f"(this build writes schema {REPORT_SCHEMA})")
        text, suffix = json.dumps(rep, indent=2, ensure_ascii=False), ".json"
    elif fmt == "html":
        text, suffix = _report_html(rep), ".html"
    else:
        raise Fail(f"unknown --report format `{fmt}` (json|html)")
    path = Path(out) if out else proj / f"aqlab-report{suffix}"
    # `all --report` defaults the path to a `<stem>/` dir that nothing has made
    # yet — measured on CA_v6.apk: FileNotFoundError after a 10 s pipeline, so
    # the report was the one stage that could throw away a finished build.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"[DONE] report: {path}")
    return path


def fix_supports_rtl(proj, dry=False):
    """Flip `android:supportsRtl="false"` to `"true"` — the one step `lint` calls
    an error and nothing repaired.

    The message names `aqlab rtl`, but that command is a whole migration: it also
    renames every `paddingLeft` in the tree, refuses a `minSdk` under 17 and needs
    a `res/` directory. `lint` reports the opt-out as an error, so the flip is
    done here, on the one attribute, and nothing else moves.

    Both quote characters, because aapt accepts either and the reporter does:
    a single-quoted value was reported and survived every run. The match is
    anchored on the attribute *name*, so a neighbour whose value merely spells
    `supportsRtl='false'` cannot be edited in its place (measured: the flip
    landed one attribute to the left of the real one before the anchor).

    Comments are cut out and searched as separate runs, with the offset each
    run was cut at added back: an apktool dump keeps the original manifest in a
    `<!-- -->` block, and searching the stripped text for a body read off it
    finds no position at all.

    Returns 1 when the value changed; idempotent because `"true"` is not
    `"false"`.
    """
    if manifest_supports_rtl(proj) is not False:
        return 0
    manifest = Path(proj) / "AndroidManifest.xml"
    txt = manifest.read_bytes().decode("utf-8", "replace")
    # Preceded by whitespace, never by any non-word character: `android:label=
    # "supportsRtl='false'"` puts the same characters right after a quote, and
    # that is a value, not an attribute of the tag (measured: the flip landed
    # inside `android:label`). The whitespace sits outside the group, so
    # `m.start(1)` is still the attribute name's first character.
    pat = re.compile(r"(?:^|\s)((?:[\w.-]+:)?supportsRtl\s*=\s*)"
                     r"([\x22\x27])false\2")
    pos = 0
    for seg in re.split(r"(<!--.*?-->)", txt, flags=re.DOTALL):
        if not seg.startswith("<!--"):
            m = pat.search(seg)
            if m:
                if not dry:
                    new = (txt[:pos + m.start(1)] + m.group(1) + m.group(2)
                           + "true" + m.group(2) + txt[pos + m.end():])
                    manifest.write_bytes(new.encode("utf-8"))
                return 1
        pos += len(seg)
    return 0


def gravity_fix(text, only=None):
    """Rewrite physical gravity tokens in one XML file to start/end.

    `hardcoded_gravity` names a value `aqlab rtl` would rewrite, but `--fix`
    only ran the symmetry pass, so `lint --fix` left its own warning standing —
    a fixer that reports what it cannot repair is the same bug as one that
    repairs what it never reported. The rewrite reuses the token table `aqlab
    rtl` uses, so the linter, the migrator and the fixer cannot disagree about
    what a gravity value means.

    The attribute *name* is matched first: a plain value sweep would also hit
    the `text="left"` label sitting in the same file. `textAlignment` is the
    one attribute whose RTL vocabulary is viewStart/viewEnd, not start/end.
    `only` narrows the sweep to one attribute name, which is how the
    `pinNested` fixer repairs a single row: a file-wide sweep would rewrite
    sibling rows the check never named.

    Returns (new_text, hits). Pure; idempotent (start/end never re-match).
    """
    hits = [0]

    def one(m):
        value = m.group(4)
        if only is not None and m.group(2) != only:
            return m.group(0)
        if not GRAVITY_TOKEN_HIT.search(value):
            return m.group(0)
        table = (TEXT_ALIGN_TOKEN if m.group(2) == "textAlignment"
                 else GRAVITY_RTL)
        new = re.sub(GRAVITY_TOKEN, lambda t: table[t.group(1)], value)
        if new == value:
            return m.group(0)
        hits[0] += 1
        return '%s%s%s%s' % (m.group(1), m.group(3), new, m.group(3))

    # The quote char is captured, not assumed: `hardcoded_gravity` reports a
    # single-quoted value, so the writer has to reach it and hand the file
    # back with the quotes it came in with.
    hit_re = re.compile(r'((?:\b|:)(gravity|layout_gravity|foregroundGravity|'
                        r'textAlignment)\s*=\s*)([\x22\x27])([^\x22\x27]*)\3')
    return hit_re.sub(one, text), hits[0]


# `<include layout="@layout/foo" — the only attribute that tells the fixer
# which other file the pinned row actually renders.
INCLUDE_TARGET_RE = re.compile(
    r'(?<![\w.-])layout\s*=\s*[\x22\x27](@layout/[\w.]+)[\x22\x27]')

ANY_DIR_RE = re.compile(r'(?<![\w.-])(?:android:|app:)?(?:layout|text)Direction\s*=')
# A *local* pin on a row that also carries physical attributes is the same
# defect one level up, and the fixer's answer differs: the ancestor pin in
# `pinNested` is deliberate, but a pin on the row itself only exists to keep an
# LTR-shaped row LTR in Arabic. `ltr` in an RTL-target build is therefore the
# wrong decision, not a design — drop the pin and let the row follow the locale,
# migrating its physical attrs so the row is still correct in both directions.
# `rtl` is a deliberate mirror and is left to the author.
# Both pins, in one pattern: `direction_pins` reports only the first pin it
# finds on a row (it `break`s after the match), so a row carrying
# `layoutDirection="ltr"` *and* `textDirection="ltr"` was reported once and
# repaired once — the text pin survived, the row stayed reported, and the next
# `--fix` edited it again. Dropping both is what the report promised.
LOCAL_PIN_RE = re.compile(
    r'(?<![\w.-])(?:android:|app:)?(?:layout|text)Direction\s*=\s*[\x22\x27]ltr[\x22\x27]')


def fix_pin_local(proj, dry=False):
    """Drop `layoutDirection="ltr"` from rows `direction_pins` reported as
    `layoutDirection`/`textDirection` pins, migrating the row's physical attrs.

    Returns rows changed; idempotent — the pin is gone after one pass, so the
    check names nothing on the second.
    """
    res = Path(proj) / "res"
    if not res.is_dir():
        return 0
    # `rtl_enabled` names the same `ltr` literal on a row that carries no
    # physical attributes, and nothing else repaired it: `lint --fix` promised
    # to drop the pin and the pin survived every pass. The repair is identical
    # (drop the literal), there is simply nothing to migrate, so the row joins
    # the same queue.
    #
    # The queue is a list of tag names and the walk below pairs each entry with
    # the next start tag of that name, so it has to be in document order. Two
    # sources cannot guarantee that on their own — `direction_pins` names every
    # row with physical attrs, `rtl_enabled` every row without, and a file whose
    # first row belongs to the second list queues them backwards. Duplicates are
    # harmless only because the sort below puts the real rows first and the
    # surplus entries fall off the end, so the lists are concatenated and the
    # walk is given the offsets to sort by.
    broken = {}
    for kind, path, *rest in direction_pins(proj):
        # A `supportsRtl` finding names no row, so it carries three fields where
        # the pin findings carry five; destructuring five killed the fixer.
        if kind == "supportsRtl":
            continue
        tag, val = rest[0], rest[1]
        if kind in ("layoutDirection", "textDirection") and val == "ltr":
            broken.setdefault(path, []).append(tag)
    # `rtl_enabled` findings are deliberately NOT queued for removal. A pin on
    # an element with no physical attributes is not a hole to fill and not a
    # redundant literal: it is a deliberate "this subtree stays LTR" marker.
    # Measured on 8BP: `--fix` stripped `layoutDirection="ltr"` from the
    # ExoPlayer control views, which would mirror the play/pause/skip buttons
    # in Arabic — the icons travel and only a pinned container holds them in
    # playback order. ExoPlayer ships that pin on purpose. The check stays a
    # warning so the author can decide; the fixer does not touch it.
    n = 0
    for rel, wanted in sorted(broken.items()):
        f = Path(proj) / rel
        try:
            txt = read_text(f)
        except OSError:
            continue
        # Two sources queue tags now, so the queue is not automatically in
        # document order — and the walk below matches `wanted[0]` against tags as
        # it meets them, which silently mis-pairs on a file holding `<A/><B/>`
        # reported in the other order. Index the file once and sort by offset.
        slots = {}
        for tag, body, _depth, _sc, pos in walk_layout(txt, full=True):
            if pos >= 0 and LOCAL_PIN_RE.search(body):
                slots.setdefault(tag, []).append(pos)
        ordered = []
        for tag, pos in sorted(((t, p) for t in set(wanted)
                                for p in slots.get(t, [])[:wanted.count(t)]),
                               key=lambda tp: tp[1]):
            ordered.append(tag)
        wanted = ordered
        out, i, done = [], 0, 0
        # The offset comes from the walk: a start tag repeats its neighbour's
        # text often enough (`android:orientation="horizontal"` opens half of
        # Material), and `find` from the last edit then lands on the wrong row —
        # the reported one kept its pin while a sibling was edited in its place.
        for tag, body, _depth, _sc, pos in walk_layout(txt, full=True):
            if not wanted:
                break
            if pos < 0 or tag != wanted[0] or not LOCAL_PIN_RE.search(body):
                continue
            new_body = LOCAL_PIN_RE.sub("", body)
            # Collapse the gap the removal leaves behind, or the tag reads
            # `<Row   android:paddingLeft...>` and every later diff is noise.
            new_body = re.sub(r"\s{2,}", " ", new_body)
            fixed, _hits = rewrite_xml_attrs("<x" + new_body + ">")
            wanted.pop(0)
            out.append(txt[i:pos] + fixed[2:-1])
            i = pos + len(body)
            done += 1
        if done:
            out.append(txt[i:])
            if not dry:
                write_text(f, "".join(out))
            n += done
    return n


def fix_pin_nested(proj, dry=False):
    """Migrate the physical attributes of every row `direction_pins` named
    `pinNested`.

    The pin itself is not the defect — it sits on an ancestor and is usually a
    deliberate design (a clock face, a keypad). What makes the row a finding is
    the physical `paddingLeft`/`layout_marginRight` underneath it: those keep
    pointing one way while the inherited pin keeps the row one way too. Removing
    the pin would change a layout nobody complained about; renaming the physical
    attribute to `start`/`end` fixes the row and leaves the pin to do its job.

    So the fixer is `aqlab rtl` applied to the named row only, and the row's
    reported physical attribute is the one that changes. A `gravity` on the row
    is the same defect with a different repair — the value, not the name — so
    `gravity_fix` runs over the row with the finding's own attribute list, and
    never over the file: the sibling rows were not reported and must not move.

    An `<include>` has no attributes worth migrating here that live in this
    file: `direction_pins` reports one only for the padding/margin the include
    tag carries itself, and `rewrite_xml_attrs` would migrate those in place.
    What it cannot see is the *target* layout: `<include layout="@layout/foo">`
    pulls `foo` into the pinned row, so a physical attribute inside `foo` is
    under the pin too, while `foo` is a separate file that is never reported.
    The include tag names the layout it wants; the row inside it is fixed there.

    Returns rows changed; idempotent because `migrate_attr` returns `None` for a
    start/end name and the token table has no start/end left to rewrite, and
    because a fixed include no longer holds a physical attribute.
    """
    res = Path(proj) / "res"
    if not res.is_dir():
        return 0
    broken = {}
    for finding in direction_pins(proj):
        if finding[0] != "pinNested":
            continue
        _kind, path, tag, _val, names = finding
        broken.setdefault(path, []).append((tag, names))
    n = 0
    pinned_includes = []  # [(path, target_rel)] walked after the loop
    for rel, wanted in sorted(broken.items()):
        f = Path(proj) / rel
        try:
            txt = read_text(f)
        except OSError:
            continue
        # Two rows in one file are reported separately but can sit in the same
        # walk, so `wanted` is consumed by index and only by a row the migrator
        # actually changed: a row whose only physical attribute was already
        # migrated stays in the queue instead of swallowing the next one's slot.
        out, i, done = [], 0, 0
        # Same reason as `fix_pin_local`: the walk knows where the tag starts,
        # and searching for its text finds the first row that looks like it.
        for tag, body, _depth, _sc, pos in walk_layout(txt, full=True):
            if not wanted:
                break
            # A row that sets its own direction breaks the chain, so it is never
            # `pinNested` and must not consume the next reported row's slot —
            # found by a `layoutDirection="locale"` row sitting between two real
            # ones and taking the first one's place.
            if pos < 0 or tag != wanted[0][0] or ANY_DIR_RE.search(body):
                continue
            # `rewrite_xml_attrs` renames the physical attribute *and* rewrites
            # a gravity value in the same pass (`migrate_attr`), which is why
            # a nested row carrying both is repaired by one call. The names
            # stay on the finding: the message says what will move, and the
            # row is matched by them.
            fixed, hits = rewrite_xml_attrs("<x" + body + ">")
            if not hits:
                continue
            if tag == "include":
                tgt = INCLUDE_TARGET_RE.search(body)
                # `@layout/x` is a *resource name*, so the folder qualifier is
                # unknown: the same layout may exist in `layout/`, `layout-ar/`
                # and `layout-ldrtl/`. The fix therefore runs on every file that
                # defines that name, which is what the device resolves per
                # qualifier anyway — repairing one copy leaves the Arabic build
                # broken.
                if tgt:
                    name = tgt.group(1).rsplit("/", 1)[-1]
                    for g in sorted(res.glob("layout*/*.xml")):
                        if g.stem == name and g.name.startswith(name + "."):
                            pinned_includes.append(g.relative_to(proj).as_posix())
            wanted.pop(0)
            out.append(txt[i:pos] + fixed[2:-1])
            i = pos + len(body)
            done += 1
        if done:
            out.append(txt[i:])
            if not dry:
                write_text(f, "".join(out))
            n += done
    # The include target is a whole layout, not a row: it is not under the pin
    # at its own top level, so `direction_pins` never reports it there. Every
    # physical attribute in it is rendered inside the pinned row, hence the
    # file-wide sweep.
    for rel in dict.fromkeys(pinned_includes):
        f = Path(proj) / rel
        try:
            txt = read_text(f)
        except OSError:
            continue
        out, hits = rewrite_xml_attrs(txt)
        if out != txt:
            if not dry:
                write_text(f, out)
            n += hits
    return n


def _pin_scan(proj):
    """Every `layoutDirection="ltr"` left in layout*/ after the fixers ran.

    A substring scan, not a parse: the question is only "did the fixer drop
    every pin it promised to drop", and a parse would answer the same question
    for more work.
    """
    res = Path(proj) / "res"
    out = []
    for d in sorted(res.glob("layout*")) if res.is_dir() else ():
        for f in d.glob("*.xml"):
            try:
                out.append(f.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
    return "".join(out)


def fix_gravity(proj, dry=False):
    """Apply `gravity_fix` to every file owning an `hardcoded_gravity` finding."""
    res = Path(proj) / "res"
    if not res.is_dir():
        return 0
    # Only files the check named, so the fixer's subject is the report's subject.
    todo = {path for path, _a, _v in hardcoded_gravity(proj)}
    n = 0
    for rel in sorted(todo):
        f = Path(proj) / rel
        try:
            txt = read_text(f)
        except OSError:
            continue
        out, hits = gravity_fix(txt)
        if out != txt:
            if not dry:
                write_text(f, out)
            n += hits
    return n


def fix_symmetry(proj, dry=False):
    """Apply `symmetric_fix` to every element `symmetry_problems` named.

    Scoped to layout*/ and menu*/ — the same folders the check reads — because
    `values/` XML holds ids and dimens, where a padding attribute would be
    nonsense. Returns the number of elements rewritten.
    """
    res = Path(proj) / "res"
    if not res.is_dir():
        return 0
    broken = {}
    for path, tag, have_a, _b, _v in symmetry_problems(proj):
        broken.setdefault(path, []).append((tag, have_a))
    n = 0
    elem = re.compile(r"<([A-Za-z][\w.]*)\b([^>]*?)/?>", re.DOTALL)
    for rel, wanted in sorted(broken.items()):
        f = Path(proj) / rel
        try:
            txt = read_text(f)
        except OSError:
            continue
        # Count only elements this file was reported for — each finding names one
        # hole, so the same tag may sit in `wanted` twice.
        todo = list(wanted)
        done = 0

        def repl(m):
            nonlocal todo, done
            if not todo:
                return m.group(0)
            tag, have_a = todo[0]
            if m.group(1) != tag or not re.search(
                    r'\b(?:android:)?%s\s*=' % have_a, m.group(2)):
                return m.group(0)
            todo.pop(0)
            fixed = symmetric_fix(m.group(2))
            if fixed == m.group(2):
                return m.group(0)
            body = fixed
            done += 1
            # Rebuild from the body only: the tag's opening and closing delimiters
            # are kept exactly as apktool wrote them, so a self-closing `/>` stays
            # self-closing. Reassembling them from a suffix test turned `<... />`
            # into `<... >` and an unclosed tag breaks the whole layout.
            whole = m.group(0)
            head = len(m.group(1)) + 1
            tail = whole[head + len(m.group(2)):]
            return whole[:head] + body + tail
        out = elem.sub(repl, txt)
        if out != txt and not dry:
            write_text(f, out)
        n += done
    return n


def cmd_build(args):
    proj, out = args.proj, args.out or (Path(args.proj).name + "-ar.apk")
    ap = apktool_cmd() + ["b", proj, "-o", out]
    run(ap, quiet=args.quiet)
    aligned = out
    try:
        aligned = out.replace(".apk", "-aligned.apk")
        run([tool("zipalign"), "-f", "-p", "4", out, aligned], quiet=args.quiet)
    except Fail as e:
        print(f"  ! zipalign unavailable ({e}); signing unsigned output", file=sys.stderr)
        aligned = out
    if args.ks:
        cmd = [tool("apksigner"), "sign", "--ks", args.ks,
               "--ks-pass", f"pass:{args.ks_pass}", "--key-pass", f"pass:{args.ks_pass}"]
        if args.ks_alias:
            cmd += ["--ks-key-alias", args.ks_alias]
        run(cmd + [aligned], quiet=args.quiet)
        run([tool("apksigner"), "verify", aligned], quiet=args.quiet)
        print(f"[DONE] signed -> {aligned}")
    else:
        print(f"[DONE] built -> {aligned}  (unsigned; pass --ks to sign)")
    return 0


def cmd_all(args):
    """One shot: decode -> rtl -> apply -> lint -> build, with a summary block.

    Each stage's output is held back and shown only if that stage fails; a quiet
    default is the point of the command. Lint errors do not abort — an APK whose
    strings are still untranslated still builds, and the summary has to say
    both — but a failed decode/migrate/build raises straight through `run()`.
    """
    if not args.out:
        raise Fail("aqlab all needs -o/--out <output.apk> — the summary names the artifact")
    # `all` is one command, so it cleans up after itself: a decoded tree left
    # beside the APK is 100 MB of aapt output the caller never asked for. It
    # only owns the tree it created — a directory that was already there (a
    # previous run the caller may still be editing) is never deleted.
    proj, out = Path(args.apk).stem + "-decoded", args.out
    if Path(proj).exists():
        keep = True
    else:
        proj = tempfile.mkdtemp(prefix="aqlab-all-")
        keep = False
    try:
        return _all_stages(args, proj, out, keep)
    finally:
        if not keep:
            shutil.rmtree(proj, ignore_errors=True)


def _all_stages(args, proj, out, keep=True):
    """The pipeline proper; `cmd_all` owns the tree lifetime around it.

    `keep` is passed in because the summary is printed before the caller's
    `finally` deletes the tree: asking the filesystem there says "exists" and
    the summary would name a directory that is gone one line later.
    """

    def stage(label, fn):
        """Run one stage, hold its output, and keep every line for the summary.

        A stage that raises is reported with its own last lines, because the
        tool it wrapped is what explained the failure.
        """
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = fn()
        except Fail as e:
            print(f"[FAIL] {label}: {e}", file=sys.stderr)
            for line in [l for l in buf.getvalue().splitlines() if l.strip()][-4:]:
                print("  " + line, file=sys.stderr)
            raise
        text = buf.getvalue()
        print(f"  [{'OK' if not rc else 'WARN'}] {label}")
        return rc, text

    kind = apk_layout(args.apk)
    if kind == "xapk" and not getattr(args, "split", None):
        # Every split decodes to its own dir and stage 2 needs exactly one
        # project, so `all` requires the caller to name the split instead of
        # silently reading whichever decoded last.
        raise Fail("XAPK input: pass --split <name> so the pipeline has one project "
                   "(see `aqlab decode <xapk>` for the part names)")
    if kind == "aab":
        raise Fail("AAB input: convert to a universal APK first (see `aqlab decode`)")
    stage("decode", lambda: cmd_decode(argparse.Namespace(
        apk=args.apk, out=proj, framework=args.framework, quiet=args.quiet,
        split=getattr(args, "split", None),
        with_sources=getattr(args, "with_sources", False))))
    _, rtl_out = stage("rtl", lambda: cmd_rtl(argparse.Namespace(
        proj=proj, dry_run=False, quiet=args.quiet,
        force_min_sdk=getattr(args, "force_min_sdk", False))))
    if args.tsv:
        stage("apply", lambda: cmd_apply(argparse.Namespace(
            proj=proj, tsv=args.tsv, locale=args.locale)))
    if getattr(args, "tm", None):
        # translate runs as a stage, never as an exit path: a key it cannot
        # fill must not stop the rebuild, but a TM path that does not exist is
        # a typo the pipeline says out loud (cmd_translate opens it).
        _, tr_out = stage("translate", lambda: cmd_translate(argparse.Namespace(
            proj=proj, tm=args.tm, glossary=args.glossary,
            locale=args.locale, translator=args.translator,
            threshold=args.threshold, force=args.translate_force,
            unmatched_out=args.unmatched_out, report=None, out=None,
            dry_run=False, quiet=args.quiet)))
    rc, lint_out = stage("lint", lambda: cmd_lint(argparse.Namespace(
        proj=proj, locale=args.locale, verbose=args.verbose,
        # a report here is the pipeline's own default output: `all` already
        # writes one artifact, and the second file is the evidence for it
        report=args.report,
        # default beside the APK: the project may be wiped right after this
        # stage, and a report written inside it would go with it
        out=(getattr(args, "report_out", None)
             or (Path(args.apk).resolve().parent / Path(args.apk).stem
                 / f"aqlab-report.{args.report}") if args.report else None),
        # the tree is a temp dir named after nothing the user typed, so the
        # report has to name the APK it belongs to, not `aqlab-all-p7052512`
        report_name=Path(args.apk).stem)))
    _, build_out = stage("build", lambda: cmd_build(argparse.Namespace(
        proj=proj, out=out, ks=args.ks, ks_pass=args.ks_pass,
        ks_alias=args.ks_alias, quiet=args.quiet)))

    migrated = re.search(r"\[(?:DONE|DRY)\] (\d+) file", rtl_out)
    counts = re.search(r"\[(?:OK|FAIL)\] (\d+) error\(s\), (\d+) warning\(s\)", lint_out)
    per_rule = collections.Counter(
        m.group(1) for m in re.finditer(r"^\s*(?:warn|error)\s+\[(\w+)\]", lint_out, re.M))
    # print the artifact `build` actually produced: apksigner fits the signature
    # onto the aligned copy, so the input APK stays unsigned. Trust its own line.
    # `build` names the artifact with "<verb> -> <path>" — `signed`, `built`,
    # or `mid = None` in --dry-run style runs. Reading only `signed` reported
    # `None` for every unsigned pipeline, which is most of them.
    built = re.search(r"(?:signed|built) -> (.+?)\s*(?:\(|$)", build_out, re.M)
    print("[DONE] aqlab all")
    print(f"  changes : {migrated.group(1) if migrated else '0'} file(s) migrated")
    print("  lint    : " + (f"{counts.group(1)} error(s), {counts.group(2)} warning(s)"
                            if counts else "not run")
          + "".join(f" {n}={c}" for n, c in sorted(per_rule.items())))
    if getattr(args, "tm", None):
        tr = re.search(r"\[(?:DONE|DRY)\] translate: (\d+/\d+ \([\d.]+%\))", tr_out)
        print(f"  translate: {tr.group(1) if tr else 'ran, coverage not in its output'}")
    print(f"  output  : {built.group(1) if built else out}")
    # The tree the pipeline owns is deleted on the way out, so naming it would
    # point at a directory that no longer exists by the time it is read. Only a
    # tree that survives (found on disk, or left by decode) gets a path line.
    if keep:
        print(f"  project : {proj}")
    else:
        print("  project : none — decoded tree removed (run `aqlab decode` to keep one)")
    rep = re.search(r"\[DONE\] report: (.+\.[a-z]+)\s*$", lint_out, re.M)
    if rep:
        print(f"  report  : {rep.group(1).strip()}")
    return rc


# --------------------------------------------------------------------------
# CLI





def build_parser():
    p = argparse.ArgumentParser(
        prog="aqlab",
        description="Localize any Android app into any language, at any text direction — one command.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="docs: https://github.com/Gilgamesh-110/aqlab")
    p.add_argument("--version", action="version", version=f"aqlab {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("-q", "--quiet", action="store_true", help="hide subprocess output")

    s = sub.add_parser("scan", help="read-only report (apk or decoded dir)")
    s.add_argument("target")
    s.add_argument("--json", action="store_true")
    s.add_argument("--jobs", type=parse_jobs, default=None, metavar="N",
                   help="workers for the layout sweep (0 = every CPU); "
                        "default $AQLAB_JOBS or 1")
    s.add_argument("--report", choices=("json", "html"), metavar="{json,html}",
                   help="write the same report `lint --report` writes, from the APK facts")
    s.add_argument("-o", "--out", metavar="PATH", help="report path (default: beside the APK)")
    common(s)
    s.set_defaults(func=cmd_scan)


    s = sub.add_parser("xliff", help="wrap format specifiers in <xliff:g> so translators cannot break them")
    s.add_argument("proj")
    s.add_argument("--locale", default="ar")
    s.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="print what would be written, change nothing")
    common(s)
    s.set_defaults(func=cmd_xliff)

    s = sub.add_parser("diff", help="compare the string resources of two APKs")
    s.add_argument("old")
    s.add_argument("new")
    s.add_argument("--locale", help="only this locale, e.g. ar (default: every locale)")
    s.add_argument("--limit", type=int, default=40, help="entries to list per section")
    s.add_argument("--json", action="store_true")
    common(s)
    s.set_defaults(func=cmd_diff)

    s = sub.add_parser("decode", help="unpack an APK to a project dir")
    s.add_argument("apk")
    s.add_argument("-o", "--out")
    s.add_argument("-p", "--framework", help="custom apktool framework dir")
    s.add_argument("--with-sources", action="store_true",
                   help="also disassemble smali/ (~5x slower; only needed if you "
                        "intend to edit code — aqlab itself never does)")
    s.add_argument("--split", help="XAPK only: decode just one part (name or "
                   "split_config.*.apk); default decodes them all")
    common(s)
    s.set_defaults(func=cmd_decode)

    s = sub.add_parser("rtl", help="enable RTL and migrate left/right -> start/end")
    s.add_argument("proj")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--force-min-sdk", action="store_true",
                   help="migrate even when minSdkVersion < 17 (the build will then fail)")
    common(s)
    s.set_defaults(func=cmd_rtl)

    s = sub.add_parser("scaffold", help="create res/values-<locale> + translation TSV")
    s.add_argument("proj")
    s.add_argument("--locale", default="ar")
    s.add_argument("--tsv", help="pre-seed with an existing translation table")
    s.add_argument("--force", action="store_true", help="overwrite an existing values-<locale>")
    s.add_argument("--no-locale-config", action="store_true", dest="no_locale_config",
                   help="skip res/xml/locales_config.xml and the manifest attribute")
    s.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="print what would be written, change nothing")
    common(s)
    s.set_defaults(func=cmd_scaffold)

    s = sub.add_parser("apply", help="import a translated TSV into values-<locale>")
    s.add_argument("proj")
    s.add_argument("--tsv", required=True)
    s.add_argument("--locale", default="ar")
    s.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="print what would be written, change nothing")
    common(s)
    s.set_defaults(func=cmd_apply)

    s = sub.add_parser("translate", help="fill values-<locale> from a translation memory + glossary")
    s.add_argument("proj")
    s.add_argument("--tm", required=True, help="translation memory JSON (object or [source,target] list)")
    s.add_argument("--glossary", help="JSON term map applied to matched translations")
    s.add_argument("--locale", default="ar")
    s.add_argument("--translator", metavar="cmd:ARGV",
                   help="external translator hook: JSON keys on stdin, JSON text on stdout")
    s.add_argument("--threshold", type=float, default=0.85,
                   help="minimum fuzzy score 0-1 for a TM match (default 0.85)")
    s.add_argument("--force", action="store_true",
                   help="ignore translations already in values-<locale> and refill from the TM")
    s.add_argument("--unmatched-out", dest="unmatched_out", metavar="FILE",
                   help="write the unmatched keys as a JSON list")
    s.add_argument("--report", choices=("json", "html"), metavar="{json,html}",
                   help="write the coverage + unmatched keys as the shared report")
    s.add_argument("-o", "--out", metavar="PATH", help="report path (default: beside the project)")
    s.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="print what would be written, write nothing")
    common(s)
    s.set_defaults(func=cmd_translate)

    s = sub.add_parser("lint", help="format specifier, escaping and coverage checks — CI friendly")
    s.add_argument("proj")
    s.add_argument("--locale", default="ar")
    s.add_argument("--jobs", type=parse_jobs, default=None, metavar="N",
                   help="workers for the layout sweeps (0 = every CPU); "
                        "default $AQLAB_JOBS or 1")
    s.add_argument("-v", "--verbose", action="store_true", help="include info-level notes")
    s.add_argument("--fail-under", type=float, metavar="PCT", dest="fail_under",
                   help="exit non-zero when overall coverage is below PCT")
    s.add_argument("--fail-under-file", type=float, metavar="PCT", dest="fail_under_file",
                   help="exit non-zero when any single source file is below PCT")
    s.add_argument("--fix", action="store_true",
                   help="add the missing padding/margin partner (as start/end) "
                        "and rewrite physical gravity values to start/end")
    s.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="with --fix: print what would change, write nothing")
    s.add_argument("--report", choices=("json", "html"), metavar="{json,html}",
                   help="write a machine-readable / browsable report")
    s.add_argument("-o", "--out", metavar="PATH",
                   help="report path (default: <proj>/aqlab-report.{json,html})")
    common(s)
    s.set_defaults(func=cmd_lint)

    s = sub.add_parser("build", help="rebuild, zipalign and sign")
    s.add_argument("proj")
    s.add_argument("-o", "--out")
    s.add_argument("--ks", help="keystore path")
    s.add_argument("--ks-pass", default=os.environ.get("AQLAB_KS_PASS", ""))
    s.add_argument("--ks-alias")
    common(s)
    s.set_defaults(func=cmd_build)

    s = sub.add_parser("all", help="decode + rtl + apply + lint + build")
    s.add_argument("apk")
    s.add_argument("--tsv")
    s.add_argument("--locale", default="ar")
    s.add_argument("-o", "--out")
    s.add_argument("--ks")
    s.add_argument("--ks-pass", default=os.environ.get("AQLAB_KS_PASS", ""))
    s.add_argument("--ks-alias")
    s.add_argument("-p", "--framework")
    s.add_argument("-v", "--verbose", action="store_true")
    s.add_argument("--force-min-sdk", action="store_true")
    s.add_argument("--with-sources", action="store_true",
                   help="also disassemble smali/ (~5x slower; only needed if you "
                        "intend to edit code — aqlab itself never does)")
    s.add_argument("--split", help="XAPK only: which part to pipeline (qualifier "
                   "like `ar`/`arm64_v8a`, or the split file name)")
    s.add_argument("--tm", help="fill values-<locale> from this translation memory "
                   "before the rebuild (same file `aqlab translate --tm` takes)")
    s.add_argument("--glossary", help="term map applied to the TM matches")
    s.add_argument("--translator", metavar="cmd:ARGV",
                   help="external translator hook for the keys the TM missed")
    s.add_argument("--threshold", type=float, default=0.85,
                   help="minimum fuzzy score 0-1 for a TM match (default 0.85)")
    s.add_argument("--translate-force", action="store_true", dest="translate_force",
                   help="refill every key from the TM, ignoring an existing "
                        "values-<locale> (the APK's own shipped translations)")
    s.add_argument("--unmatched-out", dest="unmatched_out", metavar="FILE",
                   help="write the keys the TM could not fill as a JSON list")
    s.add_argument("--report", choices=("json", "html"),
                   help="also write the lint report (the evidence for the summary)")
    s.add_argument("--report-out", metavar="PATH",
                   help="report path (default: beside the input APK, so a cleaned "
                        "up project never eats it)")
    common(s)
    s.set_defaults(func=cmd_all)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Fail as e:
        print(str(e), file=sys.stderr)
        return 2


def cli():
    """Console-script entry point (see pyproject.toml)."""
    sys.exit(main())


if __name__ == "__main__":
    sys.exit(main())
