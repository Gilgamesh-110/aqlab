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
