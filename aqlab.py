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
    aqlab lint    <dir>                BiDi hazards, escaping bugs, coverage
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

__version__ = "0.1.5"

# --------------------------------------------------------------------------
# Unicode classes
# --------------------------------------------------------------------------

RE_ARABIC = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
RE_LATIN = re.compile(r"[A-Za-z]")
RE_ANY_ALNUM = re.compile(r"[0-9A-Za-z\u0600-\u06FF\u0660-\u0669\u06F0-\u06F9]")
# Unicode bidi control characters a translator may already have inserted.
BIDI_MARKS = "\u200e\u200f\u061c\u2066\u2067\u2068\u2069"
# Android format specifiers: %s %d %1$s %2$d %-5.2f %%
RE_FORMAT = re.compile(r"%(?:(\d+)\$)?[-#+ 0,(]*\d*(?:\.\d+)?[a-zA-Z%]")
RE_BARE_PERCENT = re.compile(r"%(?![%nsdioxXeEfgGaAcbhp]|(?:\d+\$)?[-#+ 0,(]*\d*(?:\.\d+)?[a-zA-Z])")
# A trailing LTR run that will drag following neutral punctuation to the wrong side.
RE_BIDI_TAIL = re.compile(r"[A-Za-z0-9\u0660-\u0669\u06F0-\u06F9][)\].,:;!?%\u060c\u061b\u061f]*$")
# Markup tags inside a string value (<b>, <i>, <xliff:g>, ...) — must survive translation.
RE_MARKUP_TAG = re.compile(r"</?([A-Za-z][\w:.]*)")


def _formats(text):
    """Full format specifiers, order-sensitive: %1$s and %s are NOT equal."""
    return [m.group(0) for m in RE_FORMAT.finditer(text)]


def _tags(text):
    return sorted(set(RE_MARKUP_TAG.findall(text)))


def _tail_risk(text):
    """True when text ends in a Latin/digit run that will flip its punctuation.
    Format specifiers are stripped first — a trailing `%s` is not a hazard."""
    return bool(RE_BIDI_TAIL.search(RE_FORMAT.sub("", text)))


# --------------------------------------------------------------------------
# RTL attribute migration tables
# --------------------------------------------------------------------------

# Attribute *name* renames: left->Start, right->End.
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
RE_ATTR = re.compile(r'([A-Za-z_][\w.:-]*)\s*=\s*"([^"]*)"')

# Cheap pre-filter: a tag can only be migrated if one of the names the tables
# above know about actually appears in it. Almost every tag in a real tree
# (``layout_width``, ``text``, ``id`` …) matches nothing, so this skips both the
# attribute scan and a ``migrate_attr`` call per attribute. Derived from the
# tables, so a new rename is covered automatically and it cannot drift.
RE_MAY_MIGRATE = re.compile("|".join(map(re.escape, sorted(
    set(ATTR_RENAME) | GRAVITY_ATTRS | TEXT_ALIGN_ATTRS | set(THEME_VALUE_MAP)))))


def migrate_attr(name, value):
    """Return (new_name, new_value) for an RTL migration, or None if untouched."""
    ns, sep, local = name.rpartition(":")
    if ns == "tools":
        return None
    new_local, new_val = local, value
    if local in ATTR_RENAME:
        new_local = ATTR_RENAME[local]
    elif local in GRAVITY_ATTRS:
        new_val = re.sub(r"\b(left|right)\b", lambda g: LEFT_TOKEN[g.group(1)], value)
    elif local in TEXT_ALIGN_ATTRS:
        new_val = re.sub(r"\b(left|right)\b", lambda g: TEXT_ALIGN_TOKEN[g.group(1)], value)
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
                new_body = re.sub(r"\b(left|right)\b",
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
ARABIC_PLURAL_CATEGORIES = ("zero", "one", "two", "few", "many", "other")
PLURAL_RANGE = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "few": "3-10",
    "many": "11-99",
    "other": "100, 101, 102 …",
}
# `other` covers every count the named categories do not, and few/many carry the
# majority of real counts, so those three are errors. Verified with aapt2 that a
# translation lacking `other` still compiles and links — this is not a build
# guard, it is the finding that no Arabic value exists for those counts.
# zero and two are optional in practice.
PLURAL_REQUIRED = ("other", "few", "many")
PLURAL_OPTIONAL = ("zero", "two")


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
    if locale != "ar":
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
        for cat in ARABIC_PLURAL_CATEGORIES:
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


