# DEVLOG — aqlab continuous development

One line per round. Each round is driven by the scheduled job `aqlab-continuous-dev`
(every 10 minutes, `continuity: true` so each run sees the previous output).

**Standing orders:** no publish, no push, no version bump. Zero test failures.
Every check mutation-tested. Measure on a real APK before claiming it works.

| round | date | what | tests | real-APK result |
|---|---|---|---|---|
| 1 | 2026-11-06 | `RtlSymmetry` — one side set without its partner | 202 → 208 | 49 findings |
| 2 | 2026-11-06 | `RtlHardcoded` — `gravity="left|bottom"` etc. | 208 → 215 | 2 findings |
| 3 | 2026-11-06 | `DirectionPins` — `layoutDirection` pinned over physical attrs + `supportsRtl="false"` | 215 → 222 | 0 findings (2 pins correctly ignored) |
| 4 | 2026-11-07 | lint --fix — fill missing padding/margin partner (modern pairs only) | 222 → 234 | 38 elements fixed, 49 → 10 findings, 2nd pass writes nothing |
| 5 | 2026-11-07 | `RtlDrawableDirs` — `drawable-ldrtl*`/`mipmap-ldrtl*` empty, or a byte-identical copy of the LTR file | 234 → 243 | 0 findings on real APK (`drawable-ldrtl-xxhdpi` holds 1 spinner with no LTR twin) |
| 6 | 2026-11-07 | `GRAVITY_TOKEN` — compound gravity (`left|center`) was reported by lint but rewritten by nothing; one token test now drives lint, `migrate_attr` and style repair | 243 → 251 | 2 findings still real, and now actually fixable by `aqlab rtl` |
| 7 | 2026-11-07 | `lint --fix` repairs `RtlHardcoded` (gravity/textAlignment values rewritten with the `aqlab rtl` token tables, attribute-name scoped) | 251 → 260 | 41 → 39 warnings on real APK, both `left|center` rewritten, 2nd pass writes nothing |
| 8 | 2026-11-07 | `DirectionPins` covers `textDirection`; element scanning moved from one regex to `iter_start_tags` after finding that two same-tag siblings collapsed into one match and swallowed the next row's attributes (and a `>` inside a value cut a tag in half) | 260 → 266 | real APK unchanged at 41 warnings, 1 error; both Material pins still correctly silent |
| 9 | 2026-11-07 | `DirectionPins` follows a pin into the children (`pinNested`) via a new `walk_layout` scanner; nesting was needed and exposed a second bug — a self-closed row's trailing `/` was left on the body so the orientation test read it as set | 266 → 272 | real APK unchanged at 41 warnings / 1 error; both Material pins sit on containers whose children are physical-attr-free `include`s |
| 10 | 2026-11-07 | `lint --fix` repairs `pinNested` — migrates the physical attrs of a row that only inherits a pin instead of removing the (deliberate) ancestor pin; found while wiring it that a self-closed element's pin leaked past its container and inflated round 9's fixture from 3 inherited rows to 5 | 272 → 281 | real APK unchanged at 41 warnings / 1 error, 0 `pinNested`, `--fix --dry-run` still plans 2 (the two gravity tokens) |
| 11 | 2026-11-07 | `lint --fix` drops a local `layoutDirection="ltr"` pin (migrating that row while doing it); `rtl` mirrors stay, and the warning only promises the fix for `ltr`. Wiring it exposed a test worth keeping: a fixer must be asserted through `cmd_lint`'s own count, on a project where the other fixers cannot contribute | 281 → 296 | real APK unchanged at 41 warnings / 1 error, 0 local pins, `--fix --dry-run` still plans the same 2 gravity rewrites |
| 12 | 2026-11-07 | `DirectionPins` reads `supportsRtl="false"` only from an `<application>` start tag with `<!-- -->` blanked out — an apktool comment or an `<activity>` carrying the attribute no longer reports an app that already says `true` | 296 → 298 | real APK unchanged at 41 warnings / 1 error, 0 `supportsRtl` findings |

