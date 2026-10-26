"""Run the README's quickstart and compare it against the output the README shows.

Documentation that drifts is a bug, and so is a file the README tells you to open
that never made it into the repository (`.gitignore` swallowed `examples/ar.tsv`
for three releases). This executes the exact commands from README.md in a throwaway
copy of `examples/` and diffs the result against the fenced block below them.

Run: python tests/test_readme.py
"""

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
    return 1


if __name__ == "__main__":
    sys.exit(main())
