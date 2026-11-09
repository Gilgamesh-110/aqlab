"""Tests for load_tm and the landing page's language switch.

Two things learned the hard way and pinned here:

* load_tm accepted only JSON, but `aqlab scaffold` writes a TSV and the README
  hands that same TSV back as --tm. The tool crashed on a file it wrote itself.
* The landing page's switch has to survive a reload with "en" stored: the Arabic
  must be snapshotted before the swap, or the way back is lost.
"""
import io
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aqlab  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    if cond:
        print(f"[OK] {name}")
    else:
        print(f"[FAIL] {name} {detail}")
        FAILED.append(name)


def test_load_tm_tsv(tmp):
    """The file scaffold writes must load as a translation memory."""
    p = tmp / "tm.tsv"
    p.write_text("key\tsource\ttarget\nhello\tHello\tمرحبا\nbye\tBye\tوداعاً\n", encoding="utf-8")
    try:
        pairs = aqlab.load_tm(str(p))
    except Exception as e:  # the crash this test exists for
        check("load_tm reads the TSV scaffold writes", False,
              f"raised {type(e).__name__}: {e}")
        check("load_tm TSV keeps source/target order", False, "skipped after crash")
        check("load_tm TSV drops the header row", False, "skipped after crash")
        return
    check("load_tm reads the TSV scaffold writes",
          len(pairs) == 2, f"got {len(pairs)}")
    check("load_tm TSV keeps source/target order",
          pairs[0] == ("Hello", "مرحبا"), f"got {pairs[0] if pairs else None}")
    check("load_tm TSV drops the header row",
          all(k != "source" for k, _ in pairs))


def safe_load(path):
    """A crash inside load_tm is a failure of the check, not of the test run --
    otherwise the mutation that removes the TSV branch aborts the whole file and
    the remaining checks never report."""
    try:
        return aqlab.load_tm(str(path)), None
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


def test_load_tm_json_object(tmp):
    p = tmp / "tm.json"
    p.write_text(json.dumps({"Hello": "مرحبا"}), encoding="utf-8")
    pairs, err = safe_load(p)
    if err:
        check("load_tm still reads the JSON object form", False, err)
        return
    check("load_tm still reads the JSON object form",
          pairs == [("Hello", "مرحبا")], f"got {pairs}")


def test_load_tm_json_array(tmp):
    p = tmp / "tm2.json"
    p.write_text(json.dumps([{"source": "Hello", "target": "مرحبا"}]), encoding="utf-8")
    pairs, err = safe_load(p)
    if err:
        check("load_tm still reads the JSON array form", False, err)
        return
    check("load_tm still reads the JSON array form",
          pairs == [("Hello", "مرحبا")], f"got {pairs}")


def test_load_tm_skips_junk(tmp):
    p = tmp / "tm3.tsv"
    p.write_text("key\tsource\ttarget\nonlyonecol\n# comment\nok\tOK\tحسناً\n\t\t\n", encoding="utf-8")
    pairs, err = safe_load(p)
    if err:
        check("load_tm ignores rows it cannot use", False, err)
        return
    check("load_tm ignores rows it cannot use", pairs == [("OK", "حسناً")], f"got {pairs}")


def test_page_switch():
    """The page must keep Arabic in markup and English beside it."""
    page = (ROOT / "site" / "index.html").read_text(encoding="utf-8")

    check("page has the switch buttons",
          'id="lang-ar"' in page and 'id="lang-en"' in page)

    # snapshot must be a separate inline script that runs before the swap, and it
    # must run after the markup exists -- an earlier placement snapshotted 1 node.
    snap = re.search(r'<script>\s*/\* Snapshot the Arabic.*?window\.__aqlabArabic', page, re.S)
    check("page snapshots the Arabic before swapping", snap is not None)

    n_en = len(re.findall(r'data-en(-html)?=', page))
    check("page carries English for every translatable node", n_en > 80, f"{n_en} nodes")

    # every Arabic heading must have an English twin
    missing = []
    for m in re.finditer(r"<h2([^>]*)>(.*?)</h2>", page, re.S):
        attrs, inner = m.group(1), m.group(2)
        text = re.sub(r"<[^>]+>", "", inner)
        if re.search(r"[\u0600-\u06FF]", text) and "data-en" not in attrs:
            missing.append(text.strip()[:40])
    check("every Arabic section heading has an English twin",
          not missing, f"missing: {missing}")

    # nesting: an English value that contains markup must not be entity-escaped
    check("markup-bearing English uses data-en-html",
          "data-en-html" in page and "&lt;br&gt;" not in page)

    # the switch script resets the title too
    check("switch updates the document title", "document.title" in page)

    # and it must not fight the RTL layout
    check("switch flips dir on <html>", "document.documentElement.dir" in page)


def main():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_load_tm_tsv(tmp)
        test_load_tm_json_object(tmp)
        test_load_tm_json_array(tmp)
        test_load_tm_skips_junk(tmp)
    test_page_switch()

    print()
    if FAILED:
        print(f"[FAIL] {len(FAILED)} check(s) failed")
        return 1
    print("[OK] landing page and translation memory checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
