#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aqlab — make any Android app Arabic, RTL-correct, in one command.

Single-file, stdlib-only. Wraps apktool + aapt2/zipalign/apksigner; all the
Arabic-specific logic (RTL attribute migration, BiDi linting, resource
escaping) is implemented here.

    aqlab scan    <apk|dir>            read-only report, changes nothing
    aqlab decode  <apk>                unpack to a project dir
    aqlab rtl     <dir>                manifest supportsRtl + left/right -> start/end
    aqlab scaffold<dir>                build ar.tsv + res/values-ar/strings.xml
    aqlab apply   <dir> --tsv ar.tsv   import translated rows
    aqlab lint    <dir>                format, escaping, coverage — CI friendly
    aqlab build   <dir>                apktool b -> zipalign -> apksigner
    aqlab all     <apk>                decode + rtl + apply + lint + build

Exit code is non-zero when `lint` finds errors, so it drops straight into CI.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

__version__ = "0.1.14"

# --------------------------------------------------------------------------
# Unicode classes
# --------------------------------------------------------------------------

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
PLURAL_RANGE_BY_LOCALE = {
    "ar": {"zero": "0", "one": "1", "two": "2", "few": "3-10", "many": "11-99",
           "other": "100, 101, 102 …"},
    "he": {"one": "1", "two": "2", "many": "20, 30, 40 …", "other": "everything else"},
    "fa": {"one": "1", "other": "everything else"},
    "ur": {"one": "1", "other": "everything else"},
}


def plural_range(locale, cat):
    """Count range hint for a category, in the target locale's own terms."""
    return PLURAL_RANGE_BY_LOCALE.get(_base(locale), PLURAL_RANGE).get(cat, PLURAL_RANGE.get(cat, "?"))

# CLDR plural categories per locale, in CLDR order. Verified against
# unicode.org/cldr/charts/47/supplemental/language_plural_rules.html:
#   ar  zero one two few many other
#   he  one two many other        (no zero, no few)
#   fa  one other
#   ur  one other
# Guessing here would repeat the bidi mistake — a category the language does not
# use would be demanded of the translator forever.
PLURAL_CATEGORIES = {
    "ar": ("zero", "one", "two", "few", "many", "other"),
    "he": ("one", "two", "many", "other"),
    "fa": ("one", "other"),
    "ur": ("one", "other"),
}
# The categories that carry the most counts, so missing ones are errors: for
# Arabic `few` (3-10) and `many` (11-99); for Hebrew `many` (20, 30, …). `other`
# always covers everything the named categories do not, so it is always required.
PLURAL_REQUIRED = {
    "ar": ("other", "few", "many"),
    "he": ("other", "many"),
    "fa": ("other",),
    "ur": ("other",),
}
PLURAL_OPTIONAL = {
    "ar": ("zero", "two"),
    "he": ("one", "two"),
    "fa": ("one",),
    "ur": ("one",),
}
# Languages that read right-to-left, so `supportsRtl` and start/end matter.
RTL_LOCALES = ("ar", "he", "fa", "ur", "yi", "dv", "ps", "sd", "ug", "ckb")


def plural_categories(locale):
    """CLDR categories for a locale, empty when it has no distinct plurals.

    A base tag (`ar-rSA`, `he`) resolves on the language subtag; `pt-BR` style
    only matters for languages that differ by region, and none of the RTL ones do.
    """
    return PLURAL_CATEGORIES.get(_base(locale), ())


def plural_required(locale):
    return PLURAL_REQUIRED.get(_base(locale), ())


def plural_optional(locale):
    return PLURAL_OPTIONAL.get(_base(locale), ())


def is_rtl(locale):
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
    cands = sorted(res.glob("values*/strings.xml")) if res.is_dir() else []
    return cands[0] if cands else None


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


