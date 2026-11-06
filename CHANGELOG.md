# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **`lint --fix` repairs `pinNested`.** The pin itself is deliberate (a clock
  face, a keypad), so the fixer does not remove it: it applies `aqlab rtl` to the
  named row only, renaming the physical `paddingLeft`/`layout_marginRight` that
  sits under the inherited direction. Idempotent, `--dry-run` aware, and it never
  touches the ancestor pin or a row that sets its own direction.
- **`DirectionPins` follows a pin into the children.** A direction set on a
  container is the resolved direction of every descendant that does not
  override it, so a pinned `<LinearLayout>` whose child rows carry `paddingLeft`
  is the same defect one level up. Reported as kind `pinNested`, naming the
  inherited value; a child that sets `layoutDirection`/`textDirection` of its own
  (including `locale`) stops the chain. Nesting comes from a new
  `walk_layout` scanner, which also fixes the `orientation` test: it used to run
  on the raw tag body, so a `/>`-closed row's trailing slash hid the closing
  break. Measured on the real APK: both Material `layoutDirection="ltr"` pins sit
  on containers whose only children are `include`s with no physical attributes,
  so 0 new findings (41 warnings, 1 error, unchanged).

- **`DirectionPins` now also catches `textDirection`.** `android:textDirection="ltr"`
  (or `"rtl"`) on a horizontal row that already sets a left/right attribute
  keeps the *text* pinned while the row flips — the same author mistake one level
  down from a pinned `layoutDirection`. `firstStrong` (the platform default),
  `anyRtl`, `locale` and `inherit` follow the direction and stay silent.
  Measured on the real APK: no layout sets `textDirection`, so 0 new findings.
### Fixed
- **`DirectionPins` leaked a pin past its container.** A self-closed element's
  pin was pushed onto the scope stack, and every later sibling at the same depth
  was then reported as inheriting a direction no ancestor set — measured on the
  round-9 fixture, which counted 5 inherited rows where only 3 exist. The pin now
  ends where its element does.
- **`DirectionPins` could miss pins entirely on real layouts.** The element
  matcher was a single regex, and two siblings opening with the same tag
  collapsed into one match whose body swallowed the next element's attributes,
  so a pin could vanish and a finding could be attributed to the wrong row. It
  also cut a tag in half when an attribute value held `>` (`android:tag="a>b"`).
  Tags are now walked by `iter_start_tags`, a small scanner that keeps quoted
  values whole. Re-checked on the real APK: still the same two Material pins,
  both correctly silent because neither row carries a physical attribute.
### Added
- **`lint --fix` now repairs `RtlHardcoded` too.** The linter reported a
  physical `gravity` value and told the user to run a different command to
  repair it. `--fix` now rewrites the value to `start`/`end` (and
  `viewStart`/`viewEnd` for `textAlignment`, the one attribute with its own
  vocabulary), driven by the same token table `aqlab rtl` uses, and only on
  attributes the check named — `android:text="left"` is left alone. Measured
  on the real APK: 41 warnings -> 39, both `left|center` values rewritten,
  second pass writes nothing.
### Added
- **`RtlDrawableDirs`.** A `drawable-ldrtl*` / `mipmap-ldrtl*` folder that
  mirrors nothing: empty (or only a `.gitkeep`), or holding a file that is
  byte-identical to the same-named file in the unqualified peer directory.
  Aapt merges that copy without complaint, so the build stays green while the
  author's intent is lost. An `ldrtl`-only file with no LTR twin is a
  legitimate mirror and stays silent (measured: the real APK's
  `drawable-ldrtl-xxhdpi` holds one such spinner, 0 findings).
- **`lint --fix`.** Fills a missing `paddingStart` / `paddingEnd` /
  `layout_marginStart` / `layout_marginEnd` partner on views that already use
  the modern spellings, copying the value from the side that exists. Idempotent
  and `--dry-run` aware. Physical pairs (`paddingLeft` without `paddingRight`)
  are deliberately left to `aqlab rtl`: measured on a real APK, filling one of
  those puts a `marginStart` beside a lone `marginRight` and turns one finding
  into two.
- **`DirectionPins`.** Two ways an app decides the reading direction at
  authoring time instead of letting the locale decide:
  `android:layoutDirection="ltr"` on a horizontal row that *also* sets
  `paddingLeft` / `layout_toLeftOf` / `layout_gravity="left"` (the pin is then a
  workaround, not a design choice, and the row stays LTR in Arabic), and an
  explicit `android:supportsRtl="false"` in the manifest. `aqlab scan` and
  `aqlab lint` both report them. A pin on a row with no physical attributes —
  a clock face, a keypad — is silent by design: measured on a real APK, both of
  its pins are Material's `material_clock_display` and
  `material_timepicker_textinput_display`, and both are correctly ignored.

