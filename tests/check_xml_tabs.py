#!/usr/bin/env python3
"""Fail if a resource XML file contains a literal tab.

aapt2 rejects tabs inside attribute values, and a tab is invisible in review, so
this is a cheap guard rather than something a reader has to notice.
"""
import sys


def main(paths):
    bad = []
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for n, line in enumerate(fh, 1):
                if "\t" in line:
                    bad.append(f"{path}:{n}: tab character")
    for b in bad:
        print(b)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
