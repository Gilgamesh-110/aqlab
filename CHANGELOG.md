# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned
- plural-rule validation for the six Arabic forms (`zero/one/two/few/many/other`)
- `android:localeConfig` scaffolding for the Android 13+ per-app language picker
- vector-drawable mirroring warnings

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

[Unreleased]: https://github.com/Gilgamesh-110/aqlab/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/Gilgamesh-110/aqlab/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Gilgamesh-110/aqlab/releases/tag/v0.1.0