### Fixed

- **`aqlab rtl` never rewrote a compound gravity value.** `gravity="left|center"`
  is the common form, and the token test was `(left|right)` — in Python a
  `` only sits between a word char and a non-word char, and both sides of
  `left` in `left|center` are non-word, so `fix` matched nothing while `lint`
  reported the element. Measured on the real APK: both of its `RtlHardcoded`
  findings were exactly this form, so the fix the warning pointed at was a
  no-op. Now one `GRAVITY_TOKEN` (`(?<![A-Za-z])(left|right)(?![A-Za-z])`) drives
  the linter, `migrate_attr` and the style repair, so the value the report names
  is the value the fixer rewrites — `start|center` out. A word holding the token
  (`leftShape`) stays untouched.

## [0.1.14] — 2026-11-06

The last two items from the roadmap.

### Added
- **`aqlab diff <a.apk> <b.apk>`.** Added / removed / changed string resources
  between two APKs, read straight out of `aapt2 dump resources` — neither APK is
  decoded, so no 50-second apktool run per side and no working copies left behind.

  ```
  [FINDING] diff [ar]: v2.apk -> v3.apk
    strings        : 2998 -> 2998
    added          : 1
    removed        : 1
    changed        : 1

    changed:
      ~ (ar) changing
          - قبل
          + بعد
  ```

  Text that did not change is never listed — it is the majority of any diff and
  would bury the real changes. `--locale` narrows to one locale, `--json` for
  tooling, exit 0 when identical so CI can gate on drift. Values that aapt2
  could not resolve (`@0x…`) are skipped rather than reported as a change on
  every run.

- **`aqlab xliff <dir>`.** Wraps every format specifier in `<xliff:g id="…">`, so
  translation tooling cannot reorder, pluralise or drop it — a bare `%1$s` that a
  translator touches crashes the app at `String.format` with an argument
  mismatch, one of the most common localisation crashes there is.

  ```xml
  <string name="welcome">مرحباً <xliff:g id="arg1">%1$s</xliff:g></string>
  ```

  The id comes from the specifier's own index (`%2$d` → `arg2`), so it stays
  stable across re-translation instead of shifting with word order. `%%` is a
  literal percent and is left alone; an existing `<xliff:g>` is not wrapped
  twice; `xmlns:xliff` is declared when the file needs it. `--dry-run` included.

  Verified with aapt2: compiles, links, and the resolved value is the bare
  `%1$s` again — the tag exists only in the source.

## [0.1.13] — 2026-11-06

Three things from the plan, each measured rather than assumed.

### Added
- **`--dry-run` on `apply` and `scaffold`.** Prints the exact plan and changes
  nothing:

  ```
  [DRY] would write 3 file(s) from 1424 translation(s)
    would overwrite: proj/res/values-ar/strings.xml (1370 entries)
    would create: proj/values-ar.tsv (1462 entries)
  ```

  The writes are also collected and applied together now, so a failure part-way
  through leaves the locale folder as it was instead of half-rewritten.

- **Coverage per source file, and `--fail-under` / `--fail-under-file`.**
  One number hides the file that is behind: an overall 88% can be `strings.xml`
  at 99% and `plurals.xml` at 60%. Both ratios print, the weakest is marked, and
  either can gate CI.

- **Other RTL locales: `he`, `fa`, `ur`.** The plural tables were Arabic's six
  applied to everything. CLDR gives each language its own set — Hebrew has
  `one/two/many/other` (no `zero`, no `few`) and `many` means 20, 30, 40 …;
  Persian and Urdu have `one/other`. Demanding six of Hebrew would have left a
  `few` form on translators for a category the language never uses. Ranges in
  messages come from the locale too, so Hebrew reports `many (20, 30, 40 …)`
  rather than Arabic's `11-99`.

- **`non_resource_files()`: says when a file under `res/` will break the build.**
  `aapt2 compile --dir res` rejects anything that is not a resource, and this
  project shipped that bug twice — a `.bak` and a `.tsv`. `apply` and `scaffold`
  now check and warn where the mistake is made rather than at build time.

### Changed
- Lint messages no longer say "Arabic" for a locale that is not: `missing
  quantities`, `no <locale> <plurals> for this name`.

## [0.1.12] — 2026-11-06

### Fixed
- **The `--force` backup made projects unbuildable.** 0.1.9 started writing
  `values-ar/strings.xml.bak` next to the file it replaced. `aapt2` rejects any
  file under `res/` that is not a resource:

  ```
  error: invalid file path 'res/values-ar/strings.xml.bak'.
  ```

  Found by building a real APK instead of only running the tests — the tests
  never compiled the tree. The copy now goes to `<proj>/aqlab-backup-<locale>/`,
  outside `res/`, which is the rule the translation table already followed.
  Verified end to end: 84 MB APK built, zipaligned, signed, `apksigner verify`
  returns VERIFIED.

