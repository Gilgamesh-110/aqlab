# Support

## Where to ask

| I want to… | Go to |
|---|---|
| Ask how to use `aqlab` on a specific app | [Discussions](https://github.com/Gilgamesh-110/aqlab/discussions) |
| Report a bug (a crash, a wrong patch, a failed build) | [Open an issue](https://github.com/Gilgamesh-110/aqlab/issues/new/choose) |
| Request a feature | [Open an issue](https://github.com/Gilgamesh-110/aqlab/issues/new/choose) |
| Report a security vulnerability | [Security advisory](https://github.com/Gilgamesh-110/aqlab/security/advisories/new) — **not** a public issue |
| Contribute code | [CONTRIBUTING.md](CONTRIBUTING.md) |

## Before you file a bug

Most reports resolve in one of three ways. Try these first:

**1. Run the self-check.**

```sh
python aqlab.py --version
python tests/test_aqlab.py     # no dependencies, no network
```

If the test suite fails, say so in the issue — that is a different bug, and a
more important one.

**2. Paste the full output, not the summary.**

"I got an error" cannot be acted on. The `aapt2` line can:

```
res/values-ar/strings.xml:565: error: unescaped apostrophe in string
```

That single line identifies the file, the line and the cause.

**3. Check the docs.**

- [`docs/troubleshooting.md`](docs/troubleshooting.md) — build failures, signing, apktool version mismatches
- [`docs/bidi.md`](docs/bidi.md) — the Unicode bidi hazards the linter looks for
- [README FAQ](README.md#faq) — why a translation can be 100% complete and still wrong

## What a useful report contains

```
aqlab version:
Python version:
OS:
apktool version:
command run (full):
full output:
APK or project (link, or a minimal reproduction):
```

If you cannot share the APK, `examples/demo-project` in this repo reproduces
several classes of failure without any real app — start from there and change
one thing at a time.

## What support this project does not offer

`aqlab` is a volunteer project. There is no SLA, no paid support tier and no
guarantee of a response time. Issues get answered when a maintainer has time,
and honest "I don't know yet" is preferred to a guess.

## Response expectations

| Kind | Typical |
|---|---|
| Security report | Acknowledged within a few days |
| Bug with a reproduction | Triaged when seen; fix depends on cause |
| Bug without a reproduction | Likely closed — please add one |
| Feature request | Discussed; scope decided publicly in the issue |
