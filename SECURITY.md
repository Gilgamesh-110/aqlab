# Security policy

## Reporting a vulnerability

Open a [private security advisory](https://github.com/Gilgamesh-110/aqlab/security/advisories/new)
rather than a public issue. Include:

* the version (`aqlab --version`),
* the command you ran,
* what an attacker gains that they should not.

There is no bounty. Credit is given in the advisory unless you ask otherwise.

## What this tool touches

`aqlab` is a local command-line tool. It has no network code, no telemetry, no
update check and no daemon. It shells out to `apktool`, `aapt2`, `zipalign` and
`apksigner`, and it edits files inside the project directory you point it at.

That produces a few real risks worth knowing:

### Untrusted APK input

The APK you decode is attacker-controlled data if you did not build it yourself.
`apktool` and `aapt2` parse that input in a separate process, so a crafted APK
can at worst crash them — it does not run inside `aqlab`. Still, decode
untrusted APKs in a throwaway directory, and never from a path you then execute
scripts out of.

### Signing keys

`--ks-pass` on the command line ends up in your shell history and in the process
list. Prefer the environment variable, which the tool reads by default:

```sh
export AQLAB_KS_PASS='…'
aqlab build work --ks release.keystore --ks-alias release
```

Use a dedicated signing key for experiments. A debug key that leaks is a broken
artifact; a production key that leaks is a broken product.

### Rebuilt artifacts

A rebuilt APK is a different artifact from the one you started with: different
signature, different resource table. If the original app verifies its own
signature or checksums natively, the rebuild will trip that check at runtime.
That is expected behaviour, not a vulnerability in this tool.

### Scope

In scope: anything where `aqlab` itself reads or writes outside the project
directory, escalates privileges, executes input as code, or leaks a credential.

Out of scope: the behaviour of a rebuilt third-party app, and the consequences
of redistributing one. See the disclaimer in the README — redistribution is your
call, not the tool's.
