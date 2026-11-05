# Bidi: what this tool measured, and what it no longer claims

This document exists because `aqlab` shipped a bidi rule that was wrong for nine
releases. It is written down so the same rule does not come back.

## The rule that was wrong

For a long time `lint` reported:

```
[ERROR] folder: trailing Latin/digit run before punctuation — needs U+200F (RLM)
```

It fired when an Arabic string ended in a Latin or digit run followed by
punctuation, on the theory that the punctuation would be dragged to the wrong
side. In `0.1.7` that was replaced by a second rule reporting any LTR run
"without a bidi boundary":

```
[WARN] ver: mixed run needs a bidi boundary: `v0.1.6.`
```

Both were built the same way: by reasoning about character classes. Neither was
ever checked against a renderer.

## What the measurement found

A test app was built and run on Android 11 (`TextView`, `TEXT_DIRECTION_RTL`).
For each string it read, out of the laid-out `android.text.Layout`:

- the resolved direction of every character's own bidi run (`isRtlCharAt`)
- the horizontal span it occupies (`getPrimaryHorizontal`, `getSecondaryHorizontal`)

Two hundred mixed Arabic/Latin strings were taken from a shipped APK and run
through it.

| | claimed by the rule | measured on Android |
|---|---|---|
| mixed strings with a defect | 58 (0.1.0–0.1.6), 534 (0.1.7) | **0** |

Specifically:

```
المجلد test.            period at x=70; `test` spans 84..142 — left of it. Correct.
نسبة 50% ...            `%` at x=281; `50` spans 281..329 — left of it. Correct.
الإصدار v0.1.6.         final period left of the Latin run. Correct.
★ الترقية إلى Premium   renders correctly; no mark changes anything.
```

Adding `U+200F` where the rule said it was missing produced no change in layout
in any of them.

## Why the reasoning failed

A neutral character between two strong characters takes their direction (UAX #9
rule N1). Between a Latin run and Arabic text the character classes look
ambiguous — but the algorithm is not guessing, and neither is the reader:

- A final period after a closing Latin word resolves with the Latin run, so it
  sits at that run's *left* edge, which in an RTL line is the end of the
  sentence. That is where a reader expects it.
- A period *inside* a Latin run (`v0.1.6.`) resolves the same way.

The cases a regex cannot see are the ones that need care — but "cannot see" is
not "is broken". `%1$d` is a format specifier, not text: the formatter replaces
it before a font ever sees it.

## What replaced it

Nothing. The check was removed.

`lint` reports the failures it can demonstrate:

| finding | why it matters |
|---|---|
| `format specifiers differ` | a translator turned `%1$s` into `%s`; the app crashes or prints the wrong argument |
| `bare %` | aapt rejects the resource outright; the build fails at compile time |
| `unescaped apostrophe` | same — aapt failure, and it is invisible in a diff |
| `value starts with @ or ?` | Android reads it as a resource or theme reference, not text |
| `markup tags differ` | `<b>`, `<i>`, `<xliff:g>` lost in translation |
| `space mismatch` | two strings concatenated at runtime run together |
| `identical to source, contains Latin` | probable untranslated string shipped by accident |
| Arabic `<plurals>` missing `few`/`many`/`other` | the two categories an English source cannot supply, and the ones that cover almost every count |

Mixed Arabic + Latin is reported as `info`, shown only with `--verbose`.

## If you find real broken text

That is worth a bug report: the exact string, the device and API level, the
widget, and a screenshot or the measured spans. It is not something this linter
will guess at.

## The marks, for when you need them by hand

| mark | code point | what it does |
|---|---|---|
| RLM | `U+200F` | forces the next run RTL |
| LRM | `U+200E` | forces the next run LTR |
| ALM | `U+061C` | RTL, and does not break Arabic shaping |
| LRI / RLI / FSI / PDI | `U+2066`–`U+2069` | isolates a span so it cannot leak direction outward |

Prefer an isolate when embedding something whole — a filename, a version, a URL —
because it has two edges and one mark cannot leak. A mark fixes one boundary; an
isolate fixes a span.

```
‏لم يتم العثور على الملف ⁨report_v2.pdf⁩
```

1. **Never hand-place marks without checking on a device.** Emulators and IDE
   previews disagree with each other about edge cases.
2. **Isolate embedded LTR spans** (filenames, paths, versions, URLs).
3. **Leave format specifiers alone.** Do not insert marks inside or immediately
   before `%1$s` — you will change what the formatter sees.

## Testing bidi by hand

Print the codepoints, which tells you exactly which marks are present:

```sh
python -c "print(' '.join(hex(ord(c)) for c in 'المجلد test.'))"
```

Then render it in a real `TextView` — not a preview pane — and look.