| 13 | 2026-11-07 | `lint --fix` repairs a `gravity` on a row under an inherited pin (the finding now carries its attribute names) | 298 → 310 | real APK unchanged at 41 warnings / 1 error, 0 `pinNested`, `--fix --dry-run` still plans the same 2 rewrites |
| 14 | 2026-11-07 | attribute readers accept both quote characters — `aqlab rtl`, `RtlSymmetry` and `layoutDirection` pin detection all assumed `"`, so a valid single-quoted `paddingLeft` migrated nothing and was never reported; found by probing `rewrite_xml_attrs`, then checked the other three readers for the same assumption | 310 → 315 | real APK unchanged at 41 warnings / 1 error (it holds no single quotes), `--fix --dry-run` still plans the same 2 rewrites |
| 15 | 2026-11-07 | `fix_pin_nested` follows `<include layout="@layout/x">` into the included layout — `direction_pins` could only name the include tag itself, so a physical attribute inside the pulled-in layout stayed unmigrated while the pin kept rendering it one way; the sweep covers every qualifier defining that resource name (`layout/`, `layout-ar/`, `layout-ldrtl/`), since the device picks one per direction | 315 → 321 | real APK unchanged at 41 warnings / 1 error — its includes sit under pins but carry no physical attribute, so the resolver is never reached; `--fix --dry-run` still plans the same 2 gravity rewrites |
| 16 | 2026-11-07 | `pinNested` saw only a renamed attribute under the pin, so a row whose entire physical content was `gravity="left"` — no padding, no margin — was under an ancestor `layoutDirection` and reported by neither rule (`hardcoded_gravity` does not walk ancestors); the row-vertical guard was added back so a leaf `TextView` or a `ScrollView` child cannot be read as a row, and a locally pinned row no longer double-counts as inherited | 321 → 329 | real APK unchanged at 41 warnings / 1 error — its two pins are Material rows with no physical attribute anywhere beneath them, and the check never fires; `--fix --dry-run` still plans the same 2 gravity rewrites |
| 17 | 2026-11-07 | `pinNested` reads the inherited axis: a pinned row with no `orientation` (exactly what `--fix` leaves after migrating the pin's own `paddingLeft`) is a horizontal `wrap_content` strip, so a left/right beneath it was reported by nothing; a `vertical` row that states nothing keeps the outer axis, a leaf never becomes a row (`looks_leaf` by base name), and `fix_pin_local`/`fix_pin_nested` take the tag offset from `walk_layout` instead of `txt.find`, which used to edit a textually identical sibling | 329 → 346 | real APK unchanged at 41 warnings / 1 error — both of its Material pins are horizontal rows with no physical attribute anywhere beneath them; `--fix --dry-run` still plans the same 2 gravity rewrites |
| 18 | 2026-11-07 | `hardcoded_gravity` and both `lint --fix` writers read only double quotes — round 14 widened three readers to either quote char and missed these; a single-quoted `gravity` was never reported, while a single-quoted `paddingLeft` *was* reported by `symmetry_problems` but unreachable by `symmetric_fix`, so `--fix` promised a repair it could not do; `gravity_fix` now captures the quote and writes it back | 346 → 355 | real APK unchanged at 41 warnings / 1 error (it holds no single quotes, so the hole never fired there), `--fix --dry-run` still plans the same 2 rewrites |
| 19 | 2026-11-07 | `fix_symmetry` repaired only the first padding/margin pair of an element — `symmetric_fix` reads the attribute set once at the top, so a view with a hole in both the padding and the margin pair came back half-fixed, stayed reported, and the next `--fix` run edited the file again, contradicting its own idempotence check; found by probing a synthetic two-hole element, then confirmed the real APK's single such element is balanced | 355 → 360 | real APK unchanged at 41 warnings / 1 error — its one multi-pair element (`abc_action_mode_close_item_material.xml`) already grew both partners in round 18, so the hole never fires there; `--fix --dry-run` still plans the same 2 rewrites |
| 20 | 2026-11-07 | `fix_pin_local` dropped only one direction pin per row: `direction_pins` reports the first pin it finds and `break`s, so a `layoutDirection="ltr"` shadowed the `textDirection="ltr"` beside it — the fixer removed one, the row stayed reported, the next `--fix` edited the same file again, contradicting its own idempotence check; probing also showed the fixer accepted neither the `app:` prefix nor single quotes the reporter accepts, so an `app:layoutDirection` row was left as `<... app: android:orientation=...>`, a dangling prefix that is broken XML; `LOCAL_PIN_RE` now covers both names, both prefixes, both quotes, and the `textDirection` warning carries the `--fix` tip it was silently honouring | 360 → 371 | real APK unchanged at 41 warnings / 1 error — it sets no `textDirection` and no `app:` pin, so the holes never fired there; `--fix --dry-run` still plans the same 2 gravity rewrites and a settled second run writes nothing |
| 21 | 2026-11-07 | `RtlEnabled`: a `layoutDirection`/`textDirection` literal on an element with no physical attrs won over the locale and was reported by nothing — `direction_pins` needs a left/right beside the pin, so an Arabic screen kept that row in the direction it was authored in; six measured non-defects stay silent (`locale`, `firstStrong`/`anyRtl`/`inherit`, an `rtl` leaf, a project-declared enum in `values/attrs.xml`, a dotted custom class, and an `<include>`-only row — the real APK's two `ltr` pins are Material's clock display and time input, order-driven, not language-driven); the attribute is read off the start tag's own body, never the subtree, so a child's literal is not charged to its parent | 371 → 381 | real APK unchanged at 41 warnings / 1 error — its four `layoutDirection="locale"` are the correct spelling and its two `ltr` pins are include-only clock/timepicker rows, so the rule reports 0 there and adds no noise |

| 22 | 2026-11-07 | `lint --fix` promised to drop an `RtlEnabled` pin and never did: `rtl_enabled` names a direction literal on a row with no physical attrs, its warning says "`aqlab lint --fix` drops the pin", but the only fixer read `direction_pins`, which by definition never names such a row — probed on a two-line fixture, `fixed: 0`, warning repeated on every run; the queue now takes both sources. Joining them broke the order they relied on: `direction_pins` names rows *with* physical attrs, `rtl_enabled` rows *without*, so a file whose first row belongs to the second list queued backwards and the walk edited the wrong start tag; the queue is sorted by the offsets the file walk already produces | 381 → 399 | real APK unchanged at 41 warnings / 1 error — `rtl_enabled` names 0 rows there (four `locale` literals, two include-only clock/timepicker pins), so `--fix --dry-run` still plans the same 2 gravity rewrites and a settled second run writes nothing |
| 23 | 2026-11-07 | `RtlEnabled` was blind to resource references and to the manifest that turns RTL off: `@string/x` / `?attr/x` escaped the value class, and `supportsRtl="false"` made every `layoutDirection` row unreachable while nothing said so — probed by repointing the real APK's two clock pins to `@string/x_dir` (still 0 findings) and flipping its manifest (0 findings). Both now report; the opt-out supersedes the rows, an absent attribute does not (`aqlab rtl` is what adds it), and the manifest message is keyed by the attribute because `where` was still holding the last string key and charged the error to `character_counter_pattern` | 399 → 411 | real APK unchanged at 41 warnings / 1 error — `supportsRtl="true"` there, no reference-valued direction attrs; a copy with `supportsRtl="false"` gives 2 errors (its pre-existing format drift + the opt-out) |
| 24 | 2026-11-07 | `lint --fix` repaired nothing for the manifest opt-out it calls an error: `supportsRtl="false"` reported `run aqlab rtl` (a full migration needing minSdk 17, a `res/` dir and a rename of every `paddingLeft`), while `--fix` printed `fixed: 0` and left the error standing; the flip is now one attribute and nothing else moves. Probing found three real defects: the pattern had to anchor on the attribute name (an `android:label` whose value spelled `supportsRtl='false'` matched first and the flip landed inside the label), comments must be searched as separate runs with their offsets restored (apktool keeps the original manifest in `<!-- -->`, and `txt.index` on a body read off the stripped text finds nothing), and `direction_pins` returns three fields for this finding against five for a pin, so the five-way unpack in `fix_pin_local` raised `ValueError: not enough values to unpack` the instant the manifest error appeared; the reporter and the fixer now share one predicate, the duplicate error from two rules naming the same manifest is gone, and `issues` prints after the repairs so a fixed error no longer reaches the output | 411 → 424 | real APK unchanged at 41 warnings / 1 error — `supportsRtl="true"` there; a copy flipped to `false` reports the error and `--fix` clears it (2 errors → 1, the rest its pre-existing format drift), a settled second run writes nothing |
| 25 | 2026-11-07 | symmetry check reported a physical side that the modern pair already covers: `paddingLeft` beside `paddingStart`+`paddingEnd` rendered correctly on every start/end platform and still counted as a hole, so all ten symmetry warnings on the real APK were false positives (`minSdkVersion` 26). Suppression keyed on the kind word, half a modern pair covers nothing. | 424 -> 428 | real APK 41 warn -> 33 warn, 1 error unchanged; `--fix` still plans the same 2 gravity rewrites, settled second run writes nothing |

| 26 | 2026-11-07 | the one *error* on the reference APK was a correct translation: `character_counter_pattern` reads `%1$d/%2$d` in the source and `%2$d/%1$d` in Arabic, the RTL reading order a counter needs, and Android fills each positional specifier by its own index so both render. Format comparison is now set-based over argument numbers (`_sections`), so a reorder is silent while a dropped or invented index still reports; an unnumbered specifier (`_sections` -> None) keeps the old order-sensitive comparison, because Android counts it from 1 anyway and throws. Ten of ten format findings on the real APK were this pair. | 428 -> 431 | real APK 1 error -> 0 errors, 33 warnings unchanged, `lint` exits 0 for the first time on this APK |
| 27 | 2026-11-07 | `aqlab all` printed five stages of chatter and no answer, named the APK it *read* rather than the aligned one `build` wrote (so an unsigned run's summary pointed at a file apksigner never touched), and aborted the whole pipeline on a single lint error — an untranslated APK still builds. Now each stage's stdout is held back and shown only if that stage raises, the summary carries migrations, an aggregated per-rule warning count and `build`'s own output path, and lint errors are counted (exit non-zero) without stopping the rebuild. Missing `-o` is now an error instead of silently writing `<stem>-decoded`'s default. | 431 -> 439 | real APK end-to-end: 29 files migrated, 0 errors / 70 warnings, `out-aligned.apk` verified by apksigner (v2+v3); unsigned run reports `out.apk` |
| 28 | 2026-11-07 | `translate <dir> --tm` — fills `values-<locale>` from a translation memory (exact, then fuzzy over normalised text, then `--translator cmd:`, which is JSON over stdin/stdout and the only way anything external is ever called), plus a `--glossary` that rewrites terms inside a match and `--unmatched-out` for the gaps. A TM candidate only matches when its format signature agrees, and the signature is compared per argument index with the conversion character kept — `%1$d` for `%1$s` is a `String.format` throw, which a plain argument-set comparison called a match. A plural category the source cannot declare has no text of its own (its text is `other`'s), so only an exact `name|category` TM row may fill it, and the hook is never handed an empty item; both were written the wrong way round first and caught by tests, not by reading. Format-only strings (`%1$s`, `999+`, `:`) are excluded from the coverage denominator. `split_argv` keeps Windows backslashes that posix `shlex.split` ate. | 439 -> 452 | real APK: TM built from its own 1,410 strings -> 1,420/1,420 written, 0 format mismatches against source; thinned to 564 rows -> 582 written (568 exact, 14 fuzzy), 36 unmatched correctly left empty (all plural categories the source lacks); rebuilt with a throwaway keystore, apksigner verifies, `aapt2 dump badging` lists `ar` |
| 29 | 2026-11-07 | A3 container handling: `apk_layout()` classifies an input as `apk` / `xapk` / `aab` before apktool sees it (a split APK is also a zip of APKs *and* carries AndroidManifest.xml, so the AAB check runs first). `aab` refuses with the bundletool universal-APK recipe instead of decoding into a project with no `res/`; `xapk` decodes every part, each into its own dir, base ranked first by filename not by path or a raw sort, with `--split <qualifier|filename>` for one part. `aqlab all` refuses an XAPK without `--split` — the old behaviour decoded the container and blew up three stages later on a res-less dir. Also fixed: `all` reported `output : None` for every unsigned run because only `signed ->` was matched. | 452 -> 468 | real XAPK (built from ChessAssistant base + a dupe as split_config.ar): both parts decoded, base -> proj with full res/; AAB -> clean refusal naming bundletool; `all real.xapk` -> refusal; `all --split ar` -> 29 files migrated, 0 errors / 70 warnings, `all.ar-aligned.apk` 84 MB, aapt2 badging lists `ar` |
| 30 | 2026-11-07 | B2 `--report json|html`: every `add()` call now carries a rule id, `build_report()` emits a schema-versioned (`schema: 1`), byte-stable document (tool+version, locale, coverage overall/per file, totals, per-rule counts, every finding), and `write_report()` renders JSON or one self-contained HTML page beside the project unless `-o` names a path. Wiring it exposed two real bugs: `find_default_strings()` fell back to `sorted(glob("values*/strings.xml"))[0]`, so a tree holding only `values-ar/` had lint compare Arabic against itself and print `0 errors, 0 warnings` (now `None` -> loud refusal), and the `format specifiers differ` finding was the one `add()` without a rule id. | 468 -> 488 | real APK (C:/tmp/symdec, 1,426 strings): JSON 668 findings, `unknown` = 0, rules = translation 21w / plurals 8w / rtl-hardcoded 2w / rtl-symmetry 2w / mixed-script 635i, coverage 97.34%; HTML 110 KB, no script/CDN; two runs byte-identical |
| 31 | 2026-11-07 | B1 `scan --jobs N` over the layout sweep (0 = every CPU, `$AQLAB_JOBS` as the default, junk and negatives rejected at parse so a 0-worker pool or an `int()` traceback never happens). The parent reads every file once and serially; only the regex is spread through a `spawn` pool with `imap`, which keeps submission order, so the pool returns the serial list and nothing else changes. `gravity_parallel(src, jobs)` is the one entry point and `gravity_files()` is now the single definition of which files hold a gravity — `hardcoded_gravity` and `lint --fix` both read it, so a fixer can no longer rewrite a file the report never named. `--jobs 1` or fewer than 8 files stays in-process. Honest cost note: on 197 files (0.25 s serial) the pool is slower, 1.3-1.4 s, because Windows pays process spawn; the knob matters on multi-thousand-file trees, not on this APK, and is documented as such rather than sold as a speedup. | 488 -> 500 | real APK (C:/tmp/symdec, 197 layout/value files): `scan`, `scan --jobs 4` and `scan --jobs 0` byte-identical, 2 gravity findings in all three; `--jobs -1` -> clean usage error; mutation (force the serial branch) failed `--jobs 4 actually starts a pool` |

## ROUND 34 — translate report says whether the TM was asked
Measured on CA_v6 (1456 keys, values-ar present): the report showed 97.46%
coverage and `matched={"kept":1419}` — every key already had a target, so the
TM was never consulted, yet the report read as a success. With `--force` the
same TM resolved 5 keys (0.34%). Fixed: `translate` block gains `evaluated`
and `stale_kept`; when the TM resolves nothing a `[NOTE]` names it. Printed
above the dry-run branch, since a dry run returns before the DONE line.
Tests 524->530 (evaluated/stale_kept on kept and forced paths + NOTE text),
both mutation-tested.

## ROUND 35 — the glossary fires on a source term, and lands in the right folder
cmd_translate built the locale path from `res` when `res` was already
`<proj>/res/values`, so every run wrote `res/values/values-ar/` (aapt: no
locale). Fixed with a test that reads the real write_strings_xml argument.
apply_glossary skipped any row whose term was not already in the translated
text — the ordinary case, glossary=0 on a real APK. Rows now match the source
too; the report counts glossary_missed and the console names one.
Tests 530->543, three mutations caught (source branch off, wrong folder, source
dropped). Real APK (symdec, 1456 keys, 1378-row TM, 5-term glossary): 33
rewrites, 105 rows named as unable to fire, build -> aligned APK, aapt2 lists ar.

## ROUND 36 — `all` runs the translation stage
A1 claimed draw→migrate→table→lint→rebuild, and the table was never wired
into `all`: the only way to translate was `apply` (a TSV) or a second
`translate` invocation on a tree `all` deletes. `--tm/--glossary/--translator/
--threshold/--translate-force/--unmatched-out` are now an `all` stage between
`rtl` and `lint`, held back like every other stage, and the summary carries the
coverage (`translate: 46/1456 (3.2%)`). No `--tm` -> the pipeline is unchanged
(asserted). `translate` gets `report=None` on purpose: lint already writes the
pipeline's report and a second writer overwrote it.
Tests 543->548, three mutations caught (stage removed, force dropped, summary
line dropped). Real APK v3: 44-row TM + --force -> 46 rows written,
`resources.arsc` carries 46 `ZZ` strings and 1388 `(ar)` resources after
rebuild+align; without --force the same TM resolved nothing (97.5% "kept") and
the summary said so, which is the honest number.

## ROUND 37 — A1 on the split path, and the temp dir it was leaking

A1 was wired but only ever measured on a single APK. A hand-built XAPK
(real base + real `split_config.ar` APK under `splits/`) now runs `all
--split ar` end to end: decode -> rtl -> lint -> build, 29 files migrated,
the rebuilt `resources.arsc` carries 1388 `(ar)` resources (aapt2 dump).

Two leaks fell out of measuring it. `decode --split` copies the split out of
the container to a real `.part.apk` and never removed it -- the multi-part
path already had the `unlink`, the single-part path did not, so every XAPK
decode left a second full-size copy of the input beside the project. Fixed in
a `try/finally` around the apktool call; two mutations caught (unlink removed,
unlink pointed at a neighbouring name).

The bigger one was in the test suite itself: `_tmp_zip` called `mkdtemp()` per
zip, so every run of `test_aqlab.py` left a dozen `aqlab-layout-*` dirs in
%TEMP% -- 1848 of them, 5.6 GB, by the time anyone looked. One shared scratch
dir, removed by an `ExitStack` callback at the end of `main()`, plus a check
that measures the temp dir before and after (mutation caught: back to per-call
mkdtemp).

The `all` summary also printed `project :` with a path that its own `finally`
had already deleted, and only recognised the temp tree by a regex on its name
(a tree the caller pre-created was reported as gone too). `keep` is now
threaded into `_all_stages` and the line says `none — decoded tree removed`.
Mutations caught: `keep` hard-wired to True, and the old `is_dir()` probe.

Tests 548->552. Real APK: 552 green, `aqlab all` on the XAPK split path
produces a signed 84 MB APK with Arabic resources, nothing left in %TEMP%.

## ROUND 38 — B1: `--jobs N` reaches every layout sweep, and the parity is asserted

A1 shipped with `--jobs` wired to two passes (gravity, and a symmetry step that
did not exist: the flag reached a function that took no `jobs` argument, so
`lint --jobs 4` would have raised). The round put every sweep on one helper.

`_spread_pairs` and `text_sources` are now the only way a sweep meets a pool:
the parent reads each file once and hands the child only text, the child runs
the same body the serial path runs, `imap` keeps submission order, and the floor
stays at eight files (a `spawn` worker is ~50ms on Windows; below eight the pool
costs more than the pass). Five sweeps use it: symmetry, gravity, direction pins,
RTL literals, text-like vectors. `direction_pins` moved its four regexes to
module level — they were compiled per call inside the function, so the pool would
have shipped them to every worker.

`rtl_enabled` could not be spread as it stood: `declared_direction_values` (a
`values/attrs.xml` read) and the manifest flip are project-wide gates, and
without them a declared enum came back as a finding — caught by the existing
`a declared enum value is never a finding` check before any new test ran. The
file walk moved into `rtl_enabled_in(txt, rel, declared, supports_rtl)` and both
paths pass the same gates.

`rtl_drawable_dirs` takes `jobs` and ignores it, with a comment saying so: one
`read_bytes` per file against a name it already has is not worth a worker.
`gcd_gravity` dead code from the old inline path is gone, and `cmd_lint` now
defines `jobs` once where it used `args.jobs` per call site.

Tests 552->573 (`test_jobs_parity`: five sweeps x three worker counts against
the serial list, plus a pool-was-used assertion; 4 spawns for 5 sweeps is the
eight-file floor, and the test says so). Three mutations caught: helper ignores
`jobs`, pool reorders the input, serial symmetry list truncated.

Real APK (`C:/tmp/symdec`): `lint --jobs 1|4|8` byte-identical output,
`scan --jobs 1|4` byte-identical, same 49 symmetry / 2 gravity / 0 pin / 0 RTL
rows. Timing on this 84 MB project is flat (1.06s serial vs 10.19s at `--jobs 8`)
— spawn cost dominates 200 small files, which is why the floor and the docs say
`--jobs` is for big trees, not a default.

## Round 39 — report: schema 1 pinned to a fixture and to the builder

`--report json` shipped in B2 without anything stopping the shape from drifting:
the schema number was written at the top of `build_report` and read by nobody.
It is now `REPORT_KEYS` (a frozen set beside the number), asserted against the
dict `build_report` actually returns, refused by `write_report` when a caller
hands it a report of another schema (a consumer keys off the number, and a
schema-2 file written by a schema-1 build is worse than a failed run). The
fixture `tests/fixtures/report-v1.json` is small enough to vendor: a real lint
report is 715 findings.

Tests 573->579. `test_report_schema_fixture` round-trips the fixture through
`build_report` and compares the whole dict, not just the key set — otherwise a
value change (a percent rounded differently) passes both the fixture and the
real-run test while breaking every consumer. Three mutations caught: the
builder's key assertion off, an extra key on the returned dict, the stale-schema
refusal off. Real APK (`C:/tmp/symdec`): schema 1, keys exactly `REPORT_KEYS`,
0 errors / 80 warnings / 635 infos, 715 findings over 5 rules.

## ROUND 40 — E: the untranslated pair fired on values that are only placeholders

`lint` reported `plan_option_format` (`%1$s — %2$s`), `teach_x6_unit_rank` and
`mtrl_picker_day_of_week_column_header` (both `%1$s`) twice each: once as
"identical to source", once as "no Arabic characters". A value that is nothing
but format specifiers is identical in every locale by definition — 4 warnings on
the reference APK were false positives, and a false positive teaches the user to
ignore the rule. Fix in the rule, not the doc: both conditions now match against
`RE_FORMAT.sub("", text)`. Real APK 86 -> 82 warnings, 0 errors; the English
brand names (`Premium`, `Lichess`, `Chess.com`) still fire.
Tests 592->595, mutation-tested both branches (revert the strip -> `pure
placeholder is not called untranslated` fails).

## Round 41 — phase A scout
Five candidates recorded, none re-recorded from round 40. Two rejected outright
(Stringlate is a 2019 GUI with no CLI or report schema; Android 16 adds no
resource-level RTL surface). Three stay `pending`: pyicu (authoritative plural
rules vs our hand-rolled CLDR table), deep_translator (could seed values-ar
instead of leaving keys empty), lingua-py (offline detector that would let lint
flag Latin text inside values-ar without a whitelist). Verified first that the
obvious gap was already closed: `translatable="false"` and `formatted="false"`
are handled, and the reference APK has zero of either, so they cannot be
measured there. Tests unchanged 595, 0 errors / 82 warnings, gate untouched.

## Round 42 — phase B eval (pyicu) -> tables corrected
pyicu would not build on this host (no ICU headers), so Babel 2.18 (same CLDR
data, pure Python) was used instead. Result: our `he` plural table was wrong —
CLDR Hebrew is one/two/other, we required `many` and would have asked every
Hebrew translator for a form the language never selects. `fa`/`ur` correct.
`ar` correct. Table grew from 4 locales to 52, every row read from Babel's
samples rather than typed from the chart; `plural_required` is now derived
(`other` plus the two multi-count categories) instead of hand-listed, so a new
row cannot disagree with its own category set.

Two lint bugs found while wiring it:
- the "identical text" plural warning compared only few/many/other, assuming
  every locale has them; it now compares the locale's own categories and skips
  `other` (which legitimately repeats).
- a category the locale does not use (`few` in Hebrew) is now reported, since
  those counts silently fall through to `other`.
- a `--fix` run printed a pin warning still advertising "`aqlab lint --fix`
  drops the pin" after the run had dropped it; the tip is scrubbed when the
  tree has no `layoutDirection="ltr"` left.

Note: an editing mishap dropped the `fix_gravity` call from `--fix` and the
`force_fix_gravity` test caught it immediately — the gravity sweep is back.
Tests 595 -> 603, real APK unchanged at 0 errors / 82 warnings.

## Round 38 — `--fix` measured on two real APKs; two harmful fixes removed

The owner asked for three things: run `--fix` twice on the same tree, use a real
APK rather than fixtures, and prove the fixes do not change app behaviour or
break resources. All three ran. The audit found two fixes that were doing
damage, both invisible to fixtures.

**1. `fix_symmetry` invented attributes.** On the Material layouts of a decoded
APK it added 27 attributes across 16 files and every one changed the rendered
shape: a `statusDot` carrying only `marginEnd=8` gained `marginStart=8`, a
Spinner carrying only `marginStart=8` gained `marginEnd=8`, `progressUsage`
gained a side it never had. The missing partner was not a hole — it was the
design, an element parked against one edge on purpose. Android's own
`RtlSymmetry` is a warning for exactly this reason and never auto-fixes it.
`fix_symmetry` is no longer called from `cmd_lint`; the check stays a warning.
The helper itself is kept (tested) for a future `--fix-symmetry` opt-in.

**2. `fix_pin_local` stripped deliberate LTR pins.** On 8BP it removed
`layoutDirection="ltr"` from the ExoPlayer control views. Those views pin their
container so play/pause/skip keep playback order under Arabic — the icons travel
with the layout and the buttons would render backwards. A pin on an element
with no physical attributes is a marker, not a redundant literal, so
`rtl_enabled` findings are no longer queued for removal. Pins that *do* carry
physical attributes are still dropped, and their attrs still migrate.

Measured after the change:

| check | Chess (534 xml, 199 layout) | 8BP (509 layout) |
|---|---|---|
| second `--fix` pass changes | 0 files | — |
| attributes invented | 0 | 0 |
| deliberate pins stripped | 0 | 0 |
| XML parses | 532/532 | 509/509 |
| rebuilt APK | 84 MB, zip clean, 199 layout, 1388 ar strings | — |

Also fixed: the test suite carried two contradictory Hebrew plural assertions
(`he four` with `many` and `he three` without). Verified against CLDR 48
`release-48/common/supplemental/plurals.xml`: Hebrew is `one, two, other`,
`many` was removed in CLDR 42. The code was right; the `he four` assertion was
wrong and is gone.

Tests 603 -> 602 (the four assertions that described the harmful behaviour were
replaced by ones asserting it does not happen). 0 errors, 82 warnings on the
real APK.

## Round 43 — plural and RTL tables from CLDR, not from hand

Two generated tables replace two hand-typed ones.

`PLURAL_CATEGORIES` was 52 locales out of CLDR's 227. A locale with no row was
not linted for plurals at all, so a `de` build carrying an Arabic-shaped
`<plurals>` (zero/two/few/many) produced zero warnings — verified on a fixture
with exactly that file, silent before, one warning after. The full 227-locale
table is now generated from release-48 `plurals.xml`, grouped so each block is
one category tuple.

`_plural_locale` replaces `_base` for plural lookups. `_base` stripped the
region *before* the lookup, so `pt_PT` — Android's own qualifier spelling, and
the one row in CLDR that differs from its language (`pt` has `many`, `pt_PT`
does not) — got the wrong table. It also lowercased, which turned
`b+sr+Latn+RS` into noise. Now: exact tag, then language_SCRIPT, then language,
and the Android `b+` form is unpacked first.

`RTL_LOCALES` was ten languages; CLDR says 258 have an RTL script. The misses
were not exotic in the countries that ship these apps (`sdh`, `lrc`, `glk`,
`mzn`, `azb`, `bal`, `ks`) plus `nqo`, a right-to-left script of its own.
Android gets this right for free (`TextUtils.getLayoutDirectionFromLocale` asks
ICU), so our hardcoded list was pure false-negative surface.

Both tables are generated by `scratch/gen_*.py` — kept out of the shipped tree
because they need network and CLDR input; the generator's output is asserted
equal to `plurals.xml`/`likelySubtags.xml` at generation time.

Tests 603 -> 610, all six new ones mutation-tested: reverting `_plural_locale`
to `_base` fails `b+sr+Latn qualifier resolves`, shrinking `RTL_LOCALES` to the
old ten fails `rtl covers N'Ko…`. Real APK unchanged: 0 errors, 82 warnings.
## Round 44 — BUILD: plural table is CLDR's file, not a hand-grouped literal

Round 43 shipped `PLURAL_CATEGORIES` as a literal generated off-network and the
generator was never committed, so the shipped table was unverifiable — and
wrong in three rows when compared against CLDR's own `plurals.json`: `he` had no
`many`, `vi` was given a `one` CLDR does not produce, `tg` was absent. Hand
grouping is what produced all three (the same failure the `he` row had last
round), so the fix is not another hand pass.

The table is now keyed by category tuple (`PLURAL_CATEGORIES_BY_KIND`) with one
row per distinct shape — 10 rows covering 224 CLDR locales — plus the five
Android legacy qualifiers (`in`/`iw`/`ji`/`jw`/`tl`) mapped onto their modern
row. `tools/cldr_tables.py` regenerates it from
`tests/fixtures/cldr/plurals.json`, a 7 KB checked-in copy of CLDR core's
`supplemental/plurals.json` cardinal rules (the evaluated form `.xml` rules
expand to, so no rule parser and no network). `test_plural_table_matches_cldr`
rebuilds the table from that fixture every run: mutation — deleting `'es'` from
the `one/many/other` group fails with `differs on ['es']`.

Two lint findings fixed in the same pass, both measured on a fixture:

* A locale CLDR has no row for (`xx`, a typo) got "one, other is not a xx
  plural category" on an ordinary file — a claim nothing supports. Worse, every
  plural check is vacuous there, so the run read as clean. Now one `[NOTE] no
  CLDR plural row for xx` and no invented verdict; a real locale (`he` with a
  `zero`) still gets its category warning.
* `values-{locale}*` was `glob`, case-sensitive on Linux/macOS, so
  `--locale ar-rSA` reported a missing folder that was on disk. Now a
  case-insensitive `iterdir` prefix match.

Tests 610 -> 623, five new checks all mutation-tested (unguarded `unknown`
list, dropped NOTE, glob restored). Real APK unchanged: 0 errors, 82 warnings.
