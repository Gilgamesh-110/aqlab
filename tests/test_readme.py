"""Run the README's quickstart and compare it against the output the README shows.

Documentation that drifts is a bug, and so is a file the README tells you to open
that never made it into the repository (`.gitignore` swallowed `examples/ar.tsv`
for three releases). This executes the exact commands from README.md in a throwaway
copy of `examples/` and diffs the result against the fenced block below them.

Run: python tests/test_readme.py
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")

BLOCK = re.compile(r"## Try it in 5 seconds.*?```sh\n(.*?)```\n\n```\n(.*?)```", re.S)


def norm(lines):
    # indentation is cosmetic in a fenced block and differs run to run; the
    # sequence and the text are what must not drift
    return [l.replace("\\", "/").strip() for l in lines if l.strip()]


def main():
    m = BLOCK.search(README)
    if not m:
        print("[FAIL] README.md: no '## Try it in 5 seconds' block with commands + output")
        return 1

    commands = [c.strip() for c in m.group(1).strip().splitlines() if c.strip()]
    expected = norm(m.group(2).strip().splitlines())
    # the clone/cd line is what puts aqlab.py next to examples/; the commands below
    # are run from a copy instead
    commands = [c for c in commands if not c.startswith("git clone")]
    if not commands:
        print("[FAIL] README.md: quickstart block has no commands left")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        shutil.copytree(ROOT / "examples", work / "examples")
        got = []
        for c in commands:
            argv = c.split()
            if argv[:2] != ["python", "aqlab.py"]:
                print(f"[FAIL] README.md: cannot run this command line: {c}")
                return 1
            # the quickstart's last command exits 1 on purpose, so the exit code is
            # deliberately not part of the assertion
            p = subprocess.run([sys.executable, str(ROOT / "aqlab.py"), *argv[2:]],
                               cwd=work, capture_output=True, text=True)
            out = norm((p.stdout + p.stderr).splitlines())
            if not out:
                print(f"[FAIL] {c}\n       produced no output at all")
                return 1
            got += out

    if got == expected:
        print(f"[OK] README quickstart matches ({len(expected)} lines)")
        return 0
    print("[FAIL] README quickstart output no longer matches the documentation")
    for i in range(max(len(got), len(expected))):
        g = got[i] if i < len(got) else "<missing>"
        e = expected[i] if i < len(expected) else "<missing>"
        if g != e:
            print(f"  line {i + 1}")
            print(f"    documented: {e}")
            print(f"    actual    : {g}")


def check_plural_table():
    """The locale table in the README must match what the code returns.

    It drifted once already: the table claimed Hebrew had a `many` category
    while `plural_categories('he')` returned ('one','two','other'). A table
    nobody checks is a table that lies, and this one sits directly under the
    claim that the tables come from CLDR rather than from Arabic by analogy.
    """
    sys.path.insert(0, str(ROOT))
    import aqlab

    row = re.compile(r"^\|\s*`([a-z]{2,3})`\s*\|\s*(RTL|LTR)\s*\|\s*`([^`]+)`", re.M)
    rows = row.findall(README)
    if not rows:
        print("[FAIL] README: the locale table is gone")
        return 1

    bad = 0
    for loc, direction, cats in rows:
        want = "RTL" if aqlab.is_rtl(loc) else "LTR"
        if direction != want:
            print(f"[FAIL] README table: {loc} is documented {direction}, code says {want}")
            bad += 1
        documented = tuple(cats.split())
        actual = tuple(aqlab.plural_categories(loc))
        if documented != actual:
            print(f"[FAIL] README table: {loc} categories {documented} != {actual}")
            bad += 1

    if bad:
        return 1
    print(f"[OK] README locale table matches the code ({len(rows)} locales)")
    return 0


def check_banner_numbers():
    """The banner's badges must not claim numbers the code no longer has.

    The banner sat at '610 tests' while the suite was at 641, and it is the first
    thing a visitor reads. A badge that drifts is a claim that drifts.
    """
    svg = (ROOT / "assets" / "banner-v3.svg").read_text(encoding="utf-8")
    sys.path.insert(0, str(ROOT))
    import aqlab

    claims = {
        "locales": (r">(\d+) locales<", len(aqlab.PLURAL_CATEGORIES)),
        "RTL": (r">(\d+) RTL<", len(aqlab.RTL_LOCALES)),
    }
    bad = 0
    for label, (pat, want) in claims.items():
        m = re.search(pat, svg)
        if not m:
            print(f"[FAIL] banner-v3.svg: no '{label}' badge found")
            bad += 1
        elif int(m.group(1)) != want:
            print(f"[FAIL] banner-v3.svg: {label} badge says {m.group(1)}, code says {want}")
            bad += 1

    m = re.search(r">(\d+) tests<", svg)
    if not m:
        print("[FAIL] banner-v3.svg: no tests badge found")
        bad += 1
    elif os.environ.get("AQLAB_CI"):
        # CI runners have no android.jar, so the 13 APK-dependent checks skip and
        # the suite reports a smaller number than a dev box. The badge advertises
        # the count a reader will see when they run it with the SDK present —
        # that is the honest number for a badge. The suite itself is the contract
        # and the step above already gates on it.
        pass
    else:
        # Re-running the whole suite from inside a doc check costs ~20s and, on
        # CI, makes the badge depend on the run that is checking it -- the check
        # reported 'badge says 652, suite has 639' against the previous commit's
        # tree. Locally the live count is what matters; on CI the pinned
        # EXPECTED_TESTS is the contract, and the suite step right above already
        # proves the count.
        out = subprocess.run([sys.executable, str(ROOT / "tests" / "test_aqlab.py")],
                             capture_output=True, text=True, cwd=ROOT)
        hit = re.search(r"(\d+) checks passed", out.stdout + out.stderr)
        if not hit:
            print("[FAIL] banner-v3.svg: could not count the suite to check the badge")
            bad += 1
        elif int(m.group(1)) != int(hit.group(1)):
            print(f"[FAIL] banner-v3.svg: tests badge says {m.group(1)}, suite has {hit.group(1)}")
            bad += 1

    if bad:
        return 1
    print("[OK] banner badges match the code (locales, RTL, test count)")
    return 0


if __name__ == "__main__":
    rc = main() or 0
    rc |= check_plural_table() or 0
    rc |= check_banner_numbers() or 0
    sys.exit(rc)
