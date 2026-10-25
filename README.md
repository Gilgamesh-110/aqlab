<div align="center">

<img src="assets/banner.svg" alt="aqlab — Arabic &amp; RTL localization for Android APKs" width="100%">

**Flip any Android app to Arabic — RTL-correct — in one command.**

[![tests](https://github.com/Gilgamesh-110/aqlab/actions/workflows/ci.yml/badge.svg)](https://github.com/Gilgamesh-110/aqlab/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)
[![dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen.svg)](#requirements)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)
[![Changelog](https://img.shields.io/badge/changelog-keep_a_changelog-orange.svg)](CHANGELOG.md)

[العربية](README.ar.md) · **English**

</div>

---

Translating an app into Arabic is the easy half. The half almost everybody gets
wrong is the interface: the drawer stays on the left, the back arrow still points
left, progress bars fill left-to-right, `%d` placeholders go missing, and
punctuation lands on the wrong side of the line.

**`aqlab` does the part that is not translation** — layout direction, attribute
migration, Unicode bidi hazards, and Android resource escaping.

<p align="center">
  <img src="assets/demo.svg" alt="terminal session: scan, rtl, lint" width="100%">
</p>

## Contents

- [Why](#why)
- [Install](#install)
- [Quick start](#quick-start)
- [Commands](#commands)
- [What it fixes](#what-it-fixes)
- [The part nobody else does: bidi linting](#the-part-nobody-else-does-bidi-linting)
- [What it does *not* do](#what-it-does-not-do)
- [Tested on](#tested-on)
- [FAQ](#faq)
- [Requirements](#requirements)
- [Contributing](#contributing)
- [License](#license)

## Why

Arabic is written right-to-left. Android has had a first-class mechanism for this
since API 17 — logical `start`/`end` attributes plus `android:supportsRtl` — and
yet a large share of shipped apps never turned it on, or turned it on and left
hundreds of hardcoded `left`/`right` attributes behind.

Doing it by hand means auditing every layout, every menu, every style, and only
finding the mistakes when you look at the app running in Arabic. `aqlab`
automates the mechanical 95% and **lints the rest**, so the first Arabic build
you look at is already correct.

## Install

```sh
pipx install git+https://github.com/Gilgamesh-110/aqlab      # or
curl -O https://raw.githubusercontent.com/Gilgamesh-110/aqlab/main/aqlab.py
```

One file, standard library only. It drives tools you already have:

| tool | how it is found |
|---|---|
| `apktool` | `AQLAB_APKTOOL=/path/to/apktool.jar`, else `apktool` on `PATH` |
| `aapt2`, `zipalign`, `apksigner` | `ANDROID_HOME` / `AQLAB_SDK`, else `PATH` |

Per-tool overrides also work: `AQLAB_AAPT2`, `AQLAB_ZIPALIGN`, `AQLAB_APKSIGNER`,
`AQLAB_JAVA`.

## Quick start

```sh
aqlab scan app.apk          # what is the state of this app?
```

```
[FINDING] apk: app.apk
  package        : com.example.app
  label          : 'Example'
  locales        : 87 -> --_--, af, am, ar, as, az, be, bg, bn, bs, ca, cs, da, de
  arabic present : yes
  supportsRtl    : no (or not detectable in binary XML)
  native libs    : 6 (renaming packages in native apps can break self-checks)
```

```sh
aqlab decode app.apk -o work        # unpack
aqlab rtl    work --dry-run         # preview the migration
aqlab rtl    work                   # apply it
aqlab scaffold work                 # -> work/res/values-ar/strings.tsv
#     ... translate the last column (DeepL, an LLM, or a human) ...
aqlab apply  work --tsv work/res/values-ar/strings.tsv
aqlab lint   work                   # exits non-zero on real errors
aqlab build  work --ks release.keystore --ks-alias release
```

Or all of it at once:

```sh
aqlab all app.apk --tsv ar.tsv --ks release.keystore --ks-alias release
```

## Commands

| command | what it does |
|---|---|
| `scan <apk\|dir>` | read-only report: locales, `supportsRtl`, remaining `left/right`, coverage. `--json` for tooling. |
| `decode <apk>` | unpack an APK into an editable project |
| `rtl <dir>` | set `android:supportsRtl` and migrate `left/right` → `start/end` across `res/**`. `--dry-run` to preview. |
| `scaffold <dir>` | create `res/values-ar/` and a `strings.tsv` translation table |
| `apply <dir> --tsv` | import a translated table into `values-ar/strings.xml` |
| `lint <dir>` | bidi hazards, escaping bugs, format-specifier drift, coverage. Non-zero exit on errors. |
| `build <dir>` | `apktool b` → `zipalign` → `apksigner sign` → `apksigner verify` |
| `all <apk>` | the whole pipeline |

## What it fixes

* `android:supportsRtl="true"` on `<application>`
* `paddingLeft/Right` → `paddingStart/End`, and the same for `layout_margin*`,
  `drawable*`, `layout_align*`, `layout_alignParent*`, `layout_to*Of`
* ConstraintLayout `app:layout_constraint{Left,Right}_to{Left,Right}Of` → `{Start,End}`
* `gravity` / `layout_gravity` / `foregroundGravity` value tokens
* `textAlignment` → `viewStart` / `viewEnd`
* `<item name="android:paddingLeft">` entries inside `<style>` blocks
* theme references that would otherwise keep resolving to the LTR value
  (`?listPreferredItemPaddingLeft` → `?…Start`)
* Android resource escaping: `'`, leading `@`/`?`, and significant
  leading/trailing spaces

Every rename is guarded: when a tag already declares the `start`/`end` twin, the
old attribute is **removed** instead of duplicated. That guard is the difference
between a project that builds and one that dies inside `aapt2` with
`error: duplicate attribute` — and it is why this works on AppCompat and Material
layouts, which routinely carry both.

## The part nobody else does: bidi linting

Arabic runs right-to-left. Numbers, Latin words, file paths and `%1$s`
placeholders inside it run left-to-right. Where the two meet, the Unicode
Bidirectional Algorithm reorders runs and punctuation ends up on the wrong side.

```
text:     المجلد test.
renders:  المجلد test.          the period sits right of "test"; it should sit left
fix:      المجلد test\u200f.    U+200F RIGHT-TO-LEFT MARK pins it in place
```

`aqlab lint` finds that, plus the failures that actually break the build:

```
[FINDING] lint res/values-ar: 148/190 translated (77.9%)
  [WARN]  abc_menu_ctrl_shortcut_label: identical to source and contains Latin — probably untranslated
  [ERROR] discount: bare `%` — must be `%%` or a format specifier, aapt will reject it
  [ERROR] welcome: format specifiers differ: source ['%1$s'] vs arabic ['%s']
  [ERROR] note: unescaped apostrophe — use \' inside Android string resources
  [ERROR] folder: trailing Latin/digit run before punctuation — needs U+200F (RLM)
[FAIL] 4 error(s), 1 warning(s)
```

| rule | level |
|---|---|
| format specifiers differ from the source | error |
| bare `%` that `aapt` will reject | error |
| unescaped `'` inside a string resource | error |
| value starts with `@` or `?` | error |
| markup tags (`<b>`, `<xliff:g>`) differ from the source | error |
| trailing Latin/digit run before punctuation (needs U+200F) | error |
| value identical to the source and contains Latin | warn |
| no Arabic characters in the Arabic value | warn |
| leading/trailing space mismatch | warn |
| a plural form that drops the count | warn — Arabic legitimately omits it in `one`/`two` |
| mixed Arabic + Latin in one string | info |

Exit code is non-zero on errors, so it drops straight into CI.

## What it does *not* do

* **It does not translate anything.** No machine translation, no AI. The last
  column of the TSV is your job — the tool guarantees the mechanics survive once
  it is filled.
* It does not touch strings compiled into DEX/smali or native code.
* It does not mirror images or vector assets with baked-in Latin text.
* It deliberately does not force `android:layoutDirection="rtl"` — that would
  break every other locale in a multi-language app. `supportsRtl` with
  `start`/`end` is the correct mechanism, and it is what Google's own
  documentation specifies.

## Tested on

Real APKs, end to end (`decode` → `rtl` → `build` → sign → verify) — not just
unit tests. `supportsRtl=true` is read back out of the **compiled** manifest, and
the output is checked with `apksigner verify` and `zipalign -c -v 4`:

| app | locales | rebuilt | signed | `supportsRtl` in compiled manifest |
|---|---|---|---|---|
| multi-language app (`com.gilgamesh.vip`) | 87 | yes | yes | ✅ `true` |
| utility app (`TS_Vip_v115`) | — | yes | yes | ✅ `true` |
| utility app (`bs_1.0`) | — | yes | yes | ✅ `true` |

The `rtl` pass is idempotent: running it a second time reports
`already RTL-clean — nothing to migrate` and writes nothing.

## FAQ

**A build failed with `error: duplicate attribute`.**
You are on a version before the twin guard. Update, or open an issue with the
file named in the error.

**It refuses to run with `minSdkVersion=15`.**
`start`/`end` resources do not exist before API 17. Raise `minSdkVersion`, or
pass `--force-min-sdk` knowing the build will then fail.

**The rebuilt app crashes, or detects that it was modified.**
Expected when the original verifies its signature or dex checksums natively — a
rebuild changes both. See [SECURITY.md](SECURITY.md).

**Does it work for Hebrew, Persian or Urdu?**
The mechanics are identical — `rtl`, `lint` and the attribute tables are
script-agnostic, and `--locale` accepts any locale code. The Arabic-specific
parts are the plural rules and the bidi rules.

**Why not just use Android Studio's RTL preview?**
That shows you *a* layout. This fixes every layout in the app and tells you which
strings are still broken.

## Requirements

Python 3.9+, `apktool`, and Android SDK build-tools (`aapt2`, `zipalign`,
`apksigner`). Nothing else — no pip dependencies, no `lxml`, no `androguard`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The test suite is plain asserts and runs
anywhere:

```sh
python tests/test_aqlab.py       # 60 checks, no framework, no network, no apktool
```

Most wanted: plural validation for the six Arabic forms, `android:localeConfig`
scaffolding for the Android 13+ per-app language picker, and vector-drawable
mirroring warnings. See [CHANGELOG.md](CHANGELOG.md).

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

Run it on apps you own or are permitted to modify. Redistributing a rebuilt APK
may infringe the original app's licence or a store's terms of service. That is
your call, not the tool's.