## [0.1.11] — 2026-11-06

The three open issues, each verified against a real APK and mutated to prove
its test fails when the code is removed.

### Added
- **`scaffold` writes `res/xml/locales_config.xml`** and adds
  `android:localeConfig` to `<application>`, so Android 13+ shows the app in the
  per-app language picker. Verified with `aapt2 compile` and `aapt2 link` — a
  wrong resource name fails at link time, not at write time.

  Qualifier folders (`values-sw600dp`, `values-night`) are excluded; below
  `minSdkVersion 33` the run says `skipped (… needs 33+)` and writes nothing;
  `--no-locale-config` opts out. An app that already declares its own config is
  left alone and told so — writing ours would be a dead resource, and rewriting
  theirs would break a file the tool knows nothing about.

- **`lint` and `scan` report vector drawables that look like lettering.** A
  Latin wordmark baked into `<path>` geometry cannot mirror with the UI. Three
  conditions must all hold: it is a `<vector>` (not a `<shape>` or a
  `<layer-list>`), it does not already set `autoMirrored="true"`, and it has 12+
  paths.

  The threshold comes from measurement, not taste: 215 drawables in a real APK,
  86 of them vectors, **every one 2 paths**. A name-based rule would have flagged
  `bg_elo_badge.xml` (a `<shape>` background) and
  `abc_ic_arrow_drop_right_black_24dp.xml` (already auto-mirrored) — the same
  mistake the bidi rule made, and a user who is warned about correct files stops
  reading the warnings.

### Already satisfied, closed
- **Plural category validation.** `lint` has reported missing Arabic quantities
  per `<plurals>` block and `scaffold` has emitted all six stubs since 0.1.4;
  the issue was opened against behaviour that had already shipped. Verified and
  closed with the exact cases it asked for.

### Fixed
- **`scaffold --force` deleted four of the six Arabic plural categories.**
  Arabic needs `zero/one/two/few/many/other`; an English source declares only
  `one` and `other`, so the other four exist only in `values-ar`. Output was
  built by iterating the *source* entries, so they were dropped on every forced
  run — a real locale went from **54 quantities to 18**. `.bak` kept a copy but
  the run still reported success. Existing categories are now carried through.

- `lint --help` still advertised "BiDi hazards", which 0.1.8 removed. The first
  thing a new user reads disagreed with the README.

## [0.1.10] — 2026-11-05

### Fixed
- **Markup was escaped into text.** `escape_android` escaped every `<`, so a
  string using `<xliff:g id="name">%1$s</xliff:g>` — the standard way to protect
  an argument from translation — was written as `&lt;xliff:g …&gt;` and the
  phone printed the tag instead of substituting the name. `<b>`, `<i>`, `<a
  href>` were broken the same way. Known Android markup is now kept as markup,
  any other `<` is still escaped, and `<resources>` gets `xmlns:xliff` when the
  file uses it.

- **`formatted="false"` and other string attributes were dropped.** `apply`
  rewrote `<string name="x">` with no attributes, so `formatted="false"`
  vanished — and a translated `100%` without it is an `aapt2` compile error
  (`unescaped apostrophe` … `not a valid string`). Attributes are carried
  through from the source entry.

- **A newline in a translation broke the table.** `write_tsv` escaped newlines
  in the source column but wrote the translation column raw, so a translation
  containing a line break split its row in two and the rest of the value was
  lost on the next read. Both columns are single-line now, and the reader turns
  `\n` / `\t` back into the characters they stand for.

- **`CITATION.cff` was four releases behind.** It still said `0.1.5` while PyPI
  served `0.1.9`. Anyone citing the tool cited the wrong version.

## [0.1.9] — 2026-11-05

### Fixed
- **`apply` no longer deletes the translations it was not given.** It rewrites
  whole files from the table, so every key with a blank third column vanished
  from `values-ar`. A locale with 1,200 finished strings, applied against a
  table with five filled rows, came back as five strings — and printed
  `[DONE] 5 translation(s)` with no mention that 1,195 were gone.

  Existing translations are now read from the locale folder and merged: the
  table wins where it has text, the folder fills in the rest. The run reports
  what it kept. Verified on a 1,200-string locale: 5 updated, 1,195 kept.

- **`apply` no longer duplicates every plural item.** Plurals come back from the
  locale folder as `name|quantity` and the same keys also come from the source
  entries; both were appended, so `plurals.xml` gained duplicate `one` and
  `other` items on each run. `write_strings_xml` drops a key it has already
  written.

