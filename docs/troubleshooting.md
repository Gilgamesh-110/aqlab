# Troubleshooting

Each entry is a real failure seen in the field, with the actual cause.

---

## `error: duplicate attribute` from aapt2

```
W: res/layout/main.xml:4: error: duplicate attribute.
W: res/layout/main.xml: error: file failed to compile.
```

**Cause.** The layout (or a `<style>`) declared **both** the legacy attribute and
its logical twin — `android:paddingLeft` *and* `android:paddingStart`. Renaming
`paddingLeft` without checking produced two `paddingStart` attributes, and aapt2
aborts the entire build.

**Fix.** Update `aqlab`. From 0.1.0 the patcher works per tag: when a `start`/`end`
twin is already declared, the renamed-from attribute is **deleted** instead of
duplicated. The current version against the same input reports:

```
[PATCH] res/layout/main.xml: 6 attribute(s)
```

If it still fires, the twin is on a *different* tag or in a different file. Open
an issue with the file name from the error and the two lines around it.

---

## It refuses to run: `minSdkVersion=15`

```
aqlab: minSdkVersion=15: the start/end attributes need API 17. Raise minSdk to 17+,
or pass --force-min-sdk to migrate anyway (the build will fail unless minSdk is raised too).
```

**Cause.** `paddingStart`, `layout_marginEnd` and friends were added in **API 17**.
Below that, `aapt2` cannot resolve them.

**Fix.** Raise `minSdkVersion` to 17 in `AndroidManifest.xml` (or `build.gradle` in
a real project, then rebuild the APK). API 17 is Android 4.2 — from 2012. If you
genuinely must support lower, do not use this tool for the layout pass.

`--force-min-sdk` exists only so you can inspect the migration output; the resulting
project will not compile.

---

## `aqlab: cannot find apktool` / `cannot find aapt2`

**Cause.** The tool shells out to `apktool` and to the Android SDK build-tools.
It looks for them on `PATH` and in `ANDROID_HOME` / `ANDROID_SDK_ROOT`.

**Fix.** Point it explicitly:

```sh
export AQLAB_APKTOOL=/path/to/apktool.jar
export AQLAB_SDK=/path/to/android-sdk          # or ANDROID_HOME
# or per tool:
export AQLAB_AAPT2=/path/to/aapt2
export AQLAB_ZIPALIGN=/path/to/zipalign
export AQLAB_APKSIGNER=/path/to/apksigner
```

On Windows, pass **native** paths (`C:/tools/apktool.jar`). An MSYS-style
`/c/tools/...` path will not resolve for `java`.

---

## `already RTL-clean — nothing to migrate` for a project that is clearly not

**Cause.** The path you passed is wrong. Older versions reported this for a
directory that does not exist, which is the worst possible answer.

**Fix.** Update — current versions fail loudly:

```
aqlab: work: not a directory — pass the project dir from `aqlab decode`
aqlab: work: no res/ directory — is this a decoded project?
```

Always pass the directory `aqlab decode` printed, not the `.apk` and not the
parent of the output.

---

## The rebuilt app crashes immediately, or refuses to run

**Cause.** The original app guards its own integrity. A rebuild changes the APK
signature and the resource table, and — depending on the app — dex checksums and
native `mmap` checks of its own sections. The guard is doing exactly what it was
built to do.

**Fix.** There is no fix inside this tool, and there should not be one. `aqlab`
handles layout direction; defeating a self-check is a different job.

Detection surfaces this in `scan`:

```
native libs    : 22 (renaming packages in native apps can break self-checks)
```

A high native-lib count is the tell. Also note that re-signing changes the
certificate SHA-256 — if anything compares it, it will trip.

---

## `scaffold` refuses to run

```
aqlab: .../res/values-ar/strings.xml already exists — refusing to overwrite existing
translations. Re-run with --force, or use `apply` to update it.
```

That is deliberate. `scaffold` writes only the subset you already translated; if
the file exists you almost certainly want `apply`, which merges a TSV into it.

---

## The translations applied but the app still shows English

Check, in order:

1. **Is there a `res/values-ar/` directory in the rebuilt APK?**
   `aapt2 dump badging out.apk | head -1` then `aapt2 dump badging out.apk | grep locales`
2. **Does the device locale actually resolve to `ar`?** A device set to
   `ar-EG` matches `values-ar`. A device set to English does not.
3. **Did `lint` report 100% coverage?** Anything not translated falls back to the
   default locale — that is Android behaviour, and it is what you want.
4. **Is the string built at runtime?** `lint` cannot see those.

---

## `build` produced a file but it will not install

`zipalign` ran but `apksigner` did not, so you have an unsigned APK:

```
[DONE] built -> work-aligned.apk  (unsigned; pass --ks to sign)
```

Pass a keystore, and prefer the environment variable over the command line so the
password stays out of your shell history:

```sh
export AQLAB_KS_PASS='…'
aqlab build work --ks release.keystore --ks-alias release
```

Confirm before shipping:

```sh
apksigner verify --print-certs work-aligned.apk
zipalign -c -v 4 work-aligned.apk
```
