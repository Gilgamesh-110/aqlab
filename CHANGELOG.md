# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned
- plural-rule validation for the six Arabic forms (`zero/one/two/few/many/other`)
- `android:localeConfig` scaffolding for the Android 13+ per-app language picker
- vector-drawable mirroring warnings

## [0.1.5] — 2026-10-27

### Packaging
- Releases now publish themselves. `publish.yml` uploads on a published release
  through PyPI Trusted Publishing, so **no API token is stored anywhere** — PyPI
  verifies the workflow's identity instead of trusting a secret.
- The job refuses to upload unless the release tag matches `__version__`, and it
  installs the built wheel into a clean virtualenv and runs the console script
  before uploading. A package that installs but does not run is worse than no
  package.

### Changed
- `pip install aqlab` is the first install line in both READMEs, with the git and
  curl routes kept for anyone who does not want a package manager.

## [0.1.4] — 2026-10-26

### Added
- `scaffold` reads every `res/values/*.xml`, not just `strings.xml`, and the
  translation table carries a row for each of Arabic's six plural categories even
  where the source has none. `apply` accepts those extra rows and writes each
  entry back into the file its source came from, so an app that keeps
  `values/plurals.xml` gets `values-ar/plurals.xml`. Without this there was
  nowhere for a translator to put `few`/`many`, and every entry funnelled into
  `strings.xml` — a duplicate-resource error for exactly the apps that ship a
  separate plurals file. Verified with a real `aapt2 compile` + `link`: the built
  APK carries two categories for the default locale and six for `ar`.
- `scan` reports every locale folder, not only the ones holding a `strings.xml`.

