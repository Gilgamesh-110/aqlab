# Contributing

Thanks for helping. Localization is a domain where almost every app gets
something wrong, so real-world reports are as valuable as code.

## Setup

```sh
git clone https://github.com/Gilgamesh-110/aqlab
cd aqlab
python tests/test_aqlab.py     # no dependencies, no network, no apktool needed
```

The test suite is plain `assert`s in `tests/test_aqlab.py`. It runs anywhere
Python 3.9+ runs. If it passes locally, it passes in CI.

## Ground rules

1. **Every behaviour change comes with a check in the test file.** A migration
   rule without a test is a bug waiting to be reported. Add the failing case
   first, then the fix.
2. **Never break a build.** A tool that produces an unbuildable project is worse
   than no tool. If you touch attribute migration, prove it on a real APK before
   opening the PR — `decode` → `rtl` → `build` — and say in the PR what app you
   tested and what the build printed.
3. **Stay dependency-free.** `aqlab` is one file using the standard library.
   Adding a pip dependency needs a very good reason; a regex usually wins.
4. **Edit resource XML in place.** Do not route `strings.xml` / layouts through a
   generic XML writer — it mangles Android escaping, CDATA and `%1$s`.
5. **Lint findings must be defensible.** A rule that fires on correct
   translations gets the whole linter ignored. Mark uncertain findings as
   `warn` or `info`, never `error`. `error` means "this breaks the build or the
   text is wrong".

## What is most wanted

* **More vector-drawable mirroring cases** — assets with baked-in text are found,
  but the shapes that need flipping rather than translating are still a judgement
  call. Real examples welcome.
* **Locales nobody has run yet.** The CLDR tables cover 227 languages, 258 of them
  right-to-left; the plural rules are generated, but the *interface* has only been
  eyeballed in a handful. A screenshot of your language in a real build is worth
  more than a test.
* **RTL token tables for scripts beyond Arabic.** Arabic, Hebrew, Persian and Urdu
  are covered; the mechanics are shared but the counter-direction tokens are not.

## Reporting a bug

Use the issue template and paste the **full** command output. "It broke" is not
actionable; the `aapt2` error line is.

## Translations of the docs

`README.ar.md` is Arabic, `README.md` is English. Keep both in step when you
change user-facing text. Rendered Arabic on GitHub can hit the same bidi
hazards the linter catches — keep mixed Arabic/Latin lines clean.

## Code of conduct

Be direct, be kind, assume good faith. Harassment or personal attacks are not
tolerated in issues, PRs or discussions. The full text is in
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).

## Optional: install the commit hooks

```sh
pip install pre-commit && pre-commit install
```

The hooks run the test suite and a tab-in-XML guard before each commit. They are
optional — CI runs the same checks — but they catch the two mistakes that make a
one-line Arabic fix look like a whole-file diff.