def escape_android(text):
    """Escape a translated string for a values-ar resource file."""
    t = text.replace("&", "&amp;").replace("<", "&lt;")
    t = t.replace("'", "\\'").replace('"', '\\"')
    if t[:1] in ("@", "?"):
        t = "\\" + t
    if t != t.strip():  # Android trims; make intentional spaces explicit
        lead = " ".join("\\u0020" for _ in re.match(r"\s*", t).group(0))
        trail = " ".join("\\u0020" for _ in re.search(r"\s*$", t).group(0))
        t = lead + t.strip() + trail
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
    Blank values and `#` comments are skipped."""
    out = {}
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        key = parts[0].strip()
        val = parts[-1]
        if key and val.strip():
            out[key] = val
    return out


def write_strings_xml(out_dir, pairs, fname="strings.xml"):
    """Emit res/values-<locale>/<fname> from (entry, translated_text) pairs."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = ['<?xml version="1.0" encoding="utf-8"?>',
             f"<!-- generated by aqlab {__version__} -->",
             "<resources>"]
    pl = {}
    for e, text in pairs:
        esc = escape_android(text)
        if e.kind == "string":
            lines.append(f'    <string name="{e.key}">{esc}</string>')
        else:
            name, qty = e.key.split("|", 1)
            pl.setdefault(name, []).append((qty, esc))
    order = {q: i for i, q in enumerate(ARABIC_PLURAL_CATEGORIES)}
    for name, items in pl.items():
        # canonical CLDR order, so a generated file reads like a hand-written one
        items.sort(key=lambda t: order.get(t[0], len(order)))
        lines.append(f'    <plurals name="{name}">')
        for qty, esc in items:
            lines.append(f'        <item quantity="{qty}">{esc}</item>')
        lines.append("    </plurals>")
    lines.append("</resources>")
    (out_dir / fname).write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_tsv(path, entries, tsv_map):
    """key, source, translation — column 1 must be preserved exactly."""
    rows = ["# aqlab translation table — fill the LAST column, keep column 1 EXACT",
            "# key\tsource\ttranslation"]
    for e in entries:
        src = e.text.replace("\t", " ").replace("\n", "\\n")
        rows.append(f"{e.key}\t{src}\t{tsv_map.get(e.key, '')}")
    Path(path).write_text("\n".join(rows) + "\n", encoding="utf-8")


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

    merged = translations_from_tsv(args.tsv) if args.tsv and Path(args.tsv).exists() else {}
    have = [(e, merged[e.key]) for e in entries if merged.get(e.key)]
    by_file = {}
    for e, t in have:
        by_file.setdefault(e.src or "strings.xml", []).append((e, t))
    write_strings_xml(out_dir, by_file.get("strings.xml", []))  # always the entry point
    for fname, group in by_file.items():
        if fname != "strings.xml":
            write_strings_xml(out_dir, group, fname)
    rows = plural_rows_for(entries, args.locale)
    # The table stays OUT of res/: it is not a resource, and anything that
    # compiles the tree directly (aapt2 compile --dir res) fails on it with
    # "invalid file path". apktool happens to skip it, which is why this never
    # showed up until now.
    tsv = Path(args.proj) / f"values-{args.locale}.tsv"
    write_tsv(tsv, rows, merged)
    print(f"[DONE] scaffolded {len(entries)} entries from {len(groups)} file(s) -> {out_dir}")
    print(f"       translated: {len(have)}   pending: {len(rows) - len(have)}")
    print(f"       translate:  {tsv}")
    print(f"       then:       aqlab apply {args.proj} --tsv {tsv}")
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
        if anchor and qty in ARABIC_PLURAL_CATEGORIES:
            pairs.append((Entry(key, "plural", anchor.attrs, "", True, anchor.src), text))

    if not pairs:
        raise Fail("TSV keys match nothing in the default values/ resources")
    out_dir = res / f"values-{args.locale}"
    by_file = {}
    for e, t in pairs:
        by_file.setdefault(e.src or "strings.xml", []).append((e, t))
    for fname, group in by_file.items():
        write_strings_xml(out_dir, group, fname)
    write_tsv(Path(args.proj) / f"values-{args.locale}.tsv",
              plural_rows_for(all_entries, args.locale), merged)
    print(f"[DONE] {len(pairs)} translation(s) -> {out_dir}/{' + '.join(sorted(by_file))}")
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
    loc = sorted({p.parent.name for p in res.glob("values-*/*.xml")})
    manifest = (Path(proj) / "AndroidManifest.xml").read_text(encoding="utf-8", errors="replace")
    rtl_on = bool(re.search(r'supportsRtl\s*=\s*"true"', manifest))
    lefts = count_legacy_attrs(proj)
    ar_dirs = sorted(p.name for p in res.glob("values-ar*") if p.is_dir())
    print(f"[FINDING] project: {proj}")
    print(f"  locales        : {', '.join(l.split('-', 1)[-1] for l in loc) or 'default only'}")
    print(f"  supportsRtl    : {'yes' if rtl_on else 'NO — run `aqlab rtl`'}")
    print(f"  translatable   : {sum(1 for e in entries if e.translatable)}")
    print(f"  legacy L/R     : {lefts} attribute(s) still left/right — run `aqlab rtl`")
    print(f"  arabic res     : {'yes (' + ', '.join(ar_dirs) + ')' if ar_dirs else 'none — run `aqlab scaffold`'}")
    if args.json:
        print(json.dumps({"locales": loc, "supportsRtl": rtl_on,
                          "translatable": sum(1 for e in entries if e.translatable),
                          "legacyLTR": lefts, "arabic": bool(ar_dirs)}, ensure_ascii=False))
    return 0


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
    rtl_on = "supportsRtl" in xml and "0xffffffff" in xml.split("supportsRtl", 1)[1][:120]
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
        if RE_ARABIC.search(a) and _tail_risk(a) and not any(c in a for c in BIDI_MARKS):
            add("error", key, "trailing Latin/digit run before punctuation — needs U+200F (RLM)")
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
            add("warn", label, "no Arabic <plurals> for this name — every count "
                               "will fall back to the English form")
            continue

        hard = [q for q in PLURAL_REQUIRED if q not in got]
        if hard:
            # Not a build failure: aapt2 compiles and links a translation whose
            # <plurals> lacks `other` (verified). It is worse than that — the
            # build is silent and the counts simply have no Arabic value.
            why = ("it covers every count the other categories do not (100, 101, …) "
                   "and the build will not warn" if "other" in hard
                   else "those counts fall back to `other`")
            add("error", label, "missing Arabic "
                f"{'quantity' if len(hard) == 1 else 'quantities'}: "
                + ", ".join(f"{q} ({PLURAL_RANGE[q]})" for q in hard) + f" — {why}")

        soft = [q for q in PLURAL_OPTIONAL if q not in got]
        if soft:
            add("warn", label, "no "
                + ", ".join(f"{q} ({PLURAL_RANGE[q]})" for q in soft)
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

    print(f"[FINDING] lint res/values-{args.locale}: {covered}/{len(src)} translated ({cov:.1f}%)")
    for lvl, key, msg in issues:
        if lvl == "info" and not args.verbose:
            continue
        print(f"  [{lvl.upper()}] {key}: {msg}")
    if not issues:
        print("  clean — no hazards detected")
    print(f"[{'FAIL' if errors else 'OK'}] {errors} error(s), "
          f"{sum(1 for i in issues if i[0] == 'warn')} warning(s)")
    return 1 if errors else 0


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
    common(s)
    s.set_defaults(func=cmd_scaffold)

    s = sub.add_parser("apply", help="import a translated TSV into values-<locale>")
    s.add_argument("proj")
    s.add_argument("--tsv", required=True)
    s.add_argument("--locale", default="ar")
    common(s)
    s.set_defaults(func=cmd_apply)

    s = sub.add_parser("lint", help="BiDi hazards, escaping bugs, coverage — CI friendly")
    s.add_argument("proj")
    s.add_argument("--locale", default="ar")
    s.add_argument("-v", "--verbose", action="store_true", help="include info-level notes")
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