- **`scaffold --force` keeps a copy.** It rewrote the locale folder from the
  table and discarded translations the table did not carry, with no backup. It
  now writes `<file>.bak` beside them and reports how many it is about to lose;
  with a table present, existing translations are merged back in.

## [0.1.8] — 2026-11-05

### Removed
- **The bidi "hazard" check, which never detected anything.** Since 0.1.0 `lint`
  reported a trailing Latin/digit run before punctuation as an error needing
  `U+200F`; 0.1.7 replaced it with a rule naming any LTR run "without a bidi
  boundary". Both were written by reasoning over character classes and neither
  was ever tested against a renderer.

  It was tested now. A `TextView` app on Android 11 (`TEXT_DIRECTION_RTL`) read
  the resolved direction and horizontal span of every character out of the
  laid-out `android.text.Layout`, for 200 mixed Arabic/Latin strings taken from a
  shipped APK. **Zero rendering defects.** In `المجلد test.` the final period
  already sits to the left of `test`, which is where a reader expects it, and
  inserting `U+200F` moved nothing.

  Against that ground truth the rules fired 58 errors (0.1.0–0.1.6) and then 534
  findings (0.1.7) on correct text. On one real APK `lint` went from 58 errors to
  1 — and the one that remains is a genuine missing `<plurals>` quantity.

  Mixed Arabic and Latin is still reported, as `info`, with `--verbose`. See
  [docs/bidi.md](docs/bidi.md) for the measurement.

### Changed
- README and `docs/bidi.md` no longer claim bidi linting. The section now
  records what was measured and why the old rule was wrong.

## [0.1.7] — 2026-11-05

### Added
- **`lint` now names the LTR run that has no bidi boundary, instead of only noting
  that a string mixes scripts.** Per-block direction (`dir="auto"`) fixes the
  paragraph; it does nothing for a span inside it. `الإصدار v0.1.6.` still renders
  with the full stop on the wrong side unless the run is closed by a boundary —
  RLM/LRM/ALM on either edge, or `U+2068…U+2069` around it. The finding lists the
  offending run (`v0.1.6.`, `report_v2.pdf`, `50%`) so the fix is a wrap, not an
  investigation. Warn-level, because it is visible on a device; runs already
  inside an isolate pair are not reported. Hebrew and Persian are covered by the
  same check, not only Arabic.
- Format specifiers are opaque to the check: `%1$d` is not reported as the runs
  `1` and `d`, so a pluralised string does not light up.

### Fixed
- **`scan` reported `supportsRtl: no` on every real APK.** The check looked for
  the raw `0xffffffff` aapt1 prints; aapt2 renders the boolean symbolically
  (`supportsRtl(0x010103af)=true`), so the test never matched. Both renderings are
  accepted now, and an explicit `=false` is read as false rather than as absence.
- **Locale detection listed resource qualifiers as languages.** Any `values-*`
  directory counted, so `values-night`, `values-ldrtl`, `values-sw600dp`,
  `values-v31` and `values-w320dp-land` all appeared in the locale list — seven
  real locales buried under thirty qualifiers. Only language qualifiers are
  reported now, including the `b+sr+Latn` form.

## [0.1.6] — 2026-11-03

### Packaging
- **The PyPI page now tells the truth about what this runs on.** The classifier list
  carried a single `Programming Language :: Python :: 3`, so the rendered "Python
  Version" badge said `3` and every reader had to open `requires-python` to learn
  whether their interpreter was supported. It now lists 3.9 through 3.13
  explicitly, plus `3 :: Only` — the same set `requires-python = ">=3.9"` already
  enforced, now visible before installing.
- **`Development Status` moved from `3 - Alpha` to `4 - Beta`.** The alpha label
  was written before the tool had been run against real apps; it has since been
  validated on three, building and signing each and reading `supportsRtl` back
  from the installed manifest. Leaving "Alpha" on the page understated that.
- Added `Topic :: Software Development :: Build Tools` and `Topic :: Utilities`
  so the package is reachable from those PyPI browse paths, and `Typing :: Typed`.
- `[project.urls]` grew from two links to six: `Repository`, `Changelog`,
  `Documentation` and `Bug Tracker` now appear in the PyPI sidebar, where
  previously only Homepage and Issues existed.

### Repository
- Added `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1, with a note that
  disagreement about what a reverse-engineering tool may be used for is not
  misconduct — critiquing the technique is fine, dismissing the person is not).
- Added `SUPPORT.md`: where to ask, what a useful bug report contains, and what
  response to expect. Most reports resolve from the failing `aapt2` line, so the
  guide asks for that line rather than a summary.
- Added `.pre-commit-config.yaml` with an optional hook set, plus
  `tests/check_xml_tabs.py`, which fails on a literal tab in resource XML — aapt2
  rejects those and a tab is invisible in review.

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
