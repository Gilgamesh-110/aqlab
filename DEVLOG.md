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