### Fixed
- `examples/ar.tsv` was in `.gitignore` (`*.tsv` covers the tool's working files)
  and had never been committed, so the README's second quickstart command failed
  on every fresh clone with `FileNotFoundError`. The file is now tracked, with a
  targeted negation for it, and `tests/test_readme.py` runs the documented
  commands in CI and diffs the result against the block printed below them.
- `rtl` corrupted a self-closing `<application/>`. The `supportsRtl` insert
  stripped the trailing `>` and appended the attribute, leaving a stray `/` in
  front of it and a manifest that no longer parses — discovered only when the
  build fails, with the error pointing at the manifest rather than at the tool.
- The translation table was written to `res/values-<locale>/strings.tsv`. A `.tsv`
  is not a resource and `aapt2 compile --dir res` rejects it with
  `invalid file path`. It now lives at `<project>/values-<locale>.tsv`. apktool
  happens to skip the file, which is the only reason this went unnoticed.

### Changed
- Plural `<item>`s are written in canonical CLDR order (`zero one two few many
  other`) rather than source order, so a generated file reads like a hand-written
  one.

### Packaging
- First release on PyPI. The wheel declares its license as an SPDX expression
  (`License-Expression: MIT`) and the build is warning-free.

## [0.1.3] — 2026-10-26

### Added
- `lint` reads `<plurals>` as a structure and checks the six Arabic categories
  instead of treating each quantity as an unrelated key. Arabic needs `few`
  (3-10) and `many` (11-99), which are exactly the two an English source cannot
  provide, so the usual result is an Arabic `<plurals>` that mirrors the English
  `one`/`other`, compiles, installs, and reads wrong for every count above 2.
  Reported as errors: a missing `other`/`few`/`many`, naming the count range each
  one covers. As warnings: a missing `zero`/`two`, and `few`/`many`/`other`
  carrying identical text.
- `lint` reads every `*.xml` in the locale folder rather than only
  `strings.xml`. Plurals normally live in their own `plurals.xml` — AOSP and most
  Material apps ship them that way — so a strings-only read missed exactly the
  entries the new check exists to inspect. Region variants (`values-ar-rSA`)
  count as the locale.

### Fixed
- Coverage could exceed 100%: it was `len(arabic)/len(source)`, and a correct
  Arabic `<plurals>` has six quantities where the English source has two (233% on
  a seven-entry file). It is now the share of source entries that have an Arabic
  counterpart, which cannot exceed 100%.

### Changed
- A plural `zero`/`one`/`two` that omits the count placeholder is no longer
  reported at all. `عنصر واحد` and `عنصران` are correct Arabic, so the old
  warning was noise on every well-translated file. Inventing a placeholder the
  source does not have is still an error.

## [0.1.2] — 2026-10-26

### Performance
- `decode` (and `all`) now skip `smali/` by default. Disassembling the bytecode was
  most of the decode time and this tool never edits code, so it was ~50s of work
  nobody reads. `apktool` records the source APK in `apktool.yml` and copies the
  original `classes*.dex` back on build unchanged, so the output is unaffected.
  `--with-sources` restores the old behaviour for anyone who does want to hand-edit
  code. On a real 17 MB app (8,447-file tree, 125 layouts, 5 dex files):
  `decode` **52.1s -> 10.0s**, `build` **65.3s -> 27.0s**, whole signed pipeline
  **~117s -> 51s**. Verified end to end: every dex `sha256` identical to the
  original, `apksigner verify` OK, `zipalign -c` OK, `supportsRtl=true` and the
  migrated attributes present in the compiled resources.
- `rtl` no longer runs the rename tables over every attribute of every element.
  A tag is skipped unless one of the names the tables know about appears in it, so
  the common `layout_width` / `text` / `id` tags cost one substring search instead
  of an attribute scan plus a `migrate_attr` call each. The pre-filter is derived
  from the tables, so a new rename is covered automatically and it cannot drift.
  Same tree: **6.62s -> 4.31s** best-of-5, pure-Python calls **692k -> 288k**,
  output byte-for-byte identical to the previous implementation.

### Added
- Brand kit in `assets/`: `logo.svg`, `banner.svg`, `features.svg`,
  `before-after.svg` and `og-card.svg` (plus rendered PNGs), all drawn on the
  GitHub dark palette so they sit flush with the README.

## [0.1.1] — 2026-10-26

### Fixed
- `rtl` reported `already RTL-clean — nothing to migrate` when the path did not
  exist or was not a decoded project. It now fails loudly instead. That answer was
  the worst possible one for a wrong path.
- `minSdkVersion` is now read from `apktool.yml` when the manifest has no
  `<uses-sdk>` element. Real APKs decode that way — a 99-locale game reported
  `minSdk 23` only in `apktool.yml` — so the API-17 guard was silently doing
  nothing on exactly the apps that needed it.

### Added
- `examples/demo-project` plus `examples/ar.tsv`: a small decoded project that
  contains the three traps (a layout with both `paddingLeft` and `paddingStart`, a
  `<style>` carrying direction in an `<item>` value, and a
  `?listPreferredItemPaddingLeft` reference), so the tool can be tried without an
  APK or the Android SDK.
- `docs/bidi.md` and `docs/troubleshooting.md`.
- `.editorconfig`, `CITATION.cff`.

## [0.1.0] — 2026-10-26

First release.

### Added
- `scan` — read-only report: locales, `supportsRtl` state, count of remaining
  legacy `left/right` attributes, translation coverage. Works on an APK
  (via `aapt2 dump badging`) or on a decoded project. `--json` for tooling.
- `decode` — unpack an APK into an editable project.
- `rtl` — sets `android:supportsRtl="true"` and migrates legacy direction
  attributes to logical ones across `res/**`:
  `padding`, `layout_margin`, `drawable`, `layout_align`, `layout_alignParent`,
  `layout_to*Of`, ConstraintLayout `layout_constraint*_to*Of`, `gravity`,
  `layout_gravity`, `foregroundGravity`, `textAlignment`, plus `<item name="…">`
  entries inside `<style>` blocks. `--dry-run` to preview.
- `scaffold` / `apply` — `res/values-ar/` plus a TSV translation table that
  round-trips. Reads both 2-column (`key⇥translation`) and 3-column tables.
- `lint` — bidi trailing-run hazards, bare `%`, format-specifier drift,
  unescaped apostrophes, leading `@`/`?`, markup-tag drift, leading/trailing
  space mismatch, untranslated values, coverage. Exits non-zero on errors.
- `build` — `apktool b` → `zipalign` → `apksigner sign` → `apksigner verify`.
- `all` — the full pipeline in one command.
- Automatic tool discovery via `ANDROID_HOME` / `AQLAB_*` overrides.

### Fixed during bring-up
- A layout declaring both `paddingLeft` and `paddingStart` produced a duplicate
  attribute after migration and `aapt2` aborted the entire build. The renamed
  attribute is now dropped when its start/end twin is already present.
- `<item name="android:paddingLeft">` inside `<style>` carries the direction in
  the attribute *value*, not its name — now handled, including the
  twin-already-declared case.
- `android:paddingStart="?listPreferredItemPaddingLeft"` kept resolving to the
  left padding in RTL. Curated theme references now follow the rename.
- A trailing `%s` placeholder was reported as a bidi hazard. Format specifiers
  are stripped before the trailing-run test.
- `scaffold` could overwrite an existing `values-ar/strings.xml`. It now refuses
  without `--force`.
- A project with `minSdkVersion < 17` failed late, inside `aapt2`. It is now
  rejected up front, before anything is written.

[Unreleased]: https://github.com/Gilgamesh-110/aqlab/compare/v0.1.5...HEAD
[0.1.5]: https://github.com/Gilgamesh-110/aqlab/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/Gilgamesh-110/aqlab/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/Gilgamesh-110/aqlab/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/Gilgamesh-110/aqlab/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/Gilgamesh-110/aqlab/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Gilgamesh-110/aqlab/releases/tag/v0.1.0
