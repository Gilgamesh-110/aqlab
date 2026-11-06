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