def cmd_decode(args):
    out = args.out or (Path(args.apk).stem + "-decoded")
    ap = apktool_cmd() + ["d", "-f", "--no-debug-info"]
    # Skipping smali is the single biggest saving in the whole pipeline and it is
    # free: apktool records the source APK in apktool.yml and copies the original
    # classes*.dex back on build, byte for byte. This tool never edits code, so
    # the smali tree is ~50s of disassembly nobody reads. `--with-sources` gets it
    # back for the cases that do want it.
    if not getattr(args, "with_sources", False):
        ap.append("-s")
    if args.framework:
        ap += ["-p", args.framework]
    run(ap + [args.apk, "-o", out], quiet=args.quiet)
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

    if sink.dry:
        print(f"[DRY] would scaffold {len(entries)} entries -> {out_dir}")
        print(f"      translated: {len(have)}   pending: {len(rows) - len(have)}")
        sink.report()
        return 0
    sink.commit()
    print(f"[DONE] scaffolded {len(entries)} entries from {len(groups)} file(s) -> {out_dir}")
    print(f"       translated: {len(have)}   pending: {len(rows) - len(have)}")
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


def cmd_scan(args):
    target = Path(args.target)
    if target.is_dir():
        return scan_project(target, args)
    return scan_apk(target, args)


def scan_project(proj, args):
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
    print(f"[FINDING] project: {proj}")
    print(f"  locales        : {', '.join(loc) or 'default only'}")
    print(f"  supportsRtl    : {'yes' if rtl_on else 'NO — run `aqlab rtl`'}")
    print(f"  translatable   : {sum(1 for e in entries if e.translatable)}")
    print(f"  legacy L/R     : {lefts} attribute(s) still left/right — run `aqlab rtl`")
    sym = symmetry_problems(proj)
    if sym:
        print(f"  asymmetric     : {len(sym)} side(s) set without the opposite — "
              f"offsets off-centre when the layout flips")
    grav = hardcoded_gravity(proj)
    if grav:
        print(f"  hardcoded grav : {len(grav)} value(s) pin text to a physical side")
    pins = direction_pins(proj)
    for kind, where, *_rest in pins:
        if kind == "supportsRtl":
            print(f"  supportsRtl    : false — Arabic gets the LTR layout; "
                  f"run `aqlab rtl`")
        else:
            tag_, val = _rest[0], _rest[1]
            print(f"  direction pin  : {where}:<{tag_}> layoutDirection=\"{val}\" on a "
                  f"horizontal row with physical attrs")
    vec = text_like_vectors(proj)
    if vec:
        print(f"  text vectors   : {len(vec)} — Latin glyphs baked into geometry, check by eye")
        for pth, n, w, h in vec:
            print(f"                   {pth}  ({n} paths, {w}x{h}dp)")
    print(f"  arabic res     : {'yes (' + ', '.join(ar_dirs) + ')' if ar_dirs else 'none — run `aqlab scaffold`'}")
    if args.json:
        print(json.dumps({"locales": loc, "supportsRtl": rtl_on,
                          "translatable": sum(1 for e in entries if e.translatable),
                          "legacyLTR": lefts, "arabic": bool(ar_dirs)}, ensure_ascii=False))
    return 0


def text_like_vectors(proj):
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
    res = Path(proj) / "res"
    if not res.is_dir():
        return []
    out = []
    # Lettering is many separate contours; an icon is a handful. 12 is above
    # every Material icon in a real APK (measured: 2 paths each) and below what
    # even a short wordmark needs.
    MIN_PATHS = 12
    for f in sorted(res.glob("drawable*/*.xml")):
        try:
            txt = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "<vector" not in txt:
            continue
        if "autoMirrored" in txt and 'autoMirrored="true"' in txt:
            continue
        n = txt.count("<path")
        if n < MIN_PATHS:
            continue
        w = re.search(r'android:width="([0-9.]+)dip"', txt)
        h = re.search(r'android:height="([0-9.]+)dip"', txt)
        out.append((f.relative_to(proj).as_posix(), n,
                    w.group(1) if w else "?", h.group(1) if h else "?"))
    return out


