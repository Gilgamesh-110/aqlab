# arabize

**Make an Android app Arabic — RTL-correct — in one command.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)
[![No dependencies](https://img.shields.io/badge/dependencies-none-brightgreen.svg)](#requirements)

[العربية](README.ar.md) · English

---

Most "add Arabic" attempts produce an app where the words are Arabic and the
whole interface is still backwards — nav drawer on the left, back arrow pointing
left, progress bars filling left-to-right, `%d` placeholders dropped, punctuation
jumping to the wrong side of the line.

`arabize` handles the part that is not translation: **layout direction, attribute
migration, Unicode bidi hazards, and resource escaping.**

## What it does

| command | what happens |
|---|---|
| `scan` | read-only report: locales, RTL state, legacy `left/right` count, coverage |
| `decode` | unpack an APK into an editable project |
| `rtl` | sets `android:supportsRtl` and migrates `left/right` → `start/end` everywhere |
| `scaffold` | builds `res/values-ar/` + a translation table (`strings.tsv`) |
| `apply` | imports a translated table into `values-ar/strings.xml` |
| `lint` | bidi hazards, escaping bugs, format-specifier mismatches, coverage — CI-friendly |
| `build` | rebuild, `zipalign`, sign, verify |
| `all` | the whole pipeline |

## Install

```sh
pipx install git+https://github.com/<you>/arabize      # or
curl -O https://raw.githubusercontent.com/<you>/arabize/main/arabize.py
```

Single file, stdlib only. It shells out to tools you already have:

* `apktool` — `ARABIZE_APKTOOL=/path/to/apktool.jar`
* `aapt2`, `zipalign`, `apksigner` — from Android SDK build-tools
  (`ANDROID_HOME`, or `ARABIZE_AAPT2` etc.)

```sh
arabize scan app.apk          # what is the state of this app?
```

```
[FINDING] apk: app.apk
  package        : com.example.app
  locales        : 87 -> --_--, af, am, ar, as, az, be, bg, bn, bs, ca, cs, da, de
  arabic present : yes
  supportsRtl    : no (or not detectable in binary XML)
```

## Quick start

```sh
arabize decode app.apk -o work
arabize rtl    work                 # --dry-run first, if you like
arabize scaffold work               # -> work/res/values-ar/strings.tsv
#    ... translate column 3 (with DeepL, an LLM, or a human) ...
arabize apply  work --tsv work/res/values-ar/strings.tsv
arabize lint   work                 # exits non-zero on real errors
arabize build  work --ks release.keystore --ks-pass … --ks-alias …
```

One-shot:

```sh
arabize all app.apk --tsv ar.tsv --ks release.keystore --ks-pass …
```

## The part nobody else does: bidi linting

Arabic is a right-to-left script, but numbers, Latin words, file paths and
`%1$s` placeholders inside it are left-to-right. When the two mix, the Unicode
Bidirectional Algorithm reorders runs — and punctuation lands on the wrong side.

```
text:     المجلد test.
renders:  المجلد test.        ← the period should sit left of "test", it does not
fix:      المجلد test\u200f.  ← U+200F RIGHT-TO-LEFT MARK pins it
```

`arabize lint` catches that, plus the failures that actually break builds:

```
[FINDING] lint res/values-ar: 148/190 translated (77.9%)
  [WARN]  abc_menu_ctrl_shortcut_label: identical to source and contains Latin — probably untranslated
  [ERROR] discount: bare `%` — must be `%%` or a format specifier, aapt will reject it
  [ERROR] welcome: format specifiers differ: source ['%1$s'] vs arabic ['%s']
  [ERROR] note: unescaped apostrophe — use \' inside Android string resources
[FAIL] 3 error(s), 0 warning(s)
```

Rules: format-specifier mismatch, bare `%`, unescaped `'`, leading `@`/`?`,
XML markup-tag drift, trailing LTR runs before punctuation, untranslated
values, space mismatches, and plural forms that legitimately drop the count
(a warning in Arabic `one`/`two`, never an error).

Exit code is non-zero when it finds errors, so it drops straight into CI.

## What it fixes

* `android:supportsRtl="true"` on `<application>`
* `paddingLeft/Right` → `paddingStart/End`, and the same for
  `layout_margin*`, `drawable*`, `layout_align*`, `layout_alignParent*`, `layout_to*Of`
* ConstraintLayout `app:layout_constraint{Left,Right}_to{Left,Right}Of` → `{Start,End}`
* `gravity` / `layout_gravity` / `foregroundGravity` value tokens
* `textAlignment` → `viewStart` / `viewEnd`
* theme references that would otherwise keep resolving to the LTR value
  (`?listPreferredItemPaddingLeft` → `?…Start`)
* Android resource escaping, including `'`, leading `@`/`?`, and significant
  leading/trailing spaces

## What it does **not** do

* **It does not translate anything.** No MT, no AI. Column 3 of the TSV is your
  job — the tool guarantees the mechanics survive once it is filled.
* It does not touch strings compiled into DEX/smali or native code.
* It does not mirror images or vector assets that contain baked-in Latin text.
* It deliberately does not force `android:layoutDirection="rtl"` — that would
  break every other locale in a multi-language app. `supportsRtl` + `start/end`
  is the correct mechanism.

## Requirements

Python 3.9+, `apktool`, and Android build-tools. Nothing else — no pip
dependencies, no `lxml`, no `androguard`.

## Tests

```sh
python tests/test_arabize.py     # 47 asserts, no framework, no network
```

## Roadmap

* plural rule validation for the six Arabic forms (`zero/one/two/few/many/other`)
* `android:localeConfig` scaffolding for Android 13+ per-app language pickers
* vector-drawable text mirroring warnings

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

Use it on apps you own or have permission to modify. Redistributing a rebuilt
APK may infringe the original app's license or the store's terms; that is your
call, not the tool's.
