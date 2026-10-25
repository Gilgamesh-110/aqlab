# Bidi: why Arabic strings break in ways translation tools never see

This is the long version of the `lint` section in the README. If you only read
one part, read [The four rules of thumb](#the-four-rules-of-thumb).

## The setup

Arabic is a **strong right-to-left** script. Latin letters and digits are
**strong left-to-right**. Punctuation, spaces, `%`, `(`, `)`, `.`, `,` are
**neutral** — they take their direction from whatever is around them.

The Unicode Bidirectional Algorithm (UAX #9) resolves neutral characters by
looking at the nearest strong character on each side. When those two sides
disagree, the neutral character has to pick a side, and the rule it follows is
not the one a human reading the line would expect.

That is the whole source of the problem. Nothing is "wrong" with the text; the
rendering is correct according to the algorithm and wrong according to the reader.

## A minimal failing example

```
<string name="folder">المجلد test.</string>
```

Read by a human: *"the folder test."* — the period closes the sentence.
Rendered by Android:

```
المجلد test.
```

The period attaches to the **left-to-right** run, so it renders at the *right*
end of `test` — which in the RTL line order appears on the wrong side of the
sentence. The visual result is that the full stop has jumped.

The fix is a directional mark immediately before the neutral run:

```xml
<string name="folder">المجلد test\u200f.</string>
```

`U+200F` is RIGHT-TO-LEFT MARK — an invisible strong RTL character. It forces the
following neutrals (the period) to resolve right-to-left, i.e. to the position a
human expects.

## The marks you will actually use

| mark | code point | name | what it does |
|---|---|---|---|
| RLM | `U+200F` | RIGHT-TO-LEFT MARK | forces the next run RTL |
| LRM | `U+200E` | LEFT-TO-RIGHT MARK | forces the next run LTR |
| ALM | `U+061C` | ARABIC LETTER MARK | RTL, and does not break Arabic shaping |
| LRI / RLI / FSI / PDI | `U+2066`–`U+2069` | isolates | wraps a run so it cannot leak direction outward |

Prefer the **isolates** (`U+2066` … `U+2069`) over the marks when you are
embedding something whole — a filename, a version string, a URL. A mark fixes one
character boundary; an isolate fixes a span and cannot leak.

```
‏لم يتم العثور على الملف ⁨report_v2.pdf⁩
```

Rules of thumb, in order:

1. **Never hand-place marks without checking on a device.** Emulators and IDE
   previews vary.
2. **Isolate embedded LTR spans** (filenames, paths, versions, URLs) with
   `RLI … PDI`.
3. **Fix trailing punctuation** with `RLM` before the punctuation.
4. **Leave format specifiers alone.** Do not insert marks inside or immediately
   before `%1$s` — you will change what the formatter sees.

## What `aqlab lint` checks, and why

| finding | why it matters |
|---|---|
`trailing Latin/digit run before punctuation` | the failing example above: the marker is missing and the neutral run will flip |
`format specifiers differ` | a translator turned `%1$s` into `%s`; the app crashes or prints the wrong argument |
`bare %` | aapt rejects the resource outright; the build fails at compile time, usually with an unhelpful message |
`unescaped apostrophe` | same — aapt failure, and it is invisible in a diff |
`value starts with @ or ?` | Android reads it as a resource or theme reference, not text |
`markup tags differ` | `<b>`, `<i>`, `<xliff:g>` lost in translation, losing the styling or the placeholder |
`space mismatch` | two strings concatenated at runtime run together |
`identical to source, contains Latin` | probable untranslated string shipped by accident |

Findings that are **not** errors:

- **a plural form dropping the count.** Arabic genuinely omits the number in the
  `one` and `two` forms (`ملف واحد`, not `١ ملف واحد`). Reported as a warning.
- **mixed Arabic and Latin in one string.** Very often correct — `الإصدار 2.1`
  is fine. Reported as `info`, shown only with `--verbose`.

## What the linter cannot do

- It **does not render**. It flags the pattern that usually breaks; only a device
  confirms it.
- It cannot see strings built at runtime in Java/Kotlin or in a JS bundle.
- It does not know your string is a filename, a URL or a sentence. It reasons
  about characters, not meaning — which is why the findings are phrased as
  questions ("needs RLM?") rather than verdicts.

## Testing bidi by hand

Paste a candidate string into a file, then either:

- build and look at it on a device, or
- render the codepoints: `python -c "print(' '.join(hex(ord(c)) for c in 'المجلد test.'))"`

The second one tells you exactly which marks are present, which is usually what
you are arguing about.