def hardcoded_gravity(proj):
    """`gravity`/`textAlignment` values that pin text to a physical side.

    Android's `RtlHardcoded` check. `gravity="left"` is a left edge in every
    locale, so Arabic gets a paragraph hugging the wrong margin — unlike
    `paddingLeft`, it is not renamed by `aqlab rtl` silently, it is a value that
    has to be read.

    Returns [(path, attr, value)] and never rewrites anything: a value like
    `left|bottom` needs its tokens translated, not its attribute renamed, and
    guessing which token the author meant is how a tool breaks a layout.
    """
    res = Path(proj) / "res"
    if not res.is_dir():
        return []
    found = []
    # Both quote characters: Android accepts either, and `aqlab rtl` already
    # migrates a single-quoted attribute. Reading only double-quote made
    # a single-quoted gravity invisible here, and gravity is the one
    # defect `aqlab rtl` deliberately leaves for the user to read.
    attr_re = re.compile(r'\b(?:android:)?(gravity|layout_gravity|foregroundGravity|'
                         r'textAlignment)\s*=\s*([\x22\x27])([^\x22\x27]*)\2')
    for f in sorted(res.glob("**/*.xml")):
        if not any(part.startswith(("layout", "values", "menu", "anim"))
                   for part in f.relative_to(res).parts[:1]):
            continue
        try:
            txt = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in attr_re.finditer(txt):
            attr, val = m.group(1), m.group(3)
            # `imageLeft` matches the old word-boundary test and is real (a
            # selection-dialog row names its slot `imageLeft` in Material). What
            # has to change is the token inside the value, so only that counts.
            if not GRAVITY_TOKEN_HIT.search(val):
                continue
            found.append((f.relative_to(proj).as_posix(), attr, val))
    return found


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


def direction_pins(proj):
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

    res = Path(proj) / "res"
    if not res.is_dir():
        return out
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

    for f in sorted(res.glob("layout*/*.xml")):
        try:
            txt = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
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
                out.append((kind, f.relative_to(proj).as_posix(), tag, val))
            elif chain and not any_dir_re.search(body):
                # A child that sets its own direction breaks the chain: it and
                # everything under it follow that value, not the outer pin.
                # The attribute names travel with the finding: the fixer has to
                # migrate exactly what the report named, and the row may hold a
                # padding and a gravity at once.
                names = tuple(sorted({m.group(1) or m.group(2)
                                      for m in phys_re.finditer(body)}))
                out.append(("pinNested", f.relative_to(proj).as_posix(), tag,
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


def rtl_enabled(proj, locale="ar"):
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
    res = Path(proj) / "res"
    if not res.is_dir():
        return []
    harmless = {"locale", "firstStrong", "anyRtl", "inherit"}
    declared = declared_direction_values(proj)
    supports_rtl = manifest_supports_rtl(proj)
    out = []
    for f in sorted(res.glob("layout*/**/*.xml")):
        if not f.is_file():
            continue
        rel = f.relative_to(proj).as_posix()
        txt = f.read_text(encoding="utf-8", errors="replace")
        # `walk_layout` hands back the start tag's own text in `body`, which stops
        # at its `>` — exactly this element's attributes, never its children's.
        # `full=True` adds the body's offset and the element's depth, both needed
        # to delimit the subtree for the text-widget test below.
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
    # An opt-out is a `lint` finding, not a `--fix` one: the attribute is a
    # build-wide switch, so the warning points at the manifest instead of
    # promising a row rewrite.
    if supports_rtl is False:
        out.append(("supportsRtl", "AndroidManifest.xml", "", "false"))
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


def rtl_drawable_dirs(proj):
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


def symmetry_problems(proj):
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
    res = Path(proj) / "res"
    if not res.is_dir():
        return []
    found = []
    elem = re.compile(r"<([A-Za-z][\w.]*)\b([^>]*?)/?>", re.DOTALL)
    for f in sorted(list(res.glob("layout*/*.xml")) + list(res.glob("menu*/*.xml"))):
        try:
            txt = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
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
                    found.append((f.relative_to(proj).as_posix(), tag, left, right,
                                  have[left]))
                elif right in have and left not in have:
                    if right in covered:
                        continue
                    found.append((f.relative_to(proj).as_posix(), tag, right, left,
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
    res = proj / "res"
    if not find_default_strings(proj):
        raise Fail("no values/strings.xml — decode the APK first")

    # Read the whole values folder, not one file. Plurals routinely live in their
    # own `plurals.xml` — AOSP and most Material apps ship them that way — so
    # reading only strings.xml silently skipped exactly the entries the plural
    # check exists to inspect. Region variants (values-ar-rSA) count as `ar` too.
    src = {e.key: e for e in read_locale_entries(res, ["values"]) if e.translatable}
    ar_dirs = sorted(d.name for d in res.glob(f"values-{args.locale}*") if d.is_dir())
    issues, errors = [], 0

    if not ar_dirs:
        print(f"[FINDING] no res/values-{args.locale}/ — run `aqlab scaffold`")
        return 1
    tr = {e.key: e for e in read_locale_entries(res, ar_dirs)}

    def add(level, key, msg):
        nonlocal errors
        if level == "error":
            errors += 1
        issues.append((level, key, msg))

    for key, e in src.items():
        t = tr.get(key)
        if t is None:
            continue
        a = t.text
        if a == e.text and RE_LATIN.search(a):
            add("warn", key, "identical to source and contains Latin — probably untranslated")
        elif RE_ARABIC.search(e.text) is None and RE_ANY_ALNUM.search(e.text) and not RE_ARABIC.search(a):
            add("warn", key, "no Arabic characters in the Arabic value")
        if RE_BARE_PERCENT.search(RE_FORMAT.sub("", a)):
            add("error", key, "bare `%` — must be `%%` or a format specifier, aapt will reject it")
        d_fmt, a_fmt = _formats(e.text), _formats(a)
        if d_fmt != a_fmt:
            # The count is legitimately absent from Arabic `zero`/`one`/`two`
            # ("لا عناصر", "عنصر واحد", "عنصران"), so a specifier the Arabic value
            # does not need is not a finding. Inventing one never is.
            no_count = (e.kind == "plural"
                        and e.key.rpartition("|")[2] in ("zero", "one", "two")
                        and len(a_fmt) < len(d_fmt))
            if not no_count:
                lvl = "error" if (e.kind == "string" or len(a_fmt) > len(d_fmt)) else "warn"
                add(lvl, key, f"format specifiers differ: source {d_fmt or '-'} vs arabic {a_fmt or '-'}")
        if e.text.endswith(" ") != a.endswith(" "):
            add("warn", key, "leading/trailing space mismatch — concatenated strings will run together")
        if re.search(r"(?<!\\)'", a):
            add("error", key, "unescaped apostrophe — use \\' inside Android string resources")
        if a[:1] in ("@", "?"):
            add("error", key, "value starts with `@` or `?` — must be escaped as \\@ / \\?")
        if RE_ARABIC.search(a) and RE_LATIN.search(a):
            add("info", key, "mixed Arabic + Latin — verify order on a real device")
        if _tags(e.text) != _tags(a):
            add("error", key, f"markup tags differ: source {_tags(e.text) or '-'} "
                              f"vs arabic {_tags(a) or '-'}")

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

    ar_pl = by_plural(tr.values())
    for name in sorted(by_plural(src.values())):
        got = ar_pl.get(name)
        label = f"plurals/{name}"
        if not got:
            add("warn", label, f"no {args.locale} <plurals> for this name — every count "
                               "will fall back to the source form")
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
                + f" — {why}")

        soft = [q for q in plural_optional(args.locale) if q not in got]
        if soft:
            add("warn", label, "no "
                + ", ".join(f"{q} ({plural_range(args.locale, q)})" for q in soft)
                + " form — those counts use `other`")

        # Distinct categories carrying identical text is the tell that nobody
        # actually pluralised anything.
        same = {}
        for q in ("few", "many", "other"):
            if q in got:
                same.setdefault(got[q].text.strip(), []).append(q)
        for qs in same.values():
            if len(qs) > 1:
                add("warn", label, f"{', '.join(qs)} carry identical text — "
                                   "the plural forms are not actually pluralised")

    # Baked-in lettering in vector geometry: `lint` cannot fix it and a
    # heuristic must never fail a build, so it is a warning with the path so the
    # user can look at the drawing itself.
    for pth, n, w, h in text_like_vectors(proj):
        add("warn", pth, f"{n} paths at {w}x{h}dp — looks like lettering baked into "
                         f"geometry; it will not mirror with the UI, check it by eye")

    # Android's RtlSymmetry: one side without the other renders off-centre once
    # the layout flips, and nothing in the build says so.
    for path, tag, have_a, missing_b, value in symmetry_problems(proj):
        add("warn", f"{path}:<{tag}>",
            f"{have_a}=\"{value}\" without {missing_b} — not symmetric when the "
            f"layout flips")

    # RtlHardcoded: a physical side in a gravity value does not follow the
    # locale, so it has to be renamed by hand — say where.
    for path, attr, value in hardcoded_gravity(proj):
        add("warn", f"{path}:{attr}", f'"{value}" pins text to a physical side; '
                                      f'`aqlab lint --fix` rewrites it to start/end')

    # A pinned direction on a row that also carries left/right attributes is the
    # author working around RTL instead of supporting it — the pin wins over the
    # locale and the row stays LTR in Arabic.
    for kind, where, *rest in direction_pins(proj):
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
                f'left/right attrs — it will not flip in {args.locale}{tip}')
        elif kind == "pinNested":
            tag_, val, names = rest
            add("warn", f"{where}:<{tag_}>",
                f'inherits layoutDirection="{val}" from an ancestor and sets '
                f'{", ".join(names)} — the ancestor pin keeps this row {val} in '
                f'{args.locale}; `aqlab lint --fix` migrates the row, not the pin')
        else:
            tag_, val = rest
            add("warn", f"{where}:<{tag_}>",
                # `ltr` is repairable by `fix_pin_local` and `rtl` is left
                # alone, exactly like the `layoutDirection` message above —
                # the tip promised a repair only when there is one.
                f'textDirection="{val}" pins the text of a row that already sets '
                f'left/right attrs — it stays {val} in {args.locale}'
                + ('; `aqlab lint --fix` drops the pin and migrates the row'
                   if val == 'ltr' else ''))

    # RtlEnabled: a direction literal that is not a pin but still wins over the
    # locale. `direction_pins` only names the row that also carries physical
    # attributes; this is the same literal on a row that carries none, where the
    # default would have followed the language and the literal does not.
    for attr, path, tag_, val in rtl_enabled(proj, args.locale):
        if attr == "supportsRtl":
            # Not a row and not a `--fix` job: the switch has to be on before
            # any row can mirror, so the message names the command that flips it.
            # Keyed by the attribute, not by `where`: `where` is the loop's
            # leftover string resource key and pointed the message at a string.
            add("error", f"{path}:supportsRtl",
                'android:supportsRtl="false" — the manifest opts out of RTL '
                'entirely; run `aqlab rtl`')
            continue
        ref = val.startswith(("@", "?"))
        add("warn", f"{path}:<{tag_}>",
            f'{attr}="{val}" overrides the locale on an element with no physical '
            f'attrs — {args.locale} shows it {val}'
            # A reference is repairable nowhere: dropping it changes which value
            # the row picks, so the tip would promise a rewrite it cannot make.
            + ("; `aqlab lint --fix` drops the pin" if val == "ltr" and not ref
               else ""))

    # A `ldrtl` folder that mirrors nothing is not a translation problem, so it
    # is reported without the locale gate above — it costs the same in any RTL
    # language and the author meant it to flip.
    for where, kind, detail in rtl_drawable_dirs(proj):
        if kind == "empty":
            add("warn", where, "ldrtl folder is empty — it mirrors nothing; fill it "
                               "or delete it so the build stops looking RTL-ready")
        else:
            add("warn", where, f"byte-identical to {detail}/ — an unchanged copy does "
                               f"not mirror; redraw it or delete it")

    print(f"[FINDING] lint res/values-{args.locale}: {covered}/{len(src)} translated ({cov:.1f}%)")
    # Per file only when there is more than one, so the common case stays one line.
    if len(per_file) > 1 or args.verbose:
        worst = min(per_file.items(), key=lambda kv: kv[1][0] / max(kv[1][1], 1))
        for f in sorted(per_file, key=lambda k: per_file[k][0] / max(per_file[k][1], 1)):
            got, total = per_file[f]
            pct = 100.0 * got / max(total, 1)
            tag = "  <- weakest" if f == worst[0] and pct < cov else ""
            print(f"            {f:<20} {got}/{total}  {pct:5.1f}%{tag}")
    fixed = 0
    if getattr(args, "fix", False):
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
        fixed += fix_symmetry(proj, dry=getattr(args, "dry_run", False))
        fixed += fix_gravity(proj, dry=getattr(args, "dry_run", False))
        if fixed:
            print(f"  {'would fix' if getattr(args, 'dry_run', False) else 'fixed'}: "
                  f"{fixed} element(s)")

        # `issues` was built before the repairs, so a row this run fixed would
        # still be printed as a finding — the report and the fixer disagreeing
        # about the same byte. The manifest case is the one that also carries an
        # error count, so it is re-derived here; a row finding is repaired and
        # re-read by the next run, exactly as the row fixers always behaved.
        if fixed and manifest_supports_rtl(proj) is not False:
            if any(key == "AndroidManifest.xml:supportsRtl"
                   for _l, key, _m in issues):
                issues = [i for i in issues
                          if i[1] != "AndroidManifest.xml:supportsRtl"]
                errors -= 1

    # Reported after the repairs, not before: a fixer runs below the same
    # `issues` list this loop reads, so printing first showed a warning the run
    # had just repaired beside a `fixed: 1` and an error count that disagreed.
    for lvl, key, msg in issues:
        if lvl == "info" and not args.verbose:
            continue
        print(f"  [{lvl.upper()}] {key}: {msg}")
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
    return 1 if (errors or gate_failed) else 0


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
    for _attr, path, tag, _val in rtl_enabled(proj):
        if _val == "ltr":
            broken.setdefault(path, []).append(tag)
    n = 0
    for rel, wanted in sorted(broken.items()):
        f = Path(proj) / rel
        try:
            txt = f.read_text(encoding="utf-8", newline="")
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
                f.write_text("".join(out), encoding="utf-8", newline="")
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
            txt = f.read_text(encoding="utf-8", newline="")
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
                f.write_text("".join(out), encoding="utf-8", newline="")
            n += done
    # The include target is a whole layout, not a row: it is not under the pin
    # at its own top level, so `direction_pins` never reports it there. Every
    # physical attribute in it is rendered inside the pinned row, hence the
    # file-wide sweep.
    for rel in dict.fromkeys(pinned_includes):
        f = Path(proj) / rel
        try:
            txt = f.read_text(encoding="utf-8", newline="")
        except OSError:
            continue
        out, hits = rewrite_xml_attrs(txt)
        if out != txt:
            if not dry:
                f.write_text(out, encoding="utf-8", newline="")
            n += hits
    return n


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
            txt = f.read_text(encoding="utf-8", newline="")
        except OSError:
            continue
        out, hits = gravity_fix(txt)
        if out != txt:
            if not dry:
                f.write_text(out, encoding="utf-8", newline="")
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
            txt = f.read_text(encoding="utf-8", newline="")
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
            f.write_text(out, encoding="utf-8", newline="")
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
    proj = Path(args.apk).stem + "-decoded"
    cmd_decode(argparse.Namespace(apk=args.apk, out=proj, framework=args.framework,
                                  quiet=args.quiet,
                                  with_sources=getattr(args, "with_sources", False)))
    cmd_rtl(argparse.Namespace(proj=proj, dry_run=False, quiet=args.quiet,
                               force_min_sdk=getattr(args, "force_min_sdk", False)))
    if args.tsv:
        cmd_apply(argparse.Namespace(proj=proj, tsv=args.tsv, locale=args.locale))
    rc = cmd_lint(argparse.Namespace(proj=proj, locale=args.locale, verbose=args.verbose))
    cmd_build(argparse.Namespace(proj=proj, out=args.out, ks=args.ks,
                                 ks_pass=args.ks_pass, ks_alias=args.ks_alias, quiet=args.quiet))
    return rc


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(
        prog="aqlab",
        description="Make an Android app Arabic with correct RTL — one command.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="docs: https://github.com/<you>/aqlab")
    p.add_argument("--version", action="version", version=f"aqlab {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("-q", "--quiet", action="store_true", help="hide subprocess output")

    s = sub.add_parser("scan", help="read-only report (apk or decoded dir)")
    s.add_argument("target")
    s.add_argument("--json", action="store_true")
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
    common(s)
    s.set_defaults(func=cmd_decode)

    s = sub.add_parser("rtl", help="enable RTL and migrate left/right -> start/end")
    s.add_argument("proj")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--force-min-sdk", action="store_true",
                   help="migrate even when minSdkVersion < 17 (the build will then fail)")
    common(s)
    s.set_defaults(func=cmd_rtl)

    s = sub.add_parser("scaffold", help="create res/values-ar + translation TSV")
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

    s = sub.add_parser("lint", help="format specifier, escaping and coverage checks — CI friendly")
    s.add_argument("proj")
    s.add_argument("--locale", default="ar")
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
